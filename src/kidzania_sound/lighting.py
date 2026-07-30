"""照明卓(Zero 88 FLX S24, ZerOS)へのOSC(UDP)送信。

実機で確認済みの仕様(変更不可の前提):
    送信先: 192.168.1.10:8830 (UDP)
    コマンド書式: /zeros/playback/go/<プレイバック番号>
音源再生に連動して、対応するプレイバックのGoコマンドを送信する。
音響PC側は照明卓と同一LANセグメント(192.168.1.x/24、ゲートウェイなし)に
固定IPで接続されている必要がある(詳細は要件定義書を参照)。
"""
from __future__ import annotations

import logging
from typing import Optional

from pythonosc.udp_client import SimpleUDPClient

from .config import LightingConfig, LightingCue


class LightingController:
    """schedule.json/stage_shows.jsonのlighting_cue(照明キューid)から、
    対応するプレイバック番号にGoコマンドを送信する。config.enabled=Falseの
    場合は全メソッドがno-opになり、照明卓未接続の開発機でも安全に動作する。"""

    def __init__(self, config: LightingConfig, cues: list[LightingCue], logger: logging.Logger):
        self._logger = logger
        self._enabled = config.enabled
        self._cue_map: dict[str, list[int]] = {c.id: c.playback_numbers for c in cues}
        self._client: Optional[SimpleUDPClient] = None

        if self._enabled:
            try:
                self._client = SimpleUDPClient(config.host, config.port)
                self._logger.info("照明卓OSC送信先: %s:%s", config.host, config.port)
            except Exception:
                self._logger.exception("照明卓への接続準備に失敗しました。照明連携を無効化します")
                self._enabled = False

    def trigger_playback(self, playback_number: int) -> None:
        """プレイバック番号を直接指定してGoコマンドを送信する。"""
        if not self._enabled or self._client is None:
            return
        address = f"/zeros/playback/go/{playback_number}"
        try:
            self._client.send_message(address, [])
            self._logger.info("照明キューを送信しました: %s", address)
        except OSError:
            self._logger.exception("照明卓へのOSC送信に失敗しました: %s", address)

    def trigger_cue(self, cue_id: str) -> None:
        """schedule.jsonのlighting_cuesで定義したキューidから、対応する
        プレイバック番号全部にGoコマンドを送信する(空文字なら何もしない)。"""
        if not cue_id or not self._enabled:
            return
        numbers = self._cue_map.get(cue_id)
        if not numbers:
            self._logger.warning("照明キュー'%s'に対応するプレイバック番号が見つかりません", cue_id)
            return
        for number in numbers:
            self.trigger_playback(number)
