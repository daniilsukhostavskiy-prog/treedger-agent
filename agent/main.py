"""
agent/main.py — the program's window.

CROSS-THREAD DISCIPLINE — READ THIS BEFORE TOUCHING THIS FILE
--------------------------------------------------------------------------
Tk's `mainloop()` owns the main thread, and EVERY widget read or mutation must
happen on that same thread — Tcl/Tk itself is not thread-safe.
This is a classic, well-documented footgun, and it is
easy to "simplify away" by accident, so the rule is spelled out once, here, at the
top of the one file where it matters:

  - `AgentWindow._run_sync_worker` runs on exactly one background
    `threading.Thread`. It calls `agent.sync.run_sync`, and its `report` callback
    does exactly one thing: push a plain, Tk-free `agent.ui_state` event onto
    `self._queue`. It NEVER calls a widget method, NEVER reads `self.state`, and
    NEVER calls `root.after` itself. Every way the run can end — success, a handled
    error, or an unexpected exception — is one `try`/ordered-`except`/`finally`
    block, and the `finally` pushes EXACTLY ONE `_SyncFinishedSentinel`, so the
    window can never be left believing a run is still in flight. (The connect
    watchdog inside `agent/mt5_bridge.py` owns a short-lived helper thread of its
    own; it touches nothing in this file.) Stage updates (`sync.StageProgress`,
    including the live connect counter) travel the same way: the worker's
    `report_stage` callback only pushes a `ui_state.StageProgressEvent` onto the
    queue.
  - The ONE cross-thread object besides the queue is the per-run
    `threading.Event` behind «Отменить» (`self._cancel_event`): the main thread
    creates it in `_on_refresh_clicked` and is the only thread that ever SETS it
    (`_on_cancel_clicked`); the worker only passes it to `sync.run_sync`, which only
    READS it between steps. A `threading.Event` is itself thread-safe; nothing
    Tk-related crosses threads through it.
  - The Tk main thread drains that queue on a `root.after(100, self._poll_queue)`
    timer. Only `_poll_queue` (and the methods it calls: `_dispatch` → `_render`)
    ever folds an event through `ui_state.reduce()` and touches a widget.
  - `AgentWindow._on_periodic_tick` (the hourly sync timer) is ALSO a
    `root.after` callback, exactly like `_poll_queue` — it runs on the Tk main
    thread, never a background thread, and when it starts a sync it does so by
    calling through `_on_refresh_clicked`, the same main-thread entry point a
    manual click uses. It never touches a widget beyond what that call already
    does.
  - `AgentWindow._on_startup_sync` (the one-shot sync STARTUP_SYNC_DELAY_SECONDS
    after the window opens — quick 260925-qhs) is a `root.after` callback too, on
    the main thread, and it also starts a sync only through `_on_refresh_clicked`.
    It never reschedules itself.
  - The clipboard key handler and the entry right-click menu (`_on_entry_control_key`,
    `_show_entry_menu`, `_entry_menu_generate`) are Tk event/command callbacks on the
    main thread. They only generate Tk's own <<Paste>>/<<Copy>>/<<Cut>>/<<SelectAll>>
    virtual events on the entry — the clipboard text never enters Python and is never
    logged.
  - `agent/tray.py`'s `TrayIcon` (quick 260926-ieo) owns its OWN daemon thread — a
    SECOND producer onto `self._queue`, alongside the sync worker thread. Its
    `command_sink` (`AgentWindow.tray_command_sink`) runs ON THE TRAY THREAD and does
    exactly one thing, like the worker's `report` callback: push a plain `_TrayCommand`
    onto `self._queue`. It never touches a widget, never reads `self.state`, and never
    calls `root.after` itself — `_poll_queue` is what turns a drained `_TrayCommand`
    into `_handle_tray_command(...)` on the main thread.

Do not "simplify" this by having the worker thread call a widget method directly,
even for something that looks harmless (e.g. a one-line status update) — that is
the exact class of bug this file exists to avoid, and it will not always crash
loudly; it can just as easily corrupt Tk's internal state silently.

WHAT THIS WINDOW DELIBERATELY DOES NOT DO
--------------------------------------------------------------------------
Autostart registration exists, but it is opt-in only and never
self-registering: it is ONE per-user HKCU Run value (no Task Scheduler, no
administrator rights — quick 260925-qhs), `autostart.enable_autostart()` is
reachable from exactly one place, the checkbox's own handler, and the silent
startup self-check may only rewrite an already-existing value whose executable no
longer exists, never create one. The installer's own «Запускать вместе с Windows»
option writes the same value and is unchecked by default. No self-update check of
any kind still holds — if a version notice is ever shown (the protocol-too-old
notice below), it is plain text with a link the user follows themselves, never a
download-and-execute path.

Since quick 260926-ieo, this window ALSO has a tray icon (`agent/tray.py`): closing
the window with X hides it back to the tray (`_hide_to_tray`) rather than quitting —
only the tray menu's «Выход» (`_quit`) ends the program — with the one exception that
X quits exactly like before when no tray icon exists at all (the tray failed to
start, or this is an older/manual-launch path), so the program can never become
unreachable with neither a window nor a tray icon. The program notifies the user
through a tray BALLOON only on a transition into one of exactly two problems (a
login MT5 itself refused, or MetaTrader 5 not found) and only while the window is
hidden — never for a routine sync, and never for anything else. This window still
never raises itself above other windows on its own, except in direct response to the
person's own action (a tray click/menu choice, or starting the program a second
time) — the site still watches for a silent program via the agent's own
authenticated requests, not for this window to announce anything on its own
initiative. This window also STILL never kills the user's MT5 terminal process, never
restores a session behind their back, and never shows, hides, minimises or
reconfigures the terminal's own window — it only warns, and (since this quick task)
mutes/unmutes the terminal's own Windows PER-APPLICATION audio sessions through
`agent/audio_mute.py`, touching nothing else about the terminal.
"""
from __future__ import annotations

import logging
import queue
import sys
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

try:
    import tkinter as tk
except ImportError as exc:  # pragma: no cover - environment-specific; see README.md
    sys.stderr.write(
        "This program needs tkinter, the GUI toolkit that ships with the standard "
        "python.org Python installer. This Python installation does not have it — "
        "some minimal/embeddable Python distributions omit tkinter deliberately. "
        "Install Python from https://www.python.org/downloads/ (the default "
        "installer includes tkinter) and run this program again.\n"
    )
    raise SystemExit(1) from exc

from agent import (
    api_client,
    audio_mute,
    autostart,
    config_store,
    constants,
    diagnostics,
    errors,
    mt5_bridge,
    single_instance,
    sync,
    terminal_discovery,
    tray,
    ui_state,
)

logger = logging.getLogger(__name__)

_POLL_INTERVAL_MS = 100
_SYNC_INTERVAL_MS = constants.SYNC_INTERVAL_SECONDS * 1000
_STARTUP_SYNC_DELAY_MS = constants.STARTUP_SYNC_DELAY_SECONDS * 1000

