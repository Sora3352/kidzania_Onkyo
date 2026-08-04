"""手動トリガー用GUI(Tkinter)。

- 営業モード(通し営業/2部制営業)の切り替え
- ステージショーの開始ボタン
- スケジュール管理画面(共通/モード別/手動追加/ステージショーの
  時刻・ファイル・音量編集)
- ログ表示コンソール
"""
from __future__ import annotations

import ctypes
import logging
import queue
import time
import tkinter as tk
import uuid
from ctypes import wintypes
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from .config import (
    AppConfig,
    BlackoutWindow,
    LightingCue,
    LinkConfig,
    ScheduledJob,
    StageShow,
    StageShowClip,
    StageShowJob,
)
from .lighting import LightingController
from .link import LinkService, get_local_ip
from .player import AudioCue, FullscreenVideoPlayer
from .schedule_report import build_daily_schedule, format_daily_schedule_text, format_time_range
from .scheduler import JobScheduler

_WEEKDAY_JA = "月火水木金土日"

MEDIA_FILETYPES = [
    ("メディアファイル", "*.mp3 *.wav *.mp4 *.mov *.m4a *.wmv *.avi"),
    ("すべてのファイル", "*.*"),
]

IMAGE_FILETYPES = [
    ("画像ファイル", "*.png *.jpg *.jpeg *.bmp"),
    ("すべてのファイル", "*.*"),
]

_SPI_GETWORKAREA = 0x0030

_HEADER_COLOR = "#c4044b"
_HEADER_COLOR_ACTIVE = "#930338"
_SUBHEADER_COLOR = "#F8981D"
_START_COLOR = "#2e7d32"
_ASSETS_DIR = Path(__file__).resolve().parent / "assets"

_DISPLAY_MODE_LABELS = {
    FullscreenVideoPlayer.DISPLAY_MODE_NORMAL: "通常",
    FullscreenVideoPlayer.DISPLAY_MODE_SHOW: "ショー",
    FullscreenVideoPlayer.DISPLAY_MODE_MIRROR: "ミラーリング",
}


def _maximize_window(window: tk.Wm) -> None:
    """Tkinterのstate('zoomed')はPer-Monitor DPI Aware環境でウィンドウサイズを
    誤って計算し、画面からはみ出すことがある(例: ボタンが画面外に出て押せない)。
    そのためWin32 APIから実際の作業領域(タスクバーを除いた表示可能領域)を
    取得し、その85%程度・中央配置のサイズで直接ウィンドウを配置する
    (画面いっぱいにすると逆に端のボタンが押しづらくなるため、余白を残す)。
    スケジュール管理画面などのサブ画面向け(ホーム画面自体は_fullscreen_window()を使う)。"""
    try:
        rect = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
        work_width = rect.right - rect.left
        work_height = rect.bottom - rect.top
        width = int(work_width * 0.85)
        height = int(work_height * 0.85)
        x = rect.left + (work_width - width) // 2
        y = rect.top + (work_height - height) // 2
        window.geometry(f"{width}x{height}+{x}+{y}")
    except Exception:
        window.geometry("1000x700")


def _fullscreen_window(window: tk.Tk) -> None:
    """ホーム画面(メインウィンドウ)を、タイトルバー・タスクバーなしの
    枠なし完全フルスクリーンでプライマリモニター全体に表示する。起動時に
    スタッフの操作なしで自動的にこの状態になる。Windowsの仕様上プライマリ
    モニターの原点は常に(0, 0)であるため、work areaではなく実解像度
    (GetSystemMetrics)をそのまま使う(タスクバー領域も覆う)。
    タイトルバーが無くなり通常の[×]ボタンで閉じられなくなるため、
    ヘッダーに代わりの「終了」ボタンを用意している(MainWindow._build_header)。"""
    try:
        width = ctypes.windll.user32.GetSystemMetrics(0)  # SM_CXSCREEN
        height = ctypes.windll.user32.GetSystemMetrics(1)  # SM_CYSCREEN
    except Exception:
        width, height = 1280, 720
    window.overrideredirect(True)
    window.geometry(f"{width}x{height}+0+0")
    window.focus_force()


_BASE_FONT_SIZE = 13


def _configure_styles(root: tk.Tk) -> None:
    """全体的に文字が小さく見づらいという指摘への対応。ttkウィジェットの既定
    フォントと、Text/Listboxなど素のtkウィジェットの既定フォントをまとめて
    引き上げる。"""
    base_font = ("", _BASE_FONT_SIZE)
    bold_font = ("", _BASE_FONT_SIZE, "bold")

    root.option_add("*Font", base_font)
    root.option_add("*TCombobox*Listbox.font", base_font)

    style = ttk.Style(root)
    for widget_class in (
        "TLabel", "TButton", "TCheckbutton", "TRadiobutton",
        "TEntry", "TCombobox", "TSpinbox", "TMenubutton",
    ):
        style.configure(widget_class, font=base_font)
    style.configure("TNotebook.Tab", font=bold_font, padding=(14, 8))
    style.configure("TButton", padding=(8, 5))


class QueueLogHandler(logging.Handler):
    """バックグラウンドスレッド(スケジューラー等)からのログをTkinterに安全に流すためのハンドラ。"""

    def __init__(self, log_queue: "queue.Queue[str]"):
        super().__init__()
        self._queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        self._queue.put(self.format(record))


