"""schedule.jsonの内容に基づくAPScheduler管理。"""
from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import AppConfig, BlackoutWindow, ScheduledJob
from .player import AudioCue


class JobScheduler:
    def __init__(
        self,
        config: AppConfig,
        vlc_instance,
        logger: logging.Logger,
        on_playback_started: Optional[Callable[[str], None]] = None,
        on_playback_ended: Optional[Callable[[str], None]] = None,
    ):
        self._config = config
        self._vlc_instance = vlc_instance
        self._logger = logger
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
        self._blackout_windows = self._config.load_blackout_windows()
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

    def _make_runner(self, job: ScheduledJob) -> Callable[[], None]:
        def _run() -> None:
            if job.id in self._skip_once:
                self._skip_once.discard(job.id)
                self._logger.info("次回をスキップしました: %s", job.name)
                return

            blackout = self._find_active_blackout(datetime.now())
            if blackout is not None:
                self._logger.info(
                    "休止時間帯(%s)のため再生をスキップしました: %s", blackout.label, job.name
                )
                return

            cue = AudioCue(self._vlc_instance, self._logger)
            path = self._config.resolve_media(job.file)

            def _on_finished() -> None:
                self._active_cues.pop(job.id, None)
                if self._on_playback_ended is not None:
                    self._on_playback_ended(job.name)

            self._active_cues[job.id] = (job.name, cue)
            if self._on_playback_started is not None:
                self._on_playback_started(job.name)
            cue.play(path, job.volume, job.name, on_finished=_on_finished)

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
            if start == end:
                continue
            if start < end:
                if start <= current < end:
                    return window
            else:
                if current >= start or current < end:
                    return window
        return None

    # ------------------------------------------------------------------
    # GUIの「現在再生中/次の予定」パネル向け
    # ------------------------------------------------------------------
    def get_active_cues(self) -> list[tuple[str, str]]:
        """現在再生中のジョブ一覧を(job_id, 表示名)で返す。"""
        return [(job_id, name) for job_id, (name, _cue) in self._active_cues.items()]

    def stop_active_cue(self, job_id: str) -> None:
        entry = self._active_cues.pop(job_id, None)
        if entry is not None:
            name, cue = entry
            cue.stop()
            self._logger.info("再生を手動停止しました: %s", name)

    def get_upcoming(self, limit: int = 8) -> list[tuple]:
        """直近に実行予定のジョブを(実行時刻, job_id, 表示名)のリストで返す
        (実行時刻が近い順)。"""
        entries = [
            (j.next_run_time, j.id, j.name)
            for j in self._scheduler.get_jobs()
            if j.next_run_time is not None
        ]
        entries.sort(key=lambda e: e[0])
        return entries[:limit]

    def skip_next(self, job_id: str) -> None:
        self._skip_once.add(job_id)

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
        """現在再生中の全ジョブを停止する(連携先からの一括停止要求向け)。"""
        for job_id in list(self._active_cues.keys()):
            self.stop_active_cue(job_id)
