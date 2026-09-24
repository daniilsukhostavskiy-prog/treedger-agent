"""
agent/tests/test_ui_state.py — every `<behavior>` bullet for `agent/ui_state.py`
(39-16-PLAN.md Task 1). Pure-Python, no tkinter, no MetaTrader5, no display — this is
the real test coverage for the window's entire behaviour (see `ui_state.py`'s own
module docstring for why).
"""
from __future__ import annotations

from agent import ui_state


# ---------------------------------------------------------------------------
# "With no stored token, the initial screen is the pairing screen."
# "With a stored token and a discoverable terminal, the initial screen is ready."
# "With no discoverable terminal, the screen is the no-terminal screen and the
#  refresh action is disabled, regardless of whether a token exists."
# ---------------------------------------------------------------------------

def test_initial_screen_is_pairing_with_no_token_and_terminal_found() -> None:
    state = ui_state.initial_state(has_token=False, terminal_found=True)
    assert state.screen == ui_state.SCREEN_PAIRING
    assert state.refresh_disabled is False


def test_initial_screen_is_ready_with_token_and_terminal_found() -> None:
    state = ui_state.initial_state(has_token=True, terminal_found=True)
    assert state.screen == ui_state.SCREEN_READY
    assert state.refresh_disabled is False


def test_initial_screen_is_no_terminal_without_terminal_even_with_a_token() -> None:
    state = ui_state.initial_state(has_token=True, terminal_found=False)
    assert state.screen == ui_state.SCREEN_NO_TERMINAL
    assert state.refresh_disabled is True
    assert ui_state.NOTICE_NO_TERMINAL in state.notices


def test_initial_screen_is_no_terminal_without_terminal_and_without_a_token() -> None:
    state = ui_state.initial_state(has_token=False, terminal_found=False)
    assert state.screen == ui_state.SCREEN_NO_TERMINAL
    assert state.refresh_disabled is True


# ---------------------------------------------------------------------------
# "A successful pairing moves from pairing to ready."
# ---------------------------------------------------------------------------

def test_successful_pairing_moves_pairing_to_ready() -> None:
    state = ui_state.initial_state(has_token=False, terminal_found=True)
    assert state.screen == ui_state.SCREEN_PAIRING

    new_state = ui_state.reduce(state, ui_state.PairingSucceededEvent())

    assert new_state.screen == ui_state.SCREEN_READY
    assert new_state.pairing_error is None


def test_failed_pairing_stays_on_pairing_screen_with_a_reason() -> None:
    state = ui_state.initial_state(has_token=False, terminal_found=True)

    new_state = ui_state.reduce(
        state, ui_state.PairingFailedEvent(reason="код истёк или неверен")
    )

    assert new_state.screen == ui_state.SCREEN_PAIRING
    assert new_state.pairing_error == "код истёк или неверен"


# ---------------------------------------------------------------------------
# "An unauthorized error at any point moves back to the pairing screen, clears the
#  token and pins the revoked notice."
#
# (Clearing the actual stored token is agent/main.py's job — a filesystem side
# effect this pure reducer has no access to — but the screen transition and the
# pinned notice, the state-model half of that behaviour, belong here.)
# ---------------------------------------------------------------------------

def test_unauthorized_error_from_ready_returns_to_pairing_and_pins_revoked_notice() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_READY)

    new_state = ui_state.reduce(state, ui_state.UnauthorizedEvent())

    assert new_state.screen == ui_state.SCREEN_PAIRING
    assert ui_state.NOTICE_TOKEN_REVOKED in new_state.notices
    assert new_state.refresh_disabled is False


def test_unauthorized_error_from_running_also_returns_to_pairing() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_RUNNING, refresh_disabled=True)

    new_state = ui_state.reduce(state, ui_state.UnauthorizedEvent())

    assert new_state.screen == ui_state.SCREEN_PAIRING
    assert ui_state.NOTICE_TOKEN_REVOKED in new_state.notices


# ---------------------------------------------------------------------------
# "A pre-existing-session event pins the terminal-switched notice, and that notice
#  persists for the remainder of the run rather than being replaced by later
#  events."
# ---------------------------------------------------------------------------

def test_pre_existing_session_pins_terminal_switched_notice() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_READY)

    new_state = ui_state.reduce(
        state,
        ui_state.AccountProgressEvent(
            mt_login=None,
            phase=ui_state.PHASE_TERMINAL_CHECK,
            pre_existing_session={"login": 12345, "server": "Broker-Demo"},
        ),
    )

    assert new_state.screen == ui_state.SCREEN_RUNNING
    assert ui_state.NOTICE_TERMINAL_SWITCHED in new_state.notices


