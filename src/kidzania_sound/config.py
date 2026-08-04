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
from typing import Any, Optional


@dataclass
class ScheduledJob:
    id: str
    name: str
    file: str
    volume: int
    cron: dict[str, Any]
    enabled: bool = True
    # 照明キュー(schedule.jsonのlighting_cuesで定義したid)。空文字なら照明連携なし。
    lighting_cue: str = ""
    # 「時間帯」頻度で分単位の開始/終了を指定した場合の有効時間帯
    # ({"start_hour","start_minute","end_hour","end_minute"})。Noneなら制限なし。
    # cron自体は常時"minute"のみ(毎時トリガー)で、実際に再生するかはこのwindowで
    # 判定する(半開区間[start, end)、start>endは深夜またぎ扱い)。
    window: Optional[dict[str, int]] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "file": self.file,
            "volume": self.volume,
            "cron": self.cron,
            "enabled": self.enabled,
            "lighting_cue": self.lighting_cue,
        }
        if self.window is not None:
            d["window"] = self.window
        return d


@dataclass
class StageShowClip:
    """StageShow内の動画1本分。動画ごとに照明の演出が異なることがあるため、
    lighting_cueはクリップ単位で持つ(空文字なら照明連携なし)。"""

    file: str
    lighting_cue: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "lighting_cue": self.lighting_cue}


@dataclass
class StageShow:
    """ステージショー1つ分。clipsは再生する動画のリスト(順番に再生)。
    ダンスショーのように1本だけの場合もあれば、ファッションショーのように
    MV1・MV2…と複数本を「次のMV」ボタンで切り替える場合もある。動画(クリップ)を
    切り替えるたびに、そのクリップに紐付いた照明キューが送信される。"""

    id: str
    label: str
    clips: list[StageShowClip]
    volume: int
    # クリップ切り替え時・再生終了後の「空白」区間で、設定された待機画面の
    # 背景画像の代わりに黒背景を使うか(ファッションショーのように、動画と
    # 動画の間に暗転を挟みたい複数クリップのショー向け)。falseなら通常通り
    # standby_background_imageの設定に従う。
    black_background_on_gap: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "clips": [c.to_dict() for c in self.clips],
            "volume": self.volume,
            "black_background_on_gap": self.black_background_on_gap,
        }


@dataclass
class StageShowJob:
    """ステージショーを時刻指定で自動再生するための予定1件。stage_show_idで
    stage_shows.json側のStageShowを参照する(ショー自体の定義はここには
    持たない)。clip_indexは複数動画のショー(ファッションショー等)で
    どのクリップから再生を始めるか(既定0=先頭)。
    modeはBlackoutWindow.modeと同じ考え方で、適用する営業モード名。空文字なら
    営業モードを問わず常に適用される。"""

    id: str
    stage_show_id: str
    cron: dict[str, Any]
    enabled: bool = True
    clip_index: int = 0
    mode: str = ""
    # 「時間帯」頻度の分単位の有効時間帯(ScheduledJob.windowと同じ形式)。
    window: Optional[dict[str, int]] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "stage_show_id": self.stage_show_id,
            "cron": self.cron,
            "enabled": self.enabled,
            "clip_index": self.clip_index,
            "mode": self.mode,
        }
        if self.window is not None:
            d["window"] = self.window
        return d


@dataclass
class BlackoutWindow:
    """スケジュールジョブを一切実行しない時間帯(例: 12:00〜13:00は再生しない)。
    start > end の場合は深夜またぎ(例: 22:00〜翌2:00)として扱う。
    modeは適用する営業モード名(例: "通し営業")。空文字なら営業モードを問わず
    常に適用される(クローズ後の誤作動防止など、モードによらず効かせたい場合)。"""

    id: str
    label: str
    start_hour: int
    start_minute: int
    end_hour: int
    end_minute: int
    enabled: bool = True
    mode: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "start_hour": self.start_hour,
            "start_minute": self.start_minute,
            "end_hour": self.end_hour,
            "end_minute": self.end_minute,
            "enabled": self.enabled,
            "mode": self.mode,
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
    # 常時監視したいという運用要望により既定値は1秒(接続状態を常にほぼリアルタイムで
    # 把握できるようにするため)。
    poll_interval_seconds: int = 1
    http_timeout_seconds: int = 3

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "LinkConfig":
        return LinkConfig(
            enabled=bool(data.get("enabled", False)),
            peer_host=str(data.get("peer_host", "")),
            peer_port=int(data.get("peer_port", 8765)),
            listen_port=int(data.get("listen_port", 8765)),
            duck_volume_percent=int(data.get("duck_volume_percent", 20)),
            poll_interval_seconds=int(data.get("poll_interval_seconds", 1)),
            http_timeout_seconds=int(data.get("http_timeout_seconds", 3)),
        )


@dataclass
class LightingCue:
    """照明卓(Zero 88 FLX S24)のプレイバックをまとめて呼び出すための名前付きキュー。
    playback_numbersに複数指定すると、その全部に順番にGoコマンドを送信する。
    schedule.jsonのlighting_cuesで定義し、ScheduledJob/StageShowのlighting_cueから
    idで参照する。"""

    id: str
    label: str
    playback_numbers: list[int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "playback_numbers": self.playback_numbers,
        }


