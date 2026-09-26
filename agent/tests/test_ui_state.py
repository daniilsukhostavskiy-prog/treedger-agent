"""
agent/tests/test_ui_state.py — the test coverage for `agent/ui_state.py`,
including the «Программа устарела»
protocol-too-old notice. Pure-Python, no tkinter, no MetaTrader5, no display — this
is the real test coverage for the window's entire behaviour (see `ui_state.py`'s own
module docstring for why).
"""
from __future__ import annotations

import pytest

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


# ---------------------------------------------------------------------------
# «Программа устарела» — ProtocolTooOldEvent.
# "Reducing ProtocolTooOldEvent pins the protocol-too-old notice into the
# state's notice set."
# ---------------------------------------------------------------------------

def test_protocol_too_old_event_pins_the_notice() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_READY)

    new_state = ui_state.reduce(state, ui_state.ProtocolTooOldEvent())

    assert ui_state.NOTICE_PROTOCOL_TOO_OLD in new_state.notices


def test_protocol_too_old_event_disables_refresh() -> None:
    """"Reducing ProtocolTooOldEvent leaves the refresh action disabled, because
    retrying cannot succeed against a server that refuses this build."""
    state = ui_state.UiState(screen=ui_state.SCREEN_READY, refresh_disabled=False)

    new_state = ui_state.reduce(state, ui_state.ProtocolTooOldEvent())

    assert new_state.refresh_disabled is True


def test_protocol_too_old_notice_survives_every_subsequent_unrelated_event() -> None:
    """The pinned notice survives every subsequent event. Rule changed deliberately
    by quick 260925-qhs: a notice is never removed by an UNRELATED event, only by the
    one event that proves it stale — and nothing can prove protocol-too-old stale
    (refresh stays disabled, no later run can happen against a server that refuses
    this build), so this notice never clears within a session."""
    state = ui_state.UiState(screen=ui_state.SCREEN_READY)

    state = ui_state.reduce(state, ui_state.ProtocolTooOldEvent())
    assert ui_state.NOTICE_PROTOCOL_TOO_OLD in state.notices

    state = ui_state.reduce(
        state, ui_state.AccountProgressEvent(mt_login="111", phase=ui_state.PHASE_LOGIN)
    )
    assert ui_state.NOTICE_PROTOCOL_TOO_OLD in state.notices

    state = ui_state.reduce(
        state,
        ui_state.AccountProgressEvent(mt_login="111", phase=ui_state.PHASE_DONE, trades_sent=3),
    )
    assert ui_state.NOTICE_PROTOCOL_TOO_OLD in state.notices

    state = ui_state.reduce(state, ui_state.RateLimitedEvent(retry_after_seconds=30))
    assert ui_state.NOTICE_PROTOCOL_TOO_OLD in state.notices

    state = ui_state.reduce(state, ui_state.RunFinishedEvent())
    assert ui_state.NOTICE_PROTOCOL_TOO_OLD in state.notices


def test_notice_text_for_protocol_too_old_resolves_and_contains_the_download_url() -> None:
    """"NOTICE_TEXT has an entry for the new notice, so rendering it cannot raise a
    key error." "The notice text contains the download URL as literal text a person
    can read and type."""
    text = ui_state.NOTICE_TEXT[ui_state.NOTICE_PROTOCOL_TOO_OLD]

    assert ui_state.DOWNLOAD_URL in text


def test_notice_text_for_protocol_too_old_never_contains_an_http_status_number() -> None:
    """A person reading the window is told what to do, not what the protocol
    said — no status code anywhere in the rendered text."""
    text = ui_state.NOTICE_TEXT[ui_state.NOTICE_PROTOCOL_TOO_OLD]

    assert "426" not in text
    assert "protocol_too_old" not in text