class MainWindow:
    def __init__(
        self,
        root: tk.Tk,
        config: AppConfig,
        logger: logging.Logger,
        vlc_instance,
        scheduler: JobScheduler,
        on_reload_schedule: Callable[[], None],
        link_service: LinkService,
        lighting: LightingController,
    ):
        self._root = root
        self._config = config
        self._logger = logger
        self._scheduler = scheduler
        self._on_reload_schedule = on_reload_schedule
        self._link_service = link_service
        self._lighting = lighting
        self._vlc_instance = vlc_instance
        self._duck_refcount = 0
        self._current_show: Optional[StageShow] = None
        self._video_player = FullscreenVideoPlayer(
            root, vlc_instance, logger, config,
            on_monitor_status_changed=self._on_monitor_status_changed,
        )
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        # 単一動画のショー(fanfanバースデー等)の再生/停止ボタン。show_id -> ボタン。
        self._stage_buttons: dict[str, tk.Button] = {}
        self._stage_stop_buttons: dict[str, tk.Button] = {}
        # 複数動画のショー(ファッションショー等)のクリップ別選択ボタン。show_id -> ボタン列。
        self._show_clip_buttons: dict[str, list[tk.Button]] = {}
        # 拡張ディスプレイの表示モード(通常/ショー/ミラーリング)。起動時は
        # 「通常」(広告等のスライドショー)を既定とする(誤って拡張ディスプレイに
        # Windowsデスクトップが露出した状態(ミラーリング)で起動しないようにするため、
        # 前回終了時の状態は引き継がず常にこの既定値から始める)。
        self._display_mode_var = tk.StringVar(value=FullscreenVideoPlayer.DISPLAY_MODE_NORMAL)
        self._display_mode_buttons: dict[str, tk.Button] = {}
        # テストモード画面(緊急時の手動再生・音量確認用)。単一インスタンスのみ。
        self._test_mode_window: Optional["TestModeWindow"] = None

        self._update_title()
        _fullscreen_window(root)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        _configure_styles(root)
        self._build_widgets()
        self._attach_log_handler()
        self._poll_log_queue()
        self._update_clock()
        self._refresh_schedule_overview()
        if self._link_service.enabled:
            self._poll_link_queue()
            self._update_link_status()

        self._video_player.set_display_mode(self._display_mode_var.get())
        self._video_player.start_monitor_watch()

        # ショー予定(スケジュール自動再生)の発火ハンドラを登録する。scheduler
        # (main.pyでMainWindowより先に生成される)側からこのインスタンスの
        # メソッドを直接参照する構成上、コンストラクタ引数ではなくここで
        # 代入する(循環依存を避けるため)。
        self._scheduler.on_stage_show_triggered = self._on_scheduled_stage_show

    def _update_title(self) -> None:
        title = "キッザニア館内音響システム"
        if self._config.device_name:
            title += f" - {self._config.device_name}"
        self._root.title(title)

    def _device_name_label_text(self) -> str:
        return f"端末: {self._config.device_name}"

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        self._build_header()
        self._build_subheader()

        top = ttk.Frame(self._root, padding=10)
        top.pack(fill="both", expand=True)

        display_mode_frame = ttk.Frame(top)
        display_mode_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(display_mode_frame, text="拡張ディスプレイの表示:", font=("", 14, "bold")).pack(
            side="left", padx=(0, 10)
        )
        for mode in (
            FullscreenVideoPlayer.DISPLAY_MODE_NORMAL,
            FullscreenVideoPlayer.DISPLAY_MODE_SHOW,
            FullscreenVideoPlayer.DISPLAY_MODE_MIRROR,
        ):
            btn = tk.Button(
                display_mode_frame,
                text=_DISPLAY_MODE_LABELS[mode],
                width=12,
                height=2,
                font=("", 13, "bold"),
                command=lambda m=mode: self._on_display_mode_clicked(m),
            )
            btn.pack(side="left", padx=(0, 6))
            self._display_mode_buttons[mode] = btn
        self._sync_display_mode_buttons()

        # ステージショー(左)と次の予定(右)を横並びにする。
        stage_row = ttk.Frame(top)
        stage_row.pack(fill="x", pady=5)

        stage_col = ttk.Frame(stage_row)
        stage_col.pack(side="left", fill="both", expand=True, anchor="n")
        ttk.Label(stage_col, text="ステージショー", font=("", 17, "bold")).pack(anchor="w")
        self._stage_button_frame = ttk.Frame(stage_col)
        self._stage_button_frame.pack(fill="x", pady=5)
        self._render_stage_buttons(self._stage_button_frame)

        upcoming_col = ttk.Frame(stage_row)
        upcoming_col.pack(side="left", fill="both", expand=True, anchor="n", padx=(24, 0))

        upcoming_header = ttk.Frame(upcoming_col)
        upcoming_header.pack(fill="x")
        ttk.Label(upcoming_header, text="次の予定", font=("", 17, "bold")).pack(side="left")
        ttk.Button(upcoming_header, text="スケジュール管理...", command=self._open_schedule_manager).pack(
            side="right"
        )
        ttk.Button(upcoming_header, text="一日の予定を表示...", command=self._open_daily_schedule).pack(
            side="right", padx=(0, 5)
        )

        self._upcoming_frame = ttk.Frame(upcoming_col)
        self._upcoming_frame.pack(fill="x", pady=5)

        control_frame = ttk.Frame(top)
        control_frame.pack(fill="x", pady=(0, 5))
        ttk.Button(control_frame, text="■ すべて停止(緊急)", command=self._on_stop_clicked).pack(
            side="left", padx=(0, 5)
        )
        self._next_button = ttk.Button(control_frame, text="次のMV ▶", command=self._on_next_clip_clicked)
        self._next_button.pack(side="left")
        self._next_button.state(["disabled"])

        # 現在再生中(そのまま)と、残りの領域いっぱいに動作ログを表示する。
        overview_frame = ttk.Frame(top)
        overview_frame.pack(fill="both", expand=True, pady=(5, 0))

        ttk.Label(overview_frame, text="現在再生中", font=("", 14, "bold")).pack(anchor="w")
        self._active_frame = ttk.Frame(overview_frame)
        self._active_frame.pack(fill="x")
        # job_id -> {frame, name, value_label}。スライダー操作中に行を破棄・再生成
        # してしまわないよう、_render_active_cues()は毎回全消去せず差分更新する。
        self._active_cue_rows: dict[str, dict] = {}
        self._active_empty_label: Optional[tk.Widget] = None

        log_frame = ttk.Frame(overview_frame)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        ttk.Label(log_frame, text="動作ログ", font=("", 14, "bold")).pack(anchor="w")

        self._log_text = tk.Text(log_frame, height=10, state="disabled", wrap="word", font=("", 12))
        self._log_text.pack(fill="both", expand=True, side="left")
        scrollbar = ttk.Scrollbar(log_frame, command=self._log_text.yview)
        scrollbar.pack(fill="y", side="right")
        self._log_text.configure(yscrollcommand=scrollbar.set)

    def _build_header(self) -> None:
        header = tk.Frame(self._root, bg=_HEADER_COLOR, height=64)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        logo_path = _ASSETS_DIR / "kidzania_logo.png"
        if logo_path.exists():
            # PhotoImageはローカル変数にすると参照が切れて画像が消えるため、
            # インスタンス属性として保持しておく。
            self._logo_image = tk.PhotoImage(file=str(logo_path)).subsample(2, 2)
            tk.Label(header, image=self._logo_image, bg=_HEADER_COLOR).pack(side="left", padx=24, pady=6)
        else:
            self._logger.warning("ロゴ画像が見つかりません: %s", logo_path)

        self._add_header_icon_button(
            header, "icon_settings.png", self._open_system_settings, "システム設定"
        )
        self._add_header_icon_button(
            header, "icon_testmode.png", self._open_test_mode, "テストモード"
        )

        # ホーム画面は枠なし完全フルスクリーン(_fullscreen_window)のため、
        # 通常のウィンドウにあるはずの[×]で閉じる手段が無い。その代わりの
        # 終了操作としてヘッダー右端に置く(_on_close()に処理を委譲するため、
        # 再生中の確認ダイアログ等は通常の終了操作と同じ)。
        tk.Button(
            header,
            text="終了",
            command=self._on_close,
            bg=_HEADER_COLOR,
            activebackground=_HEADER_COLOR_ACTIVE,
            fg="white",
            activeforeground="white",
            bd=0,
            highlightthickness=0,
            font=("", 12, "bold"),
            cursor="hand2",
            padx=14,
        ).pack(side="right", pady=6, padx=16)

    def _add_header_icon_button(
        self, header: tk.Frame, icon_filename: str, command: Callable[[], None], alt_text: str
    ) -> None:
        """ヘッダー(ロゴの隣)に、白枠アイコンのみのボタンを追加する。
        アイコン画像が読み込めない場合(未生成の環境等)は、代わりに文字ボタンに
        フォールバックする(機能自体は必ず使えるようにするため)。"""
        icon_path = _ASSETS_DIR / icon_filename
        if not icon_path.exists():
            self._logger.warning("アイコン画像が見つかりません: %s", icon_path)
            ttk.Button(header, text=alt_text + "...", command=command).pack(side="left", pady=6, padx=(0, 8))
            return

        # PhotoImageはローカル変数だと参照が切れて消えるため、インスタンス属性として保持する。
        image = tk.PhotoImage(file=str(icon_path)).subsample(2, 2)
        setattr(self, f"_{icon_path.stem}_image", image)
        tk.Button(
            header,
            image=image,
            command=command,
            bg=_HEADER_COLOR,
            activebackground=_HEADER_COLOR_ACTIVE,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        ).pack(side="left", pady=6, padx=(0, 8))

    def _build_subheader(self) -> None:
        subheader = tk.Frame(self._root, bg=_SUBHEADER_COLOR)
        subheader.pack(fill="x", side="top")

        tk.Label(
            subheader, text="営業モード:", font=("", 14, "bold"), bg=_SUBHEADER_COLOR, fg="white"
        ).pack(side="left", padx=(16, 6), pady=10)

        self._mode_var = tk.StringVar(value=self._config.get_active_mode())
        self._mode_combo = ttk.Combobox(
            subheader,
            textvariable=self._mode_var,
            values=self._config.get_mode_names(),
            state="readonly",
            width=16,
        )
        self._mode_combo.pack(side="left", pady=10)
        self._mode_combo.bind("<<ComboboxSelected>>", self._on_mode_changed)

        self._device_name_var = tk.StringVar(value=self._device_name_label_text())
        self._device_name_label = tk.Label(
            subheader, textvariable=self._device_name_var, font=("", 13, "bold"),
            bg=_SUBHEADER_COLOR, fg="white",
        )
        if self._config.device_name:
            self._device_name_label.pack(side="left", padx=(16, 6), pady=10)

        self._monitor_status_var = tk.StringVar(value="モニター: 確認中…")
        self._monitor_status_label = tk.Label(
            subheader, textvariable=self._monitor_status_var, font=("", 13, "bold"),
            bg=_SUBHEADER_COLOR, fg="white",
        )
        self._monitor_status_label.pack(side="left", padx=(16, 6), pady=10)

        if self._link_service.enabled:
            self._link_status_var = tk.StringVar(value="連携: 未接続")
            tk.Label(
                subheader, textvariable=self._link_status_var, font=("", 13, "bold"),
                bg=_SUBHEADER_COLOR, fg="white",
            ).pack(side="left", padx=(16, 6), pady=10)

        self._clock_var = tk.StringVar()
        tk.Label(
            subheader, textvariable=self._clock_var, font=("", 22, "bold"), bg=_SUBHEADER_COLOR, fg="white"
        ).pack(side="right", padx=16)

    def _render_stage_buttons(self, frame: ttk.Frame) -> None:
        for widget in frame.winfo_children():
            widget.destroy()
        self._stage_buttons.clear()
        self._stage_stop_buttons.clear()
        self._show_clip_buttons.clear()

        shows = self._config.load_stage_shows()
        if not shows:
            ttk.Label(frame, text="(stage_shows.json にショーが登録されていません)").pack()
            return

        for i, show in enumerate(shows):
            show_frame = ttk.LabelFrame(frame, text=show.label, padding=8)
            show_frame.grid(row=i // 2, column=i % 2, padx=8, pady=4, sticky="w")
            row = ttk.Frame(show_frame)
            row.pack()

            if len(show.clips) > 1:
                # 複数動画を持つショー(ファッションショー等): クリップごとに
                # 直接選択できる大きなボタンを並べる(順送りではなく直接選択)。
                clip_buttons: list[tk.Button] = []
                for ci, clip in enumerate(show.clips):
                    btn = _make_square_button(
                        row, f"{ci + 1}\n{Path(clip.file).stem}",
                        lambda s=show, idx=ci: self._select_stage_clip(s, idx),
                    )
                    btn.pack(side="left", padx=3)
                    clip_buttons.append(btn)
                self._show_clip_buttons[show.id] = clip_buttons
                stop_btn = _make_square_button(
                    row, "■\n停止", self._on_show_stop_clicked, bg=_HEADER_COLOR, fg="white"
                )
                stop_btn.pack(side="left", padx=(12, 3))
                self._stage_stop_buttons[show.id] = stop_btn
            else:
                play_btn = _make_square_button(
                    row, "▶\n再生", lambda s=show: self._start_stage_show(s), bg=_START_COLOR, fg="white"
                )
                play_btn.pack(side="left", padx=3)
                stop_btn = _make_square_button(
                    row, "■\n停止", self._on_show_stop_clicked, bg=_HEADER_COLOR, fg="white"
                )
                stop_btn.pack(side="left", padx=3)
                self._stage_buttons[show.id] = play_btn
                self._stage_stop_buttons[show.id] = stop_btn

        self._sync_stage_ui()

    def _sync_stage_ui(self) -> None:
        """メイン画面のステージショーボタン(単一動画ショーの再生/停止、複数動画
        ショーのクリップ別選択/停止)を、現在の再生状態に合わせて更新する。再生中は
        再生中のショーの停止ボタン・クリップボタンのみ有効になり、それ以外の
        再生ボタンは無効になる。"""
        playing = self._video_player.is_playing()
        current_id = self._current_show.id if self._current_show is not None else None
        current_index = self._video_player.current_clip_index() if playing else -1
        stage_mode_on = self._display_mode_var.get() == FullscreenVideoPlayer.DISPLAY_MODE_SHOW

        for show_id, play_btn in self._stage_buttons.items():
            play_btn.configure(state=("disabled" if (playing or not stage_mode_on) else "normal"))

        for show_id, clip_buttons in self._show_clip_buttons.items():
            this_active = playing and current_id == show_id
            other_playing = playing and current_id != show_id
            for i, btn in enumerate(clip_buttons):
                btn.configure(
                    state=("disabled" if (other_playing or not stage_mode_on) else "normal"),
                    relief=("sunken" if this_active and i == current_index else "raised"),
                )

        for show_id, stop_btn in self._stage_stop_buttons.items():
            stop_btn.configure(state=("normal" if (playing and current_id == show_id) else "disabled"))

    # ------------------------------------------------------------------
    # 現在日時の表示
    # ------------------------------------------------------------------
    def _update_clock(self) -> None:
        now = datetime.now()
        weekday = _WEEKDAY_JA[now.weekday()]
        self._clock_var.set(now.strftime(f"%Y-%m-%d({weekday}) %H:%M:%S"))
        self._root.after(1000, self._update_clock)

    # ------------------------------------------------------------------
    # 現在再生中/次の予定パネル(スケジュール自動再生ジョブ向け)
    # ------------------------------------------------------------------
    def _refresh_schedule_overview(self) -> None:
        self._render_active_cues()
        self._render_upcoming()
        self._root.after(1000, self._refresh_schedule_overview)

    def _render_active_cues(self) -> None:
        """現在再生中パネルを更新する。1秒おきに呼ばれるため、音量スライダーを
        ドラッグ中に行ごと破棄・再生成してしまわないよう、表示され続けている
        ジョブの行はそのまま残し、開始/終了した分だけ差分で追加・削除する。"""
        active = self._scheduler.get_active_cues()
        active_ids = {job_id for job_id, _name in active}

        for job_id in list(self._active_cue_rows.keys()):
            if job_id not in active_ids:
                self._active_cue_rows.pop(job_id)["frame"].destroy()

        if not active:
            if self._active_empty_label is None or not self._active_empty_label.winfo_exists():
                self._active_empty_label = ttk.Label(
                    self._active_frame, text="(再生中の項目はありません)", foreground="#888888"
                )
                self._active_empty_label.pack(anchor="w")
            return

        if self._active_empty_label is not None:
            if self._active_empty_label.winfo_exists():
                self._active_empty_label.destroy()
            self._active_empty_label = None

        for job_id, name in active:
            row = self._active_cue_rows.get(job_id)
            if row is None:
                self._active_cue_rows[job_id] = self._make_active_cue_row(job_id, name)

    def _make_active_cue_row(self, job_id: str, name: str) -> dict:
        """現在再生中の1ジョブ分の行(名前・音量スライダー・停止ボタン)を作る。
        不具合で大音量になった際にすぐ絞れるよう、スケジュール再生中でも
        リアルタイムで音量調整できるようにしている。"""
        row = ttk.Frame(self._active_frame)
        row.pack(fill="x", pady=1)
        ttk.Label(row, text=name, width=22, anchor="w").pack(side="left")

        initial = self._scheduler.get_active_volume(job_id)
        if initial is None:
            initial = 0
        value_label = ttk.Label(row, text=f"{initial}%", width=5)

        def _on_change(value: str) -> None:
            percent = int(float(value))
            self._scheduler.set_active_volume(job_id, percent)
            value_label.configure(text=f"{percent}%")

        scale = ttk.Scale(row, from_=0, to=100, orient="horizontal", length=120, command=_on_change)
        scale.set(initial)
        scale.pack(side="left", padx=4)
        value_label.pack(side="left")

        ttk.Button(
            row, text="■ 停止", width=8, command=lambda jid=job_id: self._on_stop_active_cue(jid)
        ).pack(side="left", padx=4)

        return {"frame": row, "name": name}

    def _on_stop_active_cue(self, job_id: str) -> None:
        self._scheduler.stop_active_cue(job_id)
        self._render_active_cues()

    def _render_upcoming(self) -> None:
        for widget in self._upcoming_frame.winfo_children():
            widget.destroy()

        upcoming = self._scheduler.get_upcoming(limit=5)
        if not upcoming:
            ttk.Label(self._upcoming_frame, text="(予定はありません)", foreground="#888888").pack(anchor="w")
            return

        for next_run_time, job_id, name in upcoming:
            row = ttk.Frame(self._upcoming_frame)
            row.pack(fill="x", pady=1)
            skipped = self._scheduler.is_skipped(job_id)
            label_text = f"{next_run_time:%H:%M:%S}  {name}"
            if skipped:
                label_text += "  [スキップ済み]"
            ttk.Label(
                row, text=label_text, width=36, anchor="w",
                foreground=("#888888" if skipped else ""),
            ).pack(side="left")
            if skipped:
                ttk.Button(
                    row, text="スキップ取消", width=10,
                    command=lambda jid=job_id: self._on_cancel_skip(jid),
                ).pack(side="left", padx=4)
            else:
                ttk.Button(
                    row, text="次回スキップ", width=10,
                    command=lambda jid=job_id, nm=name, t=next_run_time: self._on_skip_next(jid, nm, t),
                ).pack(side="left", padx=4)

    def _on_skip_next(self, job_id: str, name: str, next_run_time: datetime) -> None:
        if not messagebox.askyesno(
            "次回スキップ", f"{next_run_time:%H:%M} の {name} をスキップしますか？"
        ):
            return
        self._scheduler.skip_next(job_id)
        self._render_upcoming()

    def _on_cancel_skip(self, job_id: str) -> None:
        self._scheduler.cancel_skip(job_id)
        self._render_upcoming()

    # ------------------------------------------------------------------
    # 営業モード切り替え
    # ------------------------------------------------------------------
    def _on_mode_changed(self, _event=None) -> None:
        mode = self._mode_var.get()
        self._config.set_active_mode(mode)
        self._on_reload_schedule()
        self._logger.info("営業モードを切り替えました: %s", mode)
        self._link_service.notify_async("/event/mode-changed", {"mode": mode})

    # ------------------------------------------------------------------
    # 拡張ディスプレイの表示モード(通常/ショー/ミラーリング)
    # ------------------------------------------------------------------
    def _sync_display_mode_buttons(self) -> None:
        current = self._display_mode_var.get()
        for mode, btn in self._display_mode_buttons.items():
            if mode == current:
                bg = _HEADER_COLOR if mode == FullscreenVideoPlayer.DISPLAY_MODE_MIRROR else _START_COLOR
                btn.configure(
                    bg=bg, fg="white", activebackground=bg, activeforeground="white", relief="sunken",
                )
            else:
                btn.configure(
                    bg="SystemButtonFace", fg="black",
                    activebackground="SystemButtonFace", activeforeground="black", relief="raised",
                )

    def _on_display_mode_clicked(self, mode: str) -> None:
        if mode == self._display_mode_var.get():
            return
        if mode == FullscreenVideoPlayer.DISPLAY_MODE_MIRROR:
            if not messagebox.askyesno(
                "ミラーリングに切り替えますか?",
                "PC画面が出力されます。よろしいですか?",
            ):
                return

        self._display_mode_var.set(mode)
        self._video_player.set_display_mode(mode)
        if mode != FullscreenVideoPlayer.DISPLAY_MODE_SHOW:
            self._next_button.state(["disabled"])
        self._sync_display_mode_buttons()
        self._sync_stage_ui()

    def _on_monitor_status_changed(self, connected: bool) -> None:
        self._monitor_status_var.set("モニター: 接続中" if connected else "モニター: 未接続")
        self._monitor_status_label.configure(fg=("white" if connected else "#ffe066"))

    # ------------------------------------------------------------------
    # ステージショー再生
    # ------------------------------------------------------------------
    def _start_stage_show(self, show: StageShow, start_index: int = 0) -> None:
        if self._display_mode_var.get() != FullscreenVideoPlayer.DISPLAY_MODE_SHOW:
            messagebox.showwarning(
                "表示モードが「ショー」ではありません", "先に拡張ディスプレイの表示を「ショー」に切り替えてください。"
            )
            return

        if self._video_player.is_playing():
            messagebox.showwarning("再生中", "別のショーを再生中です。終了までお待ちください。")
            return

        self._launch_stage_show(show, start_index)

    def _launch_stage_show(self, show: StageShow, start_index: int = 0, auto_revert_after: bool = False) -> None:
        """ステージショーの再生を実際に開始する(表示モード/再生中チェックは
        呼び出し側の責務)。手動再生(_start_stage_show)とスケジュール自動再生
        (_handle_scheduled_stage_show)の両方から共通で使う。auto_revert_afterが
        Trueの場合、再生終了時に表示モードがまだ「ショー」のままなら自動的に
        「通常」へ戻す(スケジュール発火時のみ。手動再生では従来通り、
        終了後も「ショー」の待機画面のままにする)。"""
        files = [self._config.resolve_media(c.file) for c in show.clips]

        def _on_finished() -> None:
            self._current_show = None
            self._sync_stage_ui()
            self._next_button.state(["disabled"])
            self._link_service.notify_async("/event/playback-ended", {"label": show.label})
            if auto_revert_after and self._display_mode_var.get() == FullscreenVideoPlayer.DISPLAY_MODE_SHOW:
                self._logger.info(
                    "スケジュールされたショーの再生が終了したため、表示を「通常」に戻します: %s", show.label
                )
                self._display_mode_var.set(FullscreenVideoPlayer.DISPLAY_MODE_NORMAL)
                self._video_player.set_display_mode(FullscreenVideoPlayer.DISPLAY_MODE_NORMAL)
                self._sync_display_mode_buttons()

        self._current_show = show
        self._video_player.play(
            files, show.volume, show.label, _on_finished,
            start_index=start_index, black_background_on_gap=show.black_background_on_gap,
        )
        self._sync_stage_ui()
        self._next_button.state(["!disabled"] if len(show.clips) > 1 else ["disabled"])
        if show.clips and 0 <= start_index < len(show.clips):
            self._lighting.trigger_cue(show.clips[start_index].lighting_cue)
        self._link_service.notify_async("/event/playback-started", {"label": show.label})

    # ------------------------------------------------------------------
    # ショー予定(スケジュール自動再生)
    # ------------------------------------------------------------------
    def _on_scheduled_stage_show(self, stage_show_id: str, clip_index: int, show_label: str) -> None:
        """JobScheduler(APSchedulerのバックグラウンドスレッド)から呼ばれる。
        Tkinterウィジェットの操作はメインスレッドでのみ行えるため(スレッド
        セーフではない)、root.after()でメインスレッドの処理へ委譲する。"""
        self._root.after(0, lambda: self._handle_scheduled_stage_show(stage_show_id, clip_index, show_label))

    def _handle_scheduled_stage_show(self, stage_show_id: str, clip_index: int, show_label: str) -> None:
        """スケジュールされたショーの発火を実際に処理する(Tkinterメインスレッド上)。
        無人で発火するため、messageboxのようなモーダルダイアログは一切使わず、
        再生できない場合はログに記録してスキップするだけにとどめる
        (ダイアログを出すとスタッフが気づくまでアプリ全体が固まってしまうため)。"""
        show = next((s for s in self._config.load_stage_shows() if s.id == stage_show_id), None)
        if show is None:
            self._logger.error("スケジュールされたショーの参照先が見つかりません: %s", stage_show_id)
            return

        if self._video_player.is_playing():
            self._logger.warning(
                "既に別の動画を再生中のため、スケジュールされたショーの再生をスキップしました: %s", show.label
            )
            return

        current_mode = self._display_mode_var.get()
        if current_mode == FullscreenVideoPlayer.DISPLAY_MODE_MIRROR:
            self._logger.warning(
                "ミラーリング中のため、スケジュールされたショーの再生をスキップしました: %s", show.label
            )
            return

        if current_mode != FullscreenVideoPlayer.DISPLAY_MODE_SHOW:
            self._logger.info("スケジュールにより表示モードを「ショー」に自動切替します: %s", show.label)
            self._display_mode_var.set(FullscreenVideoPlayer.DISPLAY_MODE_SHOW)
            self._video_player.set_display_mode(FullscreenVideoPlayer.DISPLAY_MODE_SHOW)
            self._sync_display_mode_buttons()

        start_index = clip_index if show.clips and 0 <= clip_index < len(show.clips) else 0
        self._logger.info("スケジュールによりショーを自動再生します: %s", show.label)
        self._launch_stage_show(show, start_index=start_index, auto_revert_after=True)

    def _select_stage_clip(self, show: StageShow, index: int) -> None:
        """ファッションショーのクリップ別ボタン等から、特定の動画を直接再生する。
        同じショーを再生中ならそのクリップへ直接ジャンプし、そうでなければ
        (何も再生していなければ)そのクリップからショーを開始する。"""
        if self._video_player.is_playing() and self._current_show is not None and self._current_show.id == show.id:
            new_index = self._video_player.jump_to_clip(index)
            if new_index is not None and 0 <= new_index < len(show.clips):
                self._lighting.trigger_cue(show.clips[new_index].lighting_cue)
            self._sync_stage_ui()
            return
        self._start_stage_show(show, start_index=index)

    def _on_stop_clicked(self) -> None:
        """■ すべて停止(緊急)。再生中の動画があれば即座に(フェードなしで)
        停止するが、待機画面・表示モード(通常/ショー)は解除しない
        (ミラーリングに切り替わってしまうと拡張ディスプレイ経由でゲスト側
        モニターにWindowsのデスクトップが露出してしまうため)。"""
        self._video_player.stop_playback()
        self._link_service.notify_async("/event/stop-all", {})

    def _on_show_stop_clicked(self) -> None:
        """ショーごとの「■ 停止」ボタン。緊急停止とは異なり、フェードアウト
        しながら再生を止める(待機画面・表示モードはstop_playback()と同様に
        解除しない)。"""
        self._video_player.fade_out_and_stop()

    def _on_next_clip_clicked(self) -> None:
        index = self._video_player.next_clip()
        if index is not None and self._current_show is not None and 0 <= index < len(self._current_show.clips):
            self._lighting.trigger_cue(self._current_show.clips[index].lighting_cue)
        self._sync_stage_ui()

    # ------------------------------------------------------------------
    # スケジュール管理画面
    # ------------------------------------------------------------------
    def _open_schedule_manager(self) -> None:
        ScheduleManagerWindow(self._root, self._config, self._on_schedule_saved_locally)

    def _open_daily_schedule(self) -> None:
        DailyScheduleWindow(self._root, self._config, self._mode_var.get())

    def _on_schedule_saved_locally(self) -> None:
        """ローカルのスケジュール管理画面で保存されたときのみ呼ぶ。連携先へ設定を
        送信する(受信側の_apply_remote_configはこの経路を通らないため、送り返す
        無限ループにはならない)。"""
        self._on_schedule_saved()
        self._link_service.push_config_async()

    def _on_schedule_saved(self) -> None:
        # モード一覧が変わっている可能性は無いが、選択肢と表示だけ最新化する
        self._mode_combo.configure(values=self._config.get_mode_names())
        self._mode_var.set(self._config.get_active_mode())
        self._render_stage_buttons(self._stage_button_frame)
        self._on_reload_schedule()

    # ------------------------------------------------------------------
    # システム設定画面(端末名・リンク設定)
    # ------------------------------------------------------------------
    def _open_system_settings(self) -> None:
        SystemSettingsWindow(self._root, self._config, self._on_system_settings_saved)

    # ------------------------------------------------------------------
    # テストモード(緊急時の手動再生・音量確認)
    # ------------------------------------------------------------------
    def _open_test_mode(self) -> None:
        if self._test_mode_window is not None:
            if self._test_mode_window.winfo_exists():
                self._test_mode_window.lift()
                return
            self._test_mode_window = None
        self._test_mode_window = TestModeWindow(
            self._root, self._config, self._vlc_instance, self._logger, self._link_service,
            on_close=self._close_test_mode_window,
        )

    def _close_test_mode_window(self) -> None:
        if self._test_mode_window is None:
            return
        window = self._test_mode_window
        self._test_mode_window = None
        window.shutdown()

    def _on_system_settings_saved(self) -> None:
        self._update_title()
        self._device_name_var.set(self._device_name_label_text())
        if self._config.device_name:
            self._device_name_label.pack(side="left", padx=(16, 6), pady=10)
        else:
            self._device_name_label.pack_forget()
        # カーソル制限のON/OFFを、表示モードを切り替えなくても即座に反映する。
        self._video_player.apply_cursor_confinement()

    # ------------------------------------------------------------------
    # リンク機能(2台のSurface連携)
    # ------------------------------------------------------------------
    def _update_link_status(self) -> None:
        ts = self._link_service.last_contact_ts
        threshold = self._config.link.poll_interval_seconds * 2
        if ts is not None and (time.time() - ts) < threshold:
            peer = self._link_service.peer_device_name
            self._link_status_var.set(f"連携: 接続中({peer})" if peer else "連携: 接続中")
        else:
            self._link_status_var.set("連携: 未接続")
        self._root.after(2000, self._update_link_status)

    def _poll_link_queue(self) -> None:
        try:
            while True:
                path, payload = self._link_service.inbound_queue.get_nowait()
                self._dispatch_remote_event(path, payload)
        except queue.Empty:
            pass
        self._root.after(200, self._poll_link_queue)

    def _dispatch_remote_event(self, path: str, payload: dict) -> None:
        if path == "/event/playback-started":
            self._apply_remote_playback_started()
        elif path == "/event/playback-ended":
            self._apply_remote_playback_ended()
        elif path == "/event/stop-all":
            self._apply_remote_stop_all()
        elif path == "/event/mode-changed":
            self._apply_remote_mode_changed(payload.get("mode", ""))
        elif path == "/config/push":
            self._apply_remote_config(payload.get("schedule", {}), payload.get("stage_shows", {}))

    def _apply_remote_playback_started(self) -> None:
        """連携先が再生を開始した(参照カウント方式: 複数が同時に再生中でも、
        最初の1件が始まった時だけダッキングを実際に適用する)。"""
        self._duck_refcount += 1
        if self._duck_refcount == 1:
            percent = self._config.link.duck_volume_percent
            self._scheduler.duck_all_active(percent)
            self._video_player.duck(percent)

    def _apply_remote_playback_ended(self) -> None:
        self._duck_refcount = max(0, self._duck_refcount - 1)
        if self._duck_refcount == 0:
            self._scheduler.restore_all_active()
            self._video_player.restore()

    def _apply_remote_stop_all(self) -> None:
        """連携先からの一括停止要求。自機の再生中のもの(ステージ動画・
        スケジュールBGM双方)を全て停止するが、待機黒画面・ステージショー
        モードは解除しない(ゲスト側モニターへのデスクトップ露出を防ぐため)。"""
        self._video_player.stop_playback()
        self._scheduler.stop_all_active()
        self._logger.info("連携先からの一括停止要求を適用しました")

    def _apply_remote_mode_changed(self, mode: str) -> None:
        if not mode or mode == self._mode_var.get():
            return
        self._config.set_active_mode(mode)
        self._mode_var.set(mode)
        self._on_reload_schedule()
        self._logger.info("連携先からの営業モード変更を適用しました: %s", mode)

    def _apply_remote_config(self, schedule_json: dict, stage_shows_json: dict) -> None:
        """連携先からのconfig push、またはポーリングによるconfig pullを適用する。
        自分から連携先への再送は行わない(無限ループ防止)。"""
        if schedule_json:
            self._config.write_schedule_raw(schedule_json)
        if stage_shows_json:
            self._config.write_stage_shows_raw(stage_shows_json)
        self._on_schedule_saved()
        self._logger.info("連携先からの設定変更を適用しました")

    # ------------------------------------------------------------------
    # ログ表示
    # ------------------------------------------------------------------
    def _attach_log_handler(self) -> None:
        handler = QueueLogHandler(self._log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
        self._logger.addHandler(handler)

    def _poll_log_queue(self) -> None:
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self._log_text.configure(state="normal")
                self._log_text.insert("end", msg + "\n")
                self._log_text.see("end")
                self._log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self._root.after(300, self._poll_log_queue)

    def _on_close(self) -> None:
        if self._video_player.is_playing():
            if not messagebox.askyesno("終了確認", "ステージショー再生中です。終了しますか?"):
                return
        self._close_test_mode_window()
        # カーソル範囲制限(ClipCursor)はプロセス終了時にOSが自動解除するのが
        # 通常の挙動だが、念のため明示的に解除してから終了する。
        self._video_player.release_cursor_confinement()
        self._root.destroy()


def _make_square_button(parent, text: str, command, bg: Optional[str] = None, fg: Optional[str] = None) -> tk.Button:
    """大きく押しやすい正方形寄りのボタン(ステージ関連の各種操作で共通利用)。"""
    kwargs: dict = dict(
        text=text, command=command, width=8, height=4,
        font=("", 13, "bold"), justify="center",
    )
    if bg:
        kwargs["bg"] = bg
        kwargs["activebackground"] = bg
    if fg:
        kwargs["fg"] = fg
        kwargs["activeforeground"] = fg
    return tk.Button(parent, **kwargs)


class TestModeWindow(tk.Toplevel):
    """テストモード画面。緊急時に任意の音源をその場で手動再生したり、
    スケジュール登録前に音量をリアルタイムで調整して確認したりするための
    独立した簡易プレイヤー。再生タイミングは「今すぐ」または「カウントダウンで
    指定秒後」を選べる。スケジュール/ステージショーの設定とは無関係な、
    使い捨ての単発再生であることに注意(ここでの音量はあくまで確認用で、
    実際にスケジュールへ反映するには「スケジュール管理」画面に別途入力する)。"""

    def __init__(
        self,
        parent: tk.Tk,
        config: AppConfig,
        vlc_instance,
        logger: logging.Logger,
        link_service: LinkService,
        on_close: Callable[[], None],
    ):
        super().__init__(parent)
        self.title("テストモード(手動再生・音量確認)")
        self.transient(parent)
        self.resizable(False, False)

        self._config = config
        self._vlc_instance = vlc_instance
        self._logger = logger
        self._link_service = link_service
        self._on_close_callback = on_close
        self._cue: Optional[AudioCue] = None
        self._file_value: str = ""
        self._countdown_job: Optional[str] = None
        self._countdown_remaining: int = 0

        self._build_widgets()
        self._update_button_states()
        self.protocol("WM_DELETE_WINDOW", self._handle_close)

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        file_row = ttk.Frame(frame)
        file_row.pack(fill="x", pady=4)
        ttk.Label(file_row, text="音源ファイル:").pack(side="left")
        self._file_display_var = tk.StringVar(value="(未選択)")
        ttk.Label(file_row, textvariable=self._file_display_var, width=32, anchor="w").pack(
            side="left", padx=6
        )
        ttk.Button(file_row, text="参照...", command=self._browse).pack(side="left")

        vol_row = ttk.Frame(frame)
        vol_row.pack(fill="x", pady=8)
        ttk.Label(vol_row, text="音量:").pack(side="left")
        self._volume_var = tk.DoubleVar(value=80)
        ttk.Scale(
            vol_row, from_=0, to=100, variable=self._volume_var, orient="horizontal",
            length=240, command=self._on_volume_changed,
        ).pack(side="left", padx=6)
        self._volume_label = ttk.Label(vol_row, width=4)
        self._volume_label.pack(side="left")
        self._update_volume_label()
        ttk.Label(
            frame, text="※再生中はこの音量が即座に反映されます。実際のスケジュールへの反映は別途「スケジュール管理」から行ってください。",
            foreground="#888888", wraplength=400, justify="left",
        ).pack(anchor="w", pady=(0, 8))

        timing_frame = ttk.LabelFrame(frame, text="再生タイミング", padding=8)
        timing_frame.pack(fill="x", pady=8)
        self._timing_var = tk.StringVar(value="now")
        ttk.Radiobutton(timing_frame, text="今すぐ再生", variable=self._timing_var, value="now").pack(anchor="w")
        countdown_row = ttk.Frame(timing_frame)
        countdown_row.pack(anchor="w", pady=(4, 0))
        ttk.Radiobutton(
            countdown_row, text="カウントダウンで", variable=self._timing_var, value="countdown"
        ).pack(side="left")
        self._countdown_seconds_var = tk.StringVar(value="10")
        ttk.Spinbox(
            countdown_row, from_=1, to=3600, textvariable=self._countdown_seconds_var, width=6
        ).pack(side="left", padx=4)
        ttk.Label(countdown_row, text="秒後に再生").pack(side="left")

        self._status_var = tk.StringVar(value="待機中")
        ttk.Label(frame, textvariable=self._status_var, font=("", 13, "bold")).pack(pady=10)

        btn_row = ttk.Frame(frame)
        btn_row.pack(pady=4)
        self._play_button = ttk.Button(btn_row, text="▶ 再生", command=self._on_play_clicked, width=12)
        self._play_button.pack(side="left", padx=4)
        self._stop_button = ttk.Button(btn_row, text="■ 停止", command=self._on_stop_clicked, width=12)
        self._stop_button.pack(side="left", padx=4)

        ttk.Button(frame, text="閉じる", command=self._handle_close).pack(anchor="e", pady=(12, 0))

    def _update_volume_label(self, *_a) -> None:
        self._volume_label.configure(text=str(int(self._volume_var.get())))

    def _on_volume_changed(self, _value) -> None:
        self._update_volume_label()
        if self._cue is not None:
            self._cue.set_volume(int(self._volume_var.get()))

    def _browse(self) -> None:
        path_str = filedialog.askopenfilename(
            initialdir=str(self._config.media_root),
            filetypes=MEDIA_FILETYPES,
        )
        if not path_str:
            return
        path = Path(path_str)
        self._file_value = self._config.to_media_relative(path)
        self._file_display_var.set(path.name)
        self._update_button_states()

    # ------------------------------------------------------------------
    # 再生
    # ------------------------------------------------------------------
    def _on_play_clicked(self) -> None:
        if not self._file_value:
            messagebox.showwarning("ファイル未選択", "再生する音源ファイルを選択してください。")
            return
        if self._cue is not None or self._countdown_job is not None:
            return

        if self._timing_var.get() == "countdown":
            try:
                seconds = max(1, int(self._countdown_seconds_var.get()))
            except ValueError:
                seconds = 10
            self._countdown_remaining = seconds
            self._tick_countdown()
        else:
            self._play_now()

    def _tick_countdown(self) -> None:
        if self._countdown_remaining <= 0:
            self._countdown_job = None
            self._play_now()
            return
        self._status_var.set(f"カウントダウン中: あと{self._countdown_remaining}秒")
        self._countdown_remaining -= 1
        self._countdown_job = self.after(1000, self._tick_countdown)
        self._update_button_states()

    def _play_now(self) -> None:
        path = self._config.resolve_media(self._file_value)
        label = f"[テスト] {path.name}"
        cue = AudioCue(self._vlc_instance, self._logger)

        def _on_finished() -> None:
            self._cue = None
            self._status_var.set("待機中")
            self._update_button_states()
            self._link_service.notify_async("/event/playback-ended", {"label": label})

        self._cue = cue
        cue.play(path, int(self._volume_var.get()), label, on_finished=_on_finished)
        self._status_var.set(f"再生中: {path.name}")
        self._update_button_states()
        self._link_service.notify_async("/event/playback-started", {"label": label})

    def _on_stop_clicked(self) -> None:
        if self._countdown_job is not None:
            self.after_cancel(self._countdown_job)
            self._countdown_job = None
            self._countdown_remaining = 0
            self._status_var.set("待機中")
            self._update_button_states()
            return
        if self._cue is not None:
            cue = self._cue
            self._cue = None
            cue.stop()
            self._status_var.set("待機中")
            self._update_button_states()
            self._link_service.notify_async("/event/playback-ended", {"label": "[テスト] 手動停止"})

    def _update_button_states(self) -> None:
        busy = self._cue is not None or self._countdown_job is not None
        self._play_button.state(["disabled"] if busy or not self._file_value else ["!disabled"])
        self._stop_button.state(["!disabled"] if busy else ["disabled"])

    # ------------------------------------------------------------------
    # 終了処理
    # ------------------------------------------------------------------
    def _handle_close(self) -> None:
        # Xボタン/「閉じる」ボタンどちらもMainWindow側のコールバックへ委譲する。
        # コールバック(MainWindow._close_test_mode_window)が参照クリア+shutdown()を行う。
        self._on_close_callback()

    def shutdown(self) -> None:
        if self._countdown_job is not None:
            try:
                self.after_cancel(self._countdown_job)
            except Exception:
                pass
            self._countdown_job = None
        if self._cue is not None:
            self._cue.stop()
            self._cue = None
        self.destroy()


class CronScheduleFields(ttk.Frame):
    """「毎日◯時◯分」「毎時◯分」「時間帯」の頻度・時刻設定UI(cron/window
    辞書の生成・復元)をまとめた部品。JobRow(音源スケジュール)と
    StageShowScheduleRow(ショー予定)の両方から共通で使う。内部はgridで
    配置し、頻度切替時はgrid_remove()/grid()で表示・非表示する
    (pack_forget()だと再表示時に他ウィジェットとの左右順序が崩れるため)。"""

    def __init__(self, parent: tk.Widget, default_cron: dict, default_window: Optional[dict]):
        super().__init__(parent)

        freq_default = self._detect_freq(default_cron, default_window)
        start_hour, start_minute, end_hour, end_minute, trigger_minute = self._detect_schedule_values(
            default_cron, default_window, freq_default
        )

        col = 0
        self.freq_var = tk.StringVar(value=freq_default)
        freq_combo = ttk.Combobox(
            self, textvariable=self.freq_var, values=["毎日", "毎時", "時間帯"],
            width=5, state="readonly",
        )
        freq_combo.grid(row=0, column=col, padx=2)
        col += 1

        self.hour_var = tk.StringVar(value=str(start_hour))
        self.hour_spin = ttk.Spinbox(self, from_=0, to=23, textvariable=self.hour_var, width=3)
        self.hour_spin.grid(row=0, column=col, padx=(2, 0))
        col += 1
        ttk.Label(self, text="時").grid(row=0, column=col)
        col += 1

        self._start_colon_label = ttk.Label(self, text=":")
        self._start_colon_label.grid(row=0, column=col)
        col += 1
        self.start_minute_var = tk.StringVar(value=str(start_minute))
        self.start_minute_spin = ttk.Spinbox(self, from_=0, to=59, textvariable=self.start_minute_var, width=3)
        self.start_minute_spin.grid(row=0, column=col, padx=(0, 2))
        col += 1

        self._range_label = ttk.Label(self, text="〜")
        self._range_label.grid(row=0, column=col)
        col += 1

        self.end_hour_var = tk.StringVar(value=str(end_hour))
        self.end_hour_spin = ttk.Spinbox(self, from_=0, to=23, textvariable=self.end_hour_var, width=3)
        self.end_hour_spin.grid(row=0, column=col, padx=(2, 0))
        col += 1
        self._range_end_label = ttk.Label(self, text="時")
        self._range_end_label.grid(row=0, column=col)
        col += 1

        self._end_colon_label = ttk.Label(self, text=":")
        self._end_colon_label.grid(row=0, column=col)
        col += 1
        self.end_minute_var = tk.StringVar(value=str(end_minute))
        self.end_minute_spin = ttk.Spinbox(self, from_=0, to=59, textvariable=self.end_minute_var, width=3)
        self.end_minute_spin.grid(row=0, column=col, padx=(0, 2))
        col += 1

        self._hourly_prefix_label = ttk.Label(self, text="毎時")
        self._hourly_prefix_label.grid(row=0, column=col)
        col += 1

        self.minute_var = tk.StringVar(value=str(trigger_minute))
        minute_spin = ttk.Spinbox(self, from_=0, to=59, textvariable=self.minute_var, width=3)
        minute_spin.grid(row=0, column=col, padx=(2, 0))
        col += 1
        ttk.Label(self, text="分").grid(row=0, column=col)
        col += 1

        def _update_freq_state(*_a) -> None:
            freq = self.freq_var.get()
            if freq == "時間帯":
                self.hour_spin.state(["!disabled"])
                self.end_hour_spin.state(["!disabled"])
                self.start_minute_spin.state(["!disabled"])
                self.end_minute_spin.state(["!disabled"])
                self._start_colon_label.grid()
                self._end_colon_label.grid()
                self.start_minute_spin.grid()
                self._range_label.grid()
                self._range_end_label.grid()
                self.end_hour_spin.grid()
                self.end_minute_spin.grid()
                self._hourly_prefix_label.grid()
            else:
                self.end_hour_spin.state(["disabled"])
                self._start_colon_label.grid_remove()
                self._end_colon_label.grid_remove()
                self.start_minute_spin.grid_remove()
                self._range_label.grid_remove()
                self._range_end_label.grid_remove()
                self.end_hour_spin.grid_remove()
                self.end_minute_spin.grid_remove()
                self._hourly_prefix_label.grid_remove()
                if freq == "毎時":
                    self.hour_spin.state(["disabled"])
                else:
                    self.hour_spin.state(["!disabled"])

        self.freq_var.trace_add("write", _update_freq_state)
        _update_freq_state()

    @staticmethod
    def _detect_freq(cron: dict, window: Optional[dict]) -> str:
        if window is not None:
            return "時間帯"
        hour_raw = cron.get("hour")
        if isinstance(hour_raw, str) and "-" in hour_raw:
            # 旧仕様(時間範囲cron文字列、分単位の境界指定はできなかった)からの後方互換。
            return "時間帯"
        if "hour" in cron:
            return "毎日"
        return "毎時"

    @staticmethod
    def _detect_schedule_values(
        cron: dict, window: Optional[dict], freq: str
    ) -> tuple[int, int, int, int, int]:
        """(開始時, 開始分, 終了時, 終了分, 毎時トリガー分)を、cron/window辞書から復元する。"""
        minute_val = cron.get("minute", 0)
        # 旧仕様(間隔指定 "*/N")で保存された古いデータを開いた場合の後方互換。
        minute_s = str(minute_val)
        minute_s = minute_s.split("/", 1)[1] if "/" in minute_s else minute_s
        trigger_minute = CronScheduleFields._safe_int(minute_s, 0, 59, 0)

        if freq == "時間帯":
            if window is not None:
                start_hour = CronScheduleFields._safe_int(str(window.get("start_hour", 10)), 0, 23, 10)
                start_minute = CronScheduleFields._safe_int(str(window.get("start_minute", 0)), 0, 59, 0)
                end_hour = CronScheduleFields._safe_int(str(window.get("end_hour", 18)), 0, 23, 18)
                end_minute = CronScheduleFields._safe_int(str(window.get("end_minute", 0)), 0, 59, 0)
            else:
                # 旧仕様(cron.hourが"H1-H2"の範囲文字列、分の境界は常に0扱い)。
                hour_raw = str(cron.get("hour", "10-18"))
                start_s, _, end_s = hour_raw.partition("-")
                start_hour = CronScheduleFields._safe_int(start_s, 0, 23, 10)
                start_minute = 0
                end_hour = CronScheduleFields._safe_int(end_s, 0, 23, 18)
                end_minute = 0
            return start_hour, start_minute, end_hour, end_minute, trigger_minute

        hour_val = cron.get("hour", 9)
        hour = CronScheduleFields._safe_int(str(hour_val), 0, 23, 9)
        return hour, 0, 18, 0, trigger_minute

    @staticmethod
    def _safe_int(value: str, lo: int, hi: int, default: int) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, n))

    def get_cron_and_window(self, label_for_errors: str) -> tuple[dict, Optional[dict]]:
        """現在のUI入力からcron/window辞書を組み立てる。「時間帯」で開始≧終了の
        場合はValueErrorを送出する(呼び出し側でエラーメッセージとして表示する)。"""
        freq = self.freq_var.get()
        if freq == "時間帯":
            start_hour = self._safe_int(self.hour_var.get(), 0, 23, 10)
            start_minute = self._safe_int(self.start_minute_var.get(), 0, 59, 0)
            end_hour = self._safe_int(self.end_hour_var.get(), 0, 23, 18)
            end_minute = self._safe_int(self.end_minute_var.get(), 0, 59, 0)
            trigger_minute = self._safe_int(self.minute_var.get(), 0, 59, 0)
            if (start_hour, start_minute) >= (end_hour, end_minute):
                raise ValueError(
                    f"「{label_for_errors}」の時間帯設定が不正です"
                    "(開始時刻は終了時刻より前にしてください。日をまたぐ場合は2行に分けて登録してください)"
                )
            cron = {"minute": trigger_minute}
            window = {
                "start_hour": start_hour,
                "start_minute": start_minute,
                "end_hour": end_hour,
                "end_minute": end_minute,
            }
            return cron, window

        hour = self._safe_int(self.hour_var.get(), 0, 23, 9)
        minute = self._safe_int(self.minute_var.get(), 0, 59, 0)
        if freq == "毎時":
            return {"minute": minute}, None
        return {"hour": hour, "minute": minute}, None


