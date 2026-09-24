"""
agent/ui_state.py — the window's entire behaviour, as a pure, Tk-free reducer.

WHY THIS FILE IMPORTS NO GUI TOOLKIT AT ALL
--------------------------------------------------------------------------
`agent/main.py` (the only place tkinter is ever imported in this package, 39-16-PLAN.md
Task 2) is deliberately thin: it builds widgets, drains a queue on a `root.after` timer,
and renders whatever `UiState` this module hands it. Every actual DECISION the window
makes — which screen is showing, what each account row says, which notice is pinned —
lives here instead, behind one pure function: `reduce(state, event) -> UiState`. That
split is what makes the window's entire logic testable on a machine with no display at
all (this repo's own CI has none): `agent/tests/test_ui_state.py` is the real test
coverage for this program's GUI; the Tk layer on top of it stays thin enough not to need
its own suite.

No `import tkinter` (or any other GUI toolkit) appears anywhere below — enforced
mechanically by 39-16-PLAN.md's own AST-based verify command for this file, not merely
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

from dataclasses import dataclass, field
from typing import Optional, Union

# ---------------------------------------------------------------------------
# Screens — exactly the four named in 39-16-PLAN.md's own artifacts section.
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
# Pinned notices — a small, closed set. Once a notice is pinned it is NEVER removed
# by a later, unrelated event (39-CONTEXT.md D-23's "persists for the remainder of the
# run" requirement, and this plan's own behaviour bullet) — `UiState.notices` only
# ever grows across a reduce() call, never shrinks.
# ---------------------------------------------------------------------------
NOTICE_TERMINAL_SWITCHED = "terminal_switched"
NOTICE_TOKEN_REVOKED = "token_revoked"
NOTICE_NO_TERMINAL = "no_terminal"

NOTICE_TEXT: "dict[str, str]" = {
    # Verbatim from 39-CONTEXT.md D-23 — no login restore, no separate terminal
    # instance, because neither exists. This is a warning, not a fix-it button.
    NOTICE_TERMINAL_SWITCHED: (
        "Программа переключала счета в вашем терминале MT5. "
        "Если вы в нём работали — войдите в свой счёт заново."
    ),
    NOTICE_TOKEN_REVOKED: "Токен был отозван. Пройдите привязку заново.",
    # Verbatim from 39-CONTEXT.md D-09 §12.2.
    NOTICE_NO_TERMINAL: "MetaTrader 5 не найден на этом компьютере.",
}


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
        succeeded (this is readiness criterion 8's visible half: the run as a whole
        finishes even though one row is red). `0` accounts is `0.0`, never a
        `ZeroDivisionError`.
        """
        if self.total_accounts == 0:
            return 0.0
        return (self.completed_count + self.failed_count) / self.total_accounts * 100.0


def initial_state(*, has_token: bool, terminal_found: bool) -> UiState:
    """
    The screen the window opens to, decided once at startup from exactly two facts:
    whether a token is already stored, and whether a terminal could be found at all.

    A missing terminal wins regardless of token state (39-CONTEXT.md D-09 §12.2) —
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
    D-23 fired) is this reducer's run-start marker: it clears the previous run's
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
    screen back to pairing and pins the revoked notice (readiness criterion 9's
    user-visible half).
    """


@dataclass(frozen=True)
class RateLimitedEvent:
    """`AgentRateLimitedError` surfaced during a run. Never moves the screen away
    from wherever it already was — only records the retry hint."""

    retry_after_seconds: Optional[int] = None


@dataclass(frozen=True)
class RunFinishedEvent:
    """
    `run_sync()` returned, or raised something not already handled by one of the
    events above — `agent/main.py` fires this exactly once per run, from the
    main-thread poller, so the `running` screen returns to `ready` and «Обновить»
    is re-enabled. Rows stay exactly as they last were, so the user can read the
    final tally before starting another run.
    """


@dataclass(frozen=True)
class TerminalNotFoundEvent:
    """
    `agent.terminal_discovery.TerminalNotFoundError` surfaced mid-run (as opposed
    to at startup, where `initial_state()` already decides the very first screen) —
    moves straight to the same blocking, non-crashing no-terminal screen.
    """


Event = Union[
    AccountProgressEvent,
    PairingSucceededEvent,
    PairingFailedEvent,
    UnauthorizedEvent,
    RateLimitedEvent,
    RunFinishedEvent,
    TerminalNotFoundEvent,
]


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
    """
    if isinstance(event, AccountProgressEvent):
        if event.phase == PHASE_TERMINAL_CHECK:
            notices = state.notices
            if event.pre_existing_session is not None:
                notices = notices | {NOTICE_TERMINAL_SWITCHED}
            return UiState(
                screen=SCREEN_RUNNING,
                rows={},
                notices=notices,
                pairing_error=None,
                rate_limit_retry_after_seconds=None,
                refresh_disabled=True,
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
        return UiState(
            screen=state.screen,
            rows=rows,
            notices=state.notices,
            pairing_error=state.pairing_error,
            rate_limit_retry_after_seconds=state.rate_limit_retry_after_seconds,
            refresh_disabled=state.refresh_disabled,
        )

    if isinstance(event, PairingSucceededEvent):
        return UiState(
            screen=SCREEN_READY,
            rows=dict(state.rows),
            notices=state.notices,
            pairing_error=None,
            rate_limit_retry_after_seconds=state.rate_limit_retry_after_seconds,
            refresh_disabled=state.refresh_disabled,
        )

    if isinstance(event, PairingFailedEvent):
        return UiState(
            screen=SCREEN_PAIRING,
            rows=dict(state.rows),
            notices=state.notices,
            pairing_error=event.reason,
            rate_limit_retry_after_seconds=state.rate_limit_retry_after_seconds,
            refresh_disabled=state.refresh_disabled,
        )

    if isinstance(event, UnauthorizedEvent):
        return UiState(
            screen=SCREEN_PAIRING,
            rows=dict(state.rows),
            notices=state.notices | {NOTICE_TOKEN_REVOKED},
            pairing_error=None,
            rate_limit_retry_after_seconds=None,
            refresh_disabled=False,
        )

    if isinstance(event, RateLimitedEvent):
        return UiState(
            screen=state.screen,
            rows=dict(state.rows),
            notices=state.notices,
            pairing_error=state.pairing_error,
            rate_limit_retry_after_seconds=event.retry_after_seconds,
            refresh_disabled=state.refresh_disabled,
        )

    if isinstance(event, RunFinishedEvent):
        screen = SCREEN_READY if state.screen == SCREEN_RUNNING else state.screen
        return UiState(
            screen=screen,
            rows=dict(state.rows),
            notices=state.notices,
            pairing_error=state.pairing_error,
            rate_limit_retry_after_seconds=state.rate_limit_retry_after_seconds,
            refresh_disabled=False,
        )

    if isinstance(event, TerminalNotFoundEvent):
        return UiState(
            screen=SCREEN_NO_TERMINAL,
            rows=dict(state.rows),
            notices=state.notices | {NOTICE_NO_TERMINAL},
            pairing_error=state.pairing_error,
            rate_limit_retry_after_seconds=None,
            refresh_disabled=True,
        )

    raise TypeError(f"reduce() received an unknown event type: {type(event)!r}")