def test_reduce_still_raises_on_a_genuinely_unknown_event_type() -> None:
    """The new ProtocolTooOldEvent branch does not swallow the fallthrough — an
    unrecognised event type still raises."""
    state = ui_state.UiState(screen=ui_state.SCREEN_READY)

    with pytest.raises(TypeError):
        ui_state.reduce(state, object())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Structural: ui_state.py still imports no GUI toolkit (asserted separately by the
# verify command's own `ast` check too — this is the same assertion authored as a
# real pytest test rather than only a shell one-liner).
# ---------------------------------------------------------------------------

def test_module_imports_no_gui_toolkit() -> None:
    import ast
    import pathlib

    source_path = pathlib.Path(ui_state.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])

    assert "tkinter" not in modules


# ---------------------------------------------------------------------------
# 260925-k6y — whole-run failure line
# ---------------------------------------------------------------------------

def _running_state() -> "ui_state.UiState":
    state = ui_state.UiState(screen=ui_state.SCREEN_READY)
    return ui_state.reduce(
        state, ui_state.AccountProgressEvent(mt_login=None, phase=ui_state.PHASE_TERMINAL_CHECK)
    )


def test_run_failed_from_running_returns_to_ready_with_error_text() -> None:
    state = _running_state()
    assert state.screen == ui_state.SCREEN_RUNNING

    state = ui_state.reduce(
        state,
        ui_state.RunFailedEvent(kind=ui_state.RUN_ERROR_TERMINAL_UNRESPONSIVE, waited_seconds=90),
    )

    assert state.screen == ui_state.SCREEN_READY
    assert state.refresh_disabled is False
    assert state.run_error == ui_state.run_error_text(
        ui_state.RUN_ERROR_TERMINAL_UNRESPONSIVE, waited_seconds=90
    )


def test_unresponsive_text_has_seconds_and_windows_dialog_hint() -> None:
    text = ui_state.run_error_text(ui_state.RUN_ERROR_TERMINAL_UNRESPONSIVE, waited_seconds=90)
    assert "(90 с)" in text
    assert "системное окно Windows" in text
    assert "администратора" not in text

    with_elevation = ui_state.run_error_text(
        ui_state.RUN_ERROR_TERMINAL_UNRESPONSIVE, waited_seconds=90, elevation_mismatch=True
    )
    assert "администратора" in with_elevation


@pytest.mark.parametrize(
    "kind",
    [
        ui_state.RUN_ERROR_TERMINAL_UNRESPONSIVE,
        ui_state.RUN_ERROR_TERMINAL_STILL_BLOCKED,
        ui_state.RUN_ERROR_TERMINAL_FAILED,
        ui_state.RUN_ERROR_LAUNCH_FAILED,
        ui_state.RUN_ERROR_SERVER,
        ui_state.RUN_ERROR_INTERNAL,
        ui_state.RUN_ERROR_ALGO_TRADING,
    ],
)
def test_run_error_texts_never_name_a_button(kind: str) -> None:
    text = ui_state.run_error_text(kind, waited_seconds=5)
    assert text
    assert "«" not in text


# ---------------------------------------------------------------------------
# quick 260925-qhs — «Алготрейдинг» notice + run error
# ---------------------------------------------------------------------------

def _terminal_check(algo: "bool | None") -> "ui_state.AccountProgressEvent":
    return ui_state.AccountProgressEvent(
        mt_login=None, phase=ui_state.PHASE_TERMINAL_CHECK, algo_trading_allowed=algo
    )


def test_algo_trading_run_error_text_is_a_specific_instruction() -> None:
    text = ui_state.run_error_text(ui_state.RUN_ERROR_ALGO_TRADING)
    assert text.startswith("Включите „Алготрейдинг“ в терминале")
    assert "Algo Trading" in text
    assert "«" not in text
    assert text != ui_state.run_error_text(ui_state.RUN_ERROR_TERMINAL_FAILED)


def test_algo_trading_notice_text_resolves() -> None:
    text = ui_state.NOTICE_TEXT[ui_state.NOTICE_ALGO_TRADING_OFF]
    assert text.startswith("Включите „Алготрейдинг“ в терминале")
    assert "Algo Trading" in text