def test_terminal_check_with_no_pre_existing_session_does_not_pin_the_notice() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_READY)

    new_state = ui_state.reduce(
        state,
        ui_state.AccountProgressEvent(
            mt_login=None, phase=ui_state.PHASE_TERMINAL_CHECK, pre_existing_session=None
        ),
    )

    assert ui_state.NOTICE_TERMINAL_SWITCHED not in new_state.notices


def test_terminal_switched_notice_survives_every_subsequent_event() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_READY)

    state = ui_state.reduce(
        state,
        ui_state.AccountProgressEvent(
            mt_login=None,
            phase=ui_state.PHASE_TERMINAL_CHECK,
            pre_existing_session={"login": 12345, "server": "Broker-Demo"},
        ),
    )
    assert ui_state.NOTICE_TERMINAL_SWITCHED in state.notices

    # Fire a whole sequence of unrelated, later events — none of them may ever
    # remove the pinned notice.
    state = ui_state.reduce(
        state, ui_state.AccountProgressEvent(mt_login="12345", phase=ui_state.PHASE_LOGIN)
    )
    assert ui_state.NOTICE_TERMINAL_SWITCHED in state.notices

    state = ui_state.reduce(
        state,
        ui_state.AccountProgressEvent(
            mt_login="12345", phase=ui_state.PHASE_DONE, trades_sent=7
        ),
    )
    assert ui_state.NOTICE_TERMINAL_SWITCHED in state.notices

    state = ui_state.reduce(state, ui_state.RateLimitedEvent(retry_after_seconds=30))
    assert ui_state.NOTICE_TERMINAL_SWITCHED in state.notices

    state = ui_state.reduce(state, ui_state.RunFinishedEvent())
    assert ui_state.NOTICE_TERMINAL_SWITCHED in state.notices


# ---------------------------------------------------------------------------
# "A per-account progress event updates only that account's row."
# ---------------------------------------------------------------------------

def test_progress_event_updates_only_that_accounts_row() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_RUNNING)
    state = ui_state.reduce(
        state, ui_state.AccountProgressEvent(mt_login="111", phase=ui_state.PHASE_LOGIN)
    )
    state = ui_state.reduce(
        state, ui_state.AccountProgressEvent(mt_login="222", phase=ui_state.PHASE_LOGIN)
    )
    row_222_before = state.rows["222"]

    state = ui_state.reduce(
        state, ui_state.AccountProgressEvent(mt_login="111", phase=ui_state.PHASE_READING)
    )

    assert state.rows["111"].phase == ui_state.PHASE_READING
    assert state.rows["222"] == row_222_before


# ---------------------------------------------------------------------------
# "An account error event sets that row to an error state with its reason and
#  leaves every other row untouched."
# ---------------------------------------------------------------------------

def test_account_error_sets_that_row_and_leaves_others_untouched() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_RUNNING)
    state = ui_state.reduce(
        state, ui_state.AccountProgressEvent(mt_login="111", phase=ui_state.PHASE_READING)
    )
    state = ui_state.reduce(
        state,
        ui_state.AccountProgressEvent(mt_login="222", phase=ui_state.PHASE_DONE, trades_sent=3),
    )
    row_222_before = state.rows["222"]

    state = ui_state.reduce(
        state,
        ui_state.AccountProgressEvent(
            mt_login="111", phase=ui_state.PHASE_FAILED, error_reason="login failed (auth_failed)"
        ),
    )

    assert state.rows["111"].phase == ui_state.PHASE_FAILED
    assert state.rows["111"].error_reason == "login failed (auth_failed)"
    assert state.rows["222"] == row_222_before


# ---------------------------------------------------------------------------
# "The overall progress line reflects completed-plus-failed over total, so a run
#  with one failed account still reaches 100%."
# ---------------------------------------------------------------------------

