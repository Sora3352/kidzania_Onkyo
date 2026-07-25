"""schedule.jsonの内容に基づくAPScheduler管理。"""
from __future__ import annotations

import logging
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import AppConfig, ScheduledJob
from .player import AudioCue


class JobScheduler:
    def __init__(self, config: AppConfig, vlc_instance, logger: logging.Logger):
        self._config = config
        self._vlc_instance = vlc_instance
        self._logger = logger
        self._scheduler = BackgroundScheduler()
        # job_id -> (表示名, 再生中のAudioCue)。GUIの「現在再生中」パネルから参照・停止する。
        self._active_cues: dict[str, tuple[str, AudioCue]] = {}
        # 次回だけスキップしたいジョブのid集合(GUIの「次回スキップ」から追加)。
        self._skip_once: set[str] = set()

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

            cue = AudioCue(self._vlc_instance, self._logger)
            path = self._config.resolve_media(job.file)

            def _on_finished() -> None:
                self._active_cues.pop(job.id, None)

            self._active_cues[job.id] = (job.name, cue)
            cue.play(path, job.volume, job.name, on_finished=_on_finished)

        return _run

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