def test_run_failed_with_algo_kind_shows_the_instruction() -> None:
    state = ui_state.reduce(_running_state(), ui_state.RunFailedEvent(kind=ui_state.RUN_ERROR_ALGO_TRADING))
    assert state.run_error is not None
    assert state.run_error.startswith("Включите „Алготрейдинг“ в терминале")


def test_terminal_check_with_algo_off_pins_the_notice_and_on_removes_it() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_READY)

    state = ui_state.reduce(state, _terminal_check(False))
    assert ui_state.NOTICE_ALGO_TRADING_OFF in state.notices

    # Unknown (None) leaves it where it was.
    state = ui_state.reduce(state, _terminal_check(None))
    assert ui_state.NOTICE_ALGO_TRADING_OFF in state.notices

    state = ui_state.reduce(state, _terminal_check(True))
    assert ui_state.NOTICE_ALGO_TRADING_OFF not in state.notices


def test_terminal_check_with_unknown_algo_state_never_pins_the_notice() -> None:
    state = ui_state.reduce(ui_state.UiState(screen=ui_state.SCREEN_READY), _terminal_check(None))
    assert ui_state.NOTICE_ALGO_TRADING_OFF not in state.notices


def test_account_progress_event_algo_field_is_last_and_defaults_to_none() -> None:
    import dataclasses

    fields = dataclasses.fields(ui_state.AccountProgressEvent)
    assert fields[-1].name == "algo_trading_allowed"
    assert ui_state.AccountProgressEvent(mt_login="1", phase=ui_state.PHASE_LOGIN).algo_trading_allowed is None


# ---------------------------------------------------------------------------
# quick 260925-qhs — stale notices are cleared by the event that proves them stale
# ---------------------------------------------------------------------------

def _revoked_state() -> "ui_state.UiState":
    state = ui_state.reduce(ui_state.UiState(screen=ui_state.SCREEN_READY), ui_state.UnauthorizedEvent())
    assert ui_state.NOTICE_TOKEN_REVOKED in state.notices
    return state


def test_token_revoked_is_cleared_by_a_successful_pairing() -> None:
    state = ui_state.reduce(_revoked_state(), ui_state.PairingSucceededEvent())
    assert ui_state.NOTICE_TOKEN_REVOKED not in state.notices


def test_token_revoked_is_cleared_by_a_successful_account_list_fetch() -> None:
    state = ui_state.reduce(
        _revoked_state(),
        ui_state.StageProgressEvent(stage=ui_state.STAGE_FETCH_ACCOUNTS, status=ui_state.STAGE_DONE, detail="1"),
    )
    assert ui_state.NOTICE_TOKEN_REVOKED not in state.notices


def test_token_revoked_is_not_cleared_by_a_fetch_still_in_progress_or_failed() -> None:
    for status in (ui_state.STAGE_IN_PROGRESS, ui_state.STAGE_FAILED):
        state = ui_state.reduce(
            _revoked_state(), ui_state.StageProgressEvent(stage=ui_state.STAGE_FETCH_ACCOUNTS, status=status)
        )
        assert ui_state.NOTICE_TOKEN_REVOKED in state.notices


def test_token_revoked_is_cleared_by_a_run_with_at_least_one_success() -> None:
    state = ui_state.reduce(_revoked_state(), ui_state.RunFinishedEvent(succeeded=1, failed=0))
    assert ui_state.NOTICE_TOKEN_REVOKED not in state.notices


def test_token_revoked_survives_unrelated_events_and_a_run_with_no_success() -> None:
    state = _revoked_state()
    for event in (
        ui_state.RateLimitedEvent(retry_after_seconds=30),
        ui_state.AccountProgressEvent(mt_login="1", phase=ui_state.PHASE_LOGIN),
        ui_state.AccountProgressEvent(mt_login="1", phase=ui_state.PHASE_DONE, trades_sent=5),
        ui_state.RunFinishedEvent(succeeded=0, failed=2),
        ui_state.RunFinishedEvent(),
        ui_state.StageProgressEvent(stage=ui_state.STAGE_CONNECT, status=ui_state.STAGE_DONE),
    ):
        state = ui_state.reduce(state, event)
        assert ui_state.NOTICE_TOKEN_REVOKED in state.notices, event


