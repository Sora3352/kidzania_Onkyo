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
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from .config import AppConfig, BlackoutWindow, LinkConfig, ScheduledJob, StageShow
from .link import LinkService
from .player import FullscreenVideoPlayer
from .scheduler import JobScheduler

_WEEKDAY_JA = "月火水木金土日"

MEDIA_FILETYPES = [
    ("メディアファイル", "*.mp3 *.wav *.mp4 *.mov *.m4a *.wmv *.avi"),
    ("すべてのファイル", "*.*"),
]

_SPI_GETWORKAREA = 0x0030

_HEADER_COLOR = "#c4044b"
_SUBHEADER_COLOR = "#F8981D"
_ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def _maximize_window(window: tk.Wm) -> None:
    """Tkinterのstate('zoomed')はPer-Monitor DPI Aware環境でウィンドウサイズを
    誤って計算し、画面からはみ出すことがある(例: ボタンが画面外に出て押せない)。
    そのためWin32 APIから実際の作業領域(タスクバーを除いた表示可能領域)を
    取得し、その85%程度・中央配置のサイズで直接ウィンドウを配置する
    (画面いっぱいにすると逆に端のボタンが押しづらくなるため、余白を残す)。"""
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
    ):
        self._root = root
        self._config = config
        self._logger = logger
        self._scheduler = scheduler
        self._on_reload_schedule = on_reload_schedule
        self._link_service = link_service
        self._duck_refcount = 0
        self._video_player = FullscreenVideoPlayer(root, vlc_instance, logger)
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._stage_buttons: dict[str, ttk.Button] = {}
        self._stage_mode_var = tk.BooleanVar(value=False)

        self._update_title()
        _maximize_window(root)
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
        top.pack(fill="x")

        stage_mode_frame = ttk.Frame(top)
        stage_mode_frame.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(
            stage_mode_frame,
            text="ステージショーモード(拡張ディスプレイに待機黒画面を表示)",
            variable=self._stage_mode_var,
            command=self._on_stage_mode_toggle,
        ).pack(side="left")

        ttk.Label(top, text="ステージショー", font=("", 17, "bold")).pack(anchor="w")

        self._stage_button_frame = ttk.Frame(top)
        self._stage_button_frame.pack(fill="x", pady=5)
        self._render_stage_buttons(self._stage_button_frame)

        control_frame = ttk.Frame(top)
        control_frame.pack(fill="x", pady=(0, 5))
        ttk.Button(control_frame, text="■ 停止", command=self._on_stop_clicked).pack(side="left", padx=(0, 5))
        self._next_button = ttk.Button(control_frame, text="次のMV ▶", command=self._on_next_clip_clicked)
        self._next_button.pack(side="left")
        self._next_button.state(["disabled"])

        overview_frame = ttk.Frame(top)
        overview_frame.pack(fill="x", pady=(5, 0))

        overview_left = ttk.Frame(overview_frame)
        overview_left.pack(side="left", fill="both", expand=True)

        ttk.Label(overview_left, text="現在再生中", font=("", 14, "bold")).pack(anchor="w")
        self._active_frame = ttk.Frame(overview_left)
        self._active_frame.pack(fill="x")

        ttk.Label(overview_left, text="次の予定", font=("", 14, "bold")).pack(anchor="w", pady=(4, 0))
        self._upcoming_frame = ttk.Frame(overview_left)
        self._upcoming_frame.pack(fill="x")

        ttk.Button(overview_frame, text="スケジュール管理...", command=self._open_schedule_manager).pack(
            side="right", anchor="n"
        )

        log_frame = ttk.Frame(self._root, padding=(10, 0, 10, 10))
        log_frame.pack(fill="both", expand=True)
        ttk.Label(log_frame, text="動作ログ").pack(anchor="w")

        self._log_text = tk.Text(log_frame, height=18, state="disabled", wrap="word", font=("", 12))
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

        ttk.Button(subheader, text="システム設定...", command=self._open_system_settings).pack(
            side="right", padx=(0, 16), pady=10
        )

    def _render_stage_buttons(self, frame: ttk.Frame) -> None:
        for widget in frame.winfo_children():
            widget.destroy()
        self._stage_buttons.clear()

        shows = self._config.load_stage_shows()
        if not shows:
            ttk.Label(frame, text="(stage_shows.json にショーが登録されていません)").pack()
            return

        for show in shows:
            btn = ttk.Button(
                frame,
                text=f"▶ {show.label}",
                command=lambda s=show: self._start_stage_show(s),
            )
            btn.pack(side="left", padx=5)
            if not self._stage_mode_var.get():
                btn.state(["disabled"])
            self._stage_buttons[show.id] = btn

    def _set_stage_buttons_enabled(self, enabled: bool) -> None:
        for btn in self._stage_buttons.values():
            btn.state(["!disabled"] if enabled else ["disabled"])

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
        for widget in self._active_frame.winfo_children():
            widget.destroy()

        active = self._scheduler.get_active_cues()
        if not active:
            ttk.Label(self._active_frame, text="(再生中の項目はありません)", foreground="#888888").pack(anchor="w")
            return

        for job_id, name in active:
            row = ttk.Frame(self._active_frame)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=name, width=32, anchor="w").pack(side="left")
            ttk.Button(
                row, text="■ 停止", width=8, command=lambda jid=job_id: self._on_stop_active_cue(jid)
            ).pack(side="left", padx=4)

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
            ttk.Label(row, text=f"{next_run_time:%H:%M:%S}  {name}", width=32, anchor="w").pack(side="left")
            ttk.Button(
                row, text="次回スキップ", width=10, command=lambda jid=job_id: self._on_skip_next(jid)
            ).pack(side="left", padx=4)

    def _on_skip_next(self, job_id: str) -> None:
        self._scheduler.skip_next(job_id)
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
    # ステージショーモード(拡張ディスプレイの待機黒画面)
    # ------------------------------------------------------------------
    def _on_stage_mode_toggle(self) -> None:
        if self._stage_mode_var.get():
            self._video_player.enter_standby()
            self._set_stage_buttons_enabled(True)
        else:
            self._video_player.force_close()
            self._set_stage_buttons_enabled(False)
            self._next_button.state(["disabled"])

    # ------------------------------------------------------------------
    # ステージショー再生
    # ------------------------------------------------------------------
    def _start_stage_show(self, show: StageShow) -> None:
        if not self._stage_mode_var.get():
            messagebox.showwarning("ステージショーモードOFF", "先に「ステージショーモード」をONにしてください。")
            return

        if self._video_player.is_playing():
            messagebox.showwarning("再生中", "別のショーを再生中です。終了までお待ちください。")
            return

        self._set_stage_buttons_enabled(False)
        files = [self._config.resolve_media(f) for f in show.files]

        def _on_finished() -> None:
            self._set_stage_buttons_enabled(self._stage_mode_var.get())
            self._next_button.state(["disabled"])
            self._link_service.notify_async("/event/playback-ended", {"label": show.label})

        self._video_player.play(files, show.volume, show.label, _on_finished)
        self._next_button.state(["!disabled"] if len(show.files) > 1 else ["disabled"])
        self._link_service.notify_async("/event/playback-started", {"label": show.label})

    def _on_stop_clicked(self) -> None:
        self._video_player.request_stop()
        if not self._video_player.has_standby_window():
            # 待機画面ごと閉じた場合はモード表示もOFFに揃える
            self._stage_mode_var.set(False)
            self._set_stage_buttons_enabled(False)
            self._next_button.state(["disabled"])
        self._link_service.notify_async("/event/stop-all", {})

    def _on_next_clip_clicked(self) -> None:
        self._video_player.next_clip()

    # ------------------------------------------------------------------
    # スケジュール管理画面
    # ------------------------------------------------------------------
    def _open_schedule_manager(self) -> None:
        ScheduleManagerWindow(self._root, self._config, self._on_schedule_saved_locally)

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

    def _on_system_settings_saved(self) -> None:
        self._update_title()
        self._device_name_var.set(self._device_name_label_text())
        if self._config.device_name:
            self._device_name_label.pack(side="left", padx=(16, 6), pady=10)
        else:
            self._device_name_label.pack_forget()

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
        スケジュールBGM双方)を全て停止する。"""
        self._video_player.force_close()
        self._stage_mode_var.set(False)
        self._set_stage_buttons_enabled(False)
        self._next_button.state(["disabled"])
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
        self._root.destroy()


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
    ):
        self._config = config
        self.include_schedule = include_schedule
        self.include_enabled = include_enabled
        self.removed = False

        prefix = "manual" if job is None else "job"
        if job is not None:
            self.job_id = job.id
        else:
            self.job_id = f"{prefix}_{uuid.uuid4().hex[:8]}"

        default_name = ""
        default_file = ""
        default_volume = 80
        default_cron: dict = {}
        default_enabled = True
        if job is not None:
            default_volume = job.volume
            default_file = job.file
            default_name = job.name
            default_cron = job.cron
            default_enabled = job.enabled

        self.frame = ttk.Frame(parent, relief="groove", padding=4)
        self.frame.pack(fill="x", pady=2)

        col = 0
        self.name_var = tk.StringVar(value=default_name)
        ttk.Entry(self.frame, textvariable=self.name_var, width=18).grid(row=0, column=col, padx=2)
        col += 1

        if include_schedule:
            freq_default = self._detect_freq(default_cron)
            default_hour, default_end_hour, default_minute = self._detect_schedule_values(default_cron, freq_default)

            self.freq_var = tk.StringVar(value=freq_default)
            freq_combo = ttk.Combobox(
                self.frame, textvariable=self.freq_var, values=["毎日", "毎時", "時間帯"],
                width=5, state="readonly",
            )
            freq_combo.grid(row=0, column=col, padx=2)
            col += 1

            self.hour_var = tk.StringVar(value=str(default_hour))
            self.hour_spin = ttk.Spinbox(self.frame, from_=0, to=23, textvariable=self.hour_var, width=3)
            self.hour_spin.grid(row=0, column=col, padx=(2, 0))
            col += 1
            ttk.Label(self.frame, text="時").grid(row=0, column=col)
            col += 1

            self._range_label = ttk.Label(self.frame, text="〜")
            self._range_label.grid(row=0, column=col)
            col += 1

            self.end_hour_var = tk.StringVar(value=str(default_end_hour))
            self.end_hour_spin = ttk.Spinbox(self.frame, from_=0, to=23, textvariable=self.end_hour_var, width=3)
            self.end_hour_spin.grid(row=0, column=col, padx=(2, 0))
            col += 1
            self._range_end_label = ttk.Label(self.frame, text="時")
            self._range_end_label.grid(row=0, column=col)
            col += 1

            self.minute_var = tk.StringVar(value=str(default_minute))
            minute_spin = ttk.Spinbox(self.frame, from_=0, to=59, textvariable=self.minute_var, width=3)
            minute_spin.grid(row=0, column=col, padx=(2, 0))
            col += 1
            ttk.Label(self.frame, text="分").grid(row=0, column=col)
            col += 1

            def _update_freq_state(*_a) -> None:
                freq = self.freq_var.get()
                if freq == "時間帯":
                    self.hour_spin.state(["!disabled"])
                    self.end_hour_spin.state(["!disabled"])
                    self._range_label.grid()
                    self._range_end_label.grid()
                    self.end_hour_spin.grid()
                else:
                    self.end_hour_spin.state(["disabled"])
                    self._range_label.grid_remove()
                    self._range_end_label.grid_remove()
                    self.end_hour_spin.grid_remove()
                    if freq == "毎時":
                        self.hour_spin.state(["disabled"])
                    else:
                        self.hour_spin.state(["!disabled"])

            self.freq_var.trace_add("write", _update_freq_state)
            _update_freq_state()

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

        if include_enabled:
            self.enabled_var = tk.BooleanVar(value=default_enabled)
            ttk.Checkbutton(self.frame, text="有効", variable=self.enabled_var).grid(row=0, column=col, padx=4)
            col += 1
        else:
            self.enabled_var = None

        ttk.Button(self.frame, text="削除", command=self._remove).grid(row=0, column=col, padx=2)

    @staticmethod
    def _detect_freq(cron: dict) -> str:
        hour_raw = cron.get("hour")
        if isinstance(hour_raw, str) and "-" in hour_raw:
            return "時間帯"
        if "hour" in cron:
            return "毎日"
        return "毎時"

    @staticmethod
    def _detect_schedule_values(cron: dict, freq: str) -> tuple[int, int, int]:
        """(開始時/毎日の時, 終了時, 分)を、cron辞書から復元する。"""
        minute_val = cron.get("minute", 0)
        # 旧仕様(間隔指定 "*/N")で保存された古いデータを開いた場合の後方互換。
        minute_s = str(minute_val)
        minute_s = minute_s.split("/", 1)[1] if "/" in minute_s else minute_s
        minute = JobRow._safe_int(minute_s, 0, 59, 0)

        if freq == "時間帯":
            hour_raw = str(cron.get("hour", "10-18"))
            start_s, _, end_s = hour_raw.partition("-")
            start_hour = JobRow._safe_int(start_s, 0, 23, 10)
            end_hour = JobRow._safe_int(end_s, 0, 23, 18)
            return start_hour, end_hour, minute

        hour_val = cron.get("hour", 9)
        hour = JobRow._safe_int(str(hour_val), 0, 23, 9)
        return hour, 18, minute

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

    @staticmethod
    def _safe_int(value: str, lo: int, hi: int, default: int) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, n))

    def to_scheduled_job(self) -> ScheduledJob:
        if self.include_schedule:
            freq = self.freq_var.get()
            if freq == "時間帯":
                start_hour = self._safe_int(self.hour_var.get(), 0, 23, 10)
                end_hour = self._safe_int(self.end_hour_var.get(), 0, 23, 18)
                minute = self._safe_int(self.minute_var.get(), 0, 59, 0)
                if start_hour > end_hour:
                    raise ValueError(
                        f"「{self.name_var.get() or self.job_id}」の時間帯設定が不正です"
                        "(開始時は終了時以下にしてください。日をまたぐ場合は2行に分けて登録してください)"
                    )
                cron = {"hour": f"{start_hour}-{end_hour}", "minute": minute}
            else:
                hour = self._safe_int(self.hour_var.get(), 0, 23, 9)
                minute = self._safe_int(self.minute_var.get(), 0, 59, 0)
                if freq == "毎時":
                    cron = {"minute": minute}
                else:
                    cron = {"hour": hour, "minute": minute}
        else:
            cron = {}
        enabled = self.enabled_var.get() if self.enabled_var is not None else True
        return ScheduledJob(
            id=self.job_id,
            name=self.name_var.get() or self.job_id,
            file=self.file_value,
            volume=int(self.volume_var.get()),
            cron=cron,
            enabled=enabled,
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
    ):
        super().__init__(parent)
        self._config = config
        self._include_schedule = include_schedule
        self._include_enabled = include_enabled

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
        row = JobRow(
            self._rows_frame,
            self._config,
            job=job,
            include_schedule=self._include_schedule,
            include_enabled=self._include_enabled,
        )
        self.rows.append(row)

    def get_jobs(self) -> list[ScheduledJob]:
        return [r.to_scheduled_job() for r in self.rows if not r.removed]


class StageShowRow:
    """スケジュール管理画面「ステージショー」タブの1行。
    1つのショーに複数動画(MV1/MV2…)を持たせられ、順番に一覧表示する。"""

    def __init__(self, parent: ttk.Frame, config: AppConfig, show: Optional[StageShow] = None):
        self._config = config
        self.removed = False
        self.show_id = show.id if show is not None else f"show_{uuid.uuid4().hex[:8]}"
        self.files: list[str] = list(show.files) if show is not None else []

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

        ttk.Button(header, text="削除", command=self._remove).pack(side="right", padx=2)

        body = ttk.Frame(self.frame)
        body.pack(fill="x", pady=(4, 0))
        self._listbox = tk.Listbox(body, height=3, width=45, font=("", 12))
        self._listbox.pack(side="left", fill="x", expand=True)
        btns = ttk.Frame(body)
        btns.pack(side="left", padx=4)
        ttk.Button(btns, text="+ 動画追加", command=self._add_file, width=12).pack(pady=1)
        ttk.Button(btns, text="選択を削除", command=self._remove_selected_file, width=12).pack(pady=1)

        self._refresh_listbox()

    def _refresh_listbox(self) -> None:
        self._listbox.delete(0, "end")
        for i, f in enumerate(self.files, start=1):
            self._listbox.insert("end", f"{i}. {Path(f).name}")

    def _add_file(self) -> None:
        path_str = filedialog.askopenfilename(
            initialdir=str(self._config.media_root),
            filetypes=MEDIA_FILETYPES,
        )
        if not path_str:
            return
        self.files.append(self._config.to_media_relative(Path(path_str)))
        self._refresh_listbox()

    def _remove_selected_file(self) -> None:
        selection = self._listbox.curselection()
        if not selection:
            return
        del self.files[selection[0]]
        self._refresh_listbox()

    def _remove(self) -> None:
        self.frame.destroy()
        self.removed = True

    def to_stage_show(self) -> StageShow:
        return StageShow(
            id=self.show_id,
            label=self.name_var.get() or self.show_id,
            files=list(self.files),
            volume=int(self.volume_var.get()),
        )


class StageShowListEditor(ttk.Frame):
    """「ステージショー」タブの中身(複数動画対応のショー一覧)。"""

    def __init__(self, parent: ttk.Frame, config: AppConfig, shows: list[StageShow]):
        super().__init__(parent)
        self._config = config

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
        row = StageShowRow(self._rows_frame, self._config, show)
        self.rows.append(row)

    def get_stage_shows(self) -> list[StageShow]:
        return [r.to_stage_show() for r in self.rows if not r.removed]


class BlackoutWindowRow:
    """スケジュール管理画面「休止時間帯」タブの1行。指定時間帯は共通/モード別/
    手動追加を問わず全ジョブの再生をスキップする。"""

    def __init__(self, parent: ttk.Frame, window: Optional[BlackoutWindow] = None):
        self.removed = False
        self.window_id = window.id if window is not None else f"blackout_{uuid.uuid4().hex[:8]}"

        self.frame = ttk.Frame(parent, relief="groove", padding=4)
        self.frame.pack(fill="x", pady=2)

        col = 0
        self.label_var = tk.StringVar(value=(window.label if window is not None else ""))
        ttk.Entry(self.frame, textvariable=self.label_var, width=16).grid(row=0, column=col, padx=2)
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
        return BlackoutWindow(
            id=self.window_id,
            label=self.label_var.get() or self.window_id,
            start_hour=self._safe_int(self.start_hour_var.get(), 0, 23, 12),
            start_minute=self._safe_int(self.start_minute_var.get(), 0, 59, 0),
            end_hour=self._safe_int(self.end_hour_var.get(), 0, 23, 13),
            end_minute=self._safe_int(self.end_minute_var.get(), 0, 59, 0),
            enabled=self.enabled_var.get(),
        )


class BlackoutWindowListEditor(ttk.Frame):
    """「休止時間帯」タブの中身。開始>終了の場合は深夜またぎとして扱われる
    (scheduler.py側の判定)ため、ここでは入力値のバリデーションは行わない。"""

    def __init__(self, parent: ttk.Frame, windows: list[BlackoutWindow]):
        super().__init__(parent)

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
        row = BlackoutWindowRow(self._rows_frame, window)
        self.rows.append(row)

    def get_blackout_windows(self) -> list[BlackoutWindow]:
        return [r.to_blackout_window() for r in self.rows if not r.removed]


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

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        common_tab = ttk.Frame(notebook)
        notebook.add(common_tab, text="共通(両モード)")
        self._common_editor = ScheduleListEditor(common_tab, config, config.load_common_jobs())
        self._common_editor.pack(fill="both", expand=True)

        self._mode_editors: dict[str, ScheduleListEditor] = {}
        for mode in config.get_mode_names():
            tab = ttk.Frame(notebook)
            notebook.add(tab, text=mode)
            editor = ScheduleListEditor(tab, config, config.load_mode_jobs(mode))
            editor.pack(fill="both", expand=True)
            self._mode_editors[mode] = editor

        manual_tab = ttk.Frame(notebook)
        notebook.add(manual_tab, text="手動追加")
        self._manual_editor = ScheduleListEditor(manual_tab, config, config.load_manual_jobs())
        self._manual_editor.pack(fill="both", expand=True)

        shows_tab = ttk.Frame(notebook)
        notebook.add(shows_tab, text="ステージショー")
        self._shows_editor = StageShowListEditor(shows_tab, config, config.load_stage_shows())
        self._shows_editor.pack(fill="both", expand=True)

        blackout_tab = ttk.Frame(notebook)
        notebook.add(blackout_tab, text="休止時間帯")
        self._blackout_editor = BlackoutWindowListEditor(blackout_tab, config.load_blackout_windows())
        self._blackout_editor.pack(fill="both", expand=True)

        button_frame = ttk.Frame(self)
        button_frame.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(button_frame, text="保存", command=self._save).pack(side="right")
        ttk.Button(button_frame, text="キャンセル", command=self.destroy).pack(side="right", padx=5)

    def _save(self) -> None:
        try:
            common_jobs = self._common_editor.get_jobs()
            mode_jobs = {mode: editor.get_jobs() for mode, editor in self._mode_editors.items()}
            manual_jobs = self._manual_editor.get_jobs()
        except ValueError as e:
            messagebox.showerror("入力エラー", str(e))
            return

        self._config.save_common_jobs(common_jobs)
        for mode, jobs in mode_jobs.items():
            self._config.save_mode_jobs(mode, jobs)
        self._config.save_manual_jobs(manual_jobs)
        self._config.save_stage_shows(self._shows_editor.get_stage_shows())
        self._config.save_blackout_windows(self._blackout_editor.get_blackout_windows())

        self._on_saved()
        self.destroy()


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
        link = config.link

        form = ttk.Frame(self, padding=16)
        form.pack(fill="both", expand=True)

        row = 0
        ttk.Label(form, text="この端末の名前").grid(row=row, column=0, sticky="w", pady=4)
        self.device_name_var = tk.StringVar(value=config.device_name)
        ttk.Entry(form, textvariable=self.device_name_var, width=24).grid(row=row, column=1, sticky="w", pady=4)
        row += 1

        ttk.Separator(form, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)
        row += 1

        ttk.Label(form, text="連携(2台のSurface連携)", font=("", 13, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1

        self.enabled_var = tk.BooleanVar(value=link.enabled)
        ttk.Checkbutton(form, text="リンク機能を有効にする(再起動が必要)", variable=self.enabled_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=4
        )
        row += 1

        ttk.Label(form, text="相手端末のIPアドレス").grid(row=row, column=0, sticky="w", pady=4)
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
        ttk.Spinbox(form, from_=10, to=600, textvariable=self.poll_interval_var, width=8).grid(
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

    def _save(self) -> None:
        device_name = self.device_name_var.get().strip()
        link = LinkConfig(
            enabled=self.enabled_var.get(),
            peer_host=self.peer_host_var.get().strip(),
            peer_port=self._safe_int(self.peer_port_var.get(), 1, 65535, 8765),
            listen_port=self._safe_int(self.listen_port_var.get(), 1, 65535, 8765),
            duck_volume_percent=self._safe_int(self.duck_var.get(), 0, 100, 20),
            poll_interval_seconds=self._safe_int(self.poll_interval_var.get(), 10, 600, 60),
            http_timeout_seconds=self._config.link.http_timeout_seconds,
        )
        if link.enabled and not link.peer_host:
            messagebox.showerror("入力エラー", "リンク機能を有効にする場合は、相手端末のIPアドレスを入力してください。")
            return

        self._config.save_system_settings(device_name, link)
        self._on_saved()
        messagebox.showinfo(
            "保存しました",
            "設定を保存しました。相手端末のIP/ポート・ダッキング音量・監視間隔は次回の通信から反映されます。\n"
            "リンク機能の有効/無効・待受ポート番号の変更を反映するには、アプリを再起動してください。",
        )
        self.destroy()
