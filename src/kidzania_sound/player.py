"""VLCエンジンを使った再生処理。

- AudioCue: スケジュール再生用の一回限りの音声再生(街時計音楽など)。
- FullscreenVideoPlayer: ステージショー用のフルスクリーン動画再生
  (音声・映像同期、Tkinterウィンドウに埋め込み)。
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

import vlc
from PIL import Image, ImageTk

from .config import AppConfig

# 通常の手動停止(緊急停止以外)で使うフェードアウトの時間・分割数。
# 緊急停止(stop()/stop_playback())はこれを使わず即座に止める。
_FADE_DURATION_SECONDS = 1.5
_FADE_STEPS = 20

# AudioCue.play()のprepare_delay(頭切れ対策の準備遅延)向け。
# Playing状態への遷移を待つ上限(通常は数十ms程度で遷移するが、遅い場合に
# 備えた安全弁。prepare_delay自体がこれより短ければそちらを優先する)。
_PREPARE_STATE_WAIT_TIMEOUT_SECONDS = 1.0
_PREPARE_POLL_INTERVAL_SECONDS = 0.01

_user32 = ctypes.windll.user32
_user32.SetWindowPos.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
_user32.SetWindowPos.restype = ctypes.c_bool

# カーソルを画面範囲に閉じ込める(外部ディスプレイへのカーソル移動防止)ための
# ClipCursor(NULLを渡すと解除)。RECTへのポインタを渡すのでNoneも渡せるよう
# argtypesをPOINTERにしておく(ctypesはPOINTER引数にNoneを渡すとNULLになる)。
_user32.ClipCursor.argtypes = [ctypes.POINTER(wintypes.RECT)]
_user32.ClipCursor.restype = wintypes.BOOL

_HWND_TOPMOST = ctypes.c_void_p(-1)
_SWP_SHOWWINDOW = 0x0040
_GA_ROOT = 2
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1


def _top_level_hwnd(hwnd: int) -> int:
    """tkinterのToplevel.winfo_id()はWS_CHILDスタイルの描画用子ウィンドウの
    HWNDを返す(overrideredirect時など)。SetWindowPosの座標は子ウィンドウでは
    親からの相対位置として扱われてしまうため、実際に画面上を動かせる
    トップレベル祖先ウィンドウをGetAncestorで取得してから使う必要がある。"""
    root_hwnd = ctypes.windll.user32.GetAncestor(wintypes.HWND(hwnd), _GA_ROOT)
    return root_hwnd or hwnd


def _move_window_to_monitor(hwnd: int, x: int, y: int, width: int, height: int) -> None:
    """TkinterのgeometryX/Y文字列は先頭の符号を「画面端からの位置指定」フラグ
    として扱うため、プライマリより左/上にある拡張ディスプレイ(負の座標)を
    正しく指定できない。そのためWin32 APIで直接ウィンドウ位置を指定する。"""
    target_hwnd = _top_level_hwnd(hwnd)
    _user32.SetWindowPos(ctypes.c_void_p(target_hwnd), _HWND_TOPMOST, x, y, width, height, _SWP_SHOWWINDOW)


def _primary_monitor_rect() -> wintypes.RECT:
    """プライマリモニターの範囲を返す(Windowsの仕様上、プライマリモニターの
    原点は常に仮想デスクトップ座標の(0, 0)になる)。カーソルをプライマリモニター
    内に閉じ込める(外部ディスプレイへの移動を防ぐ)際の範囲指定に使う。"""
    width = ctypes.windll.user32.GetSystemMetrics(_SM_CXSCREEN)
    height = ctypes.windll.user32.GetSystemMetrics(_SM_CYSCREEN)
    return wintypes.RECT(0, 0, width, height)


def clamp_volume(volume: int) -> int:
    return max(0, min(100, volume))


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


_MONITORINFOF_PRIMARY = 0x1
_MonitorEnumProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)


def _enum_monitors(logger: logging.Logger) -> list:
    """Win32 APIから接続中のモニター一覧(座標・プライマリ判定)を取得する。
    サードパーティ実装(screeninfo等)はプライマリ判定を誤ることがあったため、
    OSの一次情報(GetMonitorInfoWのMONITORINFOF_PRIMARYフラグ)を直接使う。"""
    monitors: list = []

    def _callback(hmonitor, _hdc, _rect_ptr, _lparam):
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if ctypes.windll.user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            rect = info.rcMonitor
            monitors.append(
                SimpleNamespace(
                    x=rect.left,
                    y=rect.top,
                    width=rect.right - rect.left,
                    height=rect.bottom - rect.top,
                    is_primary=bool(info.dwFlags & _MONITORINFOF_PRIMARY),
                )
            )
        return True

    try:
        ctypes.windll.user32.EnumDisplayMonitors(None, None, _MonitorEnumProc(_callback), 0)
    except Exception:
        logger.exception("モニター情報の取得に失敗しました")
    return monitors


def _select_extended_monitor(logger: logging.Logger):
    """ステージショー/スライドショーを映す拡張ディスプレイ(プライマリではない
    モニター)を選ぶ。見つからない場合(1台構成、未接続等)はNoneを返す。
    この関数は接続状態の監視ループから毎秒呼ばれるため、ここではログを
    出さない(呼び出し側で接続状態が変化した時だけログを出す)。"""
    monitors = _enum_monitors(logger)
    for m in monitors:
        if not m.is_primary:
            return m
    return None


class AudioCue:
    """スケジュールジョブ用の一回限りの音声再生。再生終了後は自動的に破棄される。
    手動で途中停止することもできる(stop())。"""

    def __init__(self, instance: vlc.Instance, logger: logging.Logger):
        self._instance = instance
        self._logger = logger
        self._player: Optional[vlc.MediaPlayer] = None
        self._on_finished: Optional[Callable[[], None]] = None
        self._base_volume: int = 0
        self._ducked: bool = False
        self._fading: bool = False

    def play(
        self,
        path: Path,
        volume: int,
        label: str,
        on_finished: Optional[Callable[[], None]] = None,
        prepare_delay: float = 0.0,
        on_ready: Optional[Callable[[], None]] = None,
    ) -> None:
        """音源を再生する。prepare_delayを正の値にすると、スケジュール発火時刻
        ちょうどに再生を開始せず、まず無音状態でオーディオデバイス/デコーダーの
        準備だけを済ませたうえで一時停止し、prepare_delay秒後に本来の音量で
        再生を再開する(オーディオデバイスが直前まで無音だった場合に起きがちな
        再生開始直後の頭切れ対策)。0以下なら従来通り即座に再生する。
        on_readyは実際に音が出始める瞬間(prepare_delayが0以下なら即座に、
        正の値ならその秒数後)に呼ばれる。照明キューのように音の出始めと
        同期させたい処理はこちらに渡す。"""
        self._on_finished = on_finished

        if not path.exists():
            self._logger.error("音源ファイルが見つかりません(%s): %s", label, path)
            if on_finished is not None:
                on_finished()
            return

        try:
            media = self._instance.media_new(str(path))
            player = self._instance.media_player_new()
            player.set_media(media)
            self._base_volume = clamp_volume(volume)
            self._ducked = False

            events = player.event_manager()
            events.event_attach(vlc.EventType.MediaPlayerEndReached, self._make_cleanup(player, label))
            events.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._make_error_handler(player, label))

            self._player = player

            if prepare_delay > 0:
                # 無音のまま再生開始し、実際にPlaying状態へ遷移し次第すぐ一時停止する
                # ことで、頭出し位置(≒0秒)を保ったままオーディオデバイスと
                # デコーダーの初期化だけを先に済ませておく。実際に音が出るのは
                # prepare_delay秒後、_prepare_then_play()が一時停止を解除した時点から。
                player.audio_set_volume(0)
                player.play()
                self._logger.info(
                    "再生準備開始(%s): %s (volume=%d, 準備遅延=%.2f秒)", label, path.name, volume, prepare_delay
                )
                threading.Thread(
                    target=self._prepare_then_play, args=(player, label, prepare_delay, on_ready), daemon=True
                ).start()
            else:
                player.audio_set_volume(self._base_volume)
                player.play()
                self._logger.info("再生開始(%s): %s (volume=%d)", label, path.name, volume)
                if on_ready is not None:
                    on_ready()
        except Exception:
            self._logger.exception("再生に失敗しました(%s): %s", label, path)
            if on_finished is not None:
                on_finished()

    def _prepare_then_play(
        self, player: vlc.MediaPlayer, label: str, delay: float, on_ready: Optional[Callable[[], None]]
    ) -> None:
        started = time.monotonic()
        deadline = started + min(delay, _PREPARE_STATE_WAIT_TIMEOUT_SECONDS)
        while time.monotonic() < deadline:
            if self._player is not player:
                return
            try:
                state = player.get_state()
            except Exception:
                return
            if state == vlc.State.Playing:
                try:
                    player.set_pause(1)
                except Exception:
                    pass
                break
            if state in (vlc.State.Error, vlc.State.Ended, vlc.State.Stopped):
                return
            time.sleep(_PREPARE_POLL_INTERVAL_SECONDS)

        remaining = delay - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)

        # 待機中にstop()/fade_out_and_stop()等で別のプレイヤーに差し替わっている
        # (または解放済みの)場合は何もしない。
        if self._player is not player:
            return
        try:
            # duck()が待機中に呼ばれていた場合はそちらの音量を優先し、
            # そうでなければ本来の音量に戻したうえで一時停止を解除する。
            if not self._ducked:
                player.audio_set_volume(self._base_volume)
            player.set_pause(0)
            self._logger.info("再生開始(%s)", label)
        except Exception:
            self._logger.exception("準備後の再生開始に失敗しました(%s)", label)
            return
        if on_ready is not None:
            on_ready()

    def stop(self) -> None:
        """即座に停止する(緊急停止向け。GUIの「現在再生中」パネルからは
        通常fade_out_and_stop()を使う)。"""
        player = self._player
        if player is None:
            return
        self._player = None
        # 自然終了時のクリーンアップ(別スレッドでstop()/release()する経路)と
        # 二重に解放してしまわないよう、先にイベントハンドラを外しておく。
        try:
            events = player.event_manager()
            events.event_detach(vlc.EventType.MediaPlayerEndReached)
            events.event_detach(vlc.EventType.MediaPlayerEncounteredError)
        except Exception:
            pass
        try:
            player.stop()
            player.release()
        except Exception:
            self._logger.exception("再生の手動停止に失敗しました")

    def fade_out_and_stop(self) -> None:
        """フェードアウトしながら停止する(GUIの「現在再生中」パネルの
        個別「■ 停止」ボタンなど、通常の手動停止向け)。既に停止済み・
        フェード中なら何もしない。別スレッドで音量を段階的に下げてから
        最終的にstop()で停止する。途中でstop()が別経路(緊急停止等)から
        呼ばれた場合は、次のステップでそれを検知して静かに終了する。"""
        player = self._player
        if player is None or self._fading:
            return
        self._fading = True
        start_volume = self._base_volume

        def _run() -> None:
            try:
                for i in range(_FADE_STEPS - 1, 0, -1):
                    if self._player is not player:
                        return
                    try:
                        player.audio_set_volume(int(start_volume * i / _FADE_STEPS))
                    except Exception:
                        return
                    time.sleep(_FADE_DURATION_SECONDS / _FADE_STEPS)
                if self._player is player:
                    self.stop()
            finally:
                self._fading = False

        threading.Thread(target=_run, daemon=True).start()

    def duck(self, percent: int) -> None:
        """連携先端末が再生を開始した際、元の音量のpercent%まで一時的に下げる。
        既にダッキング済みなら何もしない(多重に下げてしまうのを防ぐ)。"""
        if self._player is None or self._ducked:
            return
        self._ducked = True
        try:
            self._player.audio_set_volume(clamp_volume(self._base_volume * clamp_volume(percent) // 100))
        except Exception:
            self._logger.exception("ダッキングに失敗しました")

    def restore(self) -> None:
        """duck()で下げた音量を元に戻す。"""
        if self._player is None or not self._ducked:
            return
        self._ducked = False
        try:
            self._player.audio_set_volume(self._base_volume)
        except Exception:
            self._logger.exception("音量復帰に失敗しました")

    def set_volume(self, percent: int) -> None:
        """再生中の音量を即座に変更する(テストモードでのリアルタイム音量調整向け)。
        以後duck()/restore()が基準にする音量もこの値に更新される。"""
        if self._player is None:
            return
        self._base_volume = clamp_volume(percent)
        self._ducked = False
        try:
            self._player.audio_set_volume(self._base_volume)
        except Exception:
            self._logger.exception("音量変更に失敗しました")

    def get_volume(self) -> int:
        """現在の基準音量(duckで下げる前の値)を返す。GUIのスライダー初期表示向け。"""
        return self._base_volume

    def _make_cleanup(self, player: vlc.MediaPlayer, label: str) -> Callable:
        def _cleanup(_event) -> None:
            self._logger.info("再生終了(%s)", label)
            # libvlcのイベントコールバックスレッド上でstop()/release()を呼ぶと
            # デッドロックする既知の問題があるため、別スレッドに逃がす。
            threading.Thread(target=self._release_player, args=(player, label), daemon=True).start()

        return _cleanup

    def _make_error_handler(self, player: vlc.MediaPlayer, label: str) -> Callable:
        def _on_error(_event) -> None:
            self._logger.error("再生中にエラーが発生しました(%s)", label)
            threading.Thread(target=self._release_player, args=(player, label), daemon=True).start()

        return _on_error

    def _release_player(self, player: vlc.MediaPlayer, label: str) -> None:
        try:
            player.stop()
            player.release()
        except Exception:
            self._logger.exception("再生リソースの解放に失敗しました(%s)", label)
        finally:
            self._player = None
            if self._on_finished is not None:
                self._on_finished()


class FullscreenVideoPlayer:
    """ステージショー動画を拡張ディスプレイ(会場側スクリーン)にフルスクリーン
    表示する。あわせて、拡張ディスプレイの「表示モード」(通常/ショー/
    ミラーリング)の管理と、拡張ディスプレイの接続状態の監視もこのクラスが担う。

    - 通常(NORMAL): 設定フォルダー内の画像をスライドショーで表示する
      (広告・注意事項など、ショーが無い通常時の表示)
    - ショー(SHOW): 待機画面(背景画像 or 黒)を表示する。ステージショーの
      ボタンを押すとその画面に動画を流す。動画再生後もウィンドウ(待機画面)は
      保持し、スタッフが明示的に停止/モード切替するまでWindowsデスクトップを
      露出させない
    - ミラーリング(MIRROR): ウィンドウを閉じ、拡張ディスプレイをWindowsの
      通常の拡張デスクトップとして露出させる(PC画面をそのまま出力する用途)

    ファッションショーのように複数動画(MV1/MV2…)を持つショーは next_clip() で
    次の動画へ切り替えられる(自動では次に進まない)。
    """

    DISPLAY_MODE_NORMAL = "normal"
    DISPLAY_MODE_SHOW = "show"
    DISPLAY_MODE_MIRROR = "mirror"

    _MONITOR_POLL_INTERVAL_MS = 2000
    _SLIDESHOW_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp"}

    def __init__(
        self,
        root: tk.Tk,
        instance: vlc.Instance,
        logger: logging.Logger,
        config: AppConfig,
        on_monitor_status_changed: Optional[Callable[[bool], None]] = None,
    ):
        self._root = root
        self._instance = instance
        self._logger = logger
        self._config = config
        self._on_monitor_status_changed = on_monitor_status_changed
        self._window: Optional[tk.Toplevel] = None
        self._monitor = None
        self._player: Optional[vlc.MediaPlayer] = None
        self._poll_job: Optional[str] = None
        self._monitor_poll_job: Optional[str] = None
        self._monitor_connected: bool = False
        self._display_mode: str = self.DISPLAY_MODE_MIRROR
        self._on_finished: Optional[Callable[[], None]] = None
        self._playlist: list[Path] = []
        self._playlist_index: int = -1
        self._volume: int = 80
        self._label: str = ""
        self._ducked: bool = False
        self._fading: bool = False
        # 待機画面/スライドショーの背景画像(ウィンドウが存在する間、参照保持してGCを防ぐ)。
        self._background_photo: Optional[ImageTk.PhotoImage] = None
        self._background_label: Optional[tk.Label] = None
        # スライドショー(通常モード)の状態。
        self._slideshow_files: list[Path] = []
        self._slideshow_index: int = -1
        self._slideshow_job: Optional[str] = None

    def is_playing(self) -> bool:
        """実際に動画を再生中かどうか(待機中の黒画面はTrueにしない)。"""
        return self._player is not None

    def has_standby_window(self) -> bool:
        return self._window is not None

    def has_multiple_clips(self) -> bool:
        return self._playlist_index >= 0 and len(self._playlist) > 1

    def current_clip_index(self) -> int:
        return self._playlist_index

    def duck(self, percent: int) -> None:
        """連携先端末が再生を開始した際、元の音量のpercent%まで一時的に下げる。"""
        if self._player is None or self._ducked:
            return
        self._ducked = True
        try:
            self._player.audio_set_volume(clamp_volume(self._volume * clamp_volume(percent) // 100))
        except Exception:
            self._logger.exception("ダッキングに失敗しました")

    def restore(self) -> None:
        """duck()で下げた音量を元に戻す。"""
        if self._player is None or not self._ducked:
            return
        self._ducked = False
        try:
            self._player.audio_set_volume(self._volume)
        except Exception:
            self._logger.exception("音量復帰に失敗しました")

    # ------------------------------------------------------------------
    # 拡張ディスプレイの接続監視(通常/ショー/ミラーリング共通)
    # ------------------------------------------------------------------
    def is_monitor_connected(self) -> bool:
        return self._monitor_connected

    def start_monitor_watch(self) -> None:
        """アプリ起動時に一度だけ呼ぶ。以後は常時、拡張ディスプレイの接続状態を
        定期的に確認する。以前は「見つかりません」というログを検出できるまで
        繰り返し出し続けていたが、運用上ログが埋もれてしまうため、接続状態が
        変化した時だけログとGUI通知(モニター: 接続中/未接続)を行う方式に変更した。
        通常/ショーモード中に未接続→接続へ変化した場合は、その時点で待機画面/
        スライドショーのウィンドウを自動的に開く。"""
        self._poll_monitor()

    def _poll_monitor(self) -> None:
        monitor = _select_extended_monitor(self._logger)
        connected = monitor is not None
        if connected != self._monitor_connected:
            self._monitor_connected = connected
            if connected:
                self._logger.info(
                    "拡張ディスプレイを検出しました (%dx%d+%d+%d)",
                    monitor.width, monitor.height, monitor.x, monitor.y,
                )
            else:
                self._logger.warning("拡張ディスプレイが見つかりません")
            if self._on_monitor_status_changed is not None:
                self._on_monitor_status_changed(connected)

        if connected and self._window is None and self._display_mode != self.DISPLAY_MODE_MIRROR:
            self._create_window(self._window_label_for_mode(), monitor)
            self._refresh_window_content()

        self._monitor_poll_job = self._root.after(self._MONITOR_POLL_INTERVAL_MS, self._poll_monitor)

    # ------------------------------------------------------------------
    # 表示モード(通常/ショー/ミラーリング)
    # ------------------------------------------------------------------
    def _window_label_for_mode(self) -> str:
        return "スライドショー" if self._display_mode == self.DISPLAY_MODE_NORMAL else "ステージショー待機画面"

    def set_display_mode(self, mode: str) -> None:
        """拡張ディスプレイの表示モードを切り替える。
        - ミラーリング: 再生・待機画面/スライドショーを閉じ、拡張ディスプレイを
          Windowsの通常のデスクトップとして露出させる
        - 通常/ショー: 拡張ディスプレイが見つかっていればウィンドウを開き、
          モードに応じた内容(スライドショー/待機画面)を表示する。見つからない
          場合は接続監視ループ(_poll_monitor)が検出でき次第自動的に開く"""
        self._display_mode = mode
        self.apply_cursor_confinement()
        if mode == self.DISPLAY_MODE_MIRROR:
            self.force_close()
            return

        if self._window is None:
            monitor = _select_extended_monitor(self._logger)
            if monitor is not None:
                self._create_window(self._window_label_for_mode(), monitor)
        else:
            self._window.title(self._window_label_for_mode())
        self._refresh_window_content()

    # ------------------------------------------------------------------
    # カーソル制限(外部ディスプレイへのカーソル移動防止)
    # ------------------------------------------------------------------
    def apply_cursor_confinement(self) -> None:
        """通常/ショーモードでは、営業中のスタッフの誤操作でカーソルが会場側の
        拡張ディスプレイに映り込まないよう、プライマリモニター内にカーソルを
        閉じ込める。ミラーリングモード(PC画面を意図的に拡張ディスプレイへ
        出力している状態)や、設定でこの機能自体がOFFの場合は制限しない。
        表示モードの切り替え時に加え、設定画面での有効/無効切り替え直後にも
        即座に反映させたい場合に呼び直せるよう公開メソッドにしている。"""
        if not self._config.confine_cursor_to_primary_monitor or self._display_mode == self.DISPLAY_MODE_MIRROR:
            self.release_cursor_confinement()
            return
        try:
            rect = _primary_monitor_rect()
            _user32.ClipCursor(ctypes.byref(rect))
        except Exception:
            self._logger.exception("カーソルの範囲制限に失敗しました")

    def release_cursor_confinement(self) -> None:
        """カーソルの範囲制限を解除する(ミラーリング切替時・設定OFF時・
        アプリ終了時に呼ぶ)。制限していない状態で呼んでも無害。"""
        try:
            _user32.ClipCursor(None)
        except Exception:
            self._logger.exception("カーソルの範囲制限解除に失敗しました")

    def _refresh_window_content(self) -> None:
        """現在の表示モードに応じて、待機画面(背景画像/黒)かスライドショーかを
        ウィンドウに反映する。ウィンドウが無ければ何もしない(次にウィンドウが
        開かれた時点でこのメソッドが再度呼ばれる)。"""
        if self._window is None:
            return
        self._stop_slideshow()
        if self._display_mode == self.DISPLAY_MODE_NORMAL:
            self._start_slideshow()
        else:
            self._apply_standby_background()

    def force_close(self) -> None:
        """状態に関わらず再生・待機画面/スライドショーを強制的に閉じる
        (ミラーリングへの切り替え時に使う)。"""
        self._stop_slideshow()
        self._stop_playback_keep_window()
        self._close_window()

    def request_stop(self) -> None:
        """ESCキー/停止ボタン共通の1段階分の停止。
        再生中なら待機画面を保持したまま停止し、既に待機中(待機画面/
        スライドショー)ならウィンドウを閉じる(ウィンドウは接続監視ループにより
        現在のモードが通常/ショーのままであれば自動的に再度開く)。"""
        if self._player is not None:
            self._stop_playback_keep_window()
            if self._on_finished is not None:
                self._on_finished()
        elif self._window is not None:
            self._close_window()

    def stop_playback(self) -> None:
        """緊急停止向け: 再生中なら即座に停止するが、待機黒画面・ステージショー
        モードは一切解除しない(request_stop()と異なり、待機画面を閉じることが
        ないため拡張ディスプレイにWindowsのデスクトップが露出する心配がない)。
        何も再生していなければ何もしない。フェードはかけない(緊急停止用)。"""
        if self._player is not None:
            self._stop_playback_keep_window()
            if self._on_finished is not None:
                self._on_finished()

    def fade_out_and_stop(self) -> None:
        """フェードアウトしながら再生を停止する(待機画面は保持)。個別の
        「■ 停止」ボタンなど、通常の手動停止向け(緊急停止はstop_playback()で
        フェードなしに即座に止める)。既に停止済み・フェード中なら何もしない。
        on_finishedコールバックがGUIウィジェットを操作するため、別スレッドは
        使わずroot.after()の連鎖でTkinterメインスレッド上だけで音量を段階的に
        下げる(TkinterはTclの都合上メインスレッド以外からの操作が安全でない
        ため)。途中でstop_playback()等が別経路から呼ばれた場合は、次の
        ステップでそれを検知して静かに終了する。"""
        player = self._player
        if player is None or self._fading:
            return
        self._fading = True
        self._fade_step(player, self._volume, _FADE_STEPS - 1)

    def _fade_step(self, player: vlc.MediaPlayer, start_volume: int, remaining: int) -> None:
        if self._player is not player:
            self._fading = False
            return
        if remaining <= 0:
            self._fading = False
            self._finish_fade_stop()
            return
        try:
            player.audio_set_volume(int(start_volume * remaining / _FADE_STEPS))
        except Exception:
            self._fading = False
            return
        self._root.after(
            int(1000 * _FADE_DURATION_SECONDS / _FADE_STEPS),
            lambda: self._fade_step(player, start_volume, remaining - 1),
        )

    def _finish_fade_stop(self) -> None:
        if self._player is not None:
            self._stop_playback_keep_window()
            if self._on_finished is not None:
                self._on_finished()

    # ------------------------------------------------------------------
    # 再生
    # ------------------------------------------------------------------
    def play(
        self,
        files: list[Path],
        volume: int,
        label: str,
        on_finished: Callable[[], None],
        start_index: int = 0,
        black_background_on_gap: bool = False,
    ) -> None:
        if self.is_playing():
            self._logger.warning("既に別の動画を再生中のため、再生要求を無視しました(%s)", label)
            return

        if not files:
            self._logger.error("再生対象のファイルが設定されていません(%s)", label)
            on_finished()
            return

        if self._window is None:
            monitor = _select_extended_monitor(self._logger)
            if monitor is None:
                self._logger.error(
                    "拡張ディスプレイが見つからないため再生できません(%s)。ディスプレイ接続を確認してください", label
                )
                on_finished()
                return
            self._create_window(label, monitor)
            self._refresh_window_content()
        else:
            self._window.title(label)

        self._set_gap_background_black(black_background_on_gap)

        self._playlist = files
        self._playlist_index = start_index if 0 <= start_index < len(files) else 0
        self._volume = volume
        self._label = label
        self._on_finished = on_finished

        self._play_current_clip()

    def next_clip(self) -> Optional[int]:
        """複数動画を持つショーで、次の動画へ切り替える(手動のみ、自動進行はしない)。
        切り替え後のクリップindexを返す(切り替えなかった場合はNone。呼び出し側が
        そのクリップに対応する照明キューを発火する際に使う)。"""
        if not self.has_multiple_clips():
            return None
        self._stop_playback_keep_window()
        self._playlist_index = (self._playlist_index + 1) % len(self._playlist)
        self._play_current_clip()
        return self._playlist_index

    def jump_to_clip(self, index: int) -> Optional[int]:
        """複数動画を持つショーで、任意のクリップへ直接切り替える(ファッションショーの
        個別ボタン等、順送りでない選択向け)。再生中でない、または範囲外のindexなら
        Noneを返す(呼び出し側は何もしない)。既に指定クリップを再生中なら何もせず
        そのindexを返す。"""
        if self._player is None or not (0 <= index < len(self._playlist)):
            return None
        if index == self._playlist_index:
            return self._playlist_index
        self._stop_playback_keep_window()
        self._playlist_index = index
        self._play_current_clip()
        return self._playlist_index
        return self._playlist_index

    def _play_current_clip(self) -> None:
        path = self._playlist[self._playlist_index]
        if not path.exists():
            self._logger.error("動画ファイルが見つかりません(%s): %s", self._label, path)
            if self._on_finished is not None:
                self._on_finished()
            return

        try:
            media = self._instance.media_new(str(path))
            player = self._instance.media_player_new()
            player.set_media(media)
            player.audio_set_volume(clamp_volume(self._volume))
            self._window.update_idletasks()
            player.set_hwnd(self._window.winfo_id())

            self._ducked = False
            self._player = player
            player.play()
            self._logger.info(
                "ステージショー再生開始: %s (%d/%d: %s)",
                self._label,
                self._playlist_index + 1,
                len(self._playlist),
                path.name,
            )
            self._poll_end_state()
        except Exception:
            self._logger.exception("ステージショー再生に失敗しました(%s): %s", self._label, path)
            if self._player is not None:
                try:
                    self._player.stop()
                    self._player.release()
                except Exception:
                    pass
                self._player = None
            if self._on_finished is not None:
                self._on_finished()

    def _ensure_background_label(self) -> tk.Label:
        """待機画面/スライドショー用の画像を表示するLabelを1つだけ用意する。
        モード切替のたびに作り直すと、古いLabelが破棄されずウィンドウの子として
        残り続けてしまう(数値上は見えないが積み重なる)ため、ウィンドウが
        存在する間は使い回す。"""
        if self._background_label is None:
            self._background_label = tk.Label(self._window, bd=0, highlightthickness=0)
        return self._background_label

    def _set_background_image(self, path: Optional[Path]) -> None:
        """待機画面/スライドショー用に、背景ラベルへ1枚の画像を設定する。
        pathがNoneなら何も表示しない(黒背景のまま)。動画再生中はVLCの映像が
        この背景画像の上に重なって表示される(set_hwndでウィンドウ全面に
        描画されるため)。"""
        if self._window is None:
            return
        if path is None:
            self._background_photo = None
            if self._background_label is not None:
                self._background_label.place_forget()
            return
        try:
            image = Image.open(path).convert("RGB").resize((self._monitor.width, self._monitor.height))
            self._background_photo = ImageTk.PhotoImage(image)
            label = self._ensure_background_label()
            label.configure(image=self._background_photo)
            label.place(x=0, y=0, relwidth=1, relheight=1)
        except Exception:
            self._logger.exception("背景画像の読み込みに失敗しました: %s", path)
            self._background_photo = None

    def _apply_standby_background(self) -> None:
        """「ショー」モードの待機画面の背景を設定する。
        config.standby_background_imageが空なら何もしない(黒背景のまま)。"""
        image_setting = self._config.standby_background_image
        if not image_setting:
            self._set_background_image(None)
            return
        path = self._config.resolve_media(image_setting)
        if not path.exists():
            self._logger.error("待機画面の背景画像が見つかりません: %s", path)
            self._set_background_image(None)
            return
        self._set_background_image(path)

    # ------------------------------------------------------------------
    # スライドショー(「通常」モード)
    # ------------------------------------------------------------------
    def _start_slideshow(self) -> None:
        folder = self._config.slideshow_folder
        if not folder:
            self._set_background_image(None)
            return
        dir_path = self._config.resolve_media(folder)
        files = (
            sorted(p for p in dir_path.iterdir() if p.suffix.lower() in self._SLIDESHOW_IMAGE_SUFFIXES)
            if dir_path.is_dir()
            else []
        )
        if not files:
            self._logger.warning("スライドショー用フォルダーに画像が見つかりません: %s", dir_path)
            self._set_background_image(None)
            return
        self._slideshow_files = files
        self._slideshow_index = 0
        self._show_slideshow_frame()

    def _show_slideshow_frame(self) -> None:
        if self._window is None or not self._slideshow_files:
            return
        path = self._slideshow_files[self._slideshow_index]
        self._set_background_image(path)
        self._slideshow_index = (self._slideshow_index + 1) % len(self._slideshow_files)
        interval_ms = max(1, self._config.slideshow_interval_seconds) * 1000
        self._slideshow_job = self._root.after(interval_ms, self._show_slideshow_frame)

    def _stop_slideshow(self) -> None:
        if self._slideshow_job is not None:
            try:
                self._root.after_cancel(self._slideshow_job)
            except Exception:
                pass
            self._slideshow_job = None
        self._slideshow_files = []
        self._slideshow_index = -1

    def _set_gap_background_black(self, black: bool) -> None:
        """クリップ切り替え時・再生終了後の空白区間で、設定された背景画像の
        代わりに黒背景(ウィンドウ自体の黒地)を見せるかどうかを切り替える。
        動画再生中はVLCの映像がこのラベルの上に重なって表示されるため、
        再生中かどうかに関わらず切り替えて構わない(見た目に影響しない)。"""
        if self._background_label is None:
            return
        try:
            if black:
                self._background_label.place_forget()
            else:
                self._background_label.place(x=0, y=0, relwidth=1, relheight=1)
        except Exception:
            self._logger.exception("背景表示の切り替えに失敗しました")

    def _create_window(self, label: str, monitor) -> None:
        window = tk.Toplevel(self._root)
        window.title(label)
        window.configure(bg="black")
        window.overrideredirect(True)
        window.geometry(f"{monitor.width}x{monitor.height}")
        window.update_idletasks()

        window.attributes("-topmost", True)
        window.bind("<Escape>", lambda _e: self.request_stop())
        window.focus_force()
        # ESCキー自体は引き続き有効(操作用)。ただし待機画面/スライドショーは
        # ゲスト側モニターにも表示されるため、案内文などの表示は一切出さない
        # (画面内容(背景画像/スライドショー/黒)のみを表示する)。

        # TkのgeometryX/Y文字列は先頭の符号を「画面端からの位置指定」フラグとして
        # 扱うため負の座標(プライマリより左/上の拡張ディスプレイ)を指定できず、
        # またwinfo_id()はWS_CHILDの描画用子ウィンドウを返す(overrideredirect時)
        # ため、そのままSetWindowPosしても親からの相対位置として扱われ画面に
        # 反映されない。そのためWin32 APIで真のトップレベル祖先ウィンドウに対して
        # 直接位置指定する。-topmost等の属性設定より後に行わないとTk側の内部
        # 状態で位置が上書きされてしまうため、最後に実行する。
        window.update_idletasks()
        _move_window_to_monitor(window.winfo_id(), monitor.x, monitor.y, monitor.width, monitor.height)
        self._logger.info(
            "拡張ディスプレイ用ウィンドウを配置しました (%dx%d+%d+%d, primary=%s)",
            monitor.width, monitor.height, monitor.x, monitor.y, monitor.is_primary,
        )

        self._window = window
        self._monitor = monitor
        self._background_photo = None
        self._background_label = None

    def _poll_end_state(self) -> None:
        if self._player is None:
            return
        state = self._player.get_state()
        if state in (vlc.State.Ended, vlc.State.Error, vlc.State.Stopped):
            self._stop_playback_keep_window()
            if self._on_finished is not None:
                self._on_finished()
            return
        self._poll_job = self._root.after(300, self._poll_end_state)

    def _stop_playback_keep_window(self) -> None:
        if self._poll_job is not None:
            try:
                self._root.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None
        if self._player is not None:
            try:
                self._player.stop()
                self._player.release()
            except Exception:
                self._logger.exception("動画プレイヤーの解放に失敗しました")
            self._player = None
        self._logger.info("再生を停止しました(待機画面を保持)")

    def _close_window(self) -> None:
        self._stop_slideshow()
        self._playlist = []
        self._playlist_index = -1
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
            self._window = None
        self._monitor = None
        self._background_photo = None
        self._background_label = None
        self._logger.info("待機画面/スライドショーを閉じました")