def test_no_terminal_is_cleared_once_a_run_found_the_terminal() -> None:
    state = ui_state.reduce(ui_state.UiState(screen=ui_state.SCREEN_RUNNING), ui_state.TerminalNotFoundEvent())
    assert ui_state.NOTICE_NO_TERMINAL in state.notices

    unchanged = ui_state.reduce(
        state, ui_state.StageProgressEvent(stage=ui_state.STAGE_FIND_TERMINAL, status=ui_state.STAGE_IN_PROGRESS)
    )
    assert ui_state.NOTICE_NO_TERMINAL in unchanged.notices

    found = ui_state.reduce(
        state,
        ui_state.StageProgressEvent(
            stage=ui_state.STAGE_FIND_TERMINAL, status=ui_state.STAGE_DONE, detail=r"C:\MT5\terminal64.exe"
        ),
    )
    assert ui_state.NOTICE_NO_TERMINAL not in found.notices


def test_terminal_switched_and_protocol_too_old_never_clear_on_the_new_clearing_events() -> None:
    state = ui_state.UiState(
        screen=ui_state.SCREEN_READY,
        notices=frozenset({ui_state.NOTICE_TERMINAL_SWITCHED, ui_state.NOTICE_PROTOCOL_TOO_OLD}),
    )
    for event in (
        ui_state.PairingSucceededEvent(),
        ui_state.StageProgressEvent(stage=ui_state.STAGE_FETCH_ACCOUNTS, status=ui_state.STAGE_DONE, detail="1"),
        ui_state.StageProgressEvent(stage=ui_state.STAGE_FIND_TERMINAL, status=ui_state.STAGE_DONE, detail="x"),
        ui_state.RunFinishedEvent(succeeded=3),
        _terminal_check(True),
    ):
        state = ui_state.reduce(state, event)
        assert ui_state.NOTICE_TERMINAL_SWITCHED in state.notices, event
        assert ui_state.NOTICE_PROTOCOL_TOO_OLD in state.notices, event


def test_run_failed_from_ready_stays_on_ready() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_READY)
    state = ui_state.reduce(state, ui_state.RunFailedEvent(kind=ui_state.RUN_ERROR_SERVER))
    assert state.screen == ui_state.SCREEN_READY
    assert state.run_error == ui_state.run_error_text(ui_state.RUN_ERROR_SERVER)


def test_terminal_check_clears_a_previous_run_error() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_READY)
    state = ui_state.reduce(state, ui_state.RunFailedEvent(kind=ui_state.RUN_ERROR_INTERNAL))
    assert state.run_error is not None

    state = ui_state.reduce(
        state, ui_state.AccountProgressEvent(mt_login=None, phase=ui_state.PHASE_TERMINAL_CHECK)
    )
    assert state.run_error is None


def test_run_error_survives_unrelated_rate_limit_event() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_READY)
    state = ui_state.reduce(state, ui_state.RunFailedEvent(kind=ui_state.RUN_ERROR_SERVER))
    state = ui_state.reduce(state, ui_state.RateLimitedEvent(retry_after_seconds=5))
    assert state.run_error == ui_state.run_error_text(ui_state.RUN_ERROR_SERVER)


# ---------------------------------------------------------------------------
# 260925-k6y Task 2 — stage list, cancel, last successful sync
# ---------------------------------------------------------------------------

def _started() -> "ui_state.UiState":
    state = ui_state.UiState(screen=ui_state.SCREEN_READY, last_success_label="24.09.2026 10:00")
    return ui_state.reduce(state, ui_state.RunStartedEvent())


