"""設定ファイル(settings.json / schedule.json / stage_shows.json)の読み書き。

schedule.json は次の構造を持つ:
- common_jobs   : 営業モードに関わらず常に有効なジョブ(街時計など)
- modes         : {モード名: ジョブ一覧} (例: 通し営業/2部制営業)
- manual_jobs   : GUIから手動追加されたジョブ。モードに関わらず常に有効
- active_mode   : 現在選択されている営業モード名
"""
from __future__ import annotations

import json
import os
import uuid
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
class BlackoutWindow:
    """スケジュールジョブを一切実行しない時間帯(例: 12:00〜13:00は再生しない)。
    共通/モード別/手動追加の区別なく、全ジョブに対して適用される。
    start > end の場合は深夜またぎ(例: 22:00〜翌2:00)として扱う。"""

    id: str
    label: str
    start_hour: int
    start_minute: int
    end_hour: int
    end_minute: int
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "start_hour": self.start_hour,
            "start_minute": self.start_minute,
            "end_hour": self.end_hour,
            "end_minute": self.end_minute,
            "enabled": self.enabled,
        }


@dataclass
class LinkConfig:
    """2台のSurfaceを連携させるための設定(settings.jsonの'link'セクション)。
    enabled=Falseの場合は単一PC運用時と完全に同じ挙動になる(サーバー未起動・送信no-op)。
    duck_volume_percentは「相手が再生を開始した間、自分の再生中音量を元の音量の
    何%まで下げるか」(例: 20なら元音量の20%まで下げる)。"""

    enabled: bool = False
    peer_host: str = ""
    peer_port: int = 8765
    listen_port: int = 8765
    duck_volume_percent: int = 20
    poll_interval_seconds: int = 60
    http_timeout_seconds: int = 3

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "LinkConfig":
        return LinkConfig(
            enabled=bool(data.get("enabled", False)),
            peer_host=str(data.get("peer_host", "")),
            peer_port=int(data.get("peer_port", 8765)),
            listen_port=int(data.get("listen_port", 8765)),
            duck_volume_percent=int(data.get("duck_volume_percent", 20)),
            poll_interval_seconds=int(data.get("poll_interval_seconds", 60)),
            http_timeout_seconds=int(data.get("http_timeout_seconds", 3)),
        )


@dataclass
class AppConfig:
    base_dir: Path
    media_root: Path = field(init=False)
    log_dir: Path = field(init=False)
    log_level: str = field(init=False)
    prevent_sleep: bool = field(init=False)
    schedule_path: Path = field(init=False)
    stage_shows_path: Path = field(init=False)
    settings_path: Path = field(init=False)
    device_name: str = field(init=False)
    link: LinkConfig = field(init=False)

    def __post_init__(self) -> None:
        self.settings_path = self.base_dir / "config" / "settings.json"
        settings = _read_json(self.settings_path)

        self.media_root = self.base_dir / settings["media_root"]
        self.log_dir = self.base_dir / settings["log_dir"]
        self.log_level = settings.get("log_level", "INFO")
        self.prevent_sleep = settings.get("prevent_sleep", True)
        self.schedule_path = self.base_dir / settings["schedule_file"]
        self.stage_shows_path = self.base_dir / settings["stage_shows_file"]
        self.device_name = settings.get("device_name", "")
        self.link = LinkConfig.from_dict(settings.get("link", {}))

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
    # 休止時間帯(blackout_windows)
    # ------------------------------------------------------------------
    def load_blackout_windows(self) -> list[BlackoutWindow]:
        raw = self._read_schedule().get("blackout_windows", [])
        return [
            BlackoutWindow(
                id=b["id"],
                label=b.get("label", ""),
                start_hour=int(b.get("start_hour", 0)),
                start_minute=int(b.get("start_minute", 0)),
                end_hour=int(b.get("end_hour", 0)),
                end_minute=int(b.get("end_minute", 0)),
                enabled=bool(b.get("enabled", True)),
            )
            for b in raw
        ]

    def save_blackout_windows(self, windows: list[BlackoutWindow]) -> None:
        data = self._read_schedule()
        data["blackout_windows"] = [w.to_dict() for w in windows]
        _write_json(self.schedule_path, data)

    # ------------------------------------------------------------------
    # 生JSON(リンク機能: config push/pull用)
    # ------------------------------------------------------------------
    def read_schedule_raw(self) -> dict[str, Any]:
        return self._read_schedule()

    def write_schedule_raw(self, data: dict[str, Any]) -> None:
        _write_json(self.schedule_path, data)

    def read_stage_shows_raw(self) -> dict[str, Any]:
        return _read_json(self.stage_shows_path)

    def write_stage_shows_raw(self, data: dict[str, Any]) -> None:
        _write_json(self.stage_shows_path, data)

    # ------------------------------------------------------------------
    # システム設定(端末名・リンク設定)
    # ------------------------------------------------------------------
    def save_system_settings(self, device_name: str, link: LinkConfig) -> None:
        """GUIの「システム設定」画面から呼ばれる。settings.jsonに保存し、
        メモリ上のdevice_name/linkも即座に更新する(peer_host/peer_port/
        duck_volume_percent/poll_interval_secondsは次回通信・次回ポーリングから
        即座に反映される。enabled/listen_portの変更はサーバー再起動が必要)。"""
        data = _read_json(self.settings_path)
        data["device_name"] = device_name
        # 既存の"link"辞書を丸ごと差し替えるのではなく更新する。"_comment"等、
        # このアプリが関知しない既存キーを保存のたびに消してしまわないため。
        existing_link = data.get("link")
        if not isinstance(existing_link, dict):
            existing_link = {}
        existing_link.update(
            {
                "enabled": link.enabled,
                "peer_host": link.peer_host,
                "peer_port": link.peer_port,
                "listen_port": link.listen_port,
                "duck_volume_percent": link.duck_volume_percent,
                "poll_interval_seconds": link.poll_interval_seconds,
                "http_timeout_seconds": link.http_timeout_seconds,
            }
        )
        data["link"] = existing_link
        _write_json(self.settings_path, data)
        self.device_name = device_name
        self.link = link

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
    """一時ファイルに書き込んでから os.replace() でアトミックに置き換える。
    書き込み中のクラッシュ/電源断や、リンク機能によるconfig受信とローカル保存の
    同時発生があっても、読み取り側が壊れかけのJSONを掴まないようにするため。"""
    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
