"""
Unit tests for agent/errors.py — the classifier logic.

Nothing here imports MetaTrader5; every code is passed as a plain int, matching how
`agent/mt5_bridge.py`'s `last_error_tuple()` output is expected to be unpacked by its
caller before reaching this module.

Plan 39-20 (39-CONTEXT.md D-33): the sustained-failure `broker_closed` heuristic that
used to live in `classify_outcome()` is DELETED, not merely unfed — see
`agent/errors.py`'s own comment on the AUTH_FAILED branch for why. This file's job
changed to match: instead of exercising the heuristic's three guard conditions (the
threshold boundary, the connection/timeout immunity, the prior-successful-sync
requirement), it now proves the heuristic's ABSENCE structurally — no combination of
`classify_outcome()`'s arguments, across the FULL cartesian product of everything it
accepts, can ever produce `"broker_closed"`. A hand-picked sample would only prove
the deleted code used to behave correctly, which is no longer the property that
matters.
"""
from __future__ import annotations

import itertools

from agent.error_codes import DEFAULT_CATEGORY, ErrorCategory
from agent.errors import (
    OUTCOME_ALGOTRADING_DISABLED,
    OUTCOME_AUTH_FAILED,
    OUTCOME_OK,
    OUTCOME_SERVER_UNAVAILABLE,
    OUTCOME_TIMEOUT,
    classify_login_error,
    classify_outcome,
)


# ---------------------------------------------------------------------------
# classify_login_error
# ---------------------------------------------------------------------------

def test_known_code_maps_to_its_table_category():
    assert classify_login_error(-10004) is ErrorCategory.AUTH_FAILED
    assert classify_login_error(-10007) is ErrorCategory.TIMEOUT
    assert classify_login_error(-10006) is ErrorCategory.ALGOTRADING_DISABLED
    assert classify_login_error(-6) is ErrorCategory.CONNECTION_ERROR


def test_unknown_code_falls_back_to_connection_error():
    assert classify_login_error(-999999) is DEFAULT_CATEGORY
    assert classify_login_error(-999999) is ErrorCategory.CONNECTION_ERROR


def test_none_code_falls_back_to_default_category():
    assert classify_login_error(None) is DEFAULT_CATEGORY


# ---------------------------------------------------------------------------
# classify_outcome — success path
# ---------------------------------------------------------------------------

def test_successful_login_is_always_ok_regardless_of_other_args():
    outcome = classify_outcome(
        login_succeeded=True,
        error_code=-10004,  # would be auth_failed if login had failed — ignored
        consecutive_auth_failures=99,
        has_synced_successfully_before=True,
    )
    assert outcome == OUTCOME_OK


# ---------------------------------------------------------------------------
# classify_outcome — plain category pass-through
# ---------------------------------------------------------------------------

def test_timeout_category_returns_timeout_outcome():
    outcome = classify_outcome(login_succeeded=False, error_code=-10007)
    assert outcome == OUTCOME_TIMEOUT


def test_connection_category_returns_server_unavailable_outcome():
    outcome = classify_outcome(login_succeeded=False, error_code=-6)
    assert outcome == OUTCOME_SERVER_UNAVAILABLE


def test_algotrading_disabled_category_returns_its_own_outcome():
    outcome = classify_outcome(login_succeeded=False, error_code=-10006)
    assert outcome == OUTCOME_ALGOTRADING_DISABLED


# ---------------------------------------------------------------------------
# classify_outcome can NEVER return "broker_closed" — structural proof (D-33)
# ---------------------------------------------------------------------------
# Covers one representative code per real ErrorCategory (AUTH_FAILED, TIMEOUT,
# ALGOTRADING_DISABLED, CONNECTION_ERROR), plus an unknown code and a `None`
# code — every branch `classify_login_error()` can take — crossed with both
# `login_succeeded` values and a wide spread of `consecutive_auth_failures`/
# `has_synced_successfully_before` values, i.e. exactly the two now-ignored
# parameters the deleted heuristic used to turn into a verdict.

_LOGIN_SUCCEEDED_VALUES = (True, False)
_ERROR_CODE_VALUES = (-10004, -10007, -10006, -6, -999999, None)
_CONSECUTIVE_AUTH_FAILURES_VALUES = (0, 1, 2, 3, 4, 10, 999)
_HAS_SYNCED_SUCCESSFULLY_BEFORE_VALUES = (True, False)


def test_classify_outcome_can_never_return_broker_closed_for_any_input():
    combinations = list(
        itertools.product(
            _LOGIN_SUCCEEDED_VALUES,
            _ERROR_CODE_VALUES,
            _CONSECUTIVE_AUTH_FAILURES_VALUES,
            _HAS_SYNCED_SUCCESSFULLY_BEFORE_VALUES,
        )
    )
    # Sanity: the cartesian product actually has entries — an accidentally
    # empty loop would make every assertion below vacuously true.
    assert len(combinations) == (
        len(_LOGIN_SUCCEEDED_VALUES)
        * len(_ERROR_CODE_VALUES)
        * len(_CONSECUTIVE_AUTH_FAILURES_VALUES)
        * len(_HAS_SYNCED_SUCCESSFULLY_BEFORE_VALUES)
    )

    for (
        login_succeeded,
        error_code,
        consecutive_auth_failures,
        has_synced_successfully_before,
    ) in combinations:
        outcome = classify_outcome(
            login_succeeded=login_succeeded,
            error_code=error_code,
            consecutive_auth_failures=consecutive_auth_failures,
            has_synced_successfully_before=has_synced_successfully_before,
        )
        assert outcome != "broker_closed", (
            "classify_outcome returned the forbidden outcome for "
            f"login_succeeded={login_succeeded!r}, error_code={error_code!r}, "
            f"consecutive_auth_failures={consecutive_auth_failures!r}, "
            f"has_synced_successfully_before={has_synced_successfully_before!r} "
            "— the heuristic must be gone, not merely unfed (D-33)"
        )