class JobRow:
    """スケジュール管理画面(共通/モード別/手動追加)の1行
    (名前・時刻・ファイル・音量・有効/削除)。"""

    def __init__(
        self,
        parent: ttk.Frame,
        config: AppConfig,
        job=None,
        include_schedule: bool = True,
        include_enabled: bool = True,
        lighting_cues: Optional[list[LightingCue]] = None,
        on_move_up: Optional[Callable[[], None]] = None,
        on_move_down: Optional[Callable[[], None]] = None,
    ):
        self._config = config
        self.include_schedule = include_schedule
        self.include_enabled = include_enabled
        self.removed = False
        self._cue_label_to_id: dict[str, str] = {"(なし)": ""}
        self._cue_id_to_label: dict[str, str] = {"": "(なし)"}
        for cue in lighting_cues or []:
            self._cue_label_to_id[cue.label] = cue.id
            self._cue_id_to_label[cue.id] = cue.label

        prefix = "manual" if job is None else "job"
        if job is not None:
            self.job_id = job.id
        else:
            self.job_id = f"{prefix}_{uuid.uuid4().hex[:8]}"

        default_name = ""
        default_file = ""
        default_volume = 80
        default_cron: dict = {}
        default_window: Optional[dict] = None
        default_enabled = True
        default_cue_id = ""
        if job is not None:
            default_volume = job.volume
            default_file = job.file
            default_name = job.name
            default_cron = job.cron
            default_window = job.window
            default_enabled = job.enabled
            default_cue_id = job.lighting_cue

        self.frame = ttk.Frame(parent, relief="groove", padding=4)
        self.frame.pack(fill="x", pady=2)

        col = 0
        self.name_var = tk.StringVar(value=default_name)
        ttk.Entry(self.frame, textvariable=self.name_var, width=18).grid(row=0, column=col, padx=2)
        col += 1

        self.schedule_fields: Optional[CronScheduleFields] = None
        if include_schedule:
            self.schedule_fields = CronScheduleFields(self.frame, default_cron, default_window)
            self.schedule_fields.grid(row=0, column=col, padx=2)
            col += 1

        self.file_value = default_file
        self.file_display_var = tk.StringVar(value=self._basename(default_file))
        ttk.Label(self.frame, textvariable=self.file_display_var, width=26, anchor="w").grid(row=0, column=col, padx=4)
        col += 1
        ttk.Button(self.frame, text="参照...", command=self._browse, width=7).grid(row=0, column=col, padx=2)
        col += 1

        self.volume_var = tk.DoubleVar(value=default_volume)
        ttk.Scale(self.frame, from_=0, to=100, variable=self.volume_var, orient="horizontal", length=100).grid(
            row=0, column=col, padx=4
        )
        col += 1
        self._volume_label = ttk.Label(self.frame, width=4)
        self._volume_label.grid(row=0, column=col)
        col += 1

        def _update_volume_label(*_a) -> None:
            self._volume_label.configure(text=str(int(self.volume_var.get())))

        self.volume_var.trace_add("write", _update_volume_label)
        _update_volume_label()

        self.lighting_cue_var = tk.StringVar(value=self._cue_id_to_label.get(default_cue_id, "(なし)"))
        ttk.Combobox(
            self.frame, textvariable=self.lighting_cue_var,
            values=list(self._cue_label_to_id.keys()), width=14, state="readonly",
        ).grid(row=0, column=col, padx=4)
        col += 1

        if include_enabled:
            self.enabled_var = tk.BooleanVar(value=default_enabled)
            ttk.Checkbutton(self.frame, text="有効", variable=self.enabled_var).grid(row=0, column=col, padx=4)
            col += 1
        else:
            self.enabled_var = None

        # 並び替え(表示順のみ変更、時刻等の設定内容には影響しない)。
        up_btn = ttk.Button(self.frame, text="▲", width=2, command=lambda: on_move_up() if on_move_up else None)
        up_btn.grid(row=0, column=col, padx=(8, 0))
        col += 1
        down_btn = ttk.Button(
            self.frame, text="▼", width=2, command=lambda: on_move_down() if on_move_down else None
        )
        down_btn.grid(row=0, column=col, padx=(0, 2))
        col += 1

        ttk.Button(self.frame, text="削除", command=self._remove).grid(row=0, column=col, padx=2)

    @staticmethod
    def _basename(value: str) -> str:
        return Path(value).name if value else "(未選択)"

    def _browse(self) -> None:
        path_str = filedialog.askopenfilename(
            initialdir=str(self._config.media_root),
            filetypes=MEDIA_FILETYPES,
        )
        if not path_str:
            return
        path = Path(path_str)
        self.file_value = self._config.to_media_relative(path)
        self.file_display_var.set(path.name)

    def _remove(self) -> None:
        self.frame.destroy()
        self.removed = True

    def to_scheduled_job(self) -> ScheduledJob:
        if self.schedule_fields is not None:
            cron, window = self.schedule_fields.get_cron_and_window(self.name_var.get() or self.job_id)
        else:
            cron, window = {}, None
        enabled = self.enabled_var.get() if self.enabled_var is not None else True
        return ScheduledJob(
            id=self.job_id,
            name=self.name_var.get() or self.job_id,
            file=self.file_value,
            volume=int(self.volume_var.get()),
            cron=cron,
            enabled=enabled,
            lighting_cue=self._cue_label_to_id.get(self.lighting_cue_var.get(), ""),
            window=window,
        )

