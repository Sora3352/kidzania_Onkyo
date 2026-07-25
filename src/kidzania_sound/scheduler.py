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

    def start(self) -> None:
        self._load_jobs()
        self._scheduler.start()
        self._logger.info("スケジューラーを開始しました")

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
        self._logger.info("スケジューラーを停止しました")

    def reload(self) -> None:
        self._scheduler.remove_all_jobs()
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
            cue = AudioCue(self._vlc_instance, self._logger)
            path = self._config.resolve_media(job.file)
            cue.play(path, job.volume, job.name)

        return _run