# quick 260926-ieo — derived from agent/audio_mute.py's own cadence constants, never a
# repeated literal (OR-8).
_MUTE_FIRST_DELAY_MS = audio_mute.FIRST_APPLY_DELAY_SECONDS * 1000
_MUTE_INTERVAL_MS = audio_mute.REAPPLY_INTERVAL_SECONDS * 1000
_MUTE_FAST_INTERVAL_MS = audio_mute.REAPPLY_INTERVAL_DURING_SYNC_SECONDS * 1000

# Windows virtual-key codes → Tk's clipboard virtual events (quick 260925-qhs, brief
# E1). Tk binds <<Paste>> & co. to LATIN keysyms, so under the Russian layout Ctrl+V
# arrives as keysym "Cyrillic_em" and nothing pastes. `event.keycode` on Windows is the
# virtual-key code, the same under every layout: V=86, C=67, X=88, A=65.
_CLIPBOARD_VIRTUAL_EVENTS: "dict[int, str]" = {
    86: "<<Paste>>",
    67: "<<Copy>>",
    88: "<<Cut>>",
    65: "<<SelectAll>>",
}


def clipboard_virtual_event(keycode: int) -> Optional[str]:
    """The Tk virtual event a Ctrl+<key> with this Windows virtual-key code means, or
    None when it is not one of the four clipboard keys."""
    return _CLIPBOARD_VIRTUAL_EVENTS.get(keycode)
_DEFAULT_BASE_URL = "https://treedger.com"
_MT5_DOWNLOAD_URL = "https://www.metatrader5.com/en/download"

_COLOR_ERROR = "#a33333"
_COLOR_SUCCESS = "#2f7a2f"
_COLOR_NEUTRAL = "#1a1a1a"

_PHASE_LABELS: "dict[str, str]" = {
    ui_state.PHASE_LOGIN: "вход",
    ui_state.PHASE_READING: "чтение",
    ui_state.PHASE_SENDING: "отправка",
}