def test_overall_progress_reaches_100_percent_with_one_failed_account() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_RUNNING)

    # Account 111 succeeds.
    for phase in (ui_state.PHASE_LOGIN, ui_state.PHASE_READING, ui_state.PHASE_SENDING):
        state = ui_state.reduce(
            state, ui_state.AccountProgressEvent(mt_login="111", phase=phase)
        )
    state = ui_state.reduce(
        state,
        ui_state.AccountProgressEvent(mt_login="111", phase=ui_state.PHASE_DONE, trades_sent=12),
    )

    # Account 222 fails.
    state = ui_state.reduce(
        state, ui_state.AccountProgressEvent(mt_login="222", phase=ui_state.PHASE_LOGIN)
    )
    state = ui_state.reduce(
        state,
        ui_state.AccountProgressEvent(
            mt_login="222", phase=ui_state.PHASE_FAILED, error_reason="wrong password"
        ),
    )

    assert state.total_accounts == 2
    assert state.completed_count == 1
    assert state.failed_count == 1
    assert state.overall_progress_pct == 100.0


def test_overall_progress_is_zero_with_no_accounts_yet() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_RUNNING)
    assert state.total_accounts == 0
    assert state.overall_progress_pct == 0.0


def test_overall_progress_is_partial_mid_run() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_RUNNING)
    state = ui_state.reduce(
        state, ui_state.AccountProgressEvent(mt_login="111", phase=ui_state.PHASE_READING)
    )
    assert state.total_accounts == 1
    assert state.completed_count == 0
    assert state.failed_count == 0
    assert state.overall_progress_pct == 0.0


# ---------------------------------------------------------------------------
# "A rate-limited error surfaces its retry hint without leaving the ready screen."
# ---------------------------------------------------------------------------

def test_rate_limited_error_surfaces_retry_hint_without_leaving_the_ready_screen() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_READY)

    new_state = ui_state.reduce(state, ui_state.RateLimitedEvent(retry_after_seconds=45))

    assert new_state.screen == ui_state.SCREEN_READY
    assert new_state.rate_limit_retry_after_seconds == 45


def test_rate_limited_error_during_a_run_does_not_leave_the_running_screen() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_RUNNING, refresh_disabled=True)

    new_state = ui_state.reduce(state, ui_state.RateLimitedEvent(retry_after_seconds=None))

    assert new_state.screen == ui_state.SCREEN_RUNNING
    assert new_state.rate_limit_retry_after_seconds is None


# ---------------------------------------------------------------------------
# Supplementary coverage: RunFinishedEvent and TerminalNotFoundEvent, the two
# additional events agent/main.py needs to orchestrate a full run that this
# plan's own <behavior> list does not separately enumerate.
# ---------------------------------------------------------------------------

def test_run_finished_returns_running_screen_to_ready_and_reenables_refresh() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_RUNNING, refresh_disabled=True)
    state = ui_state.reduce(
        state,
        ui_state.AccountProgressEvent(mt_login="111", phase=ui_state.PHASE_DONE, trades_sent=4),
    )

    new_state = ui_state.reduce(state, ui_state.RunFinishedEvent())

    assert new_state.screen == ui_state.SCREEN_READY
    assert new_state.refresh_disabled is False
    # Rows survive so the user can read the final tally.
    assert new_state.rows["111"].trades_sent == 4


def test_run_finished_from_pairing_screen_does_not_move_to_ready() -> None:
    """
    An UnauthorizedEvent mid-run already sent the screen to pairing; the worker
    thread's own RunFinishedEvent (fired unconditionally once run_sync's call
    returns/raises) must never bounce the user back to ready from there.
    """
    state = ui_state.UiState(screen=ui_state.SCREEN_PAIRING)

    new_state = ui_state.reduce(state, ui_state.RunFinishedEvent())

    assert new_state.screen == ui_state.SCREEN_PAIRING


def test_terminal_not_found_mid_run_moves_to_no_terminal_screen() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_RUNNING, refresh_disabled=True)

    new_state = ui_state.reduce(state, ui_state.TerminalNotFoundEvent())

    assert new_state.screen == ui_state.SCREEN_NO_TERMINAL
    assert new_state.refresh_disabled is True
    assert ui_state.NOTICE_NO_TERMINAL in new_state.notices


# ---------------------------------------------------------------------------
# reduce() is a pure function — never mutates its input.
# ---------------------------------------------------------------------------

def test_reduce_never_mutates_the_input_state() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_RUNNING)
    state = ui_state.reduce(
        state, ui_state.AccountProgressEvent(mt_login="111", phase=ui_state.PHASE_LOGIN)
    )
    snapshot_rows = dict(state.rows)
    snapshot_notices = frozenset(state.notices)

    ui_state.reduce(
        state, ui_state.AccountProgressEvent(mt_login="222", phase=ui_state.PHASE_LOGIN)
    )

    assert state.rows == snapshot_rows
    assert state.notices == snapshot_notices