@dataclass
class LightingConfig:
    """照明卓との通信設定(settings.jsonの'lighting'セクション)。
    enabled=Falseの場合はOSC送信を一切行わない(実機未接続の開発機等でも安全)。"""

    enabled: bool = False
    host: str = "192.168.1.10"
    port: int = 8830

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "LightingConfig":
        return LightingConfig(
            enabled=bool(data.get("enabled", False)),
            host=str(data.get("host", "192.168.1.10")),
            port=int(data.get("port", 8830)),
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
    lighting: LightingConfig = field(init=False)
    # ステージショー待機画面の背景画像(media_root相対パス)。空文字なら黒背景。
    standby_background_image: str = field(init=False)
    # 「通常」モードのスライドショーで表示する画像フォルダー(media_root相対パス)。
    # 空文字なら何も表示しない(黒画面のまま)。
    slideshow_folder: str = field(init=False)
    # スライドショー1枚あたりの表示時間(秒)。
    slideshow_interval_seconds: int = field(init=False)
    # 起動時にこのアプリ以外の音声セッションをミュートするか(HDMI出力事故防止)。
    mute_other_audio_on_startup: bool = field(init=False)
    # スケジュール音源の再生開始を実発火時刻から何秒遅らせるか(秒、小数可)。
    # 発火時刻には無音でオーディオデバイス/デコーダーを準備しておき、この秒数後に
    # 本来の音量で再生を再開することで、再生開始直後の頭切れを防ぐ(0なら即座に再生)。
    playback_prepare_delay_seconds: float = field(init=False)
    # 通常/ショーモード中、スタッフの誤操作でカーソルが会場側の拡張ディスプレイに
    # 映り込まないよう、プライマリモニター内にカーソルを閉じ込めるか。ミラーリング
    # モード中は(このフラグに関わらず)常に制限しない。falseにすると常に制限しない。
    confine_cursor_to_primary_monitor: bool = field(init=False)

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
        self.lighting = LightingConfig.from_dict(settings.get("lighting", {}))
        self.standby_background_image = settings.get("standby_background_image", "")
        self.slideshow_folder = settings.get("slideshow_folder", "")
        self.slideshow_interval_seconds = int(settings.get("slideshow_interval_seconds", 8))
        self.mute_other_audio_on_startup = bool(settings.get("mute_other_audio_on_startup", True))
        self.playback_prepare_delay_seconds = max(
            0.0, float(settings.get("playback_prepare_delay_seconds", 1.0))
        )
        self.confine_cursor_to_primary_monitor = bool(
            settings.get("confine_cursor_to_primary_monitor", True)
        )

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

    def validate_media_files(self) -> list[tuple[str, str]]:
        """schedule.json/stage_shows.jsonで参照されている音源・動画ファイルが
        実際に存在するかをまとめてチェックする。営業モードは選択中のものに
        限らず全モードを対象にする(モード切替で初めて欠落が発覚するのを
        防ぐため)。戻り値は存在しないもののみ(表示名, 解決後のパス)のリスト。"""
        missing: list[tuple[str, str]] = []

        def _check(name: str, file: str) -> None:
            path = self.resolve_media(file)
            if not path.exists():
                missing.append((name, str(path)))

        data = self._read_schedule()
        for job in self._parse_jobs(data.get("common_jobs", [])):
            _check(job.name, job.file)
        for mode_jobs in data.get("modes", {}).values():
            for job in self._parse_jobs(mode_jobs):
                _check(job.name, job.file)
        for job in self._parse_jobs(data.get("manual_jobs", [])):
            _check(job.name, job.file)

        for show in self.load_stage_shows():
            for clip in show.clips:
                _check(show.label, clip.file)

        return missing

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
        shows = []
        for s in data.get("shows", []):
            if "clips" in s:
                clips = [
                    StageShowClip(file=c["file"], lighting_cue=c.get("lighting_cue", ""))
                    for c in s["clips"]
                ]
            else:
                # 旧形式(files配列 + ショー単位のlighting_cue)からの後方互換。
                # 全クリップにショー全体のキューを引き継いだ状態で読み込み、次回保存時に
                # クリップ単位のclips形式へ自動移行する。
                legacy_cue = s.get("lighting_cue", "")
                clips = [StageShowClip(file=f, lighting_cue=legacy_cue) for f in s.get("files", [])]
            shows.append(
                StageShow(
                    id=s["id"],
                    label=s["label"],
                    clips=clips,
                    volume=int(s.get("volume", 90)),
                    black_background_on_gap=bool(s.get("black_background_on_gap", False)),
                )
            )
        return shows

    def save_stage_shows(self, shows: list[StageShow]) -> None:
        data = _read_json(self.stage_shows_path)
        data["shows"] = [s.to_dict() for s in shows]
        _write_json(self.stage_shows_path, data)

    # ------------------------------------------------------------------
    # ショー予定(stage_show_schedule、schedule.json内。BlackoutWindowと同様
    # 単一のフラットな一覧で、mode指定によって適用する営業モードを絞る)
    # ------------------------------------------------------------------
    def load_stage_show_schedule(self) -> list[StageShowJob]:
        raw = self._read_schedule().get("stage_show_schedule", [])
        jobs = []
        for j in raw:
            window = j.get("window")
            if window is not None:
                window = {
                    "start_hour": int(window.get("start_hour", 0)),
                    "start_minute": int(window.get("start_minute", 0)),
                    "end_hour": int(window.get("end_hour", 23)),
                    "end_minute": int(window.get("end_minute", 59)),
                }
            jobs.append(
                StageShowJob(
                    id=j["id"],
                    stage_show_id=j["stage_show_id"],
                    cron=j.get("cron", {}),
                    enabled=bool(j.get("enabled", True)),
                    clip_index=int(j.get("clip_index", 0)),
                    mode=j.get("mode", ""),
                    window=window,
                )
            )
        return jobs

    def save_stage_show_schedule(self, jobs: list[StageShowJob]) -> None:
        data = self._read_schedule()
        data["stage_show_schedule"] = [j.to_dict() for j in jobs]
        _write_json(self.schedule_path, data)

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
                mode=b.get("mode", ""),
            )
            for b in raw
        ]

    def save_blackout_windows(self, windows: list[BlackoutWindow]) -> None:
        data = self._read_schedule()
        data["blackout_windows"] = [w.to_dict() for w in windows]
        _write_json(self.schedule_path, data)

    # ------------------------------------------------------------------
    # 照明キュー(lighting_cues)
    # ------------------------------------------------------------------
    def load_lighting_cues(self) -> list[LightingCue]:
        raw = self._read_schedule().get("lighting_cues", [])
        return [
            LightingCue(
                id=c["id"],
                label=c.get("label", c["id"]),
                playback_numbers=[int(n) for n in c.get("playback_numbers", [])],
            )
            for c in raw
        ]

    def save_lighting_cues(self, cues: list[LightingCue]) -> None:
        data = self._read_schedule()
        data["lighting_cues"] = [c.to_dict() for c in cues]
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
    def save_system_settings(
        self,
        device_name: str,
        link: LinkConfig,
        standby_background_image: str = "",
        slideshow_folder: str = "",
        slideshow_interval_seconds: int = 8,
        playback_prepare_delay_seconds: float = 1.0,
        confine_cursor_to_primary_monitor: bool = True,
    ) -> None:
        """GUIの「システム設定」画面から呼ばれる。settings.jsonに保存し、
        メモリ上のdevice_name/link/standby_background_image/slideshow_folder/
        slideshow_interval_seconds/playback_prepare_delay_seconds/
        confine_cursor_to_primary_monitorも即座に更新する(peer_host/peer_port/
        duck_volume_percent/poll_interval_secondsは次回通信・次回ポーリングから
        即座に反映される。enabled/listen_portの変更はサーバー再起動が必要。
        standby_background_imageは次回「ショー」表示への切り替え時から、
        slideshow_folderは次回「通常」表示への切り替え時から反映される。
        slideshow_interval_secondsはスライドショー表示中でも次のスライド切替
        タイミングから反映される。playback_prepare_delay_secondsは次回のスケジュール
        音源発火から反映される。confine_cursor_to_primary_monitorは呼び出し側が
        カーソル制限を再適用すれば即座に反映される)。"""
        data = _read_json(self.settings_path)
        data["device_name"] = device_name
        data["standby_background_image"] = standby_background_image
        data["slideshow_folder"] = slideshow_folder
        data["slideshow_interval_seconds"] = slideshow_interval_seconds
        data["playback_prepare_delay_seconds"] = playback_prepare_delay_seconds
        data["confine_cursor_to_primary_monitor"] = confine_cursor_to_primary_monitor
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
        self.standby_background_image = standby_background_image
        self.slideshow_folder = slideshow_folder
        self.slideshow_interval_seconds = slideshow_interval_seconds
        self.playback_prepare_delay_seconds = max(0.0, playback_prepare_delay_seconds)
        self.confine_cursor_to_primary_monitor = confine_cursor_to_primary_monitor

    # ------------------------------------------------------------------
    # 内部ヘルパー
    # ------------------------------------------------------------------
    def _read_schedule(self) -> dict[str, Any]:
        return _read_json(self.schedule_path)

    @staticmethod
    def _parse_jobs(raw: list[dict[str, Any]]) -> list[ScheduledJob]:
        jobs = []
        for j in raw:
            window = j.get("window")
            if window is not None:
                window = {
                    "start_hour": int(window.get("start_hour", 0)),
                    "start_minute": int(window.get("start_minute", 0)),
                    "end_hour": int(window.get("end_hour", 23)),
                    "end_minute": int(window.get("end_minute", 59)),
                }
            jobs.append(
                ScheduledJob(
                    id=j["id"],
                    name=j["name"],
                    file=j["file"],
                    volume=int(j.get("volume", 80)),
                    cron=j.get("cron", {}),
                    enabled=bool(j.get("enabled", True)),
                    lighting_cue=j.get("lighting_cue", ""),
                    window=window,
                )
            )
        return jobs


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