class AgentWindow:
    """Owns the Tk root, the cross-thread queue, and the current `ui_state.UiState`."""

    def __init__(self, root: "tk.Tk") -> None:
        self.root = root
        self.root.title("Treedger — синхронизация MT5")
        self.root.geometry("560x480")
        self.root.minsize(420, 320)

        # Cross-thread channel: the ONLY thing `_run_sync_worker` (background
        # thread) is allowed to touch. See module docstring.
        self._queue: "queue.Queue[object]" = queue.Queue()
        self._client: Optional[api_client.ApiClient] = None
        self._row_labels: "dict[str, tk.Label]" = {}
        self._sync_in_flight = False

        token = config_store.load_token()
        terminal_path = terminal_discovery.find_terminal_path()
        self.state = ui_state.initial_state(
            has_token=bool(token), terminal_found=terminal_path is not None
        )
        if token:
            base_url = config_store.load_base_url() or _DEFAULT_BASE_URL
            self._client = api_client.ApiClient(base_url=base_url, token=token)

        # Per-run cancel flag: created on the main thread for each run, SET only by the
        # main thread («Отменить»), and only ever READ by the worker. It is the one
        # cross-thread object besides the queue.
        self._cancel_event: Optional[threading.Event] = None

        # Tray + mute state (quick 260926-ieo). Deliberately NO tray creation, no
        # Core Audio call, and no `root.withdraw()`/`deiconify()` here (F5) — a bare
        # `AgentWindow.__new__` test window must be able to call every EXISTING method
        # this __init__ already supported without gaining a new required attribute
        # that method now reads. The tray itself is created and started in `main()`
        # and handed in afterwards via `attach_tray`.
        self._tray: "Optional[tray.TrayIcon]" = None
        self._window_visible = False
        self._sounds_muted = config_store.load_mt5_sounds_muted()
        self._mute_pending = config_store.load_mt5_mute_pending()
        # Lazy: does no COM work until its first apply()/restore() call.
        self._mute = audio_mute.MuteController()
        self._run_login_failures: "list[str]" = []
        self._last_attention: "frozenset[str]" = frozenset()
        self._last_tooltip: "Optional[str]" = None
        self._quitting = False

        # Silent, log-only self-check — see repair_autostart_path()'s own docstring
        # for why this never surfaces a dialog: the person made no mistake, so there
        # is nothing to tell them. Runs on every start, regardless of whether
        # autostart is enabled — it only rewrites an ALREADY-EXISTING Run value whose
        # executable no longer exists; it can never create one (see
        # _on_autostart_toggled, the one place enable_autostart() is ever called).
        # Runs BEFORE the widgets are built, so a dead path is repaired first and the
        # autostart checkbox then reflects the real, verified state.
        logger.info("autostart path self-check: %s", autostart.repair_autostart_path())

        self._build_widgets()
        self._render()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(_POLL_INTERVAL_MS, self._poll_queue)
        self._schedule_periodic_sync()
        # One sync shortly after the window opens, however it was launched (quick
        # 260925-qhs — implements Phase 40 D-21's premise; never tied to --minimized).
        self.root.after(_STARTUP_SYNC_DELAY_MS, self._on_startup_sync)
        # First MT5-sound reconcile shortly after the window opens (quick 260926-ieo,
        # OR-8) — independent of `--minimized` and of the tray, exactly like the
        # startup sync above; `_on_mute_tick` reschedules itself thereafter.
        self._schedule_mute_tick(_MUTE_FIRST_DELAY_MS)

    # -----------------------------------------------------------------
    # Widget construction — built once. `_render()` only ever mutates these
    # existing widgets (text, colour, pack/forget); it never rebuilds them.
    # -----------------------------------------------------------------
    def _build_widgets(self) -> None:
        # ONE shared right-click menu for every entry field; the entry it acts on is
        # stored by _show_entry_menu at the moment of the click.
        self._entry_menu_target: "Optional[tk.Widget]" = None
        self._entry_menu = tk.Menu(self.root, tearoff=0)
        self._entry_menu.add_command(
            label="Вырезать", command=lambda: self._entry_menu_generate("<<Cut>>")
        )
        self._entry_menu.add_command(
            label="Копировать", command=lambda: self._entry_menu_generate("<<Copy>>")
        )
        self._entry_menu.add_command(
            label="Вставить", command=lambda: self._entry_menu_generate("<<Paste>>")
        )
        self._entry_menu.add_separator()
        self._entry_menu.add_command(
            label="Выделить всё", command=lambda: self._entry_menu_generate("<<SelectAll>>")
        )

        self._notice_var = tk.StringVar(value="")
        self._notice_label = tk.Label(
            self.root,
            textvariable=self._notice_var,
            fg=_COLOR_ERROR,
            wraplength=520,
            justify="left",
            anchor="w",
        )
        self._notice_label.pack(fill="x", padx=12, pady=(10, 0))

        # Footer, packed at the bottom BEFORE any screen frame so it stays visible on
        # every screen (pairing, no-terminal, ready): the log folder is exactly what a
        # person needs when something went wrong on any of them.
        footer = tk.Frame(self.root)
        footer.pack(side="bottom", fill="x", padx=12, pady=(0, 8))
        tk.Button(
            footer,
            text="Открыть папку журнала",
            command=self._on_open_log_folder_clicked,
            font=("TkDefaultFont", 8),
        ).pack(side="right")

        self._build_pairing_screen()
        self._build_no_terminal_screen()
        self._build_ready_screen()

    def _build_pairing_screen(self) -> None:
        frame = tk.Frame(self.root)
        self._pairing_frame = frame

        tk.Label(frame, text="Адрес сервера").pack(anchor="w", padx=12, pady=(16, 0))
        self._base_url_var = tk.StringVar(value=config_store.load_base_url() or _DEFAULT_BASE_URL)
        self._base_url_entry = tk.Entry(frame, textvariable=self._base_url_var, width=44)
        self._base_url_entry.pack(anchor="w", padx=12)
        self._install_entry_clipboard_support(self._base_url_entry)

        tk.Label(frame, text="Код привязки (из Настройки → Аккаунт → Программа синхронизации)").pack(
            anchor="w", padx=12, pady=(12, 0)
        )
        self._code_var = tk.StringVar(value="")
        self._code_entry = tk.Entry(frame, textvariable=self._code_var, width=44)
        self._code_entry.pack(anchor="w", padx=12)
        self._install_entry_clipboard_support(self._code_entry)

        self._pairing_error_var = tk.StringVar(value="")
        tk.Label(
            frame, textvariable=self._pairing_error_var, fg=_COLOR_ERROR, wraplength=480, justify="left"
        ).pack(anchor="w", padx=12, pady=(6, 0))

        tk.Button(frame, text="Привязать", command=self._on_pair_clicked).pack(
            anchor="w", padx=12, pady=14
        )

    def _build_no_terminal_screen(self) -> None:
        frame = tk.Frame(self.root)
        self._no_terminal_frame = frame

        tk.Label(
            frame,
            text=ui_state.NOTICE_TEXT[ui_state.NOTICE_NO_TERMINAL],
            fg=_COLOR_ERROR,
            wraplength=500,
            justify="left",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(anchor="w", padx=12, pady=(24, 8))
        tk.Label(
            frame,
            text="Установите MetaTrader 5, затем запустите эту программу заново.",
            wraplength=500,
            justify="left",
        ).pack(anchor="w", padx=12)
        link = tk.Label(frame, text=_MT5_DOWNLOAD_URL, fg="#3355cc", cursor="hand2")
        link.pack(anchor="w", padx=12, pady=(2, 12))
        link.bind("<Button-1>", lambda _event: webbrowser.open(_MT5_DOWNLOAD_URL))

        # Deliberately DISABLED, never re-enabled from this screen — a
        # fresh launch after installing MT5 is the only way forward.
        tk.Button(frame, text="Синхронизировать", state="disabled").pack(anchor="w", padx=12)

    def _build_ready_screen(self) -> None:
        frame = tk.Frame(self.root)
        self._ready_frame = frame

        header = tk.Frame(frame)
        header.pack(fill="x", padx=12, pady=(16, 6))
        self._progress_var = tk.StringVar(value="")
        tk.Label(header, textvariable=self._progress_var).pack(side="left")
        self._refresh_button = tk.Button(header, text="Синхронизировать", command=self._on_refresh_clicked)
        self._refresh_button.pack(side="right")
        # «Отменить» stops the run AFTER the current step: a blocking MT5 call cannot
        # be interrupted (only the connect watchdog bounds it).
        self._cancel_button = tk.Button(header, text="Отменить", command=self._on_cancel_clicked)
        self._cancel_button.pack(side="right", padx=(0, 6))

        # The stage list — one label per STAGE_ORDER entry, built ONCE here. _render
        # only changes text/colour and packs or forgets each one by presence.
        self._stages_frame = tk.Frame(frame)
        self._stages_frame.pack(fill="x", padx=12)
        self._stage_labels: "dict[str, tk.Label]" = {
            name: tk.Label(self._stages_frame, anchor="w", justify="left", wraplength=520)
            for name in ui_state.STAGE_ORDER
        }

        # In memory only — never written to config.json (see config_store's own
        # closed allow-list, which holds only the token, base URL and the two
        # «Звуки MT5» booleans — this value is never one of them).
        self._last_success_var = tk.StringVar(value="")
        self._last_success_label = tk.Label(frame, textvariable=self._last_success_var, anchor="w")
        self._last_success_label.pack(anchor="w", padx=12, pady=(4, 0))

        self._rate_limit_var = tk.StringVar(value="")
        tk.Label(frame, textvariable=self._rate_limit_var, fg=_COLOR_ERROR, wraplength=520, justify="left").pack(
            anchor="w", padx=12
        )

        # The last whole-run failure (terminal unresponsive, launch refused, server
        # unreachable, …) — set from state.run_error, never from the worker thread.
        self._run_error_var = tk.StringVar(value="")
        tk.Label(frame, textvariable=self._run_error_var, fg=_COLOR_ERROR, wraplength=520, justify="left").pack(
            anchor="w", padx=12
        )

        self._rows_container = tk.Frame(frame)
        self._rows_container.pack(fill="both", expand=True, padx=12, pady=(6, 14))

        # Shows ONLY a verified state: ticked when the per-user TreedgerAgent Run value
        # names THIS executable and Task Manager has not switched it off
        # (autostart.autostart_enabled_for_this_exe), never merely because some value
        # of that name exists. The only place its own command
        # handler (`_on_autostart_toggled`) ever runs is a click on THIS checkbox. The
        # label means one thing only — whether the window is open or not — never
        # whether the timer runs. Copy is the exact RU source-of-truth string from
        # `src/lib/i18n/dictionaries/ru.ts`'s `download.step7.toggleLabel`/`.note`, not
        # retyped from memory.
        self._autostart_var = tk.BooleanVar(value=autostart.autostart_enabled_for_this_exe())
        tk.Checkbutton(
            frame,
            text="Запускать вместе с Windows",
            variable=self._autostart_var,
            command=self._on_autostart_toggled,
        ).pack(anchor="w", padx=12, pady=(4, 0))
        tk.Label(
            frame,
            text=(
                "Программа будет переключать счета в терминале — не включайте, "
                "если торгуете с этого компьютера."
            ),
            fg=_COLOR_ERROR,
            wraplength=520,
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 8))

    # -----------------------------------------------------------------
    # Entry clipboard support — MAIN THREAD ONLY (Tk event/command callbacks).
    # Layout-independent Ctrl+V/C/X/A plus a right-click menu (quick 260925-qhs,
    # brief E1). Nothing here reads the clipboard into Python or logs anything.
    # -----------------------------------------------------------------
    def _install_entry_clipboard_support(self, entry: "tk.Entry") -> None:
        """Bind the layout-independent clipboard keys and the right-click menu on ONE
        entry. Called for every tk.Entry this window builds (ast-checked)."""
        entry.bind("<Control-KeyPress>", self._on_entry_control_key)
        entry.bind("<Button-3>", self._show_entry_menu)

    def _on_entry_control_key(self, event: "tk.Event") -> Optional[str]:
        """
        Ctrl+<key> on an entry: when the Windows virtual-key code is V/C/X/A, generate
        Tk's own virtual event on that entry and return "break". This binding lives on
        the WIDGET bindtag, which Tk processes before the Entry CLASS bindtag, so
        "break" stops the class binding — a Latin-layout Ctrl+V is therefore handled
        here exactly once and never pastes a second time. Any other key: None, and Tk
        carries on as usual.
        """
        virtual = clipboard_virtual_event(getattr(event, "keycode", -1))
        if virtual is None:
            return None
        event.widget.event_generate(virtual)
        return "break"

    def _show_entry_menu(self, event: "tk.Event") -> None:
        """Right-click on an entry: remember it as the menu's target, focus it, and
        pop the shared menu up at the pointer. The grab is always released."""
        self._entry_menu_target = event.widget
        event.widget.focus_set()
        try:
            self._entry_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._entry_menu.grab_release()

    def _entry_menu_generate(self, virtual: str) -> None:
        """A menu command: the matching Tk virtual event on the right-clicked entry."""
        target = self._entry_menu_target
        if target is None:
            return
        target.event_generate(virtual)

    # -----------------------------------------------------------------
    # Rendering — MAIN THREAD ONLY. Called from __init__ and from `_dispatch`
    # (itself only ever called from Tk command callbacks or `_poll_queue`, a
    # `root.after` timer callback — all of which run on the Tk main thread).
    # -----------------------------------------------------------------
    def _render(self) -> None:
        notice_lines = [ui_state.NOTICE_TEXT[name] for name in sorted(self.state.notices)]
        self._notice_var.set("\n".join(notice_lines))

        for frame in (self._pairing_frame, self._no_terminal_frame, self._ready_frame):
            frame.pack_forget()

        if self.state.screen == ui_state.SCREEN_PAIRING:
            self._pairing_error_var.set(self.state.pairing_error or "")
            self._pairing_frame.pack(fill="both", expand=True)
        elif self.state.screen == ui_state.SCREEN_NO_TERMINAL:
            self._no_terminal_frame.pack(fill="both", expand=True)
        else:  # ready or running — same visual screen, distinguished only by state
            self._render_ready_screen()
            self._ready_frame.pack(fill="both", expand=True)

    def _render_ready_screen(self) -> None:
        self._render_stages()
        self._render_rows()

        total = self.state.total_accounts
        done = self.state.completed_count + self.state.failed_count
        running = self.state.screen == ui_state.SCREEN_RUNNING or self._sync_in_flight
        if self.state.cancel_requested:
            self._progress_var.set("Отмена после текущего шага…")
        elif total == 0:
            self._progress_var.set("Синхронизация…" if running else "Готово к запуску")
        else:
            pct = self.state.overall_progress_pct
            self._progress_var.set(f"Прогресс: {done}/{total} ({pct:.0f}%)")

        self._refresh_button.config(state="disabled" if self.state.refresh_disabled else "normal")
        self._cancel_button.config(
            state="normal" if (self.state.cancel_enabled and self._sync_in_flight) else "disabled"
        )

        if self.state.last_success_label:
            self._last_success_var.set(f"Последняя успешная синхронизация: {self.state.last_success_label}")
        else:
            self._last_success_var.set("")

        if self.state.rate_limit_retry_after_seconds is not None:
            self._rate_limit_var.set(
                f"Сервер временно ограничил запросы. Повтор через {self.state.rate_limit_retry_after_seconds} с."
            )
        else:
            self._rate_limit_var.set("")

        self._run_error_var.set(self.state.run_error or "")

    def _render_stages(self) -> None:
        for label in self._stage_labels.values():
            label.pack_forget()
        for stage_state in ui_state.ordered_stages(self.state):
            label = self._stage_labels[stage_state.stage]
            if stage_state.status == ui_state.STAGE_FAILED:
                color = _COLOR_ERROR
            elif stage_state.status == ui_state.STAGE_DONE:
                color = _COLOR_SUCCESS
            else:
                color = _COLOR_NEUTRAL
            label.config(text=ui_state.stage_line(stage_state), fg=color)
            label.pack(fill="x", pady=1)

    def _render_rows(self) -> None:
        # A new run clears state.rows; drop the previous run's labels with it.
        for key in [k for k in self._row_labels if k not in self.state.rows]:
            self._row_labels.pop(key).destroy()
        for key, row in self.state.rows.items():
            label = self._row_labels.get(key)
            if label is None:
                label = tk.Label(self._rows_container, anchor="w", justify="left")
                label.pack(fill="x", pady=2)
                self._row_labels[key] = label
            label.config(text=self._row_text(row), fg=self._row_color(row))

    @staticmethod
    def _row_text(row: "ui_state.AccountRowState") -> str:
        if row.phase == ui_state.PHASE_DONE:
            status = f"отправлено {row.trades_sent} сделок"
        elif row.phase == ui_state.PHASE_FAILED:
            status = f"ошибка: {row.error_reason or 'неизвестная причина'}"
        else:
            status = _PHASE_LABELS.get(row.phase, row.phase)
        return f"{row.mt_login} → {status}"

    @staticmethod
    def _row_color(row: "ui_state.AccountRowState") -> str:
        if row.phase == ui_state.PHASE_FAILED:
            return _COLOR_ERROR
        if row.phase == ui_state.PHASE_DONE:
            return _COLOR_SUCCESS
        return _COLOR_NEUTRAL

    # -----------------------------------------------------------------
    # User actions — Tk command callbacks, main thread.
    # -----------------------------------------------------------------
    def _on_pair_clicked(self) -> None:
        base_url = self._base_url_var.get().strip()
        code = self._code_var.get().strip()
        if not base_url or not code:
            self._dispatch(ui_state.PairingFailedEvent(reason="Укажите адрес сервера и код привязки"))
            return

        # Logs the server address only — never the pairing code, never the token.
        logger.info("pairing attempt: base_url=%s", base_url)
        pairing_client = api_client.ApiClient(base_url=base_url)
        try:
            token = pairing_client.redeem_pairing_code(code)
        except api_client.AgentApiError as exc:
            logger.warning("pairing failed: %s", exc.__class__.__name__)
            self._dispatch(ui_state.PairingFailedEvent(reason=str(exc)))
            return
        logger.info("pairing succeeded")

        config_store.save_base_url(base_url)
        config_store.save_token(token)
        self._client = api_client.ApiClient(base_url=base_url, token=token)
        self._dispatch(ui_state.PairingSucceededEvent())

    def _on_refresh_clicked(self) -> None:
        if self.state.refresh_disabled or self._sync_in_flight or self._client is None:
            return
        # Disabled immediately, on the main thread, so a rapid double-click can
        # never start two overlapping sync runs — this is a direct widget update
        # from a Tk command callback (main thread), not from the worker thread.
        self._sync_in_flight = True
        self._refresh_button.config(state="disabled")
        client = self._client
        # A fresh cancel flag per run — created and set on this (main) thread only;
        # the worker only ever reads it.
        cancel_event = threading.Event()
        self._cancel_event = cancel_event
        # A fresh run's own login-failure tally (quick 260926-ieo, F13) — reassigned by
        # value, on the main thread, before the worker exists, exactly like
        # `_cancel_event` above; the worker only ever APPENDS to it via
        # `_LoginFailedSignal` through the queue, never reads or clears it directly.
        self._run_login_failures = []
        # The run-start marker goes through the reducer BEFORE the worker exists, so
        # the stage list and «Отменить» appear the instant the button is pressed.
        self._dispatch(ui_state.RunStartedEvent())
        worker = threading.Thread(
            target=self._run_sync_worker, args=(client, cancel_event), daemon=True
        )
        worker.start()

    def _on_cancel_clicked(self) -> None:
        """«Отменить»: sets the per-run flag; the worker stops after the current step."""
        if not self._sync_in_flight or self._cancel_event is None:
            return
        logger.info("cancel requested by the user")
        self._cancel_event.set()
        self._dispatch(ui_state.CancelRequestedEvent())

    def _on_open_log_folder_clicked(self) -> None:
        diagnostics.open_log_folder()

    def _on_autostart_toggled(self) -> None:
        """
        Tk command callback for the autostart checkbox — the ONLY place in this
        entire program that ever calls `autostart.enable_autostart()` (ast-audited).
        The checkbox's own `tk.BooleanVar` already reflects the state the person just
        requested by clicking it, so this handler acts on that value: calls
        `enable_autostart()` exactly once when it reads True, `disable_autostart()`
        exactly once when it reads False. Never called from `__init__` — the silent
        startup self-check (`autostart.repair_autostart_path()`) can only rewrite an
        already-existing Run value, never create one; this handler is the sole
        create-capable path, matching `agent/autostart.py`'s own structural split.
        No administrator rights are involved: the value lives under HKCU.

        Afterwards the checkbox is set to the RE-READ, verified state — so a write
        that failed, or a value Task Manager has switched off, never leaves the box
        showing a state that is not real.
        """
        requested = bool(self._autostart_var.get())
        if requested:
            autostart.enable_autostart()
        else:
            autostart.disable_autostart()
        actual = autostart.autostart_enabled_for_this_exe()
        if actual != requested:
            logger.warning(
                "autostart toggle: requested=%s but the verified state is %s", requested, actual
            )
        self._autostart_var.set(actual)

    # -----------------------------------------------------------------
    # Background worker — runs on its OWN thread. Touches `self._queue` and
    # nothing else belonging to this window: no widget, no `self.state`, no
    # `root.after`. See module docstring.
    # -----------------------------------------------------------------
    def _run_sync_worker(
        self,
        client: "api_client.ApiClient",
        cancel_event: "Optional[threading.Event]" = None,
    ) -> None:
        def report_stage(progress: "sync.StageProgress") -> None:
            self._queue.put(
                ui_state.StageProgressEvent(
                    stage=progress.stage,
                    status=progress.status,
                    detail=progress.detail,
                    elapsed_seconds=progress.elapsed_seconds,
                )
            )

        def report(progress: "sync.AccountProgress") -> None:
            self._queue.put(
                ui_state.AccountProgressEvent(
                    mt_login=progress.mt_login,
                    phase=progress.phase,
                    trades_sent=progress.trades_sent,
                    error_reason=progress.error_reason,
                    account_id=progress.account_id,
                    pre_existing_session=progress.pre_existing_session,
                    algo_trading_allowed=getattr(progress, "algo_trading_allowed", None),
                )
            )
            # quick 260926-ieo, F13: a SECOND, separate queue item — this worker still
            # touches only the queue (module docstring). "Login failed" means only
            # `auth_failed` (MT5 itself refused the credentials); a transient
            # `server_unavailable`/`timeout` never counts and never pushes this signal.
            if progress.phase == sync.PHASE_FAILED and getattr(progress, "outcome", None) == (
                errors.OUTCOME_AUTH_FAILED
            ):
                self._queue.put(_LoginFailedSignal(progress.mt_login))

        logger.info("sync run started")
        try:
            summary = sync.run_sync(
                client, report, report_stage=report_stage, cancel_event=cancel_event
            )
            succeeded = int(getattr(summary, "succeeded", 0) or 0)
            failed = int(getattr(summary, "failed", 0) or 0)
            self._queue.put(
                ui_state.RunFinishedEvent(
                    succeeded=succeeded,
                    failed=failed,
                    finished_at_label=datetime.now().strftime("%d.%m.%Y %H:%M"),
                )
            )
            logger.info("sync run finished: succeeded=%d failed=%d", succeeded, failed)
        except sync.SyncCancelledError as exc:
            logger.info("sync run cancelled: %s", exc)
            self._queue.put(ui_state.RunCancelledEvent())
        except terminal_discovery.TerminalNotFoundError as exc:
            logger.warning("sync run: terminal not found: %s", exc)
            self._queue.put(ui_state.TerminalNotFoundEvent())
        except api_client.AgentUnauthorizedError as exc:
            logger.warning("sync run: unauthorized (%s) — token cleared", exc.__class__.__name__)
            # Filesystem side effect belongs on this thread, not in ui_state's
            # pure reducer — see UnauthorizedEvent's own docstring.
            config_store.clear_token()
            self._queue.put(ui_state.UnauthorizedEvent())
        except api_client.AgentRateLimitedError as exc:
            logger.warning("sync run: rate limited, retry_after=%s", exc.retry_after_seconds)
            self._queue.put(ui_state.RateLimitedEvent(retry_after_seconds=exc.retry_after_seconds))
            self._queue.put(ui_state.RunFinishedEvent())
        except api_client.AgentProtocolError as exc:
            # MUST precede the generic AgentApiError clause below: AgentProtocolError
            # is a SUBCLASS of AgentApiError (agent/api_client.py), and Python
            # matches the FIRST clause whose type the raised exception is an
            # instance of — swap this clause's position and the generic one below
            # silently swallows every protocol-too-old refusal instead. This branch
            # pushes a UI event and nothing else: no token clear, no retry, no
            # fetch, no open.
            logger.warning("sync run: protocol too old: %s", exc)
            self._queue.put(ui_state.ProtocolTooOldEvent())
        except mt5_bridge.TerminalUnresponsiveError as exc:
            logger.error(
                "sync run: terminal unresponsive: %s (waited=%s s, still_blocked=%s, "
                "elevation_mismatch=%s)",
                exc, exc.waited_seconds, exc.still_blocked_from_previous, exc.elevation_mismatch,
            )
            kind = (
                ui_state.RUN_ERROR_TERMINAL_STILL_BLOCKED
                if exc.still_blocked_from_previous
                else ui_state.RUN_ERROR_TERMINAL_UNRESPONSIVE
            )
            self._queue.put(
                ui_state.RunFailedEvent(
                    kind=kind,
                    elevation_mismatch=exc.elevation_mismatch,
                    waited_seconds=exc.waited_seconds,
                )
            )
        except sync.AlgoTradingSuspectedError as exc:
            # MUST precede SyncAbortedError: AlgoTradingSuspectedError is its subclass
            # (quick 260925-qhs) — swap the order and every «Алготрейдинг»-off connect
            # failure silently shows the generic «Не удалось подключиться…» line.
            logger.error("sync run: connect failed, algo-trading suspected (code=%s): %s", exc.error_code, exc)
            self._queue.put(ui_state.RunFailedEvent(kind=ui_state.RUN_ERROR_ALGO_TRADING))
        except sync.TerminalLaunchError as exc:
            # MUST precede SyncAbortedError: TerminalLaunchError is its subclass.
            logger.error("sync run: terminal launch failed: %s (cause: %r)", exc, exc.__cause__)
            self._queue.put(ui_state.RunFailedEvent(kind=ui_state.RUN_ERROR_LAUNCH_FAILED))
        except sync.SyncAbortedError as exc:
            logger.error("sync run: terminal failed to initialise: %s", exc)
            self._queue.put(ui_state.RunFailedEvent(kind=ui_state.RUN_ERROR_TERMINAL_FAILED))
        except api_client.AgentApiError as exc:
            # The guarantee this program makes is that ONE account failing never
            # stops the rest, not that a whole-run network failure is invisible —
            # so it is shown, and the next sync click retries the whole run.
            logger.error("sync run: server error %s: %s", exc.__class__.__name__, exc)
            self._queue.put(ui_state.RunFailedEvent(kind=ui_state.RUN_ERROR_SERVER))
        except Exception:  # noqa: BLE001 — the window must never be left in flight
            logger.exception("sync run: unexpected error")
            self._queue.put(ui_state.RunFailedEvent(kind=ui_state.RUN_ERROR_INTERNAL))
        finally:
            self._queue.put(_SyncFinishedSentinel())

    # -----------------------------------------------------------------
    # Queue draining — MAIN THREAD ONLY (a `root.after` timer callback).
    # -----------------------------------------------------------------
    def _dispatch(self, event: "ui_state.Event") -> None:
        self.state = ui_state.reduce(self.state, event)
        self._render()
        self._refresh_tray_status()

    def _poll_queue(self) -> None:
        try:
            while True:
                event = self._queue.get_nowait()
                if isinstance(event, _SyncFinishedSentinel):
                    self._sync_in_flight = False
                    self._evaluate_attention()
                    continue
                if isinstance(event, _TrayCommand):
                    self._handle_tray_command(event.command)
                    continue
                if isinstance(event, _LoginFailedSignal):
                    self._run_login_failures.append(event.mt_login or "?")
                    continue
                self._dispatch(event)
        except queue.Empty:
            pass
        self.root.after(_POLL_INTERVAL_MS, self._poll_queue)

    # -----------------------------------------------------------------
    # Periodic sync timer — MAIN THREAD ONLY (a `root.after` timer callback,
    # exactly like `_poll_queue` above — see the module docstring's cross-thread
    # discipline section).
    # -----------------------------------------------------------------
    def _schedule_periodic_sync(self) -> None:
        """
        Schedules the NEXT hourly tick via `root.after`, using
        `agent.constants.SYNC_INTERVAL_SECONDS` converted to milliseconds — never
        a literal. Called once from `__init__` for the very first tick, and
        again by `_on_periodic_tick` itself after every subsequent tick, so the
        timer keeps running for as long as this window stays open.

        The first tick fires a full interval AFTER launch, never AT launch (PKG40-15).
        The sync at launch is a separate one-shot, `_on_startup_sync`, scheduled
        `agent.constants.STARTUP_SYNC_DELAY_SECONDS` after the window opens (quick
        260925-qhs). Until then this docstring claimed "the window's own startup
        already triggers the first sync run" — that was false in code: nothing
        synced until this hourly tick, which broke the unattended logon chain.

        Runs regardless of whether autostart is enabled or how the program was
        launched — see `agent/constants.py`'s own docstring for why the timer is
        deliberately NOT tied to the autostart checkbox: tying them together would
        produce a state impossible to explain to a user (the program open,
        visible, working, and syncing nothing because a checkbox whose meaning is
        "start me on login" happens to be unticked).
        """
        self.root.after(_SYNC_INTERVAL_MS, self._on_periodic_tick)

    def _on_periodic_tick(self) -> None:
        """
        Fires once per `agent.constants.SYNC_INTERVAL_SECONDS`. Reuses the
        EXISTING in-flight guard (`self._sync_in_flight`) rather than any
        timer-local state: if a sync is already running — whether started by a
        manual «Синхронизировать» click or a previous tick — this tick performs no sync
        and starts no thread (no queue entry, no second `threading.Thread`); it
        only reschedules. When idle, it calls through `_on_refresh_clicked` rather
        than duplicating that method's thread-start logic, so there remains
        exactly ONE place in this whole file that ever starts a sync worker
        thread.

        Always reschedules itself via `_schedule_periodic_sync()`, whether or not
        THIS particular tick ran a sync — the timer must never silently stop just
        because one tick found a sync already in flight.
        """
        if not self._sync_in_flight:
            self._on_refresh_clicked()
        self._schedule_periodic_sync()

    def _on_startup_sync(self) -> None:
        """
        One-shot `root.after` callback (main thread), scheduled once from `__init__`
        `agent.constants.STARTUP_SYNC_DELAY_SECONDS` after the window opens — however
        the program was launched, minimized or not (the `--minimized` flag still
        changes only the initial window state). Starts a sync through
        `_on_refresh_clicked`, the one method that starts a worker thread, and only
        when no sync is already in flight; `_on_refresh_clicked`'s own guards (refresh
        disabled, no pairing yet) still apply. Never reschedules itself — the hourly
        timer is separate.
        """
        logger.info("startup sync")
        if not self._sync_in_flight:
            self._on_refresh_clicked()

    # -----------------------------------------------------------------
    # Tray + startup visibility (quick 260926-ieo). Everything below runs on the Tk
    # main thread — `tray_command_sink` is the one exception, and it only enqueues.
    # -----------------------------------------------------------------
    @property
    def sounds_muted(self) -> bool:
        """Read-only accessor for `main()` — never reads the private attribute
        directly from outside this class."""
        return self._sounds_muted

    def tray_command_sink(self, command: str) -> None:
        """
        The ONLY thing the tray thread is allowed to call on this window (see the
        module docstring's CROSS-THREAD DISCIPLINE section) — it does nothing but
        enqueue, exactly like the sync worker's own `report` callback.
        """
        self._queue.put(_TrayCommand(command))

    def attach_tray(self, icon: "Optional[tray.TrayIcon]") -> None:
        """Hands this window a STARTED tray icon (or `None`, when `tray.TrayIcon.start()`
        itself failed) — called once from `main()`, after `AgentWindow.__init__`."""
        self._tray = icon
        if icon is not None:
            icon.set_sounds_muted(self._sounds_muted)
            tooltip = ui_state.tray_tooltip_text(self.state)
            icon.set_tooltip(tooltip)
            self._last_tooltip = tooltip

    def show_initial(self, *, minimized: bool) -> None:
        """
        Decides the window's INITIAL visibility (OR-2/OR-3) — called once from
        `main()`, after `attach_tray`. Stays hidden in the tray ONLY when ALL THREE
        hold: `minimized` was requested, a tray icon is actually attached, and the
        screen is not the pairing screen (an unpaired program has nothing useful to do
        hidden — OR-3). Every other combination shows the window exactly as before this
        quick task existed.
        """
        if minimized and self._tray is not None and self.state.screen != ui_state.SCREEN_PAIRING:
            logger.info("started in the notification area (tray icon, no window)")
        else:
            self._show_window()
        self._evaluate_attention()

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self._window_visible = True

    def _hide_to_tray(self) -> None:
        self.root.withdraw()
        self._window_visible = False

    def _handle_tray_command(self, command: str) -> None:
        if command == tray.CMD_SYNC_NOW:
            self._on_refresh_clicked()
        elif command == tray.CMD_OPEN_WINDOW:
            self._show_window()
        elif command == tray.CMD_OPEN_SITE:
            self._open_site()
        elif command == tray.CMD_TOGGLE_SOUNDS:
            self._on_sound_toggled()
        elif command == tray.CMD_EXIT:
            self._quit()
        elif command == tray.CMD_TRAY_FAILED:
            # NIM_ADD never succeeded even after two minutes of retries (agent/tray.py's
            # own LOGON TIMING section) — the tray is treated as unavailable and the
            # window is shown so the program never becomes unreachable.
            self._tray = None
            self._show_window()
        else:
            logger.warning("tray: unknown command %r", command)

    def _open_site(self) -> None:
        """«Открыть Treedger» — only ever a stored `https://`/`http://` base URL, or
        the fixed default. Never a `file://` or otherwise unexpected scheme (T-ieo-06)."""
        url = config_store.load_base_url() or ""
        if not (url.startswith("https://") or url.startswith("http://")):
            url = _DEFAULT_BASE_URL
        webbrowser.open(url)

    def _on_sound_toggled(self) -> None:
        """The tray menu's «Звуки MT5» checkable item (OR-10/OR-11)."""
        self._sounds_muted = not self._sounds_muted
        try:
            config_store.save_mt5_sounds_muted(self._sounds_muted)
        except OSError:
            logger.warning("failed to persist mt5_sounds_muted", exc_info=True)
        if self._tray is not None:
            self._tray.set_sounds_muted(self._sounds_muted)
        self._reconcile_mt5_sound()

    def _set_mute_pending(self, value: bool) -> None:
        """Updates the in-memory flag FIRST (authoritative for this run — see
        `agent/audio_mute.py`'s "WHO OWNS A MUTE"), then best-effort persists it."""
        self._mute_pending = value
        try:
            config_store.save_mt5_mute_pending(value)
        except OSError:
            logger.warning("failed to persist mt5_mute_pending", exc_info=True)

    def _reconcile_mt5_sound(self) -> None:
        """One tick of the MT5-sound reconcile — called from `_on_mute_tick` and right
        after a toggle. Never raises (see `_on_mute_tick`'s own wrapper too)."""
        if self._sounds_muted:
            result = self._mute.apply(adopt_already_muted=self._mute_pending)
            if result.newly_muted > 0 and not self._mute_pending:
                self._set_mute_pending(True)
        elif self._mute_pending:
            result = self._mute.restore(adopt_already_muted=True)
            if result.terminal_sessions_seen > 0 and result.errors == 0:
                self._set_mute_pending(False)
        # else: sounds are on and nothing is pending — no controller call at all.

    def _schedule_mute_tick(self, delay_ms: int) -> None:
        self.root.after(delay_ms, self._on_mute_tick)

    def _on_mute_tick(self) -> None:
        try:
            self._reconcile_mt5_sound()
        except Exception:  # noqa: BLE001 — a tick must never crash the window
            logger.exception("mute tick failed")
        self._schedule_mute_tick(_MUTE_FAST_INTERVAL_MS if self._sync_in_flight else _MUTE_INTERVAL_MS)

    def _refresh_tray_status(self) -> None:
        if self._tray is None:
            return
        tooltip = ui_state.tray_tooltip_text(self.state)
        if tooltip != self._last_tooltip:
            self._tray.set_tooltip(tooltip)
            self._last_tooltip = tooltip

    def _evaluate_attention(self) -> None:
        """
        Balloons the tray ONLY on a fresh transition into one of the two named
        problems (OR-6, F14), and ONLY while the window is hidden — a visible window
        already shows the problem. Dedupe state (`_last_attention`) is updated
        regardless of visibility, so a problem that first appears while the window is
        open does not immediately balloon the moment the window is later hidden.
        """
        kinds = ui_state.attention_kinds(self.state, login_failed=bool(self._run_login_failures))
        new_kinds = kinds - self._last_attention
        self._last_attention = kinds
        if not new_kinds or self._tray is None or self._window_visible:
            return
        for kind in new_kinds:
            title, text = ui_state.attention_balloon(kind, mt_logins=tuple(self._run_login_failures))
            self._tray.show_balloon(title, text)

    # -----------------------------------------------------------------
    # Shutdown
    # -----------------------------------------------------------------
    def _on_close(self) -> None:
        """
        WM_DELETE_WINDOW (the X button). With a tray icon attached, this only HIDES the
        window (OR-4) — sync timers keep running, exactly as if the window had never
        been closed. The program quits ONLY through `_quit()` (the tray's «Выход»), with
        the one exception below.
        """
        if self._tray is not None:
            self._hide_to_tray()
            return
        # No tray icon exists (it failed to start, or this is an older/manual-launch
        # path) — X must still quit, so the program can never become unreachable with
        # neither a window nor a tray icon.
        self._quit()

    def _quit(self) -> None:
        """
        «Выход» (or X with no tray). Restores every session's own mute BEFORE tearing
        anything else down, so a person who quits with sounds muted always gets MT5's
        sounds back — each step is independently guarded so one failing step never
        prevents `root.destroy()` from running. Guarded against a second call (e.g. a
        double click) by `_quitting`.
        """
        if self._quitting:
            return
        self._quitting = True

        if self._sounds_muted or self._mute_pending:
            try:
                result = self._mute.restore(adopt_already_muted=not self._sounds_muted)
                if result.terminal_sessions_seen > 0 and result.errors == 0 and not self._mute.has_recorded():
                    self._set_mute_pending(False)
            except Exception:  # noqa: BLE001 — quitting must never get stuck here
                logger.exception("mute restore on quit failed")

        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:  # noqa: BLE001
                logger.exception("tray stop on quit failed")

        try:
            mt5_bridge.shutdown_terminal()
        finally:
            self.root.destroy()


