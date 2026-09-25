"""
agent/ui_state.py — the window's entire behaviour, as a pure, Tk-free reducer.

WHY THIS FILE IMPORTS NO GUI TOOLKIT AT ALL
--------------------------------------------------------------------------
`agent/main.py` (the only place tkinter is ever imported in this package) is deliberately
thin: it builds widgets, drains a queue on a `root.after` timer,
and renders whatever `UiState` this module hands it. Every actual DECISION the window
makes — which screen is showing, what each account row says, which notice is pinned —
lives here instead, behind one pure function: `reduce(state, event) -> UiState`. That
split is what makes the window's entire logic testable on a machine with no display at
all (this repo's own CI has none): `agent/tests/test_ui_state.py` is the real test
coverage for this program's GUI; the Tk layer on top of it stays thin enough not to need
its own suite.

No `import tkinter` (or any other GUI toolkit) appears anywhere below — enforced
mechanically by this folder's own AST-based structural audit, not merely
promised in this docstring.

Why the phase constants below are a SEPARATE copy of `agent/sync.py`'s `PHASE_*`
constants, not an import
--------------------------------------------------------------------------
`agent/sync.py` imports `agent/mt5_bridge.py`, which attempts an `import MetaTrader5`
(gracefully degrading when the package is absent — see `agent/tests/conftest.py`'s own
docstring). Importing `agent.sync` from here would work today, but it would make this
module's importability depend, transitively, on a chain that has nothing to do with pure
state reduction. `agent/errors.py` already sets the project's precedent for this
exact trade-off — it mirrors `src/lib/api/agent/contract.ts`'s outcome strings as its own
literals rather than importing across the Python/TypeScript boundary — and this module
follows the same rule at the Python/Python boundary: duplicate the handful of string
literals that must match, and say so here in one place, rather than reach for an
import that would compromise the whole point of this file being pure.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional, Union

# ---------------------------------------------------------------------------
# Screens — exactly the four this window can ever show.
# ---------------------------------------------------------------------------
SCREEN_PAIRING = "pairing"
SCREEN_READY = "ready"
SCREEN_RUNNING = "running"
SCREEN_NO_TERMINAL = "no_terminal"

Screen = str  # one of the four SCREEN_* constants above

# ---------------------------------------------------------------------------
# Per-account phases — mirror `agent/sync.py`'s own `PHASE_*` constants verbatim (see
# module docstring for why this is a copy, not an import). `PHASE_TERMINAL_CHECK` is
# the one `AccountProgressEvent` this reducer treats specially: it is this program's
# run-start marker, not a row update.
# ---------------------------------------------------------------------------
PHASE_TERMINAL_CHECK = "terminal_check"
PHASE_LOGIN = "login"
PHASE_READING = "reading"
PHASE_SENDING = "sending"
PHASE_DONE = "done"
PHASE_FAILED = "failed"

# ---------------------------------------------------------------------------
# Run stages — mirror `agent/sync.py`'s own `STAGE_*` strings verbatim, for the same
# mirror-don't-import reason the phase constants above are a copy (see the module
# docstring). `STAGE_CANCELLED` exists only here: sync.py never reports it; this
# reducer turns an in-progress stage into it when a run is cancelled.
# ---------------------------------------------------------------------------
STAGE_FIND_TERMINAL = "find_terminal"
STAGE_LAUNCH_TERMINAL = "launch_terminal"
STAGE_CONNECT = "connect"
STAGE_FETCH_ACCOUNTS = "fetch_accounts"

STAGE_IN_PROGRESS = "in_progress"
STAGE_DONE = "done"
STAGE_FAILED = "failed"
STAGE_CANCELLED = "cancelled"

STAGE_ORDER: "tuple[str, ...]" = (
    STAGE_FIND_TERMINAL,
    STAGE_LAUNCH_TERMINAL,
    STAGE_CONNECT,
    STAGE_FETCH_ACCOUNTS,
)

STAGE_LABELS: "dict[str, str]" = {
    STAGE_FIND_TERMINAL: "Поиск терминала",
    STAGE_LAUNCH_TERMINAL: "Запуск терминала",
    STAGE_CONNECT: "Подключение к терминалу",
    STAGE_FETCH_ACCOUNTS: "Получение списка счетов",
}

_STAGE_ICONS: "dict[str, str]" = {
    STAGE_IN_PROGRESS: "…",
    STAGE_DONE: "✓",
    STAGE_FAILED: "✗",
    STAGE_CANCELLED: "–",
}


@dataclass(frozen=True)
class StageState:
    """One line of the stage list: which stage, its status, and its detail (found
    path, «запущен свёрнутым», account count) or live connect seconds."""

    stage: str
    status: str
    detail: Optional[str] = None
    elapsed_seconds: Optional[int] = None


def stage_line(stage_state: StageState) -> str:
    """The exact text of one stage-list line, e.g. "✓ Поиск терминала — C:\\...\\terminal64.exe",
    "… Подключение к терминалу — 12 с", "✓ Получение списка счетов — найдено: 2"."""
    icon = _STAGE_ICONS.get(stage_state.status, "?")
    label = STAGE_LABELS.get(stage_state.stage, stage_state.stage)
    if stage_state.status == STAGE_CANCELLED:
        return f"{icon} {label} — отменено"
    if stage_state.stage == STAGE_FETCH_ACCOUNTS and stage_state.detail is not None:
        suffix: Optional[str] = f"найдено: {stage_state.detail}"
    elif stage_state.stage == STAGE_CONNECT and stage_state.elapsed_seconds is not None:
        suffix = f"{stage_state.elapsed_seconds} с"
    else:
        suffix = stage_state.detail
    return f"{icon} {label} — {suffix}" if suffix else f"{icon} {label}"

# ---------------------------------------------------------------------------
# Pinned notices — a small, closed set. Once a notice is pinned it is NEVER removed
# by a later, unrelated event — a pinned notice persists for the remainder of the
# run, by design — `UiState.notices` only
# ever grows across a reduce() call, never shrinks.
# ---------------------------------------------------------------------------
NOTICE_TERMINAL_SWITCHED = "terminal_switched"
NOTICE_TOKEN_REVOKED = "token_revoked"
NOTICE_NO_TERMINAL = "no_terminal"
NOTICE_PROTOCOL_TOO_OLD = "protocol_too_old"

# The product's own step-by-step download page.
# Transcribed by hand, matching `agent/main.py`'s own `_DEFAULT_BASE_URL`
# literal — this constant is deliberately NOT derived from `config_store`'s stored
# `base_url`, because the download page must resolve even for a user whose stored
# base URL is stale or missing.
DOWNLOAD_URL = "https://treedger.com/download"

NOTICE_TEXT: "dict[str, str]" = {
    # No login restore, no separate terminal
    # instance, because neither exists. This is a warning, not a fix-it button.
    NOTICE_TERMINAL_SWITCHED: (
        "Программа переключала счета в вашем терминале MT5. "
        "Если вы в нём работали — войдите в свой счёт заново."
    ),
    NOTICE_TOKEN_REVOKED: "Токен был отозван. Пройдите привязку заново.",
    NOTICE_NO_TERMINAL: "MetaTrader 5 не найден на этом компьютере.",
    # A heading, one line of reason, and the /download URL as literal text a
    # person can read and type — DELIBERATELY NO HTTP STATUS NUMBER AND NO ERROR
    # CODE anywhere in this string. A person reading the window is told what to do,
    # not what the protocol said.
    NOTICE_PROTOCOL_TOO_OLD: (
        f"Программа устарела. Она больше не может синхронизировать данные с сервером. "
        f"Скачайте новую версию: {DOWNLOAD_URL}"
    ),
}

# ---------------------------------------------------------------------------
# Whole-run failures — one short Russian line on the ready screen. Deliberately
# none of these names a button: the button labels are the window's business
# (agent/main.py) and change independently of this text.
# ---------------------------------------------------------------------------
RUN_ERROR_TERMINAL_UNRESPONSIVE = "terminal_unresponsive"
RUN_ERROR_TERMINAL_STILL_BLOCKED = "terminal_still_blocked"
RUN_ERROR_TERMINAL_FAILED = "terminal_failed"
RUN_ERROR_LAUNCH_FAILED = "launch_failed"
RUN_ERROR_SERVER = "server"
RUN_ERROR_INTERNAL = "internal"

_ELEVATION_HINT = (
    "Похоже, MetaTrader 5 запущен от имени администратора, а эта программа — нет, "
    "поэтому подключиться к нему нельзя. Закройте MetaTrader 5 и запустите его "
    "обычным способом."
)


def run_error_text(
    kind: str, *, elevation_mismatch: bool = False, waited_seconds: Optional[int] = None
) -> str:
    """The exact line the ready screen shows for a whole-run failure `kind`."""
    if kind == RUN_ERROR_TERMINAL_UNRESPONSIVE:
        text = (
            f"Терминал MetaTrader 5 не отвечает ({waited_seconds or 0} с). Проверьте, не "
            "открыто ли за окнами системное окно Windows (например, предупреждение о "
            "запуске программы) — закройте его и повторите синхронизацию. Подробности — "
            "в журнале программы."
        )
        if elevation_mismatch:
            text = f"{text}\n{_ELEVATION_HINT}"
        return text
    if kind == RUN_ERROR_TERMINAL_STILL_BLOCKED:
        return (
            "Предыдущая попытка подключения к терминалу ещё не завершилась. Закройте "
            "эту программу и запустите её заново."
        )
    if kind == RUN_ERROR_TERMINAL_FAILED:
        return "Не удалось подключиться к терминалу MetaTrader 5. Подробности — в журнале программы."
    if kind == RUN_ERROR_LAUNCH_FAILED:
        return "Не удалось запустить MetaTrader 5. Подробности — в журнале программы."
    if kind == RUN_ERROR_SERVER:
        return "Не удалось связаться с сервером Treedger. Повторите синхронизацию позже."
    return "Непредвиденная ошибка. Подробности — в журнале программы."


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AccountRowState:
    """One account's row: the account label, its phase, trades sent so far (once
    known), or an error reason (once failed). `account_id` is carried through purely
    so a future screen could deep-link on it; nothing in this reducer keys off it
    except as a row-identity fallback (see `_row_key` below)."""

    mt_login: str
    phase: str = PHASE_LOGIN
    trades_sent: int = 0
    error_reason: Optional[str] = None
    account_id: Optional[str] = None


@dataclass(frozen=True)
class UiState:
    """
    The whole window's model. Always produced by `initial_state()` (once, at
    startup) or by `reduce()` (on every event thereafter) — never hand-built by
    `agent/main.py` except in those two places.

    `rows` is keyed by the account's own `mt_login` string (see `_row_key`), and
    a plain `dict` naturally preserves insertion order in this Python version, so
    `agent/main.py` can render rows in the order accounts were first seen without
    a second ordering field.
    """

    screen: Screen
    rows: "dict[str, AccountRowState]" = field(default_factory=dict)
    notices: "frozenset[str]" = frozenset()
    pairing_error: Optional[str] = None
    rate_limit_retry_after_seconds: Optional[int] = None
    refresh_disabled: bool = False
    # The last whole-run failure, as the Russian text the ready screen shows under
    # the rate-limit line (see run_error_text). Cleared when the next run starts.
    run_error: Optional[str] = None
    # The current (or last) run's stage list, keyed by STAGE_* — see ordered_stages().
    stages: "dict[str, StageState]" = field(default_factory=dict)
    # «Отменить» was pressed; the worker stops after the current step.
    cancel_requested: bool = False
    # «Последняя успешная синхронизация: …» — in memory only, never persisted.
    last_success_label: Optional[str] = None

    @property
    def cancel_enabled(self) -> bool:
        """«Отменить» is clickable only while a run is going and not yet cancelled."""
        return self.screen == SCREEN_RUNNING and not self.cancel_requested

    @property
    def total_accounts(self) -> int:
        return len(self.rows)

    @property
    def completed_count(self) -> int:
        return sum(1 for row in self.rows.values() if row.phase == PHASE_DONE)

    @property
    def failed_count(self) -> int:
        return sum(1 for row in self.rows.values() if row.phase == PHASE_FAILED)

    @property
    def overall_progress_pct(self) -> float:
        """
        `(completed + failed) / total * 100` — a run with one failed account still
        reaches 100%, because a failed account has still been WALKED, just not
        succeeded (the run as a whole
        finishes even though one row is red — one account's failure never stops the
        rest). `0` accounts is `0.0`, never a
        `ZeroDivisionError`.
        """
        if self.total_accounts == 0:
            return 0.0
        return (self.completed_count + self.failed_count) / self.total_accounts * 100.0


def initial_state(*, has_token: bool, terminal_found: bool) -> UiState:
    """
    The screen the window opens to, decided once at startup from exactly two facts:
    whether a token is already stored, and whether a terminal could be found at all.

    A missing terminal wins regardless of token state —
    there is nothing useful this program can do with a token if MT5 itself cannot be
    reached, so the no-terminal screen takes priority and the refresh action starts
    disabled.
    """
    if not terminal_found:
        return UiState(
            screen=SCREEN_NO_TERMINAL,
            notices=frozenset({NOTICE_NO_TERMINAL}),
            refresh_disabled=True,
        )
    if has_token:
        return UiState(screen=SCREEN_READY)
    return UiState(screen=SCREEN_PAIRING)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AccountProgressEvent:
    """
    Mirrors one `agent.sync.AccountProgress` report, field-for-field —
    `agent/main.py` is the one place that translates between the two (see this
    module's own docstring for why this file does not import `agent.sync` itself
    to reuse its dataclass directly). The one `PHASE_TERMINAL_CHECK` event per run
    (`mt_login` and `account_id` both `None`, `pre_existing_session` set only when
    a pre-existing terminal session was found) is this reducer's run-start marker: it clears the previous run's
    rows and moves the screen to `running`, rather than becoming a row itself.
    """

    mt_login: Optional[str]
    phase: str
    trades_sent: int = 0
    error_reason: Optional[str] = None
    account_id: Optional[str] = None
    pre_existing_session: Optional[dict] = None


@dataclass(frozen=True)
class PairingSucceededEvent:
    """The pairing code was redeemed and the token saved (`agent/main.py`'s job, not
    this reducer's — this event only records that it already happened)."""


@dataclass(frozen=True)
class PairingFailedEvent:
    """The pairing code was rejected, expired, or the form was incomplete."""

    reason: str


@dataclass(frozen=True)
class UnauthorizedEvent:
    """
    `AgentUnauthorizedError` (`agent/api_client.py`) surfaced anywhere in a run.
    `agent/main.py` is the one that calls `config_store.clear_token()` when it sees
    this — this reducer has no filesystem access of its own; it only moves the
    screen back to pairing and pins the revoked notice.
    """


@dataclass(frozen=True)
class RateLimitedEvent:
    """`AgentRateLimitedError` surfaced during a run. Never moves the screen away
    from wherever it already was — only records the retry hint."""

    retry_after_seconds: Optional[int] = None


@dataclass(frozen=True)
class RunFinishedEvent:
    """
    `run_sync()` returned (or a rate-limit ended the run) — `agent/main.py` fires this
    at most once per run, so the `running` screen returns to `ready` and
    «Синхронизировать» is re-enabled. Rows stay exactly as they last were, so the user
    can read the final tally before starting another run. `succeeded`/`failed` are the
    run's own counts; `finished_at_label` (a local "dd.mm.yyyy HH:MM" string built by
    the window) becomes the «Последняя успешная синхронизация» line, but only when at
    least one account succeeded. The no-argument form still works.
    """

    succeeded: int = 0
    failed: int = 0
    finished_at_label: Optional[str] = None


@dataclass(frozen=True)
class TerminalNotFoundEvent:
    """
    `agent.terminal_discovery.TerminalNotFoundError` surfaced mid-run (as opposed
    to at startup, where `initial_state()` already decides the very first screen) —
    moves straight to the same blocking, non-crashing no-terminal screen.
    """


@dataclass(frozen=True)
class ProtocolTooOldEvent:
    """
    `agent.api_client.AgentProtocolError` (HTTP 426, `protocol_too_old`) surfaced
    anywhere in a run — the server has refused this build's `X-Agent-Protocol`
    version outright. `agent/main.py`'s own except chain is what wires this event
    in; this event only defines the
    state transition that clause dispatches into. Note the ordering constraint
    that wiring depends on: `AgentProtocolError` is a subclass of
    `agent.api_client.AgentApiError`, so its `except` clause must come FIRST, or the
    generic `AgentApiError` clause silently swallows it.

    WHY A PINNED NOTICE, NOT A NEW SCREEN: the message is deliberately shown "as text in
    the program window, never as an error code", and the existing pinned-notice
    machinery (`NOTICE_TERMINAL_SWITCHED`, `NOTICE_TOKEN_REVOKED`,
    `NOTICE_NO_TERMINAL`) already renders multi-line text above whatever screen is
    showing. A screen swap would discard the account rows the person is looking at
    for no gain — this reducer's job is to inform, not to hide what the user was
    already reading.

    WHAT THIS DELIBERATELY DOES NOT DO (mirrors `agent/main.py`'s own head
    docstring): no auto-download, no self-replacement, no download-and-execute path
    of any kind. A version notice is plain text with a link the user follows
    themselves — this program never fetches, opens, or executes anything on this
    condition.
    """


@dataclass(frozen=True)
class RunFailedEvent:
    """
    The whole run ended on a failure that is not a single account's (terminal
    unresponsive or unreachable, a refused terminal launch, the server unreachable,
    an unexpected error). `kind` is one of the `RUN_ERROR_*` constants; the ready
    screen shows `run_error_text(kind, ...)` so the person is never left looking at
    an endless «Проверка терминала…».
    """

    kind: str
    elevation_mismatch: bool = False
    waited_seconds: Optional[int] = None


@dataclass(frozen=True)
class RunStartedEvent:
    """
    The run-start marker, dispatched on the main thread the moment a sync is started
    (before the worker thread even exists): the screen goes to `running`, the previous
    run's rows, stages and failure line are cleared, «Синхронизировать» is disabled and
    «Отменить» enabled. The last successful sync time is kept.
    """


@dataclass(frozen=True)
class StageProgressEvent:
    """Mirrors one `agent.sync.StageProgress` (see the module docstring for why this
    is a copy, not an import). A field left `None` keeps the stage's previous value,
    so a bare "failed" update does not erase the connect counter or the found path."""

    stage: str
    status: str
    detail: Optional[str] = None
    elapsed_seconds: Optional[int] = None


@dataclass(frozen=True)
class CancelRequestedEvent:
    """«Отменить» was pressed. Only meaningful while running; a no-op otherwise."""


@dataclass(frozen=True)
class RunCancelledEvent:
    """The worker stopped at a step boundary after «Отменить» (`SyncCancelledError`)."""


Event = Union[
    AccountProgressEvent,
    PairingSucceededEvent,
    PairingFailedEvent,
    UnauthorizedEvent,
    RateLimitedEvent,
    RunFinishedEvent,
    TerminalNotFoundEvent,
    ProtocolTooOldEvent,
    RunFailedEvent,
    RunStartedEvent,
    StageProgressEvent,
    CancelRequestedEvent,
    RunCancelledEvent,
]


def ordered_stages(state: UiState) -> "list[StageState]":
    """The stages present in `state`, in STAGE_ORDER — an absent stage (e.g. the
    launch stage when the terminal was already running) is simply skipped."""
    return [state.stages[name] for name in STAGE_ORDER if name in state.stages]


def _settle_in_progress(stages: "dict[str, StageState]", status: str) -> "dict[str, StageState]":
    """A copy of `stages` with every still-in-progress stage turned into `status`."""
    return {
        name: (replace(s, status=status) if s.status == STAGE_IN_PROGRESS else s)
        for name, s in stages.items()
    }


def _row_key(mt_login: Optional[str], account_id: Optional[str]) -> str:
    """
    The dict key a row is stored under. `mt_login` is the normal case. A malformed
    account row that never resolved even that far (`agent/sync.py`'s own
    "mt_login is not a valid integer" branch, which can report `mt_login=None`)
    falls back to `account_id`, then to a fixed placeholder — this function never
    raises for a `None`/`None` pair, matching every other function in this package's
    own "never raise on a bad row" convention.
    """
    if mt_login is not None:
        return str(mt_login)
    if account_id is not None:
        return str(account_id)
    return "unknown"


def reduce(state: UiState, event: Event) -> UiState:
    """
    The one place every screen transition and row update happens. Pure: never
    mutates `state` or anything inside it, always returns a brand new `UiState`.

    Every branch is written as `dataclasses.replace(state, ...)` naming only the
    fields that branch changes, so a field added to `UiState` later carries through
    every existing transition unchanged instead of being silently reset. The
    `rows=dict(state.rows)` copies are kept deliberately: the new state never shares
    a mutable dict with the old one.
    """
    if isinstance(event, AccountProgressEvent):
        if event.phase == PHASE_TERMINAL_CHECK:
            notices = state.notices
            if event.pre_existing_session is not None:
                notices = notices | {NOTICE_TERMINAL_SWITCHED}
            return replace(
                state,
                screen=SCREEN_RUNNING,
                rows={},
                notices=notices,
                pairing_error=None,
                rate_limit_retry_after_seconds=None,
                refresh_disabled=True,
                run_error=None,
            )

        key = _row_key(event.mt_login, event.account_id)
        rows = dict(state.rows)
        rows[key] = AccountRowState(
            mt_login=event.mt_login if event.mt_login is not None else key,
            phase=event.phase,
            trades_sent=event.trades_sent,
            error_reason=event.error_reason,
            account_id=event.account_id,
        )
        return replace(state, rows=rows)

    if isinstance(event, PairingSucceededEvent):
        return replace(state, screen=SCREEN_READY, rows=dict(state.rows), pairing_error=None)

    if isinstance(event, PairingFailedEvent):
        return replace(
            state, screen=SCREEN_PAIRING, rows=dict(state.rows), pairing_error=event.reason
        )

    if isinstance(event, UnauthorizedEvent):
        return replace(
            state,
            screen=SCREEN_PAIRING,
            rows=dict(state.rows),
            notices=state.notices | {NOTICE_TOKEN_REVOKED},
            pairing_error=None,
            rate_limit_retry_after_seconds=None,
            refresh_disabled=False,
        )

    if isinstance(event, RateLimitedEvent):
        return replace(
            state,
            rows=dict(state.rows),
            rate_limit_retry_after_seconds=event.retry_after_seconds,
        )

    if isinstance(event, RunFinishedEvent):
        screen = SCREEN_READY if state.screen == SCREEN_RUNNING else state.screen
        last_success_label = state.last_success_label
        if event.succeeded > 0 and event.finished_at_label:
            last_success_label = event.finished_at_label
        return replace(
            state,
            screen=screen,
            rows=dict(state.rows),
            refresh_disabled=False,
            cancel_requested=False,
            last_success_label=last_success_label,
        )

    if isinstance(event, TerminalNotFoundEvent):
        return replace(
            state,
            screen=SCREEN_NO_TERMINAL,
            rows=dict(state.rows),
            notices=state.notices | {NOTICE_NO_TERMINAL},
            rate_limit_retry_after_seconds=None,
            refresh_disabled=True,
        )

    if isinstance(event, ProtocolTooOldEvent):
        # Pinned notice, not a screen swap — see the event's own docstring for why.
        # `refresh_disabled=True` because retrying cannot succeed against a server
        # that refuses this build outright; the screen itself is left exactly as it
        # was, matching the "notices only ever grow" convention every other pinned
        # notice in this module already follows.
        return replace(
            state,
            rows=dict(state.rows),
            notices=state.notices | {NOTICE_PROTOCOL_TOO_OLD},
            refresh_disabled=True,
        )

    if isinstance(event, RunFailedEvent):
        screen = SCREEN_READY if state.screen == SCREEN_RUNNING else state.screen
        return replace(
            state,
            screen=screen,
            rows=dict(state.rows),
            stages=_settle_in_progress(state.stages, STAGE_FAILED),
            refresh_disabled=False,
            cancel_requested=False,
            run_error=run_error_text(
                event.kind,
                elevation_mismatch=event.elevation_mismatch,
                waited_seconds=event.waited_seconds,
            ),
        )

    if isinstance(event, RunStartedEvent):
        # Only the ready/running screen becomes "running"; a run started from any
        # other screen (a periodic tick after a revoked token, say) must not hide it.
        screen = SCREEN_RUNNING if state.screen in (SCREEN_READY, SCREEN_RUNNING) else state.screen
        return replace(
            state,
            screen=screen,
            rows={},
            stages={},
            pairing_error=None,
            rate_limit_retry_after_seconds=None,
            refresh_disabled=True,
            cancel_requested=False,
            run_error=None,
        )

    if isinstance(event, StageProgressEvent):
        previous = state.stages.get(event.stage)
        detail = event.detail
        elapsed = event.elapsed_seconds
        if previous is not None:
            detail = detail if detail is not None else previous.detail
            elapsed = elapsed if elapsed is not None else previous.elapsed_seconds
        stages = dict(state.stages)
        stages[event.stage] = StageState(
            stage=event.stage, status=event.status, detail=detail, elapsed_seconds=elapsed
        )
        return replace(state, stages=stages)

    if isinstance(event, CancelRequestedEvent):
        if state.screen != SCREEN_RUNNING:
            return state
        return replace(state, cancel_requested=True)

    if isinstance(event, RunCancelledEvent):
        screen = SCREEN_READY if state.screen == SCREEN_RUNNING else state.screen
        return replace(
            state,
            screen=screen,
            rows=dict(state.rows),
            stages=_settle_in_progress(state.stages, STAGE_CANCELLED),
            refresh_disabled=False,
            cancel_requested=False,
        )

    raise TypeError(f"reduce() received an unknown event type: {type(event)!r}")