def test_run_started_from_ready_resets_and_enables_cancel() -> None:
    before = ui_state.UiState(
        screen=ui_state.SCREEN_READY,
        rows={"1": ui_state.AccountRowState(mt_login="1", phase=ui_state.PHASE_DONE)},
        stages={
            ui_state.STAGE_CONNECT: ui_state.StageState(ui_state.STAGE_CONNECT, ui_state.STAGE_DONE)
        },
        run_error="old error",
        last_success_label="24.09.2026 10:00",
    )
    state = ui_state.reduce(before, ui_state.RunStartedEvent())

    assert state.screen == ui_state.SCREEN_RUNNING
    assert state.rows == {}
    assert state.stages == {}
    assert state.run_error is None
    assert state.refresh_disabled is True
    assert state.cancel_enabled is True
    assert state.last_success_label == "24.09.2026 10:00"


def test_run_started_does_not_hide_the_pairing_screen() -> None:
    state = ui_state.reduce(ui_state.UiState(screen=ui_state.SCREEN_PAIRING), ui_state.RunStartedEvent())
    assert state.screen == ui_state.SCREEN_PAIRING


def test_stage_progress_adds_and_replaces_and_ticks_update_elapsed() -> None:
    state = _started()
    state = ui_state.reduce(
        state, ui_state.StageProgressEvent(ui_state.STAGE_CONNECT, ui_state.STAGE_IN_PROGRESS, elapsed_seconds=0)
    )
    state = ui_state.reduce(
        state, ui_state.StageProgressEvent(ui_state.STAGE_CONNECT, ui_state.STAGE_IN_PROGRESS, elapsed_seconds=12)
    )
    assert state.stages[ui_state.STAGE_CONNECT].elapsed_seconds == 12
    assert len(state.stages) == 1

    # A bare status update keeps the previous counter.
    state = ui_state.reduce(
        state, ui_state.StageProgressEvent(ui_state.STAGE_CONNECT, ui_state.STAGE_FAILED)
    )
    assert state.stages[ui_state.STAGE_CONNECT].status == ui_state.STAGE_FAILED
    assert state.stages[ui_state.STAGE_CONNECT].elapsed_seconds == 12


def test_ordered_stages_follow_stage_order_and_skip_absent_launch() -> None:
    state = _started()
    for stage in (ui_state.STAGE_FETCH_ACCOUNTS, ui_state.STAGE_FIND_TERMINAL, ui_state.STAGE_CONNECT):
        state = ui_state.reduce(state, ui_state.StageProgressEvent(stage, ui_state.STAGE_DONE))

    names = [s.stage for s in ui_state.ordered_stages(state)]
    assert names == [ui_state.STAGE_FIND_TERMINAL, ui_state.STAGE_CONNECT, ui_state.STAGE_FETCH_ACCOUNTS]
    assert ui_state.STAGE_LAUNCH_TERMINAL not in names


def test_stage_line_texts() -> None:
    path = "C:\\Program Files\\MetaTrader 5\\terminal64.exe"
    assert ui_state.stage_line(
        ui_state.StageState(ui_state.STAGE_FIND_TERMINAL, ui_state.STAGE_DONE, detail=path)
    ) == f"✓ Поиск терминала — {path}"
    assert ui_state.stage_line(
        ui_state.StageState(ui_state.STAGE_CONNECT, ui_state.STAGE_IN_PROGRESS, elapsed_seconds=12)
    ) == "… Подключение к терминалу — 12 с"
    assert ui_state.stage_line(
        ui_state.StageState(ui_state.STAGE_FETCH_ACCOUNTS, ui_state.STAGE_DONE, detail="2")
    ) == "✓ Получение списка счетов — найдено: 2"
    assert ui_state.stage_line(
        ui_state.StageState(ui_state.STAGE_CONNECT, ui_state.STAGE_FAILED, elapsed_seconds=90)
    ).startswith("✗")
    cancelled = ui_state.stage_line(
        ui_state.StageState(ui_state.STAGE_CONNECT, ui_state.STAGE_CANCELLED)
    )
    assert cancelled.startswith("–")
    assert "отменено" in cancelled


