"""設定ファイル(settings.json / schedule.json / stage_shows.json)の読み書き。

schedule.json は次の構造を持つ:
- common_jobs   : 営業モードに関わらず常に有効なジョブ(街時計など)
- modes         : {モード名: ジョブ一覧} (例: 通し営業/2部制営業)
- manual_jobs   : GUIから手動追加されたジョブ。モードに関わらず常に有効
- active_mode   : 現在選択されている営業モード名
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ScheduledJob:
    id: str
    name: str
    file: str
    volume: int
    cron: dict[str, Any]
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "file": self.file,
            "volume": self.volume,
            "cron": self.cron,
            "enabled": self.enabled,
        }


@dataclass
class StageShow:
    """ステージショー1つ分。filesは再生する動画のリスト(順番に再生)。
    ダンスショーのように1本だけの場合もあれば、ファッションショーのように
    MV1・MV2…と複数本を「次のMV」ボタンで切り替える場合もある。"""

    id: str
    label: str
    files: list[str]
    volume: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "files": self.files,
            "volume": self.volume,
        }


@dataclass
class AppConfig:
    base_dir: Path
    media_root: Path = field(init=False)
    log_dir: Path = field(init=False)
    log_level: str = field(init=False)
    prevent_sleep: bool = field(init=False)
    schedule_path: Path = field(init=False)
    stage_shows_path: Path = field(init=False)

    def __post_init__(self) -> None:
        settings_path = self.base_dir / "config" / "settings.json"
        settings = _read_json(settings_path)

        self.media_root = self.base_dir / settings["media_root"]
        self.log_dir = self.base_dir / settings["log_dir"]
        self.log_level = settings.get("log_level", "INFO")
        self.prevent_sleep = settings.get("prevent_sleep", True)
        self.schedule_path = self.base_dir / settings["schedule_file"]
        self.stage_shows_path = self.base_dir / settings["stage_shows_file"]

    # ------------------------------------------------------------------
    # メディアパス
    # ------------------------------------------------------------------
    def resolve_media(self, relative_or_abs_path: str) -> Path:
        p = Path(relative_or_abs_path)
        return p if p.is_absolute() else self.media_root / p

    def to_media_relative(self, path: Path) -> str:
        """ファイル選択ダイアログ等で得た絶対パスを、可能ならmedia_root相対の
        文字列に変換する(media_root配下でなければ絶対パスのまま返す)。"""
        try:
            return str(path.relative_to(self.media_root)).replace("\\", "/")
        except ValueError:
            return str(path)

    # ------------------------------------------------------------------
    # 営業モード
    # ------------------------------------------------------------------
    def get_mode_names(self) -> list[str]:
        return list(self._read_schedule().get("modes", {}).keys())

    def get_active_mode(self) -> str:
        data = self._read_schedule()
        modes = list(data.get("modes", {}).keys())
        active = data.get("active_mode")
        if active in modes:
            return active
        return modes[0] if modes else ""

    def set_active_mode(self, mode: str) -> None:
        data = self._read_schedule()
        data["active_mode"] = mode
        _write_json(self.schedule_path, data)

    # ------------------------------------------------------------------
    # ジョブ読み込み
    # ------------------------------------------------------------------
    def load_common_jobs(self) -> list[ScheduledJob]:
        return self._parse_jobs(self._read_schedule().get("common_jobs", []))

    def load_mode_jobs(self, mode: str) -> list[ScheduledJob]:
        data = self._read_schedule()
        return self._parse_jobs(data.get("modes", {}).get(mode, []))

    def load_manual_jobs(self) -> list[ScheduledJob]:
        return self._parse_jobs(self._read_schedule().get("manual_jobs", []))

    def load_active_jobs(self) -> list[ScheduledJob]:
        """現在の営業モードで実際にスケジューラーへ登録すべきジョブ一覧
        (共通 + 選択中モード + 手動追加)。"""
        active_mode = self.get_active_mode()
        return (
            self.load_common_jobs()
            + self.load_mode_jobs(active_mode)
            + self.load_manual_jobs()
        )

    # ------------------------------------------------------------------
    # ジョブ保存
    # ------------------------------------------------------------------
    def save_common_jobs(self, jobs: list[ScheduledJob]) -> None:
        self._save_job_list("common_jobs", jobs)

    def save_mode_jobs(self, mode: str, jobs: list[ScheduledJob]) -> None:
        data = self._read_schedule()
        data.setdefault("modes", {})[mode] = [j.to_dict() for j in jobs]
        _write_json(self.schedule_path, data)

    def save_manual_jobs(self, jobs: list[ScheduledJob]) -> None:
        self._save_job_list("manual_jobs", jobs)

    def _save_job_list(self, key: str, jobs: list[ScheduledJob]) -> None:
        data = self._read_schedule()
        data[key] = [j.to_dict() for j in jobs]
        _write_json(self.schedule_path, data)

    # ------------------------------------------------------------------
    # ステージショー
    # ------------------------------------------------------------------
    def load_stage_shows(self) -> list[StageShow]:
        data = _read_json(self.stage_shows_path)
        return [
            StageShow(
                id=s["id"],
                label=s["label"],
                files=list(s.get("files", [])),
                volume=int(s.get("volume", 90)),
            )
            for s in data.get("shows", [])
        ]

    def save_stage_shows(self, shows: list[StageShow]) -> None:
        data = _read_json(self.stage_shows_path)
        data["shows"] = [s.to_dict() for s in shows]
        _write_json(self.stage_shows_path, data)

    # ------------------------------------------------------------------
    # 内部ヘルパー
    # ------------------------------------------------------------------
    def _read_schedule(self) -> dict[str, Any]:
        return _read_json(self.schedule_path)

    @staticmethod
    def _parse_jobs(raw: list[dict[str, Any]]) -> list[ScheduledJob]:
        return [
            ScheduledJob(
                id=j["id"],
                name=j["name"],
                file=j["file"],
                volume=int(j.get("volume", 80)),
                cron=j.get("cron", {}),
                enabled=bool(j.get("enabled", True)),
            )
            for j in raw
        ]


def _read_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