class ScheduleListEditor(ttk.Frame):
    """1つのジョブ一覧(共通/モード別/手動追加)を編集するタブの中身。"""

    def __init__(
        self,
        parent: ttk.Frame,
        config: AppConfig,
        jobs: list,
        include_schedule: bool = True,
        include_enabled: bool = True,
        lighting_cues: Optional[list[LightingCue]] = None,
    ):
        super().__init__(parent)
        self._config = config
        self._include_schedule = include_schedule
        self._include_enabled = include_enabled
        self._lighting_cues = lighting_cues or []

        canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self._rows_frame = ttk.Frame(canvas)
        self._rows_frame.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self._rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.rows: list[JobRow] = []
        for job in jobs:
            self._add_row(job)

        ttk.Button(self, text="+ 追加", command=lambda: self._add_row(None)).pack(anchor="w", padx=5, pady=5)

    def _add_row(self, job) -> None:
        container: dict = {}
        row = JobRow(
            self._rows_frame,
            self._config,
            job=job,
            include_schedule=self._include_schedule,
            include_enabled=self._include_enabled,
            lighting_cues=self._lighting_cues,
            on_move_up=lambda: self._move_row(container["row"], -1),
            on_move_down=lambda: self._move_row(container["row"], 1),
        )
        container["row"] = row
        self.rows.append(row)

    def _move_row(self, row: "JobRow", direction: int) -> None:
        """表示順のみを入れ替える(見やすさのための並び替え)。各行のcron/window
        (実際の再生時刻)には一切影響しない。"""
        idx = self.rows.index(row)
        new_idx = idx + direction
        if not (0 <= new_idx < len(self.rows)):
            return
        self.rows[idx], self.rows[new_idx] = self.rows[new_idx], self.rows[idx]
        for r in self.rows:
            r.frame.pack_forget()
        for r in self.rows:
            r.frame.pack(fill="x", pady=2)

    def get_jobs(self) -> list[ScheduledJob]:
        return [r.to_scheduled_job() for r in self.rows if not r.removed]


