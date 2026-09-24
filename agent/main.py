"""
agent/main.py — the program's window.

CROSS-THREAD DISCIPLINE — READ THIS BEFORE TOUCHING THIS FILE
--------------------------------------------------------------------------
Tk's `mainloop()` owns the main thread, and EVERY widget read or mutation must
happen on that same thread — Tcl/Tk itself is not thread-safe
(39-RESEARCH.md Pitfall 4). This is a classic, well-documented footgun, and it is
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

Do not "simplify" this by having the worker thread call a widget method directly,
even for something that looks harmless (e.g. a one-line status update) — that is
the exact class of bug this file exists to avoid, and it will not always crash
loudly; it can just as easily corrupt Tk's internal state silently.

WHAT THIS WINDOW DELIBERATELY DOES NOT DO (39-CONTEXT.md D-24)
--------------------------------------------------------------------------
No autostart registration, no tray icon, no packaged executable, no installer, no
code signing, and no self-update check of any kind — if a version notice is ever
shown, it is plain text with a link the user follows themselves, never a
download-and-execute path. This window also never kills the user's MT5 terminal
process and never restores a session behind their back (D-23) — it only warns.
"""
from __future__ import annotations

import queue
import sys
import threading
import webbrowser
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

from agent import api_client, config_store, mt5_bridge, sync, terminal_discovery, ui_state

_POLL_INTERVAL_MS = 100
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

        # Deliberately DISABLED, never re-enabled from this screen (D-09 §12.2) — a
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
        else:  # ready or running — same visual screen, see 39-16-PLAN.md Task 2
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
        except (sync.SyncAbortedError, api_client.AgentApiError):
            # Already surfaced per-account where possible; the run simply ends —
            # readiness criterion 8 is about ONE ACCOUNT never stopping the rest,
            # not about a whole-run network failure being invisible. The next
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
    # Shutdown
    # -----------------------------------------------------------------
    def _on_close(self) -> None:
        """
        Shuts the terminal CONNECTION down — never the terminal process itself —
        so a run in flight does not leave the terminal attached after this window
        closes. This never restores a previous session; D-23 already established
        that this program never does that.
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


def main() -> None:
    root = tk.Tk()
    AgentWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
