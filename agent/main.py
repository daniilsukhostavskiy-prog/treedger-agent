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
    NEVER calls `root.after` itself.
  - The Tk main thread drains that queue on a `root.after(100, self._poll_queue)`
    timer. Only `_poll_queue` (and the methods it calls: `_dispatch` → `_render`)
    ever folds an event through `ui_state.reduce()` and touches a widget.
  - `AgentWindow._on_periodic_tick` (the hourly sync timer) is ALSO a
    `root.after` callback, exactly like `_poll_queue` — it runs on the Tk main
    thread, never a background thread, and when it starts a sync it does so by
    calling through `_on_refresh_clicked`, the same main-thread entry point a
    manual click uses. It never touches a widget beyond what that call already
    does.

Do not "simplify" this by having the worker thread call a widget method directly,
even for something that looks harmless (e.g. a one-line status update) — that is
the exact class of bug this file exists to avoid, and it will not always crash
loudly; it can just as easily corrupt Tk's internal state silently.

WHAT THIS WINDOW DELIBERATELY DOES NOT DO
--------------------------------------------------------------------------
Autostart registration now exists, but it is opt-in only and never
self-registering: the scheduler's CREATE verb is reachable from exactly one
place, the checkbox's own handler, and the silent startup self-check may only
repair an already-existing task's path, never create one. No tray icon and no
self-update check of any kind still hold — if a version notice is ever shown
(the protocol-too-old notice below), it is plain text with a link the user
follows themselves, never a download-and-execute path. This window also never
kills the user's MT5 terminal process and never restores a session behind their
back — it only warns. This program never notifies, never
pops up, and never raises itself above other windows on its own — the site
watches for a silent program (via the agent's own authenticated requests), not
for this window to announce anything.
"""
from __future__ import annotations

import logging
import queue
import sys
import threading
import webbrowser
from dataclasses import dataclass
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
    autostart,
    config_store,
    constants,
    mt5_bridge,
    single_instance,
    sync,
    terminal_discovery,
    ui_state,
)

logger = logging.getLogger(__name__)

_POLL_INTERVAL_MS = 100
_SYNC_INTERVAL_MS = constants.SYNC_INTERVAL_SECONDS * 1000
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

        self._build_widgets()
        self._render()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(_POLL_INTERVAL_MS, self._poll_queue)
        self._schedule_periodic_sync()

        # Silent, log-only self-check — see repair_task_path()'s own
        # docstring for why this never surfaces a dialog: the person made no
        # mistake, so there is nothing to tell them. Runs on every start,
        # regardless of whether autostart is enabled — it only EDITS an
        # already-existing task; it can never create one (see
        # _on_autostart_toggled, the one place create_task() is ever called).
        logger.info("autostart path self-check: %s", autostart.repair_task_path())

    # -----------------------------------------------------------------
    # Widget construction — built once. `_render()` only ever mutates these
    # existing widgets (text, colour, pack/forget); it never rebuilds them.
    # -----------------------------------------------------------------
    def _build_widgets(self) -> None:
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

        self._build_pairing_screen()
        self._build_no_terminal_screen()
        self._build_ready_screen()

    def _build_pairing_screen(self) -> None:
        frame = tk.Frame(self.root)
        self._pairing_frame = frame

        tk.Label(frame, text="Адрес сервера").pack(anchor="w", padx=12, pady=(16, 0))
        self._base_url_var = tk.StringVar(value=config_store.load_base_url() or _DEFAULT_BASE_URL)
        tk.Entry(frame, textvariable=self._base_url_var, width=44).pack(anchor="w", padx=12)

        tk.Label(frame, text="Код привязки (из Настройки → Аккаунт → Программа синхронизации)").pack(
            anchor="w", padx=12, pady=(12, 0)
        )
        self._code_var = tk.StringVar(value="")
        tk.Entry(frame, textvariable=self._code_var, width=44).pack(anchor="w", padx=12)

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
        tk.Button(frame, text="Обновить", state="disabled").pack(anchor="w", padx=12)

    def _build_ready_screen(self) -> None:
        frame = tk.Frame(self.root)
        self._ready_frame = frame

        header = tk.Frame(frame)
        header.pack(fill="x", padx=12, pady=(16, 6))
        self._progress_var = tk.StringVar(value="")
        tk.Label(header, textvariable=self._progress_var).pack(side="left")
        self._refresh_button = tk.Button(header, text="Обновить", command=self._on_refresh_clicked)
        self._refresh_button.pack(side="right")

        self._rate_limit_var = tk.StringVar(value="")
        tk.Label(frame, textvariable=self._rate_limit_var, fg=_COLOR_ERROR, wraplength=520, justify="left").pack(
            anchor="w", padx=12
        )

        self._rows_container = tk.Frame(frame)
        self._rows_container.pack(fill="both", expand=True, padx=12, pady=(6, 14))

        # Reflects `task_exists()` at build time; the only place its own
        # command handler (`_on_autostart_toggled`) ever runs is a click on THIS
        # checkbox. The label means one thing only — whether the window is open
        # or not — never whether the timer runs. Copy is the exact RU
        # source-of-truth string from `src/lib/i18n/dictionaries/ru.ts`'s
        # `download.step7.toggleLabel`/`.note`, not retyped from
        # memory.
        self._autostart_var = tk.BooleanVar(value=autostart.task_exists())
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
    # Rendering — MAIN THREAD ONLY. Called from __init__ and from `_dispatch`
    # (itself only ever called from `_on_pair_clicked`/`_on_refresh_clicked`, Tk
    # command callbacks, or `_poll_queue`, a `root.after` timer callback — all
    # three run on the Tk main thread).
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
        self._render_rows()

        total = self.state.total_accounts
        done = self.state.completed_count + self.state.failed_count
        if total == 0:
            self._progress_var.set("Готово к запуску" if not self._sync_in_flight else "Проверка терминала…")
        else:
            pct = self.state.overall_progress_pct
            self._progress_var.set(f"Прогресс: {done}/{total} ({pct:.0f}%)")

        self._refresh_button.config(state="disabled" if self.state.refresh_disabled else "normal")

        if self.state.rate_limit_retry_after_seconds is not None:
            self._rate_limit_var.set(
                f"Сервер временно ограничил запросы. Повтор через {self.state.rate_limit_retry_after_seconds} с."
            )
        else:
            self._rate_limit_var.set("")

    def _render_rows(self) -> None:
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

        pairing_client = api_client.ApiClient(base_url=base_url)
        try:
            token = pairing_client.redeem_pairing_code(code)
        except api_client.AgentApiError as exc:
            self._dispatch(ui_state.PairingFailedEvent(reason=str(exc)))
            return

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
        worker = threading.Thread(target=self._run_sync_worker, args=(client,), daemon=True)
        worker.start()

    def _on_autostart_toggled(self) -> None:
        """
        Tk command callback for the autostart checkbox — the ONLY place in this
        entire program that ever calls `autostart.create_task()`. The
        checkbox's own `tk.BooleanVar` already reflects the state the person just
        requested by clicking it, so this handler simply acts on that value: calls
        `create_task()` exactly once when it reads True, `remove_task()` exactly
        once when it reads False. Never called from `__init__` — the silent
        startup self-check (`autostart.repair_task_path()`) can only edit an
        already-existing task, never create one; this handler is the sole
        create-capable path, matching `agent/autostart.py`'s own structural split.
        """
        if self._autostart_var.get():
            autostart.create_task()
        else:
            autostart.remove_task()

    # -----------------------------------------------------------------
    # Background worker — runs on its OWN thread. Touches `self._queue` and
    # nothing else belonging to this window: no widget, no `self.state`, no
    # `root.after`. See module docstring.
    # -----------------------------------------------------------------
    def _run_sync_worker(self, client: "api_client.ApiClient") -> None:
        def report(progress: "sync.AccountProgress") -> None:
            self._queue.put(
                ui_state.AccountProgressEvent(
                    mt_login=progress.mt_login,
                    phase=progress.phase,
                    trades_sent=progress.trades_sent,
                    error_reason=progress.error_reason,
                    account_id=progress.account_id,
                    pre_existing_session=progress.pre_existing_session,
                )
            )

        try:
            sync.run_sync(client, report)
        except terminal_discovery.TerminalNotFoundError:
            self._queue.put(ui_state.TerminalNotFoundEvent())
            self._queue.put(_SyncFinishedSentinel())
            return
        except api_client.AgentUnauthorizedError:
            # Filesystem side effect belongs on this thread, not in ui_state's
            # pure reducer — see UnauthorizedEvent's own docstring.
            config_store.clear_token()
            self._queue.put(ui_state.UnauthorizedEvent())
            self._queue.put(_SyncFinishedSentinel())
            return
        except api_client.AgentRateLimitedError as exc:
            self._queue.put(ui_state.RateLimitedEvent(retry_after_seconds=exc.retry_after_seconds))
        except api_client.AgentProtocolError:
            # MUST precede the generic (sync.SyncAbortedError, AgentApiError) clause
            # below: AgentProtocolError is a SUBCLASS of AgentApiError
            # (agent/api_client.py), and Python matches the FIRST clause whose type
            # the raised exception is an instance of — swap this clause's position
            # and the generic one below silently swallows every protocol-too-old
            # refusal instead. This branch pushes a UI event and
            # nothing else: no token clear, no retry, no fetch, no open.
            self._queue.put(ui_state.ProtocolTooOldEvent())
            self._queue.put(_SyncFinishedSentinel())
            return
        except (sync.SyncAbortedError, api_client.AgentApiError):
            # Already surfaced per-account where possible; the run simply ends —
            # the guarantee this program makes is that ONE account failing never
            # stops the rest, not that a whole-run network failure is invisible.
            # The next
            # click of «Обновить» retries the whole run.
            pass

        self._queue.put(ui_state.RunFinishedEvent())
        self._queue.put(_SyncFinishedSentinel())

    # -----------------------------------------------------------------
    # Queue draining — MAIN THREAD ONLY (a `root.after` timer callback).
    # -----------------------------------------------------------------
    def _dispatch(self, event: "ui_state.Event") -> None:
        self.state = ui_state.reduce(self.state, event)
        self._render()

    def _poll_queue(self) -> None:
        try:
            while True:
                event = self._queue.get_nowait()
                if isinstance(event, _SyncFinishedSentinel):
                    self._sync_in_flight = False
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

        The first tick fires a full interval AFTER launch, never AT launch: the
        window's own startup already triggers the first sync run (the person's own
        first «Обновить» click, or the terminal-check phase already visible when
        the window opens), and scheduling a second one immediately would make two
        overlapping runs the very first thing this program does on every single
        launch.

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
        manual «Обновить» click or a previous tick — this tick performs no sync
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

    # -----------------------------------------------------------------
    # Shutdown
    # -----------------------------------------------------------------
    def _on_close(self) -> None:
        """
        Shuts the terminal CONNECTION down — never the terminal process itself —
        so a run in flight does not leave the terminal attached after this window
        closes. This never restores a previous session — this program never does
        that, on any exit path.
        """
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
class WindowLaunchOptions:
    """
    The command line's ENTIRE contribution to how this window starts, and by design
    this dataclass must never carry more than that. `minimized` affects ONLY the
    window's initial visual state (iconified vs. normal) — never "skip the
    terminal check in background", never "behave differently while minimized",
    never any second meaning. `agent/tests/test_main_args.py` asserts
    `dataclasses.fields(WindowLaunchOptions)` has length 1 for exactly this
    reason: a second field here is how that constraint erodes, one
    plausible-looking addition at a time, and the moment a background mode exists
    it will inevitably be tested worse than the foreground one — nobody watches
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
    program (a shortcut, Task Scheduler, a person's own typo) must never crash
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

    options = parse_argv(sys.argv[1:])

    root = tk.Tk()
    AgentWindow(root)
    # Applied here, and nowhere else — see WindowLaunchOptions's own docstring for
    # why this is the flag's only effect.
    if options.minimized:
        root.iconify()
    root.mainloop()


if __name__ == "__main__":
    main()
