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
import tkinter as tk
import uuid
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from .config import AppConfig, ScheduledJob, StageShow
from .player import FullscreenVideoPlayer
from .scheduler import JobScheduler

_WEEKDAY_JA = "月火水木金土日"

MEDIA_FILETYPES = [
    ("メディアファイル", "*.mp3 *.wav *.mp4 *.mov *.m4a *.wmv *.avi"),
    ("すべてのファイル", "*.*"),
]

_SPI_GETWORKAREA = 0x0030


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
    ):
        self._root = root
        self._config = config
        self._logger = logger
        self._scheduler = scheduler
        self._on_reload_schedule = on_reload_schedule
        self._video_player = FullscreenVideoPlayer(root, vlc_instance, logger)
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._stage_buttons: dict[str, ttk.Button] = {}
        self._stage_mode_var = tk.BooleanVar(value=False)

        root.title("キッザニア館内音響システム")
        _maximize_window(root)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_widgets()
        self._attach_log_handler()
        self._poll_log_queue()
        self._update_clock()
        self._refresh_schedule_overview()

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        top = ttk.Frame(self._root, padding=10)
        top.pack(fill="x")

        mode_frame = ttk.Frame(top)
        mode_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(mode_frame, text="営業モード:", font=("", 10, "bold")).pack(side="left")
        self._mode_var = tk.StringVar(value=self._config.get_active_mode())
        self._mode_combo = ttk.Combobox(
            mode_frame,
            textvariable=self._mode_var,
            values=self._config.get_mode_names(),
            state="readonly",
            width=16,
        )
        self._mode_combo.pack(side="left", padx=5)
        self._mode_combo.bind("<<ComboboxSelected>>", self._on_mode_changed)

        self._clock_var = tk.StringVar()
        ttk.Label(mode_frame, textvariable=self._clock_var, font=("", 20, "bold")).pack(side="right")

        stage_mode_frame = ttk.Frame(top)
        stage_mode_frame.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(
            stage_mode_frame,
            text="ステージショーモード(拡張ディスプレイに待機黒画面を表示)",
            variable=self._stage_mode_var,
            command=self._on_stage_mode_toggle,
        ).pack(side="left")

        ttk.Label(top, text="ステージショー", font=("", 12, "bold")).pack(anchor="w")

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

        ttk.Label(overview_left, text="現在再生中", font=("", 9, "bold")).pack(anchor="w")
        self._active_frame = ttk.Frame(overview_left)
        self._active_frame.pack(fill="x")

        ttk.Label(overview_left, text="次の予定", font=("", 9, "bold")).pack(anchor="w", pady=(4, 0))
        self._upcoming_frame = ttk.Frame(overview_left)
        self._upcoming_frame.pack(fill="x")

        ttk.Button(overview_frame, text="スケジュール管理...", command=self._open_schedule_manager).pack(
            side="right", anchor="n"
        )

        log_frame = ttk.Frame(self._root, padding=(10, 0, 10, 10))
        log_frame.pack(fill="both", expand=True)
        ttk.Label(log_frame, text="動作ログ").pack(anchor="w")

        self._log_text = tk.Text(log_frame, height=18, state="disabled", wrap="word")
        self._log_text.pack(fill="both", expand=True, side="left")
        scrollbar = ttk.Scrollbar(log_frame, command=self._log_text.yview)
        scrollbar.pack(fill="y", side="right")
        self._log_text.configure(yscrollcommand=scrollbar.set)

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

        self._video_player.play(files, show.volume, show.label, _on_finished)
        self._next_button.state(["!disabled"] if len(show.files) > 1 else ["disabled"])

    def _on_stop_clicked(self) -> None:
        self._video_player.request_stop()
        if not self._video_player.has_standby_window():
            # 待機画面ごと閉じた場合はモード表示もOFFに揃える
            self._stage_mode_var.set(False)
            self._set_stage_buttons_enabled(False)
            self._next_button.state(["disabled"])

    def _on_next_clip_clicked(self) -> None:
        self._video_player.next_clip()

    # ------------------------------------------------------------------
    # スケジュール管理画面
    # ------------------------------------------------------------------
    def _open_schedule_manager(self) -> None:
        ScheduleManagerWindow(self._root, self._config, self._on_schedule_saved)

    def _on_schedule_saved(self) -> None:
        # モード一覧が変わっている可能性は無いが、選択肢と表示だけ最新化する
        self._mode_combo.configure(values=self._config.get_mode_names())
        self._mode_var.set(self._config.get_active_mode())
        self._render_stage_buttons(self._stage_button_frame)
        self._on_reload_schedule()

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
            freq_default = "毎日" if "hour" in default_cron else "毎時"
            self.freq_var = tk.StringVar(value=freq_default)
            freq_combo = ttk.Combobox(
                self.frame, textvariable=self.freq_var, values=["毎日", "毎時"],
                width=4, state="readonly",
            )
            freq_combo.grid(row=0, column=col, padx=2)
            col += 1

            self.hour_var = tk.StringVar(value=str(default_cron.get("hour", 9)))
            self.hour_spin = ttk.Spinbox(self.frame, from_=0, to=23, textvariable=self.hour_var, width=3)
            self.hour_spin.grid(row=0, column=col, padx=(2, 0))
            col += 1
            ttk.Label(self.frame, text="時").grid(row=0, column=col)
            col += 1

            self.minute_var = tk.StringVar(value=str(default_cron.get("minute", 0)))
            minute_spin = ttk.Spinbox(self.frame, from_=0, to=59, textvariable=self.minute_var, width=3)
            minute_spin.grid(row=0, column=col, padx=(2, 0))
            col += 1
            ttk.Label(self.frame, text="分").grid(row=0, column=col)
            col += 1

            def _update_freq_state(*_a) -> None:
                if self.freq_var.get() == "毎時":
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
            hour = self._safe_int(self.hour_var.get(), 0, 23, 9)
            minute = self._safe_int(self.minute_var.get(), 0, 59, 0)
            if self.freq_var.get() == "毎時":
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
        self._listbox = tk.Listbox(body, height=3, width=45)
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

        button_frame = ttk.Frame(self)
        button_frame.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(button_frame, text="保存", command=self._save).pack(side="right")
        ttk.Button(button_frame, text="キャンセル", command=self.destroy).pack(side="right", padx=5)

    def _save(self) -> None:
        self._config.save_common_jobs(self._common_editor.get_jobs())
        for mode, editor in self._mode_editors.items():
            self._config.save_mode_jobs(mode, editor.get_jobs())
        self._config.save_manual_jobs(self._manual_editor.get_jobs())
        self._config.save_stage_shows(self._shows_editor.get_stage_shows())

        self._on_saved()
        self.destroy()