class _SyncFinishedSentinel:
    """
    A plain marker (never a `ui_state.Event`) pushed by the worker thread once its
    run truly ends, so `_poll_queue` knows to re-enable the "start another run"
    guard (`_sync_in_flight`) independently of whatever screen `ui_state.reduce()`
    landed on. Kept out of `agent/ui_state.py` deliberately: it carries no
    state-model meaning at all, only an in-flight bookkeeping signal local to this
    Tk window.
    """


@dataclass(frozen=True)
class _TrayCommand:
    """
    A plain, queue-only bookkeeping marker (quick 260926-ieo) — never a
    `ui_state.Event`, exactly like `_SyncFinishedSentinel` above. Pushed by
    `AgentWindow.tray_command_sink`, which runs ON THE TRAY THREAD and does nothing
    else (see the module docstring's CROSS-THREAD DISCIPLINE section); `_poll_queue`
    (main thread) is what turns it into a call to `_handle_tray_command`.
    """

    command: str


@dataclass(frozen=True)
class _LoginFailedSignal:
    """
    A plain, queue-only bookkeeping marker (quick 260926-ieo) — never a
    `ui_state.Event`. Pushed by the sync worker thread's own `report` callback
    alongside its normal `AccountProgressEvent`, exactly once per account whose login
    MT5 itself refused (`outcome == errors.OUTCOME_AUTH_FAILED`, F13) — this is how
    `_evaluate_attention` learns "a login failed this run" without `ui_state.UiState`
    itself gaining a dedicated field for it.
    """

    mt_login: "Optional[str]"


