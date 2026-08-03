"""起動時に自アプリ以外のWindows音声セッションをミュートする。

HDMI経由で会場スピーカーに直結している運用のため、通知音等の別アプリの音が
まぎれこむと現場事故になりかねない。そのため起動時に一度だけ、自プロセス以外の
全音声セッション(システムサウンドを含む)をミュートする。Windows専用の機能で
あり、pycaw(Core Audio APIのPythonラッパー)が使えない環境では何もせず
ログに残すのみに留める(ミュートに失敗してもアプリ本体の起動は継続する)。
"""
from __future__ import annotations

import logging
import os


def mute_other_audio_sessions(logger: logging.Logger) -> None:
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception:
        logger.warning("pycawが利用できないため、他アプリの音声ミュートをスキップしました")
        return

    own_pid = os.getpid()
    try:
        sessions = AudioUtilities.GetAllSessions()
    except Exception:
        logger.exception("音声セッション一覧の取得に失敗しました")
        return

    muted = 0
    for session in sessions:
        try:
            pid = session.Process.pid if session.Process is not None else None
            if pid == own_pid:
                continue
            volume = session.SimpleAudioVolume
            if volume is None:
                continue
            volume.SetMute(1, None)
            muted += 1
        except Exception:
            logger.exception("音声セッションのミュートに失敗しました(1件スキップ)")

    logger.info("起動時に自アプリ以外の音声セッションをミュートしました(%d件)", muted)