def test_cancel_requested_while_running_disables_cancel_and_is_noop_on_ready() -> None:
    state = ui_state.reduce(_started(), ui_state.CancelRequestedEvent())
    assert state.cancel_requested is True
    assert state.cancel_enabled is False

    ready = ui_state.UiState(screen=ui_state.SCREEN_READY)
    assert ui_state.reduce(ready, ui_state.CancelRequestedEvent()) == ready


def test_run_cancelled_returns_to_ready_and_marks_in_progress_cancelled() -> None:
    state = _started()
    state = ui_state.reduce(state, ui_state.StageProgressEvent(ui_state.STAGE_FIND_TERMINAL, ui_state.STAGE_DONE))
    state = ui_state.reduce(state, ui_state.StageProgressEvent(ui_state.STAGE_CONNECT, ui_state.STAGE_IN_PROGRESS))
    state = ui_state.reduce(state, ui_state.AccountProgressEvent(mt_login="7", phase=ui_state.PHASE_DONE))
    state = ui_state.reduce(state, ui_state.CancelRequestedEvent())

    state = ui_state.reduce(state, ui_state.RunCancelledEvent())

    assert state.screen == ui_state.SCREEN_READY
    assert state.refresh_disabled is False
    assert state.stages[ui_state.STAGE_CONNECT].status == ui_state.STAGE_CANCELLED
    assert state.stages[ui_state.STAGE_FIND_TERMINAL].status == ui_state.STAGE_DONE
    assert "7" in state.rows
    assert state.cancel_requested is False


def test_run_failed_marks_in_progress_stages_failed() -> None:
    state = _started()
    state = ui_state.reduce(state, ui_state.StageProgressEvent(ui_state.STAGE_CONNECT, ui_state.STAGE_IN_PROGRESS))
    state = ui_state.reduce(state, ui_state.RunFailedEvent(kind=ui_state.RUN_ERROR_TERMINAL_UNRESPONSIVE))
    assert state.stages[ui_state.STAGE_CONNECT].status == ui_state.STAGE_FAILED


def test_run_finished_sets_last_success_only_when_something_succeeded() -> None:
    state = ui_state.reduce(
        _started(), ui_state.RunFinishedEvent(succeeded=2, finished_at_label="25.09.2026 14:05")
    )
    assert state.last_success_label == "25.09.2026 14:05"

    state = ui_state.reduce(state, ui_state.RunStartedEvent())
    state = ui_state.reduce(
        state, ui_state.RunFinishedEvent(succeeded=0, failed=1, finished_at_label="25.09.2026 15:05")
    )
    assert state.last_success_label == "25.09.2026 14:05"

    # The no-argument form still works.
    state = ui_state.reduce(state, ui_state.RunFinishedEvent())
    assert state.last_success_label == "25.09.2026 14:05"


def test_terminal_check_keeps_the_stage_list() -> None:
    state = _started()
    state = ui_state.reduce(state, ui_state.StageProgressEvent(ui_state.STAGE_CONNECT, ui_state.STAGE_DONE))
    state = ui_state.reduce(
        state, ui_state.AccountProgressEvent(mt_login=None, phase=ui_state.PHASE_TERMINAL_CHECK)
    )
    assert ui_state.STAGE_CONNECT in state.stages


def test_new_events_never_mutate_the_input_state() -> None:
    state = _started()
    state = ui_state.reduce(state, ui_state.StageProgressEvent(ui_state.STAGE_CONNECT, ui_state.STAGE_IN_PROGRESS))
    snapshot_stages = dict(state.stages)
    snapshot_rows = dict(state.rows)

    for event in (
        ui_state.StageProgressEvent(ui_state.STAGE_CONNECT, ui_state.STAGE_DONE),
        ui_state.CancelRequestedEvent(),
        ui_state.RunCancelledEvent(),
        ui_state.RunFailedEvent(kind=ui_state.RUN_ERROR_INTERNAL),
        ui_state.RunStartedEvent(),
        ui_state.RunFinishedEvent(succeeded=1, finished_at_label="x"),
    ):
        ui_state.reduce(state, event)

    assert state.stages == snapshot_stages
    assert state.rows == snapshot_rows
    assert state.stages[ui_state.STAGE_CONNECT].status == ui_state.STAGE_IN_PROGRESS


