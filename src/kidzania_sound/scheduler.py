"""schedule.jsonの内容に基づくAPScheduler管理。"""
from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import AppConfig, BlackoutWindow, ScheduledJob, StageShowJob
from .lighting import LightingController
from .player import AudioCue


def _time_in_range(t: time, start: time, end: time) -> bool:
    """半開区間[start, end)にtが含まれるか判定する。start > end は深夜またぎ
    (例: 22:00〜翌2:00)として扱う。start == end は空区間として常にFalse
    (休止時間帯なら「常に無効=何もブロックしない」、ジョブの有効時間帯なら
    「常に無効=一度も再生されない」という、それぞれ安全側の意味になる)。"""
    if start < end:
        return start <= t < end
    if start > end:
        return t >= start or t < end
    return False


class JobScheduler:
    def __init__(
        self,
        config: AppConfig,
        vlc_instance,
        logger: logging.Logger,
        lighting: LightingController,
        on_playback_started: Optional[Callable[[str], None]] = None,
        on_playback_ended: Optional[Callable[[str], None]] = None,
    ):
        self._config = config
        self._vlc_instance = vlc_instance
        self._logger = logger
        self._lighting = lighting
        self._scheduler = BackgroundScheduler()
        # job_id -> (表示名, 再生中のAudioCue)。GUIの「現在再生中」パネルから参照・停止する。
        self._active_cues: dict[str, tuple[str, AudioCue]] = {}
        # 次回だけスキップしたいジョブのid集合(GUIの「次回スキップ」から追加)。
        self._skip_once: set[str] = set()
        self._blackout_windows: list[BlackoutWindow] = []
        # 2台のSurfaceを連携させるリンク機能向け通知フック(link.enabled=falseならNone)。
        # 表示名(label)を渡して呼ぶ。
        self._on_playback_started = on_playback_started
        self._on_playback_ended = on_playback_ended
        # ショー予定(stage_show_schedule)が発火した時のフック。GUI側(MainWindow)が
        # 実際のショー再生・表示モード切替を担うため、ここではコールバックを呼ぶだけに
        # とどめる(このクラス自体はTkinter/FullscreenVideoPlayerに依存しない)。
        # 循環依存(MainWindowの生成にscheduler、schedulerのコールバックにMainWindowの
        # メソッドが必要)を避けるため、コンストラクタ引数ではなくMainWindow側から
        # 構築後に代入してもらう公開属性にしている。
        # 引数は(stage_show_id, clip_index, ショー名)。
        self.on_stage_show_triggered: Optional[Callable[[str, int, str], None]] = None

    def start(self) -> None:
        self._load_jobs()
        self._scheduler.start()
        self._logger.info("スケジューラーを開始しました")

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
        self._logger.info("スケジューラーを停止しました")

    def reload(self) -> None:
        self._scheduler.remove_all_jobs()
        self._skip_once.clear()
        self._load_jobs()
        self._logger.info("スケジュールを再読み込みしました")

    def _load_jobs(self) -> None:
        active_mode = self._config.get_active_mode()
        self._blackout_windows = [
            w for w in self._config.load_blackout_windows() if not w.mode or w.mode == active_mode
        ]
        jobs = self._config.load_active_jobs()
        for job in jobs:
            if not job.enabled:
                self._logger.info("無効化されているジョブをスキップ: %s", job.name)
                continue
            try:
                trigger = CronTrigger(**job.cron)
            except Exception:
                self._logger.exception("cron設定が不正です(%s): %s", job.name, job.cron)
                continue

            self._scheduler.add_job(
                self._make_runner(job),
                trigger=trigger,
                id=job.id,
                name=job.name,
                replace_existing=True,
            )
            self._logger.info("ジョブを登録しました: %s (cron=%s)", job.name, job.cron)

        self._load_stage_show_jobs(active_mode)

    def _load_stage_show_jobs(self, active_mode: str) -> None:
        shows_by_id = {s.id: s for s in self._config.load_stage_shows()}
        stage_jobs = [
            j for j in self._config.load_stage_show_schedule() if not j.mode or j.mode == active_mode
        ]
        for job in stage_jobs:
            if not job.enabled:
                self._logger.info("無効化されているショー予定をスキップ: %s", job.id)
                continue
            show = shows_by_id.get(job.stage_show_id)
            if show is None:
                self._logger.warning(
                    "ショー予定の参照先が見つかりません(ショーが削除された可能性があります): %s",
                    job.stage_show_id,
                )
                continue
            try:
                trigger = CronTrigger(**job.cron)
            except Exception:
                self._logger.exception("cron設定が不正です(%s): %s", show.label, job.cron)
                continue

            self._scheduler.add_job(
                self._make_stage_show_runner(job, show.label),
                trigger=trigger,
                id=job.id,
                name=show.label,
                replace_existing=True,
            )
            self._logger.info("ショー予定を登録しました: %s (cron=%s)", show.label, job.cron)

    def _should_skip(self, job_id: str, job_name: str, window: Optional[dict], now: datetime) -> bool:
        """次回スキップ・休止時間帯・(「時間帯」頻度の場合の)有効時間帯外を
        判定する。音源ジョブ・ショー予定の両方の発火時チェックで共通して使う。
        スキップすべき場合は理由をログに残したうえでTrueを返す。"""
        if job_id in self._skip_once:
            self._skip_once.discard(job_id)
            self._logger.info("次回をスキップしました: %s", job_name)
            return True

        blackout = self._find_active_blackout(now)
        if blackout is not None:
            self._logger.info("休止時間帯(%s)のため再生をスキップしました: %s", blackout.label, job_name)
            return True

        if window is not None:
            start = time(window["start_hour"], window["start_minute"])
            end = time(window["end_hour"], window["end_minute"])
            if not _time_in_range(now.time(), start, end):
                self._logger.info("設定時間帯外のため再生をスキップしました: %s", job_name)
                return True

        return False

    def _make_runner(self, job: ScheduledJob) -> Callable[[], None]:
        def _run() -> None:
            if self._should_skip(job.id, job.name, job.window, datetime.now()):
                return

            cue = AudioCue(self._vlc_instance, self._logger)
            path = self._config.resolve_media(job.file)

            def _on_finished() -> None:
                self._active_cues.pop(job.id, None)
                if self._on_playback_ended is not None:
                    self._on_playback_ended(job.name)

            def _on_ready() -> None:
                # 実際に音が出始める瞬間(準備遅延がある場合はその後)に照明キューの
                # 発火と連携先端末への再生開始通知(ダッキング用)を行い、音とずれない
                # ようにする。
                self._lighting.trigger_cue(job.lighting_cue)
                if self._on_playback_started is not None:
                    self._on_playback_started(job.name)

            self._active_cues[job.id] = (job.name, cue)
            cue.play(
                path,
                job.volume,
                job.name,
                on_finished=_on_finished,
                prepare_delay=self._config.playback_prepare_delay_seconds,
                on_ready=_on_ready,
            )

        return _run

    def _make_stage_show_runner(self, job: StageShowJob, show_label: str) -> Callable[[], None]:
        def _run() -> None:
            if self._should_skip(job.id, show_label, job.window, datetime.now()):
                return
            if self.on_stage_show_triggered is None:
                self._logger.warning("ショー予定の発火ハンドラが未設定のため再生できません: %s", show_label)
                return
            self.on_stage_show_triggered(job.stage_show_id, job.clip_index, show_label)

        return _run

    # ------------------------------------------------------------------
    # 休止時間帯(blackout_windows)判定
    # ------------------------------------------------------------------
    def _find_active_blackout(self, now: datetime) -> Optional[BlackoutWindow]:
        """現在時刻がいずれかの有効な休止時間帯に該当すればそれを返す。
        区間は開始時刻を含み終了時刻を含まない半開区間[start, end)。
        start > end の場合は深夜またぎ(例: 22:00〜翌2:00)として扱う。"""
        current = now.time()
        for window in self._blackout_windows:
            if not window.enabled:
                continue
            start = time(window.start_hour, window.start_minute)
            end = time(window.end_hour, window.end_minute)
            if _time_in_range(current, start, end):
                return window
        return None

    # ------------------------------------------------------------------
    # GUIの「現在再生中/次の予定」パネル向け
    # ------------------------------------------------------------------
    def get_active_cues(self) -> list[tuple[str, str]]:
        """現在再生中のジョブ一覧を(job_id, 表示名)で返す。"""
        return [(job_id, name) for job_id, (name, _cue) in self._active_cues.items()]

    def stop_active_cue(self, job_id: str) -> None:
        """通常の手動停止(フェードアウト)。GUIの「現在再生中」パネルの
        個別「■ 停止」ボタンから呼ばれる(緊急停止はstop_all_active()を使う)。"""
        entry = self._active_cues.pop(job_id, None)
        if entry is not None:
            name, cue = entry
            cue.fade_out_and_stop()
            self._logger.info("再生をフェードアウトで停止しました: %s", name)

    def set_active_volume(self, job_id: str, percent: int) -> None:
        """再生中ジョブの音量をリアルタイムで変更する(不具合による大音量再生への
        緊急対応向け)。対象が既に終了していれば何もしない。"""
        entry = self._active_cues.get(job_id)
        if entry is not None:
            _name, cue = entry
            cue.set_volume(percent)

    def get_active_volume(self, job_id: str) -> Optional[int]:
        entry = self._active_cues.get(job_id)
        if entry is None:
            return None
        _name, cue = entry
        return cue.get_volume()

    def get_upcoming(self, limit: int = 8) -> list[tuple]:
        """直近に実行予定のジョブを(実行時刻, job_id, 表示名)のリストで返す
        (実行時刻が近い順)。休止時間帯に該当し実際には再生されない予定は
        一覧から除外する(その回のnext_run_timeが休止時間帯を抜けるまで、
        該当ジョブは表示されない)。"""
        entries = [
            (j.next_run_time, j.id, j.name)
            for j in self._scheduler.get_jobs()
            if j.next_run_time is not None
        ]
        entries.sort(key=lambda e: e[0])
        visible = [e for e in entries if self._find_active_blackout(e[0]) is None]
        return visible[:limit]

    def skip_next(self, job_id: str) -> None:
        self._skip_once.add(job_id)

    def is_skipped(self, job_id: str) -> bool:
        return job_id in self._skip_once

    def cancel_skip(self, job_id: str) -> None:
        self._skip_once.discard(job_id)

    # ------------------------------------------------------------------
    # リンク機能(2台のSurface連携)向け一括操作
    # ------------------------------------------------------------------
    def duck_all_active(self, percent: int) -> None:
        """相手端末が再生を開始した際に、自機で再生中の全ジョブの音量を
        元の音量のpercent%まで一時的に下げる。"""
        for name, cue in list(self._active_cues.values()):
            cue.duck(percent)
        if self._active_cues:
            self._logger.info("連携先の再生開始によりダッキングしました(%d%%)", percent)

    def restore_all_active(self) -> None:
        """duck_all_activeで下げた音量を元に戻す。"""
        for name, cue in list(self._active_cues.values()):
            cue.restore()
        if self._active_cues:
            self._logger.info("ダッキングを解除しました")

    def stop_all_active(self) -> None:
        """緊急停止向け: 現在再生中の全ジョブを即座に(フェードなしで)停止する
        (連携先からの一括停止要求からも呼ばれる)。"""
        for job_id in list(self._active_cues.keys()):
            entry = self._active_cues.pop(job_id, None)
            if entry is not None:
                name, cue = entry
                cue.stop()
                self._logger.info("再生を即時停止しました(緊急): %s", name)
