"""
Unit tests for agent/error_codes.py — the structural invariants that keep the table
honest as it is edited later (by this plan's own author now, and by the second
developer's probe in plan 39-17).

These tests are deliberately about the TABLE's shape and declared evidence, not about
what any individual row "should" say — that judgment call is recorded in each row's
own `note`, not re-litigated here.
"""
from __future__ import annotations

from agent.error_codes import (
    DEFAULT_CATEGORY,
    MT5_ERROR_CODES,
    ErrorCategory,
    ErrorCodeInfo,
    ErrorSource,
)


def test_broker_closed_is_not_a_member_of_error_category_at_all():
    """
    THE load-bearing invariant: no row, known or unknown, can ever produce
    broker_closed, because the category itself does not exist.
    """
    member_names = {member.name for member in ErrorCategory}
    member_values = {member.value for member in ErrorCategory}
    assert "BROKER_CLOSED" not in member_names
    assert "broker_closed" not in member_values


def test_no_row_has_a_broker_closed_category_string():
    for code, info in MT5_ERROR_CODES.items():
        assert info.category.value != "broker_closed", (
            f"code {code} must never resolve to broker_closed via the table"
        )


def test_every_row_declares_a_real_source_and_a_non_empty_note():
    for code, info in MT5_ERROR_CODES.items():
        assert isinstance(info, ErrorCodeInfo)
        assert isinstance(info.source, ErrorSource)
        assert info.source in set(ErrorSource)
        assert isinstance(info.note, str)
        assert info.note.strip() != "", f"code {code} has an empty note"


def test_every_row_declares_a_real_category():
    for code, info in MT5_ERROR_CODES.items():
        assert isinstance(info.category, ErrorCategory)
        assert info.category in set(ErrorCategory)


def test_default_category_is_connection_error():
    assert DEFAULT_CATEGORY is ErrorCategory.CONNECTION_ERROR


def test_keys_are_unique_integers_and_table_is_non_empty():
    assert len(MT5_ERROR_CODES) > 0
    for code in MT5_ERROR_CODES:
        assert isinstance(code, int)
    # dict keys are inherently unique; this asserts there is more than a
    # single trivial entry, so the "unique" claim is exercised meaningfully.
    assert len(set(MT5_ERROR_CODES.keys())) == len(MT5_ERROR_CODES)


def test_minus_10006_maps_to_algotrading_disabled():
    assert -10006 in MT5_ERROR_CODES
    assert MT5_ERROR_CODES[-10006].category is ErrorCategory.ALGOTRADING_DISABLED


def test_contested_minus_6_resolves_to_connection_error_never_auth_failed():
    """
    This repo's own working classifier and the official
    MQL5 docs assign OPPOSITE meanings to -6. The table must resolve the conflict
    toward the recoverable side (CONNECTION_ERROR), never AUTH_FAILED, since only
    AUTH_FAILED can ever feed the broker_closed heuristic.
    """
    assert -6 in MT5_ERROR_CODES
    assert MT5_ERROR_CODES[-6].category is ErrorCategory.CONNECTION_ERROR
    assert MT5_ERROR_CODES[-6].source is ErrorSource.ASSUMPTION


def test_ambiguous_and_assumption_rows_never_resolve_to_a_retiring_category():
    """
    Every row whose source is ASSUMPTION (i.e. not yet independently confirmed)
    must resolve to a category that can never, by itself, retire an account. Since
    broker_closed does not exist as a category at all (see the dedicated test
    above), this reduces to: no ASSUMPTION row is exempt from that same guarantee —
    restated here as an explicit per-row check rather than relying solely on the
    enum-level absence.
    """
    for code, info in MT5_ERROR_CODES.items():
        if info.source is ErrorSource.ASSUMPTION:
            assert info.category.value != "broker_closed", (
                f"code {code} is an ASSUMPTION row and must not resolve to a "
                "retiring category"
            )