def test_stage_strings_mirror_sync_module() -> None:
    from agent import sync

    for name in (
        "STAGE_FIND_TERMINAL", "STAGE_LAUNCH_TERMINAL", "STAGE_CONNECT", "STAGE_FETCH_ACCOUNTS",
        "STAGE_IN_PROGRESS", "STAGE_DONE", "STAGE_FAILED",
    ):
        assert getattr(ui_state, name) == getattr(sync, name)


# ---------------------------------------------------------------------------
# quick 260926-ieo — tray status: tooltip priority, attention kinds, balloons
# ---------------------------------------------------------------------------


def _ready() -> "ui_state.UiState":
    return ui_state.UiState(screen=ui_state.SCREEN_READY)


def test_tooltip_always_starts_with_treedger_and_is_at_most_127_chars() -> None:
    text = ui_state.tray_tooltip_text(_ready())
    assert text.startswith("Treedger")
    assert len(text) <= 127


def test_tooltip_priority_1_protocol_too_old() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_READY, notices=frozenset({ui_state.NOTICE_PROTOCOL_TOO_OLD}))
    assert ui_state.tray_tooltip_text(state) == "Treedger: Программа устарела — скачайте новую версию"


def test_tooltip_priority_2_pairing() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_PAIRING)
    assert ui_state.tray_tooltip_text(state) == "Treedger: Нужна привязка — откройте окно"


def test_tooltip_priority_3_no_terminal() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_NO_TERMINAL)
    assert ui_state.tray_tooltip_text(state) == "Treedger: MetaTrader 5 не найден"


def test_tooltip_priority_3_no_terminal_via_notice_even_on_ready_screen() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_READY, notices=frozenset({ui_state.NOTICE_NO_TERMINAL}))
    assert ui_state.tray_tooltip_text(state) == "Treedger: MetaTrader 5 не найден"


def test_tooltip_priority_4_running() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_RUNNING)
    assert ui_state.tray_tooltip_text(state) == "Treedger: Синхронизация…"


def test_tooltip_priority_5_run_error() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_READY, run_error="что-то пошло не так")
    assert ui_state.tray_tooltip_text(state) == "Treedger: Ошибка синхронизации — откройте окно"


def test_tooltip_priority_6_algo_trading_off() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_READY, notices=frozenset({ui_state.NOTICE_ALGO_TRADING_OFF}))
    assert "Алготрейдинг" in ui_state.tray_tooltip_text(state)


def test_tooltip_priority_7_failed_count() -> None:
    state = ui_state.UiState(
        screen=ui_state.SCREEN_READY,
        rows={"1": ui_state.AccountRowState(mt_login="1", phase=ui_state.PHASE_FAILED)},
    )
    assert ui_state.tray_tooltip_text(state) == "Treedger: Ошибки по счетам: 1 — откройте окно"


def test_tooltip_priority_8_last_success_label() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_READY, last_success_label="24.09.2026 10:00")
    assert ui_state.tray_tooltip_text(state) == "Treedger: Последняя синхронизация: 24.09.2026 10:00"


def test_tooltip_priority_9_otherwise_waiting() -> None:
    assert ui_state.tray_tooltip_text(_ready()) == "Treedger: Ожидание первой синхронизации"


def test_tooltip_never_contains_a_secret_looking_pairing_error() -> None:
    secret = "tok_super_secret_value_12345"
    state = ui_state.UiState(screen=ui_state.SCREEN_PAIRING, pairing_error=secret)
    assert secret not in ui_state.tray_tooltip_text(state)


def test_attention_kinds_terminal_not_found_from_screen() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_NO_TERMINAL)
    assert ui_state.ATTENTION_TERMINAL_NOT_FOUND in ui_state.attention_kinds(state, login_failed=False)