class StageShowClipRow:
    """StageShowRow内の動画1本分(ファイル・照明キュー・削除)。動画によって
    照明の演出が異なることがあるため、照明キューはクリップ単位で持たせる。"""

    def __init__(
        self,
        parent: ttk.Frame,
        config: AppConfig,
        cue_label_to_id: dict[str, str],
        cue_id_to_label: dict[str, str],
        clip: Optional[StageShowClip] = None,
    ):
        self._config = config
        self._cue_label_to_id = cue_label_to_id
        self.removed = False
        self.file_value = clip.file if clip is not None else ""

        self.frame = ttk.Frame(parent)
        self.frame.pack(fill="x", pady=1)

        self.file_display_var = tk.StringVar(value=self._basename(self.file_value))
        ttk.Label(self.frame, textvariable=self.file_display_var, width=26, anchor="w").pack(
            side="left", padx=2
        )
        ttk.Button(self.frame, text="参照...", command=self._browse, width=7).pack(side="left", padx=2)

        default_cue_id = clip.lighting_cue if clip is not None else ""
        self.lighting_cue_var = tk.StringVar(value=cue_id_to_label.get(default_cue_id, "(なし)"))
        ttk.Combobox(
            self.frame, textvariable=self.lighting_cue_var,
            values=list(cue_label_to_id.keys()), width=14, state="readonly",
        ).pack(side="left", padx=4)

        ttk.Button(self.frame, text="削除", command=self._remove, width=6).pack(side="left", padx=2)

    @staticmethod
    def _basename(value: str) -> str:
        return Path(value).name if value else "(未選択)"

    def _browse(self) -> None:
        path_str = filedialog.askopenfilename(
            initialdir=str(self._config.media_root),
            filetypes=MEDIA_FILETYPES,
        )
        if not path_str:
            return
        path = Path(path_str)
        self.file_value = self._config.to_media_relative(path)
        self.file_display_var.set(path.name)

    def _remove(self) -> None:
        self.frame.destroy()
        self.removed = True

    def to_clip(self) -> StageShowClip:
        return StageShowClip(
            file=self.file_value,
            lighting_cue=self._cue_label_to_id.get(self.lighting_cue_var.get(), ""),
        )


