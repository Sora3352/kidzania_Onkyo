"""1日分のタイムスケジュールを見やすい一覧として組み立てる。

「スケジュール管理」画面はcron設定を編集するための画面であり、○時○分・
毎時○分・時間帯といった設定値がそのまま並ぶため、運用担当者が「今日は
結局何時に何が鳴るのか」を一目で把握するには不向きだった。この画面/出力は
その逆で、cronを実際に1日分展開し、休止時間帯による無効化も反映したうえで
時刻順の一覧にする(画面表示・テキストファイル出力の両方で同じ一覧を使う)。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional

from apscheduler.triggers.cron import CronTrigger

from .config import AppConfig
from .scheduler import _time_in_range

# 1日にこれ以上発火するcronは設定ミス(無限ループ化)とみなして打ち切る安全弁。
_MAX_FIRE_TIMES_PER_DAY = 500


@dataclass
class ScheduleEntry:
    start: time
    # 休止時間帯のみ終了時刻を持つ(範囲表示のため)。ジョブは単発発火なのでNone。
    end: Optional[time]
    label: str
    kind: str  # "job" / "show" / "blackout"


def _cron_fire_times_on_day(cron: dict, day: date) -> list[time]:
    """cron辞書(APSchedulerのCronTrigger引数と同じ形式)から、指定日に
    実際に発火する時刻の一覧を計算する。日をまたぐcron表現は使っていない
    前提(このアプリのスケジュール機能自体がその前提で作られているため)。"""
    trigger = CronTrigger(**cron)
    tz = trigger.timezone
    day_start = datetime.combine(day, time.min, tzinfo=tz)
    day_end = datetime.combine(day, time.max, tzinfo=tz)

    times: list[time] = []
    previous = None
    now_marker = day_start
    for _ in range(_MAX_FIRE_TIMES_PER_DAY):
        nxt = trigger.get_next_fire_time(previous, now_marker)
        if nxt is None or nxt > day_end:
            break
        times.append(nxt.time())
        previous = nxt
        now_marker = nxt + timedelta(seconds=1)
    return times


def build_daily_schedule(config: AppConfig, mode: str, day: Optional[date] = None) -> list[ScheduleEntry]:
    """指定した営業モードで指定日(既定は本日)に実際に発火する項目を、
    休止時間帯も反映したうえで時刻順の一覧として返す。休止時間帯に該当し
    実際には再生されないジョブの発火は一覧から除外し(「次の予定」パネルと
    同じ考え方)、代わりに休止時間帯自体を1件のエントリとして表示する。
    無効化(enabled=False)されたジョブは対象外。"""
    if day is None:
        day = date.today()

    blackouts = [w for w in config.load_blackout_windows() if w.enabled and (not w.mode or w.mode == mode)]

    def _in_any_blackout(t: time) -> bool:
        return any(
            _time_in_range(t, time(w.start_hour, w.start_minute), time(w.end_hour, w.end_minute))
            for w in blackouts
        )

    jobs = config.load_common_jobs() + config.load_mode_jobs(mode) + config.load_manual_jobs()

    entries: list[ScheduleEntry] = []
    for job in jobs:
        if not job.enabled:
            continue
        try:
            fire_times = _cron_fire_times_on_day(job.cron, day)
        except Exception:
            # cron設定が不正な場合はスケジューラー側でも登録に失敗しログに残るため、
            # ここでは一覧から静かに除外するだけにとどめる。
            continue

        if job.window is not None:
            start = time(job.window["start_hour"], job.window["start_minute"])
            end = time(job.window["end_hour"], job.window["end_minute"])
            fire_times = [t for t in fire_times if _time_in_range(t, start, end)]

        for t in fire_times:
            if _in_any_blackout(t):
                continue
            entries.append(ScheduleEntry(start=t, end=None, label=job.name, kind="job"))

    shows_by_id = {s.id: s for s in config.load_stage_shows()}
    stage_jobs = [j for j in config.load_stage_show_schedule() if not j.mode or j.mode == mode]
    for job in stage_jobs:
        if not job.enabled:
            continue
        show = shows_by_id.get(job.stage_show_id)
        if show is None:
            continue
        try:
            fire_times = _cron_fire_times_on_day(job.cron, day)
        except Exception:
            continue

        if job.window is not None:
            start = time(job.window["start_hour"], job.window["start_minute"])
            end = time(job.window["end_hour"], job.window["end_minute"])
            fire_times = [t for t in fire_times if _time_in_range(t, start, end)]

        for t in fire_times:
            if _in_any_blackout(t):
                continue
            entries.append(ScheduleEntry(start=t, end=None, label=show.label, kind="show"))

    for w in blackouts:
        entries.append(
            ScheduleEntry(
                start=time(w.start_hour, w.start_minute),
                end=time(w.end_hour, w.end_minute),
                label=w.label or "休止時間帯",
                kind="blackout",
            )
        )

    entries.sort(key=lambda e: (e.start, 0 if e.kind == "blackout" else 1))
    return entries


def format_time_range(start: time, end: time) -> str:
    """休止時間帯の表示用。start > end は深夜またぎ(例: 22:00〜翌2:00)なので
    「翌」を付けて分かりやすくする。"""
    if end < start:
        return f"{start:%H:%M}〜翌{end:%H:%M}"
    return f"{start:%H:%M}〜{end:%H:%M}"


def format_daily_schedule_text(mode: str, day: date, entries: list[ScheduleEntry]) -> str:
    """テキストファイル出力用に整形する(画面表示にも同じ文字列を流用できる)。"""
    weekday_ja = "月火水木金土日"[day.weekday()]
    lines = [f"本日のタイムスケジュール  {day:%Y-%m-%d}({weekday_ja})  営業モード: {mode}", ""]
    if not entries:
        lines.append("(本日再生される予定はありません)")
        return "\n".join(lines)

    for e in entries:
        if e.kind == "blackout":
            lines.append(f"{format_time_range(e.start, e.end)}  【休止時間帯】{e.label}")
        elif e.kind == "show":
            lines.append(f"{e.start:%H:%M}          【ショー】{e.label}")
        else:
            lines.append(f"{e.start:%H:%M}          {e.label}")
    return "\n".join(lines)