def test_attention_kinds_terminal_not_found_from_notice() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_READY, notices=frozenset({ui_state.NOTICE_NO_TERMINAL}))
    assert ui_state.ATTENTION_TERMINAL_NOT_FOUND in ui_state.attention_kinds(state, login_failed=False)


def test_attention_kinds_login_failed_flag() -> None:
    state = _ready()
    kinds = ui_state.attention_kinds(state, login_failed=True)
    assert ui_state.ATTENTION_LOGIN_FAILED in kinds
    assert ui_state.ATTENTION_TERMINAL_NOT_FOUND not in kinds


def test_attention_kinds_empty_when_nothing_applies() -> None:
    assert ui_state.attention_kinds(_ready(), login_failed=False) == frozenset()


def test_attention_balloon_login_failed_names_both_logins_and_stays_within_limits() -> None:
    title, text = ui_state.attention_balloon(ui_state.ATTENTION_LOGIN_FAILED, mt_logins=["123", "456"])
    assert len(title) <= 63
    assert len(text) <= 255
    assert "123" in text
    assert "456" in text
    assert "tok_" not in text  # no token ever appears in a balloon


def test_attention_balloon_terminal_not_found_reuses_notice_text() -> None:
    title, text = ui_state.attention_balloon(ui_state.ATTENTION_TERMINAL_NOT_FOUND)
    assert ui_state.NOTICE_TEXT[ui_state.NOTICE_NO_TERMINAL] in text
    assert len(title) <= 63
    assert len(text) <= 255


# ---------------------------------------------------------------------------
# Owner follow-up to quick 260926-ieo — two more balloon states: token revoked,
# «Алготрейдинг» off.
# ---------------------------------------------------------------------------


def test_attention_kinds_token_revoked_from_notice() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_PAIRING, notices=frozenset({ui_state.NOTICE_TOKEN_REVOKED}))
    assert ui_state.ATTENTION_TOKEN_REVOKED in ui_state.attention_kinds(state, login_failed=False)


def test_attention_kinds_algo_trading_off_from_notice() -> None:
    state = ui_state.UiState(screen=ui_state.SCREEN_READY, notices=frozenset({ui_state.NOTICE_ALGO_TRADING_OFF}))
    assert ui_state.ATTENTION_ALGO_TRADING_OFF in ui_state.attention_kinds(state, login_failed=False)


def test_attention_kinds_all_four_can_combine() -> None:
    state = ui_state.UiState(
        screen=ui_state.SCREEN_NO_TERMINAL,
        notices=frozenset({ui_state.NOTICE_TOKEN_REVOKED, ui_state.NOTICE_ALGO_TRADING_OFF}),
    )
    kinds = ui_state.attention_kinds(state, login_failed=True)
    assert kinds == frozenset(
        {
            ui_state.ATTENTION_TERMINAL_NOT_FOUND,
            ui_state.ATTENTION_LOGIN_FAILED,
            ui_state.ATTENTION_TOKEN_REVOKED,
            ui_state.ATTENTION_ALGO_TRADING_OFF,
        }
    )


def test_attention_balloon_token_revoked_names_no_secret_and_stays_within_limits() -> None:
    title, text = ui_state.attention_balloon(ui_state.ATTENTION_TOKEN_REVOKED)
    assert len(title) <= 63
    assert len(text) <= 255
    assert "tok_" not in text
    assert "привяж" in text or "привязк" in text  # actionable, tells the person what to do


def test_attention_balloon_algo_trading_off_reuses_the_window_instruction() -> None:
    title, text = ui_state.attention_balloon(ui_state.ATTENTION_ALGO_TRADING_OFF)
    assert text == ui_state.NOTICE_TEXT[ui_state.NOTICE_ALGO_TRADING_OFF]
    assert len(title) <= 63
    assert len(text) <= 255


def test_module_imports_no_gui_toolkit_still_passes_after_tray_status_additions() -> None:
    import ast
    import pathlib

    source_path = pathlib.Path(__file__).resolve().parent.parent / "ui_state.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "tkinter"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "tkinter"