class StageShowRow:
    """スケジュール管理画面「ステージショー」タブの1行。
    1つのショーに複数動画(MV1/MV2…)を持たせられ、順番に一覧表示する。
    動画(クリップ)ごとに個別の照明キューを紐付けられる。"""

    def __init__(
        self,
        parent: ttk.Frame,
        config: AppConfig,
        show: Optional[StageShow] = None,
        lighting_cues: Optional[list[LightingCue]] = None,
    ):
        self._config = config
        self.removed = False
        self.show_id = show.id if show is not None else f"show_{uuid.uuid4().hex[:8]}"
        self._cue_label_to_id: dict[str, str] = {"(なし)": ""}
        self._cue_id_to_label: dict[str, str] = {"": "(なし)"}
        for cue in lighting_cues or []:
            self._cue_label_to_id[cue.label] = cue.id
            self._cue_id_to_label[cue.id] = cue.label

        self.frame = ttk.Frame(parent, relief="groove", padding=6)
        self.frame.pack(fill="x", pady=3)

        header = ttk.Frame(self.frame)
        header.pack(fill="x")
        self.name_var = tk.StringVar(value=(show.label if show is not None else ""))
        ttk.Entry(header, textvariable=self.name_var, width=18).pack(side="left", padx=2)

        self.volume_var = tk.DoubleVar(value=(show.volume if show is not None else 90))
        ttk.Scale(header, from_=0, to=100, variable=self.volume_var, orient="horizontal", length=120).pack(
            side="left", padx=4
        )
        self._volume_label = ttk.Label(header, width=4)
        self._volume_label.pack(side="left")

        def _update_volume_label(*_a) -> None:
            self._volume_label.configure(text=str(int(self.volume_var.get())))

        self.volume_var.trace_add("write", _update_volume_label)
        _update_volume_label()

        self.black_background_var = tk.BooleanVar(
            value=(show.black_background_on_gap if show is not None else False)
        )
        ttk.Checkbutton(
            header, text="クリップ切替・終了時は黒背景", variable=self.black_background_var
        ).pack(side="left", padx=6)

        ttk.Button(header, text="削除", command=self._remove).pack(side="right", padx=2)

        clips_frame = ttk.Frame(self.frame)
        clips_frame.pack(fill="x", pady=(4, 0))
        ttk.Label(clips_frame, text="動画(順番に再生。動画ごとに照明キューを設定可)").pack(anchor="w")

        self._rows_frame = ttk.Frame(clips_frame)
        self._rows_frame.pack(fill="x")

        self.clip_rows: list[StageShowClipRow] = []
        for clip in (show.clips if show is not None else []):
            self._add_clip_row(clip)

        ttk.Button(clips_frame, text="+ 動画追加", command=self._add_clip_via_dialog).pack(anchor="w", pady=(2, 0))

    def _add_clip_row(self, clip: Optional[StageShowClip]) -> None:
        row = StageShowClipRow(
            self._rows_frame, self._config, self._cue_label_to_id, self._cue_id_to_label, clip
        )
        self.clip_rows.append(row)

    def _add_clip_via_dialog(self) -> None:
        path_str = filedialog.askopenfilename(
            initialdir=str(self._config.media_root),
            filetypes=MEDIA_FILETYPES,
        )
        if not path_str:
            return
        file_value = self._config.to_media_relative(Path(path_str))
        self._add_clip_row(StageShowClip(file=file_value, lighting_cue=""))

    def _remove(self) -> None:
        self.frame.destroy()
        self.removed = True

    def to_stage_show(self) -> StageShow:
        return StageShow(
            id=self.show_id,
            label=self.name_var.get() or self.show_id,
            clips=[r.to_clip() for r in self.clip_rows if not r.removed],
            volume=int(self.volume_var.get()),
            black_background_on_gap=self.black_background_var.get(),
        )


class StageShowListEditor(ttk.Frame):
    """「ステージショー」タブの中身(複数動画対応のショー一覧)。"""

    def __init__(
        self,
        parent: ttk.Frame,
        config: AppConfig,
        shows: list[StageShow],
        lighting_cues: Optional[list[LightingCue]] = None,
    ):
        super().__init__(parent)
        self._config = config
        self._lighting_cues = lighting_cues or []

        canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self._rows_frame = ttk.Frame(canvas)
        self._rows_frame.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self._rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.rows: list[StageShowRow] = []
        for show in shows:
            self._add_row(show)

        ttk.Button(self, text="+ ショー追加", command=lambda: self._add_row(None)).pack(anchor="w", padx=5, pady=5)

    def _add_row(self, show: Optional[StageShow]) -> None:
        row = StageShowRow(self._rows_frame, self._config, show, lighting_cues=self._lighting_cues)
        self.rows.append(row)

    def get_stage_shows(self) -> list[StageShow]:
        return [r.to_stage_show() for r in self.rows if not r.removed]


_BLACKOUT_MODE_COMMON = "共通(両モード)"


class BlackoutWindowRow:
    """スケジュール管理画面「休止時間帯」タブの1行。指定時間帯は該当する営業
    モードの全ジョブ(共通/モード別/手動追加問わず)の再生をスキップする。"""

    def __init__(
        self,
        parent: ttk.Frame,
        window: Optional[BlackoutWindow] = None,
        mode_names: Optional[list[str]] = None,
    ):
        self.removed = False
        self.window_id = window.id if window is not None else f"blackout_{uuid.uuid4().hex[:8]}"

        self.frame = ttk.Frame(parent, relief="groove", padding=4)
        self.frame.pack(fill="x", pady=2)

        col = 0
        self.label_var = tk.StringVar(value=(window.label if window is not None else ""))
        ttk.Entry(self.frame, textvariable=self.label_var, width=16).grid(row=0, column=col, padx=2)
        col += 1

        mode_values = [_BLACKOUT_MODE_COMMON] + list(mode_names or [])
        default_mode = (window.mode if window is not None and window.mode else _BLACKOUT_MODE_COMMON)
        self.mode_var = tk.StringVar(value=default_mode if default_mode in mode_values else _BLACKOUT_MODE_COMMON)
        ttk.Combobox(
            self.frame, textvariable=self.mode_var, values=mode_values, width=12, state="readonly",
        ).grid(row=0, column=col, padx=4)
        col += 1

        self.start_hour_var = tk.StringVar(value=str(window.start_hour if window is not None else 12))
        ttk.Spinbox(self.frame, from_=0, to=23, textvariable=self.start_hour_var, width=3).grid(
            row=0, column=col, padx=(2, 0)
        )
        col += 1
        self.start_minute_var = tk.StringVar(value=str(window.start_minute if window is not None else 0))
        ttk.Spinbox(self.frame, from_=0, to=59, textvariable=self.start_minute_var, width=3).grid(
            row=0, column=col, padx=(2, 0)
        )
        col += 1
        ttk.Label(self.frame, text="〜").grid(row=0, column=col)
        col += 1

        self.end_hour_var = tk.StringVar(value=str(window.end_hour if window is not None else 13))
        ttk.Spinbox(self.frame, from_=0, to=23, textvariable=self.end_hour_var, width=3).grid(
            row=0, column=col, padx=(2, 0)
        )
        col += 1
        self.end_minute_var = tk.StringVar(value=str(window.end_minute if window is not None else 0))
        ttk.Spinbox(self.frame, from_=0, to=59, textvariable=self.end_minute_var, width=3).grid(
            row=0, column=col, padx=(2, 0)
        )
        col += 1
        ttk.Label(self.frame, text="は再生しない").grid(row=0, column=col)
        col += 1

        self.enabled_var = tk.BooleanVar(value=(window.enabled if window is not None else True))
        ttk.Checkbutton(self.frame, text="有効", variable=self.enabled_var).grid(row=0, column=col, padx=4)
        col += 1

        ttk.Button(self.frame, text="削除", command=self._remove).grid(row=0, column=col, padx=2)

    def _remove(self) -> None:
        self.frame.destroy()
        self.removed = True

    @staticmethod
    def _safe_int(value: str, lo: int, hi: int, default: int) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, n))

    def to_blackout_window(self) -> BlackoutWindow:
        mode = self.mode_var.get()
        return BlackoutWindow(
            id=self.window_id,
            label=self.label_var.get() or self.window_id,
            start_hour=self._safe_int(self.start_hour_var.get(), 0, 23, 12),
            start_minute=self._safe_int(self.start_minute_var.get(), 0, 59, 0),
            end_hour=self._safe_int(self.end_hour_var.get(), 0, 23, 13),
            end_minute=self._safe_int(self.end_minute_var.get(), 0, 59, 0),
            enabled=self.enabled_var.get(),
            mode=("" if mode == _BLACKOUT_MODE_COMMON else mode),
        )


class BlackoutWindowListEditor(ttk.Frame):
    """「休止時間帯」タブの中身。開始>終了の場合は深夜またぎとして扱われる
    (scheduler.py側の判定)ため、ここでは入力値のバリデーションは行わない。"""

    def __init__(self, parent: ttk.Frame, windows: list[BlackoutWindow], mode_names: Optional[list[str]] = None):
        super().__init__(parent)
        self._mode_names = mode_names or []

        canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self._rows_frame = ttk.Frame(canvas)
        self._rows_frame.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self._rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.rows: list[BlackoutWindowRow] = []
        for window in windows:
            self._add_row(window)

        ttk.Button(self, text="+ 休止時間帯を追加", command=lambda: self._add_row(None)).pack(
            anchor="w", padx=5, pady=5
        )

    def _add_row(self, window: Optional[BlackoutWindow]) -> None:
        row = BlackoutWindowRow(self._rows_frame, window, mode_names=self._mode_names)
        self.rows.append(row)

    def get_blackout_windows(self) -> list[BlackoutWindow]:
        return [r.to_blackout_window() for r in self.rows if not r.removed]