@dataclass(frozen=True)
class WindowLaunchOptions:
    """
    The command line's ENTIRE contribution to how this window starts, and by design
    this dataclass must never carry more than that. `minimized` affects ONLY the
    window's initial visual state — since quick 260926-ieo, hidden in the notification
    area (no window, no taskbar button) vs. shown, where it previously meant iconified
    vs. normal — never "skip the terminal check in background", never "behave
    differently while minimized", never any second meaning.
    `agent/tests/test_main_args.py` asserts `dataclasses.fields(WindowLaunchOptions)`
    has length 1 for exactly this reason: a second field here is how that constraint
    erodes, one plausible-looking addition at a time, and the moment a background mode
    exists it will inevitably be tested worse than the foreground one — nobody watches
    the window that isn't shown.

    Deliberately carries NO string field, and never should. The token, the base
    URL, and every other parameter this program needs come from
    `agent/config_store.py`'s `config.json` alone. Passing any of them through
    argv would put a secret or a server address on the process command line,
    visible to any other process on the machine that can enumerate command
    lines — a strictly worse exposure than the file they already live in.
    """

    minimized: bool = False


def parse_argv(argv: "list[str]") -> "WindowLaunchOptions":
    """
    Parses this process's own command-line arguments (conventionally
    `sys.argv[1:]`) into the single boolean this program ever derives from them.
    Every argument other than the one recognised flag — an unknown flag, a bare
    word, a `key=value` pair, an empty string, a flag that merely starts with the
    same prefix — is ignored silently. This function never raises.

    Deliberately NOT `argparse`: argparse's default behaviour on an unrecognised
    argument is to print a usage message and call `sys.exit(2)`, which directly
    contradicts the requirement above that anything on the command line other than
    the one recognised flag is ignored, never fatal — whatever launched this
    program (a shortcut, the Windows autostart entry, a person's own typo) must never crash
    it. A plain membership test over the argument list is the correct tool for
    "recognise exactly one flag, ignore everything else, never raise". Do not
    "improve" this into argparse; that reintroduces the exact failure mode this
    function exists to avoid.
    """
    return WindowLaunchOptions(minimized=autostart.MINIMIZED_FLAG in argv)


