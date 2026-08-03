"""キッザニア館内音響自動化システム エントリーポイント。"""
from __future__ import annotations

import ctypes
import msvcrt
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from .config import AppConfig
from .gui import MainWindow
from .lighting import LightingController
from .link import LinkService
from .logging_setup import setup_logging
from .scheduler import JobScheduler
from .system_audio import mute_other_audio_sessions

# Windows: スリープ/画面消灯を抑止するためのフラグ
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def _base_dir() -> Path:
    # src/kidzania_sound/main.py から見てプロジェクトルートは2階層上
    return Path(__file__).resolve().parents[2]


def _make_process_dpi_aware() -> None:
    """モニターごとにDPI(拡大率)が異なる環境で、拡張ディスプレイの座標が
    ずれて計算されてしまうのを防ぐ。ウィンドウを1つも作る前に呼ぶ必要がある。"""
    if sys.platform != "win32":
        return
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _try_acquire_single_instance_lock(base_dir: Path):
    """常駐プロセスは1つのみという要件を守るため、二重起動をロックファイルで防ぐ。
    取得できればファイルハンドルを返す(プロセス終了時に自動的に解放される)。
    既に他プロセスが起動中ならNoneを返す。"""
    lock_path = base_dir / ".instance.lock"
    f = open(lock_path, "w")
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return None
    return f


def _show_already_running_message() -> None:
    tmp_root = tk.Tk()
    tmp_root.withdraw()
    messagebox.showerror(
        "起動できません",
        "キッザニア館内音響自動化システムは既に起動しています。\n"
        "二重に起動すると音声が二重に再生されるおそれがあるため、今回の起動は中止します。",
    )
    tmp_root.destroy()


def _prevent_sleep(enabled: bool) -> None:
    if not enabled or sys.platform != "win32":
        return
    ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    )


def _restore_sleep_settings() -> None:
    if sys.platform != "win32":
        return
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def _warn_missing_media(config: AppConfig, logger) -> None:
    """schedule.json/stage_shows.jsonが参照する音源・動画ファイルの存在確認。
    見つからないものがあってもアプリは起動を続け、ログとダイアログで
    知らせるのみに留める(誤操作防止・無人運用を止めないため)。"""
    missing = config.validate_media_files()
    if not missing:
        return

    for name, path in missing:
        logger.warning("ファイルが見つかりません(%s): %s", name, path)

    lines = [f"・{name}: {path}" for name, path in missing[:10]]
    if len(missing) > 10:
        lines.append(f"...ほか{len(missing) - 10}件(詳細はログを確認してください)")
    messagebox.showwarning(
        "音源・動画ファイルが見つかりません",
        "以下のファイルが見つかりませんでした。該当のスケジュール/ステージショーは"
        "再生に失敗します。ファイルの配置か設定を確認してください。\n\n" + "\n".join(lines),
    )


def main() -> None:
    _make_process_dpi_aware()

    base_dir = _base_dir()

    lock_file = _try_acquire_single_instance_lock(base_dir)
    if lock_file is None:
        _show_already_running_message()
        return

    config = AppConfig(base_dir=base_dir)
    logger = setup_logging(config.log_dir, config.log_level)

    logger.info("=== キッザニア館内音響自動化システム 起動 ===")
    _prevent_sleep(config.prevent_sleep)

    if config.mute_other_audio_on_startup:
        try:
            mute_other_audio_sessions(logger)
        except Exception:
            logger.exception("他アプリの音声ミュート処理で予期しないエラーが発生しました")

    try:
        import vlc
    except Exception:
        logger.exception(
            "python-vlc の読み込みに失敗しました。VLCメディアプレーヤー本体が"
            "インストールされているか確認してください。"
        )
        raise

    vlc_instance = vlc.Instance()

    link_service = LinkService(config, logger)
    lighting = LightingController(config.lighting, config.load_lighting_cues(), logger)

    scheduler = JobScheduler(
        config,
        vlc_instance,
        logger,
        lighting,
        on_playback_started=lambda label: link_service.notify_async(
            "/event/playback-started", {"label": label}
        ),
        on_playback_ended=lambda label: link_service.notify_async(
            "/event/playback-ended", {"label": label}
        ),
    )

    root = tk.Tk()

    def _reload_schedule() -> None:
        scheduler.reload()

    MainWindow(
        root, config, logger, vlc_instance, scheduler, on_reload_schedule=_reload_schedule,
        link_service=link_service, lighting=lighting,
    )

    _warn_missing_media(config, logger)

    scheduler.start()
    link_service.start()

    try:
        root.mainloop()
    finally:
        scheduler.shutdown()
        link_service.shutdown()
        _restore_sleep_settings()
        logger.info("=== システム終了 ===")
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        lock_file.close()


if __name__ == "__main__":
    main()