class StageShowScheduleRow:
    """スケジュール管理画面「ショー予定」タブの1行。指定した時刻に、拡張
    ディスプレイの表示モードを自動的に「ショー」へ切り替えたうえで、選択した
    ステージショーを自動再生する(既に別の動画を再生中・ミラーリング中は
    自動再生をスキップしてログに記録するだけにとどめる。詳細はgui.pyの
    MainWindow._handle_scheduled_stage_show参照)。"""

    def __init__(
        self,
        parent: ttk.Frame,
        shows: list[StageShow],
        job: Optional[StageShowJob] = None,
        mode_names: Optional[list[str]] = None,
    ):
        self.removed = False
        self.job_id = job.id if job is not None else f"stageschedule_{uuid.uuid4().hex[:8]}"
        self._shows = shows
        self._show_label_to_id = {s.label: s.id for s in shows}
        self._show_id_to_label = {s.id: s.label for s in shows}

        default_show_id = job.stage_show_id if job is not None else (shows[0].id if shows else "")
        default_clip_index = job.clip_index if job is not None else 0
        default_cron: dict = job.cron if job is not None else {}
        default_window = job.window if job is not None else None
        default_enabled = job.enabled if job is not None else True
        default_mode = job.mode if job is not None else ""

        self.frame = ttk.Frame(parent, relief="groove", padding=4)
        self.frame.pack(fill="x", pady=2)

        col = 0
        show_values = list(self._show_label_to_id.keys())
        default_show_label = self._show_id_to_label.get(default_show_id, (show_values[0] if show_values else ""))
        self.show_var = tk.StringVar(value=default_show_label)
        ttk.Combobox(
            self.frame, textvariable=self.show_var, values=show_values, width=16, state="readonly",
        ).grid(row=0, column=col, padx=2)
        col += 1

        self.clip_var = tk.StringVar()
        self.clip_combo = ttk.Combobox(self.frame, textvariable=self.clip_var, width=18, state="readonly")
        self.clip_combo.grid(row=0, column=col, padx=2)
        col += 1

        def _refresh_clip_options(initial_index: int = 0) -> None:
            show = next((s for s in self._shows if s.label == self.show_var.get()), None)
            if show is None or len(show.clips) <= 1:
                self.clip_combo.configure(values=["(先頭から)"], state="disabled")
                self.clip_var.set("(先頭から)")
                return
            labels = [f"{i + 1}: {Path(c.file).stem}" for i, c in enumerate(show.clips)]
            self.clip_combo.configure(values=labels, state="readonly")
            idx = initial_index if 0 <= initial_index < len(labels) else 0
            self.clip_var.set(labels[idx])

        self.show_var.trace_add("write", lambda *_a: _refresh_clip_options())
        _refresh_clip_options(initial_index=default_clip_index)

        self.schedule_fields = CronScheduleFields(self.frame, default_cron, default_window)
        self.schedule_fields.grid(row=0, column=col, padx=2)
        col += 1

        mode_values = [_BLACKOUT_MODE_COMMON] + list(mode_names or [])
        default_mode_label = default_mode if default_mode else _BLACKOUT_MODE_COMMON
        self.mode_var = tk.StringVar(
            value=default_mode_label if default_mode_label in mode_values else _BLACKOUT_MODE_COMMON
        )
        ttk.Combobox(
            self.frame, textvariable=self.mode_var, values=mode_values, width=12, state="readonly",
        ).grid(row=0, column=col, padx=4)
        col += 1

        self.enabled_var = tk.BooleanVar(value=default_enabled)
        ttk.Checkbutton(self.frame, text="有効", variable=self.enabled_var).grid(row=0, column=col, padx=4)
        col += 1

        ttk.Button(self.frame, text="削除", command=self._remove).grid(row=0, column=col, padx=2)

    def _remove(self) -> None:
        self.frame.destroy()
        self.removed = True

    def to_stage_show_job(self) -> Optional[StageShowJob]:
        """選択中のショーが存在しない場合(ショーが1つも登録されていない場合)は
        Noneを返す(呼び出し側で除外する)。"""
        show_id = self._show_label_to_id.get(self.show_var.get())
        if show_id is None:
            return None
        cron, window = self.schedule_fields.get_cron_and_window(self.show_var.get())

        show = next((s for s in self._shows if s.id == show_id), None)
        clip_index = 0
        if show is not None and len(show.clips) > 1:
            try:
                clip_index = int(self.clip_var.get().split(":", 1)[0]) - 1
            except (ValueError, IndexError):
                clip_index = 0

        mode = self.mode_var.get()
        return StageShowJob(
            id=self.job_id,
            stage_show_id=show_id,
            cron=cron,
            enabled=self.enabled_var.get(),
            clip_index=max(0, clip_index),
            mode=("" if mode == _BLACKOUT_MODE_COMMON else mode),
            window=window,
        )


class StageShowScheduleListEditor(ttk.Frame):
    """「ショー予定」タブの中身。ステージショーを時刻指定で自動再生するための
    予定一覧を編集する(ショー自体の定義は「ステージショー」タブで行う)。"""

    def __init__(
        self,
        parent: ttk.Frame,
        shows: list[StageShow],
        jobs: list[StageShowJob],
        mode_names: Optional[list[str]] = None,
    ):
        super().__init__(parent)
        self._shows = shows
        self._mode_names = mode_names or []

        if not shows:
            ttk.Label(
                self, text="先に「ステージショー」タブでショーを登録してください。",
                foreground="#888888",
            ).pack(anchor="w", padx=5, pady=10)

        canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self._rows_frame = ttk.Frame(canvas)
        self._rows_frame.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self._rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.rows: list[StageShowScheduleRow] = []
        for job in jobs:
            self._add_row(job)

        ttk.Button(self, text="+ ショー予定を追加", command=lambda: self._add_row(None)).pack(
            anchor="w", padx=5, pady=5
        )

    def _add_row(self, job: Optional[StageShowJob]) -> None:
        if not self._shows:
            messagebox.showwarning(
                "ショーが登録されていません", "先に「ステージショー」タブでショーを登録してください。"
            )
            return
        row = StageShowScheduleRow(self._rows_frame, self._shows, job, mode_names=self._mode_names)
        self.rows.append(row)

    def get_stage_show_jobs(self) -> list[StageShowJob]:
        jobs = []
        for r in self.rows:
            if r.removed:
                continue
            job = r.to_stage_show_job()
            if job is not None:
                jobs.append(job)
        return jobs


class LightingCueRow:
    """スケジュール管理画面「照明キュー」タブの1行。照明卓(Zero 88 FLX S24)の
    プレイバック番号をまとめて呼び出すための名前付きキューを定義する。
    ジョブ・ステージショー側はこのidをlighting_cueとして参照する。"""

    def __init__(self, parent: ttk.Frame, cue: Optional[LightingCue] = None):
        self.removed = False
        self.cue_id = cue.id if cue is not None else f"cue_{uuid.uuid4().hex[:8]}"

        self.frame = ttk.Frame(parent, relief="groove", padding=4)
        self.frame.pack(fill="x", pady=2)

        col = 0
        self.label_var = tk.StringVar(value=(cue.label if cue is not None else ""))
        ttk.Entry(self.frame, textvariable=self.label_var, width=28).grid(row=0, column=col, padx=2)
        col += 1

        ttk.Label(self.frame, text="プレイバック番号(カンマ区切り、例: 37,42)").grid(row=0, column=col, padx=(4, 2))
        col += 1
        default_numbers = ",".join(str(n) for n in cue.playback_numbers) if cue is not None else ""
        self.numbers_var = tk.StringVar(value=default_numbers)
        ttk.Entry(self.frame, textvariable=self.numbers_var, width=16).grid(row=0, column=col, padx=2)
        col += 1

        ttk.Button(self.frame, text="削除", command=self._remove).grid(row=0, column=col, padx=2)

    def _remove(self) -> None:
        self.frame.destroy()
        self.removed = True

    def to_lighting_cue(self) -> LightingCue:
        numbers: list[int] = []
        for part in self.numbers_var.get().split(","):
            part = part.strip()
            if not part:
                continue
            try:
                numbers.append(int(part))
            except ValueError:
                continue
        return LightingCue(
            id=self.cue_id,
            label=self.label_var.get() or self.cue_id,
            playback_numbers=numbers,
        )


class LightingCueListEditor(ttk.Frame):
    """「照明キュー」タブの中身。"""

    def __init__(self, parent: ttk.Frame, cues: list[LightingCue]):
        super().__init__(parent)

        canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self._rows_frame = ttk.Frame(canvas)
        self._rows_frame.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self._rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.rows: list[LightingCueRow] = []
        for cue in cues:
            self._add_row(cue)

        ttk.Button(self, text="+ 照明キューを追加", command=lambda: self._add_row(None)).pack(
            anchor="w", padx=5, pady=5
        )

    def _add_row(self, cue: Optional[LightingCue]) -> None:
        row = LightingCueRow(self._rows_frame, cue)
        self.rows.append(row)

    def get_lighting_cues(self) -> list[LightingCue]:
        return [r.to_lighting_cue() for r in self.rows if not r.removed]


class ScheduleManagerWindow(tk.Toplevel):
    """共通/モード別/手動追加/ステージショーの時刻・ファイル・音量をまとめて編集する画面。"""

    def __init__(self, parent: tk.Tk, config: AppConfig, on_saved: Callable[[], None]):
        super().__init__(parent)
        self.title("スケジュール管理")
        _maximize_window(self)
        self.transient(parent)
        self.grab_set()

        self._config = config
        self._on_saved = on_saved
        lighting_cues = config.load_lighting_cues()

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        common_tab = ttk.Frame(notebook)
        notebook.add(common_tab, text="共通(両モード)")
        self._common_editor = ScheduleListEditor(
            common_tab, config, config.load_common_jobs(), lighting_cues=lighting_cues
        )
        self._common_editor.pack(fill="both", expand=True)

        self._mode_editors: dict[str, ScheduleListEditor] = {}
        for mode in config.get_mode_names():
            tab = ttk.Frame(notebook)
            notebook.add(tab, text=mode)
            editor = ScheduleListEditor(tab, config, config.load_mode_jobs(mode), lighting_cues=lighting_cues)
            editor.pack(fill="both", expand=True)
            self._mode_editors[mode] = editor

        manual_tab = ttk.Frame(notebook)
        notebook.add(manual_tab, text="手動追加")
        self._manual_editor = ScheduleListEditor(
            manual_tab, config, config.load_manual_jobs(), lighting_cues=lighting_cues
        )
        self._manual_editor.pack(fill="both", expand=True)

        shows_tab = ttk.Frame(notebook)
        notebook.add(shows_tab, text="ステージショー")
        self._shows_editor = StageShowListEditor(
            shows_tab, config, config.load_stage_shows(), lighting_cues=lighting_cues
        )
        self._shows_editor.pack(fill="both", expand=True)

        stage_schedule_tab = ttk.Frame(notebook)
        notebook.add(stage_schedule_tab, text="ショー予定")
        self._stage_schedule_editor = StageShowScheduleListEditor(
            stage_schedule_tab, config.load_stage_shows(), config.load_stage_show_schedule(),
            mode_names=config.get_mode_names(),
        )
        self._stage_schedule_editor.pack(fill="both", expand=True)

        blackout_tab = ttk.Frame(notebook)
        notebook.add(blackout_tab, text="休止時間帯")
        self._blackout_editor = BlackoutWindowListEditor(
            blackout_tab, config.load_blackout_windows(), mode_names=config.get_mode_names()
        )
        self._blackout_editor.pack(fill="both", expand=True)

        lighting_tab = ttk.Frame(notebook)
        notebook.add(lighting_tab, text="照明キュー")
        self._lighting_editor = LightingCueListEditor(lighting_tab, lighting_cues)
        self._lighting_editor.pack(fill="both", expand=True)

        button_frame = ttk.Frame(self)
        button_frame.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(button_frame, text="保存", command=self._save).pack(side="right")
        ttk.Button(button_frame, text="キャンセル", command=self.destroy).pack(side="right", padx=5)

    def _save(self) -> None:
        try:
            common_jobs = self._common_editor.get_jobs()
            mode_jobs = {mode: editor.get_jobs() for mode, editor in self._mode_editors.items()}
            manual_jobs = self._manual_editor.get_jobs()
            stage_show_jobs = self._stage_schedule_editor.get_stage_show_jobs()
        except ValueError as e:
            messagebox.showerror("入力エラー", str(e))
            return

        self._config.save_lighting_cues(self._lighting_editor.get_lighting_cues())
        self._config.save_common_jobs(common_jobs)
        for mode, jobs in mode_jobs.items():
            self._config.save_mode_jobs(mode, jobs)
        self._config.save_manual_jobs(manual_jobs)
        self._config.save_stage_shows(self._shows_editor.get_stage_shows())
        self._config.save_stage_show_schedule(stage_show_jobs)
        self._config.save_blackout_windows(self._blackout_editor.get_blackout_windows())

        self._on_saved()
        self.destroy()