def main() -> None:
    """
    Acquires the single-instance lock BEFORE any other work — before `parse_argv`,
    before `AgentWindow` is constructed, and therefore before
    `config_store.load_token()`, the token decrypt, and
    `terminal_discovery.find_terminal_path()` (both reached inside
    `AgentWindow.__init__`). A second process that gets far
    enough to touch either the config file or the MT5 terminal can corrupt another
    account's history silently, with no error raised anywhere in that sequence —
    so the lock must gate every one of those calls, not merely run alongside them.

    When the lock is not acquired, this function raises the first instance's
    window (best-effort — see `single_instance.raise_existing_window()`'s own
    docstring for why it may still return `False`) and returns WITHOUT
    constructing `AgentWindow` at all. Returning here — never `sys.exit(1)` —
    is deliberate: the person clicked a shortcut, and the correct outcome is
    that the window they already have comes forward, with the process exiting 0.
    Closing silently or exiting non-zero would both read as "it didn't work".
    """
    if not single_instance.acquire_single_instance_lock():
        single_instance.raise_existing_window()
        return

    # Logging right AFTER the lock (the lock must stay the very first call — the
    # folder's structural audit enforces it) and before anything else can fail.
    diagnostics.configure_logging()
    diagnostics.install_exception_hooks()
    diagnostics.log_startup_banner()

    options = parse_argv(sys.argv[1:])

    root = tk.Tk()
    # Withdrawn IMMEDIATELY after construction, before AgentWindow ever builds a
    # widget — never mapped before the program itself decides to show it (quick
    # 260926-ieo, OR-2). This is what makes an autostart launch produce no window and
    # no taskbar button at all, rather than the old iconified-window behaviour.
    root.withdraw()
    # Tk otherwise prints a callback's traceback to stderr, which a console-less
    # build does not have — route it into agent.log instead.
    root.report_callback_exception = diagnostics.log_tk_callback_exception
    window = AgentWindow(root)

    icon = tray.TrayIcon(
        window.tray_command_sink,
        tooltip=ui_state.tray_tooltip_text(window.state),
        sounds_muted=window.sounds_muted,
    )
    window.attach_tray(icon if icon.start() else None)
    # Replaces the old `if options.minimized: root.iconify()` — see
    # `AgentWindow.show_initial`'s own docstring for the exact visibility rule and
    # `WindowLaunchOptions`'s docstring for why this remains the flag's only effect.
    window.show_initial(minimized=options.minimized)
    root.mainloop()


if __name__ == "__main__":
    main()
