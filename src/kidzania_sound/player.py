"""VLCエンジンを使った再生処理。

- AudioCue: スケジュール再生用の一回限りの音声再生(街時計音楽など)。
- FullscreenVideoPlayer: ステージショー用のフルスクリーン動画再生
  (音声・映像同期、Tkinterウィンドウに埋め込み)。
"""
from __future__ import annotations

import ctypes
import logging
import threading
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

import vlc
from PIL import Image, ImageTk

from .config import AppConfig

_user32 = ctypes.windll.user32
_user32.SetWindowPos.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
_user32.SetWindowPos.restype = ctypes.c_bool

_HWND_TOPMOST = ctypes.c_void_p(-1)
_SWP_SHOWWINDOW = 0x0040
_GA_ROOT = 2


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
    """ステージショーを映す拡張ディスプレイ(プライマリではないモニター)を選ぶ。
    見つからない場合(1台構成、未接続等)はNoneを返す。呼び出し側は
    見つかるまでバックグラウンドで待機し、メインディスプレイには何も表示しない。"""
    monitors = _enum_monitors(logger)
    logger.info(
        "検出したモニター一覧: %s",
        [f"{m.width}x{m.height}+{m.x}+{m.y}(primary={m.is_primary})" for m in monitors],
    )
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

    def play(self, path: Path, volume: int, label: str, on_finished: Optional[Callable[[], None]] = None) -> None:
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
            player.audio_set_volume(self._base_volume)

            events = player.event_manager()
            events.event_attach(vlc.EventType.MediaPlayerEndReached, self._make_cleanup(player, label))
            events.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._make_error_handler(player, label))

            self._player = player
            player.play()
            self._logger.info("再生開始(%s): %s (volume=%d)", label, path.name, volume)
        except Exception:
            self._logger.exception("再生に失敗しました(%s): %s", label, path)
            if on_finished is not None:
                on_finished()

    def stop(self) -> None:
        """手動での途中停止(GUIの「現在再生中」パネルから呼ばれる)。"""
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
    表示する。

    「ステージショーモード」がONの間、拡張ディスプレイには黒画面を常時表示して
    おき(待機状態)、ステージショーのボタンを押すとその画面に動画を流す。
    動画再生後もウィンドウ(黒画面)は保持し、スタッフが明示的に停止/モードOFFに
    するまでWindowsデスクトップを露出させない。ファッションショーのように
    複数動画(MV1/MV2…)を持つショーは next_clip() で次の動画へ切り替えられる
    (自動では次に進まない)。
    """

    def __init__(self, root: tk.Tk, instance: vlc.Instance, logger: logging.Logger, config: AppConfig):
        self._root = root
        self._instance = instance
        self._logger = logger
        self._config = config
        self._window: Optional[tk.Toplevel] = None
        self._player: Optional[vlc.MediaPlayer] = None
        self._poll_job: Optional[str] = None
        self._monitor_wait_job: Optional[str] = None
        self._standby_wanted: bool = False
        self._on_finished: Optional[Callable[[], None]] = None
        self._playlist: list[Path] = []
        self._playlist_index: int = -1
        self._volume: int = 80
        self._label: str = ""
        self._ducked: bool = False
        # 待機黒画面/ステージ画面の背景画像(ウィンドウが存在する間、参照保持してGCを防ぐ)。
        self._background_photo: Optional[ImageTk.PhotoImage] = None
        self._background_label: Optional[tk.Label] = None

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
    # ステージショーモード(待機黒画面)
    # ------------------------------------------------------------------
    def enter_standby(self) -> None:
        """ステージショーモードON: 拡張ディスプレイに黒画面を出して待機させる。
        拡張ディスプレイが見つからない場合はメインディスプレイには何も表示せず、
        バックグラウンドで検出されるまで待つ。"""
        self._standby_wanted = True
        self._attempt_standby()

    def _attempt_standby(self) -> None:
        self._monitor_wait_job = None
        if not self._standby_wanted or self._window is not None:
            return

        monitor = _select_extended_monitor(self._logger)
        if monitor is None:
            self._logger.info("拡張ディスプレイが見つかりません。検出されるまでバックグラウンドで待機します")
            self._monitor_wait_job = self._root.after(3000, self._attempt_standby)
            return

        self._create_window("ステージショー待機画面", monitor)
        self._logger.info("ステージショーモードON: 拡張ディスプレイを黒画面で待機表示にしました")

    def force_close(self) -> None:
        """ステージショーモードOFF: 状態に関わらず再生・待機画面を強制的に閉じる。"""
        self._stop_playback_keep_window()
        self._close_window()

    def request_stop(self) -> None:
        """ESCキー/停止ボタン共通の1段階分の停止。
        再生中なら黒画面を保持したまま停止し、既に待機中(黒画面)ならウィンドウを閉じる。"""
        if self._player is not None:
            self._stop_playback_keep_window()
            if self._on_finished is not None:
                self._on_finished()
        elif self._window is not None:
            self._close_window()

    def stop_playback(self) -> None:
        """緊急停止向け: 再生中なら停止するが、待機黒画面・ステージショーモードは
        一切解除しない(request_stop()と異なり、待機画面を閉じることがないため
        拡張ディスプレイにWindowsのデスクトップが露出する心配がない)。
        何も再生していなければ何もしない。"""
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

    def _apply_background(self, window: tk.Toplevel, monitor) -> None:
        """待機黒画面/ステージ画面の背景を設定する。config.standby_background_image
        が空なら何もしない(黒背景のまま)。動画再生中はVLCの映像がこの背景
        画像の上に重なって表示される(set_hwndでウィンドウ全面に描画されるため)。"""
        self._background_photo = None
        self._background_label = None
        image_setting = self._config.standby_background_image
        if not image_setting:
            return
        path = self._config.resolve_media(image_setting)
        if not path.exists():
            self._logger.error("待機画面の背景画像が見つかりません: %s", path)
            return
        try:
            image = Image.open(path).convert("RGB").resize((monitor.width, monitor.height))
            self._background_photo = ImageTk.PhotoImage(image)
            bg_label = tk.Label(window, image=self._background_photo, bd=0, highlightthickness=0)
            bg_label.place(x=0, y=0, relwidth=1, relheight=1)
            self._background_label = bg_label
        except Exception:
            self._logger.exception("待機画面の背景画像の読み込みに失敗しました: %s", path)
            self._background_photo = None

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

        self._apply_background(window, monitor)

        window.attributes("-topmost", True)
        window.bind("<Escape>", lambda _e: self.request_stop())
        window.focus_force()
        # ESCキー自体は引き続き有効(操作用)。ただし待機黒画面はゲスト側モニターにも
        # 表示されるため、案内文などの表示は一切出さない(完全な黒画面のままにする)。

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
            "ステージショー用ウィンドウを配置しました (%dx%d+%d+%d, primary=%s)",
            monitor.width, monitor.height, monitor.x, monitor.y, monitor.is_primary,
        )

        self._window = window

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
        self._standby_wanted = False
        if self._monitor_wait_job is not None:
            try:
                self._root.after_cancel(self._monitor_wait_job)
            except Exception:
                pass
            self._monitor_wait_job = None
        self._playlist = []
        self._playlist_index = -1
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
            self._window = None
        self._background_photo = None
        self._background_label = None
        self._logger.info("待機画面を閉じました")