class DailyScheduleWindow(tk.Toplevel):
    """「一日の予定を表示」画面。スケジュール管理画面はcron設定(◯時◯分・
    毎時◯分・時間帯)をそのまま編集する画面で、運用担当者が「今日は結局
    何時に何が鳴るか」を一目で把握するには不向きだった。この画面は選択した
    営業モードのcronを実際に1日分展開し、休止時間帯による無効化も反映した
    うえで時刻順の読みやすい一覧として表示する。テキストファイルへの保存も
    でき、印刷用に使える。"""

    def __init__(self, parent: tk.Tk, config: AppConfig, initial_mode: str):
        super().__init__(parent)
        self.title("一日の予定")
        self.transient(parent)
        self.geometry("480x600")

        self._config = config
        self._day = date.today()

        top = ttk.Frame(self, padding=(12, 12, 12, 6))
        top.pack(fill="x")
        ttk.Label(top, text="営業モード:").pack(side="left")
        mode_names = config.get_mode_names()
        self._mode_var = tk.StringVar(value=(initial_mode if initial_mode in mode_names else (mode_names[0] if mode_names else "")))
        mode_combo = ttk.Combobox(
            top, textvariable=self._mode_var, values=mode_names, state="readonly", width=16
        )
        mode_combo.pack(side="left", padx=(6, 0))
        mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._render())

        ttk.Label(top, text=f"  {self._day:%Y-%m-%d}({_WEEKDAY_JA[self._day.weekday()]})").pack(side="left")

        button_row = ttk.Frame(self, padding=(12, 0, 12, 6))
        button_row.pack(fill="x")
        ttk.Button(button_row, text="テキストファイルに保存...", command=self._export).pack(side="right")

        container = ttk.Frame(self, padding=(12, 0, 12, 12))
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self._list_frame = ttk.Frame(canvas)
        self._list_frame.bind(
            "<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self._list_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._render()

    def _current_entries(self):
        return build_daily_schedule(self._config, self._mode_var.get(), self._day)

    def _render(self) -> None:
        for widget in self._list_frame.winfo_children():
            widget.destroy()

        entries = self._current_entries()
        if not entries:
            ttk.Label(self._list_frame, text="(本日再生される予定はありません)", foreground="#888888").pack(
                anchor="w", pady=10
            )
            return

        for entry in entries:
            row = ttk.Frame(self._list_frame)
            row.pack(fill="x", pady=2)
            if entry.kind == "blackout":
                text = f"{format_time_range(entry.start, entry.end)}  【休止時間帯】{entry.label}"
                ttk.Label(row, text=text, foreground="#b02a2a").pack(anchor="w")
            elif entry.kind == "show":
                text = f"{entry.start:%H:%M}    【ショー】{entry.label}"
                ttk.Label(row, text=text, font=("", 11, "bold"), foreground="#1565c0").pack(anchor="w")
            else:
                text = f"{entry.start:%H:%M}    {entry.label}"
                ttk.Label(row, text=text, font=("", 11)).pack(anchor="w")

    def _export(self) -> None:
        mode = self._mode_var.get()
        entries = self._current_entries()
        content = format_daily_schedule_text(mode, self._day, entries)

        path_str = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("テキストファイル", "*.txt")],
            initialfile=f"タイムスケジュール_{self._day:%Y%m%d}_{mode}.txt",
        )
        if not path_str:
            return
        try:
            Path(path_str).write_text(content, encoding="utf-8")
        except OSError as e:
            messagebox.showerror("保存に失敗しました", str(e))
            return
        messagebox.showinfo("保存しました", f"{path_str} に保存しました。")


class SystemSettingsWindow(tk.Toplevel):
    """「システム設定」画面。この端末の名前と、2台のSurfaceを連携させる
    リンク機能の設定(相手端末のIP等)をGUIから編集できるようにする。
    peer_host/peer_port/duck_volume_percentの変更は次回の通信・ポーリングから
    即座に反映されるが、リンク機能の有効/無効・待受ポート・監視間隔の変更は
    アプリの再起動が必要(HTTPサーバーの起動状態を保存時に変えないため)。"""

    def __init__(self, parent: tk.Tk, config: AppConfig, on_saved: Callable[[], None]):
        super().__init__(parent)
        self.title("システム設定")
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)

        self._config = config
        self._on_saved = on_saved
        self._background_image_value = config.standby_background_image
        self._slideshow_folder_value = config.slideshow_folder
        link = config.link

        form = ttk.Frame(self, padding=16)
        form.pack(fill="both", expand=True)

        row = 0
        ttk.Label(form, text="この端末の名前").grid(row=row, column=0, sticky="w", pady=4)
        self.device_name_var = tk.StringVar(value=config.device_name)
        ttk.Entry(form, textvariable=self.device_name_var, width=24).grid(row=row, column=1, sticky="w", pady=4)
        row += 1

        self.confine_cursor_var = tk.BooleanVar(value=config.confine_cursor_to_primary_monitor)
        ttk.Checkbutton(
            form,
            text="外部ディスプレイへカーソルが移動しないよう制限する(ミラーリング中は除く)",
            variable=self.confine_cursor_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=4)
        row += 1

        ttk.Separator(form, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)
        row += 1

        ttk.Label(form, text="連携(2台のSurface連携)", font=("", 13, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1

        ttk.Label(form, text="この端末のIPアドレス").grid(row=row, column=0, sticky="w", pady=4)
        ip_row = ttk.Frame(form)
        ip_row.grid(row=row, column=1, sticky="w", pady=4)
        self._own_ip = get_local_ip()
        ttk.Label(ip_row, text=self._own_ip or "(取得できませんでした)").pack(side="left")
        if self._own_ip:
            ttk.Button(ip_row, text="コピー", width=6, command=self._copy_own_ip).pack(side="left", padx=(6, 0))
        row += 1

        self.enabled_var = tk.BooleanVar(value=link.enabled)
        ttk.Checkbutton(form, text="リンク機能を有効にする(再起動が必要)", variable=self.enabled_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=4
        )
        row += 1

        ttk.Label(form, text="相手端末のIPアドレス(もう1台のこの画面に表示された値)").grid(row=row, column=0, sticky="w", pady=4)
        self.peer_host_var = tk.StringVar(value=link.peer_host)
        ttk.Entry(form, textvariable=self.peer_host_var, width=24).grid(row=row, column=1, sticky="w", pady=4)
        row += 1

        ttk.Label(form, text="相手端末のポート番号").grid(row=row, column=0, sticky="w", pady=4)
        self.peer_port_var = tk.StringVar(value=str(link.peer_port))
        ttk.Spinbox(form, from_=1, to=65535, textvariable=self.peer_port_var, width=8).grid(
            row=row, column=1, sticky="w", pady=4
        )
        row += 1

        ttk.Label(form, text="自機の待受ポート番号(再起動が必要)").grid(row=row, column=0, sticky="w", pady=4)
        self.listen_port_var = tk.StringVar(value=str(link.listen_port))
        ttk.Spinbox(form, from_=1, to=65535, textvariable=self.listen_port_var, width=8).grid(
            row=row, column=1, sticky="w", pady=4
        )
        row += 1

        ttk.Label(form, text="ダッキング音量(相手再生中、元音量の何%まで下げるか)").grid(
            row=row, column=0, sticky="w", pady=4
        )
        self.duck_var = tk.StringVar(value=str(link.duck_volume_percent))
        ttk.Spinbox(form, from_=0, to=100, textvariable=self.duck_var, width=8).grid(
            row=row, column=1, sticky="w", pady=4
        )
        row += 1

        ttk.Label(form, text="監視間隔(秒、接続状態の確認・設定同期の間隔)").grid(
            row=row, column=0, sticky="w", pady=4
        )
        self.poll_interval_var = tk.StringVar(value=str(link.poll_interval_seconds))
        ttk.Spinbox(form, from_=1, to=600, textvariable=self.poll_interval_var, width=8).grid(
            row=row, column=1, sticky="w", pady=4
        )
        row += 1

        ttk.Separator(form, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)
        row += 1

        ttk.Label(form, text="スケジュール音源の再生準備", font=("", 13, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1

        ttk.Label(form, text="再生開始の遅延(秒、頭切れ対策)").grid(row=row, column=0, sticky="w", pady=4)
        self.prepare_delay_var = tk.StringVar(value=f"{config.playback_prepare_delay_seconds:.1f}")
        ttk.Spinbox(
            form, from_=0.0, to=10.0, increment=0.1, format="%.1f",
            textvariable=self.prepare_delay_var, width=8,
        ).grid(row=row, column=1, sticky="w", pady=4)
        row += 1

        ttk.Label(
            form,
            text="発火時刻に無音で準備してから遅延後に再生することで頭切れを防ぐ(0で従来通り即再生)",
            foreground="#666666",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        row += 1

        ttk.Separator(form, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)
        row += 1

        ttk.Label(form, text="「ショー」表示の待機画面の背景", font=("", 13, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1

        ttk.Label(form, text="現在の設定:").grid(row=row, column=0, sticky="w", pady=4)
        self.background_display_var = tk.StringVar(value=self._background_label_text(self._background_image_value))
        ttk.Label(form, textvariable=self.background_display_var, anchor="w").grid(
            row=row, column=1, sticky="w", pady=4
        )
        row += 1

        bg_btn_row = ttk.Frame(form)
        bg_btn_row.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Button(bg_btn_row, text="画像を選択...", command=self._browse_background_image).pack(side="left")
        ttk.Button(bg_btn_row, text="黒背景に戻す", command=self._clear_background_image).pack(
            side="left", padx=(6, 0)
        )
        row += 1

        ttk.Separator(form, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)
        row += 1

        ttk.Label(form, text="「通常」表示のスライドショー", font=("", 13, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1

        ttk.Label(form, text="現在の設定:").grid(row=row, column=0, sticky="w", pady=4)
        self.slideshow_display_var = tk.StringVar(
            value=self._slideshow_label_text(self._slideshow_folder_value)
        )
        ttk.Label(form, textvariable=self.slideshow_display_var, anchor="w").grid(
            row=row, column=1, sticky="w", pady=4
        )
        row += 1

        slideshow_btn_row = ttk.Frame(form)
        slideshow_btn_row.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Button(slideshow_btn_row, text="フォルダーを選択...", command=self._browse_slideshow_folder).pack(
            side="left"
        )
        ttk.Button(slideshow_btn_row, text="設定を解除", command=self._clear_slideshow_folder).pack(
            side="left", padx=(6, 0)
        )
        row += 1

        ttk.Label(form, text="1枚あたりの表示時間(秒)").grid(row=row, column=0, sticky="w", pady=4)
        self.slideshow_interval_var = tk.StringVar(value=str(config.slideshow_interval_seconds))
        ttk.Spinbox(form, from_=1, to=600, textvariable=self.slideshow_interval_var, width=8).grid(
            row=row, column=1, sticky="w", pady=4
        )
        row += 1

        button_frame = ttk.Frame(self, padding=(16, 0, 16, 16))
        button_frame.pack(fill="x")
        ttk.Button(button_frame, text="保存", command=self._save).pack(side="right")
        ttk.Button(button_frame, text="キャンセル", command=self.destroy).pack(side="right", padx=5)

    @staticmethod
    def _safe_int(value: str, lo: int, hi: int, default: int) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, n))

    @staticmethod
    def _safe_float(value: str, lo: float, hi: float, default: float) -> float:
        try:
            n = float(value)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, n))

    def _copy_own_ip(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self._own_ip)

    @staticmethod
    def _background_label_text(value: str) -> str:
        return Path(value).name if value else "黒背景"

    def _browse_background_image(self) -> None:
        path_str = filedialog.askopenfilename(
            initialdir=str(self._config.media_root),
            filetypes=IMAGE_FILETYPES,
        )
        if not path_str:
            return
        self._background_image_value = self._config.to_media_relative(Path(path_str))
        self.background_display_var.set(self._background_label_text(self._background_image_value))

    def _clear_background_image(self) -> None:
        self._background_image_value = ""
        self.background_display_var.set(self._background_label_text(self._background_image_value))

    @staticmethod
    def _slideshow_label_text(value: str) -> str:
        return value if value else "(未設定)"

    def _browse_slideshow_folder(self) -> None:
        path_str = filedialog.askdirectory(initialdir=str(self._config.media_root))
        if not path_str:
            return
        self._slideshow_folder_value = self._config.to_media_relative(Path(path_str))
        self.slideshow_display_var.set(self._slideshow_label_text(self._slideshow_folder_value))

    def _clear_slideshow_folder(self) -> None:
        self._slideshow_folder_value = ""
        self.slideshow_display_var.set(self._slideshow_label_text(self._slideshow_folder_value))

    def _save(self) -> None:
        device_name = self.device_name_var.get().strip()
        link = LinkConfig(
            enabled=self.enabled_var.get(),
            peer_host=self.peer_host_var.get().strip(),
            peer_port=self._safe_int(self.peer_port_var.get(), 1, 65535, 8765),
            listen_port=self._safe_int(self.listen_port_var.get(), 1, 65535, 8765),
            duck_volume_percent=self._safe_int(self.duck_var.get(), 0, 100, 20),
            poll_interval_seconds=self._safe_int(self.poll_interval_var.get(), 1, 600, 1),
            http_timeout_seconds=self._config.link.http_timeout_seconds,
        )
        if link.enabled and not link.peer_host:
            messagebox.showerror("入力エラー", "リンク機能を有効にする場合は、相手端末のIPアドレスを入力してください。")
            return

        slideshow_interval_seconds = self._safe_int(self.slideshow_interval_var.get(), 1, 600, 8)
        prepare_delay_seconds = self._safe_float(self.prepare_delay_var.get(), 0.0, 10.0, 1.0)
        confine_cursor = self.confine_cursor_var.get()
        self._config.save_system_settings(
            device_name, link, self._background_image_value, self._slideshow_folder_value,
            slideshow_interval_seconds, prepare_delay_seconds, confine_cursor,
        )
        self._on_saved()
        messagebox.showinfo(
            "保存しました",
            "設定を保存しました。相手端末のIP/ポート・ダッキング音量・監視間隔は次回の通信から反映されます。\n"
            "リンク機能の有効/無効・待受ポート番号の変更を反映するには、アプリを再起動してください。\n"
            "待機画面の背景は次に「ショー」に切り替えたときから、スライドショーのフォルダーは"
            "次に「通常」に切り替えたときから反映されます(現在表示中の内容には即時反映されません)。\n"
            "スライドの表示時間は、表示中でも次のスライド切替のタイミングから反映されます。\n"
            "再生開始の遅延は、次回以降のスケジュール音源の発火から反映されます。\n"
            "カーソルの移動制限は今すぐ反映されます。",
        )
        self.destroy()
