"""
Unit tests for agent/positions.py — the verbatim-ported statistics core.

All deal fixtures use plain dicts — no MetaTrader5 import required anywhere in this file
or the module under test. This suite runs green on a machine with no MetaTrader5 terminal
and no MetaTrader5 pip package installed.

Deal dict keys mirror exactly what `get_history_deals()` produces on the donor side:
  time, type, entry, profit, commission, swap, position_id, symbol, volume, price
Fixtures below are built with these exact key names so the tests double as documentation
of the wire shape the agent works from.

Constants (same as in agent/positions.py):
  DEAL_TYPE_BALANCE = 2   — deposit / withdrawal
  DEAL_ENTRY_OUT    = 1   — closed trade (OUT)
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from agent.positions import (
    compute_deposit_adjusted_return,
    compute_period_returns,
    build_equity_points,
    compute_aggregate_stats,
    normalize_to_utc,
    compute_session,
    group_deals_into_positions,
    DEAL_TYPE_BALANCE,
    DEAL_TYPE_BUY,
    DEAL_TYPE_SELL,
    DEAL_ENTRY_IN,
    DEAL_ENTRY_OUT,
    VOLUME_EPSILON,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _balance(time: int, profit: float) -> dict:
    return {"time": time, "type": DEAL_TYPE_BALANCE, "entry": 0, "profit": profit,
            "commission": 0.0, "swap": 0.0}


def _trade(time: int, profit: float, commission: float = 0.0, swap: float = 0.0) -> dict:
    return {"time": time, "type": 0, "entry": DEAL_ENTRY_OUT, "profit": profit,
            "commission": commission, "swap": swap}


def _position_deal(
    position_id: int,
    time: int,
    entry: int,
    deal_type: int = DEAL_TYPE_BUY,
    symbol: str = "EURUSD",
    volume: float = 1.0,
    price: float = 1.1000,
    profit: float = 0.0,
    commission: float = 0.0,
    swap: float = 0.0,
) -> dict:
    """Build a widened deal dict (position_id/symbol/volume/price) matching the exact wire
    shape `get_history_deals()` produces."""
    return {
        "time": time,
        "type": deal_type,
        "entry": entry,
        "profit": profit,
        "commission": commission,
        "swap": swap,
        "position_id": position_id,
        "symbol": symbol,
        "volume": volume,
        "price": price,
    }


# ---------------------------------------------------------------------------
# VOLUME_EPSILON is publicly exposed (renamed from the donor's private _VOLUME_EPSILON)
# ---------------------------------------------------------------------------

def test_volume_epsilon_is_public_and_matches_donor_value():
    assert VOLUME_EPSILON == pytest.approx(1e-9)


# ---------------------------------------------------------------------------
# compute_deposit_adjusted_return
# ---------------------------------------------------------------------------

def test_deposit_adjusted_return_simple():
    """balance +1000, closed +100, closed -50 → return_all == 5.0"""
    deals = [
        _balance(1000, 1000.0),
        _trade(2000, 100.0),
        _trade(3000, -50.0),
    ]
    result = compute_deposit_adjusted_return(deals)
    assert result["return_all"] == pytest.approx(5.0, abs=0.01)
    assert result["deposit_base"] == pytest.approx(1000.0)
    assert result["first_trade_at"] == 2000  # Unix ts of first closed trade


def test_deposit_adjusted_handles_withdrawal():
    """balance +1000, closed +200, balance -500 → no crash; return computed."""
    deals = [
        _balance(1000, 1000.0),
        _trade(2000, 200.0),
        _balance(3000, -500.0),
    ]
    result = compute_deposit_adjusted_return(deals)
    # deposit_base = 1000 + (-500) = 500
    # running_balance after trade = 1200, after withdrawal = 700
    # return_all = (700 - 500) / 500 * 100 = 40.0
    assert result["deposit_base"] == pytest.approx(500.0)
    assert result["return_all"] == pytest.approx(40.0, abs=0.01)


def test_return_zero_when_no_deposit():
    """Deals with no DEAL_TYPE=2 balance op → return_all == 0.0 (divide-by-zero guard)."""
    deals = [_trade(1000, 100.0)]
    result = compute_deposit_adjusted_return(deals)
    assert result["return_all"] == 0.0


def test_deposit_adjusted_return_empty_deal_list_has_zero_deposit_base():
    """compute_deposit_adjusted_return over an empty deal list returns deposit_base 0 and
    never divides by zero."""
    result = compute_deposit_adjusted_return([])
    assert result["deposit_base"] == 0.0
    assert result["return_all"] == 0.0
    assert result["running_balance"] == 0.0
    assert result["first_trade_at"] is None
    assert result["closed_trades"] == []


# ---------------------------------------------------------------------------
# compute_period_returns
# ---------------------------------------------------------------------------

def test_period_returns_window():
    """Trades spread across dates → return_1m sums only trades within last 30 days."""
    now = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    old_ts = int((now - timedelta(days=90)).timestamp())   # 90 days ago — outside 1m
    new_ts = int((now - timedelta(days=10)).timestamp())   # 10 days ago — inside 1m

    closed_trades = [
        {"time": old_ts, "pnl": 500.0, "running_balance": 1500.0},
        {"time": new_ts, "pnl": 100.0, "running_balance": 1600.0},
    ]
    deposit_base = 1000.0
    result = compute_period_returns(closed_trades, deposit_base, now_utc=now)

    # return_1m: only 100 / 1000 * 100 = 10.0
    assert result["return_1m"] == pytest.approx(10.0, abs=0.01)
    # return_1y: all trades = 600 / 1000 * 100 = 60.0
    assert result["return_1y"] == pytest.approx(60.0, abs=0.01)


def test_period_returns_empty_deal_list_returns_zeros():
    """compute_period_returns over an empty closed-trades list returns zeros rather than
    raising or producing NaN."""
    result = compute_period_returns([], deposit_base=0.0)
    assert result == {"return_1m": 0.0, "return_3m": 0.0, "return_6m": 0.0, "return_1y": 0.0}


# ---------------------------------------------------------------------------
# build_equity_points
# ---------------------------------------------------------------------------

def test_equity_points_percent_only():
    """build_equity_points returns list of {ts, equity_pct} with % values, monotonic ts."""
    deposit_base = 1000.0
    closed_trades = [
        {"time": 1000, "pnl": 100.0, "running_balance": 1100.0},
        {"time": 2000, "pnl": -50.0, "running_balance": 1050.0},
        {"time": 3000, "pnl": 200.0, "running_balance": 1250.0},
    ]
    points = build_equity_points(closed_trades, deposit_base)

    assert len(points) == 3
    # Check % values
    assert points[0]["equity_pct"] == pytest.approx(10.0, abs=0.01)   # (1100-1000)/1000*100
    assert points[1]["equity_pct"] == pytest.approx(5.0, abs=0.01)    # (1050-1000)/1000*100
    assert points[2]["equity_pct"] == pytest.approx(25.0, abs=0.01)   # (1250-1000)/1000*100
    # Timestamps are monotonically increasing
    ts_list = [p["ts"] for p in points]
    assert ts_list == sorted(ts_list)
    # No money amounts — equity_pct must be a float, not a large money value
    for p in points:
        assert abs(p["equity_pct"]) < 10000  # sanity: % not money


def test_floating_pnl_point_appended():
    """When floating_equity_pct is supplied, an extra rightmost point is appended."""
    deposit_base = 1000.0
    closed_trades = [
        {"time": 1000, "pnl": 100.0, "running_balance": 1100.0},
    ]
    points = build_equity_points(closed_trades, deposit_base, floating_equity_pct=15.0)
    assert len(points) == 2
    last = points[-1]
    assert last["equity_pct"] == pytest.approx(15.0)
    # ts of floating point must be >= last closed trade ts
    assert last["ts"] >= points[0]["ts"]


def test_build_equity_points_never_carries_a_money_amount():
    """The module's own money-boundary rule: equity is always % — no field returned by
    build_equity_points is a currency amount, even with a large deposit base."""
    deposit_base = 1_000_000.0
    closed_trades = [
        {"time": 1000, "pnl": 250_000.0, "running_balance": 1_250_000.0},
    ]
    points = build_equity_points(closed_trades, deposit_base)
    assert points[0]["equity_pct"] == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# normalize_to_utc
# ---------------------------------------------------------------------------

def test_normalize_to_utc():
    """normalize_to_utc(broker_ts=1700000000, offset_seconds=7200) returns UTC datetime 2h earlier."""
    broker_ts = 1700000000
    offset = 7200  # UTC+2

    result = normalize_to_utc(broker_ts, offset)

    expected = datetime.fromtimestamp(broker_ts - offset, tz=timezone.utc)
    assert result == expected
    assert result.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# compute_aggregate_stats
# ---------------------------------------------------------------------------

def test_drawdown_winrate_profitfactor():
    """Known trade set → expected max_drawdown %, win_rate, profit_factor, avg_rr, total_trades."""
    # Deposit 1000, trades: +200, +100, -150, +50
    # running_balance: 1200, 1300, 1150, 1200
    # peak = 1300 → trough = 1150 → drawdown = (1300-1150)/1000*100 = 15.0%
    # wins: 3, losses: 1 → win_rate = 75.0
    # gross_profit = 350, gross_loss = 150 → profit_factor = 350/150 ≈ 2.33
    # avg_rr: not easily asserted without known RR values; just assert it's a float
    now_utc = datetime(2024, 6, 1, tzinfo=timezone.utc)
    first_trade_ts = int((now_utc - timedelta(days=60)).timestamp())

    deals = [
        _balance(first_trade_ts - 100, 1000.0),
        _trade(first_trade_ts, 200.0),
        _trade(first_trade_ts + 1000, 100.0),
        _trade(first_trade_ts + 2000, -150.0),
        _trade(first_trade_ts + 3000, 50.0),
    ]

    result = compute_aggregate_stats(deals, account_equity=1200.0, now_utc=now_utc)

    assert result["total_trades"] == 4
    assert result["win_rate"] == pytest.approx(75.0, abs=0.01)
    assert result["max_drawdown"] == pytest.approx(15.0, abs=0.01)
    assert result["profit_factor"] == pytest.approx(350.0 / 150.0, abs=0.01)
    assert isinstance(result["avg_rr"], float)
    assert result["trading_period"] >= 60  # days


def test_aggregate_stats_every_field_is_percentage_ratio_or_count():
    """compute_aggregate_stats returns every field the aggregate table needs, and every one
    of them is a percentage, a ratio or a count — no field carries a money amount."""
    import math

    now_utc = datetime(2024, 6, 1, tzinfo=timezone.utc)
    deals = [_balance(1_700_000_000, 10_000.0)]

    result = compute_aggregate_stats(deals, account_equity=10_000.0, now_utc=now_utc)

    expected_keys = {
        "return_all", "return_1m", "return_3m", "return_6m", "return_1y",
        "max_drawdown", "win_rate", "profit_factor", "avg_rr",
        "total_trades", "trading_period", "first_trade_at",
    }
    assert set(result.keys()) == expected_keys
    for key in expected_keys - {"first_trade_at"}:
        assert isinstance(result[key], (int, float))
        assert math.isfinite(result[key])


def test_zero_closed_trades_produces_finite_all_zero_stats():
    """Funded account, one deposit, zero closed trades → all-zero, all-finite stats."""
    import json
    import math

    now_utc = datetime(2024, 6, 1, tzinfo=timezone.utc)
    deposit_ts = int((now_utc - timedelta(days=10)).timestamp())

    # Exactly the shape of the reported account: 1 deposit, 0 closed trades.
    deals = [_balance(deposit_ts, 10_000.0)]

    result = compute_aggregate_stats(deals, account_equity=10_000.0, now_utc=now_utc)

    assert result["total_trades"] == 0
    assert result["return_all"] == 0.0
    assert result["max_drawdown"] == 0.0
    assert result["win_rate"] == 0.0
    assert result["avg_rr"] == 0.0
    assert result["trading_period"] == 0
    assert result["first_trade_at"] is None

    # The regression this whole test exists for: profit_factor must be finite.
    assert result["profit_factor"] == 0.0
    assert math.isfinite(result["profit_factor"])

    json.dumps(result, allow_nan=False)


def test_profit_factor_is_finite_when_there_are_no_losing_trades():
    """All-wins sample → finite profit_factor, never inf (same write-breaking bug)."""
    import math

    now_utc = datetime(2024, 6, 1, tzinfo=timezone.utc)
    first_ts = int((now_utc - timedelta(days=5)).timestamp())

    deals = [
        _balance(first_ts - 100, 1000.0),
        _trade(first_ts, 200.0),
        _trade(first_ts + 1000, 100.0),
    ]

    result = compute_aggregate_stats(deals, account_equity=1300.0, now_utc=now_utc)

    assert result["total_trades"] == 2
    assert result["win_rate"] == pytest.approx(100.0, abs=0.01)
    assert math.isfinite(result["profit_factor"])


# ---------------------------------------------------------------------------
# compute_session (locked windows; NY wins the 12:00-15:00 overlap)
# ---------------------------------------------------------------------------

def test_compute_session():
    def _at(hour: int, minute: int = 0) -> datetime:
        return datetime(2024, 6, 1, hour, minute, 0, tzinfo=timezone.utc)

    # NY wins the London/NY overlap (12:00-15:00) — must be checked FIRST
    assert compute_session(_at(12, 0)) == "NY"
    assert compute_session(_at(14, 59)) == "NY"
    assert compute_session(_at(15, 0)) == "NY"
    assert compute_session(_at(19, 59)) == "NY"

    # Frankfurt: 06:00-07:00
    assert compute_session(_at(6, 30)) == "Frankfurt"

    # London: 07:00-12:00
    assert compute_session(_at(7, 0)) == "London"
    assert compute_session(_at(11, 59)) == "London"

    # Asia: 23:00-06:00 (wraps midnight)
    assert compute_session(_at(23, 0)) == "Asia"
    assert compute_session(_at(5, 59)) == "Asia"

    # 20:00-23:00 UTC -> no session
    assert compute_session(_at(20, 30)) is None
    assert compute_session(_at(22, 59)) is None


def test_compute_session_maps_to_exactly_one_of_four_or_none():
    """compute_session maps every UTC hour to exactly one of Asia/London/NY/Frankfurt or
    None — a full sweep across all 24 hours never returns anything outside that set."""
    allowed = {"Asia", "London", "NY", "Frankfurt", None}
    for hour in range(24):
        result = compute_session(datetime(2024, 6, 1, hour, 0, 0, tzinfo=timezone.utc))
        assert result in allowed


# ---------------------------------------------------------------------------
# group_deals_into_positions (round-turn mapping; partial closes,
# reversals, still-open exclusion)
# ---------------------------------------------------------------------------

def test_group_deals_into_positions():
    """Covers a partial-close case
    AND a reversal case in one pass — one closed position -> one dict,
    still-open positions excluded, direction from the opening deal,
    entry/exit volume-weighted."""
    deals = [
        # --- Partial-close case: position 100 opened in full (1.0 lot),
        #     closed via TWO partial-close deals (0.5 + 0.5) -----------------
        _position_deal(100, 1_700_000_000, DEAL_ENTRY_IN, deal_type=DEAL_TYPE_BUY,
                        volume=1.0, price=1.1000),
        _position_deal(100, 1_700_003_600, DEAL_ENTRY_OUT, deal_type=DEAL_TYPE_BUY,
                        volume=0.5, price=1.1050, profit=25.0),
        _position_deal(100, 1_700_007_200, DEAL_ENTRY_OUT, deal_type=DEAL_TYPE_BUY,
                        volume=0.5, price=1.1100, profit=50.0),
        # --- Reversal case: position 400 (BUY) is fully closed by the
        #     reversal deal; position 401 (the new SELL position opened by
        #     that same reversal event) has only an opening deal and stays
        #     open -> must be excluded from the output ---------------------
        _position_deal(400, 1_700_000_000, DEAL_ENTRY_IN, deal_type=DEAL_TYPE_BUY,
                        volume=1.0, price=1.1000),
        _position_deal(400, 1_700_003_600, DEAL_ENTRY_OUT, deal_type=DEAL_TYPE_BUY,
                        volume=1.0, price=1.0950, profit=-50.0),
        _position_deal(401, 1_700_003_600, DEAL_ENTRY_IN, deal_type=DEAL_TYPE_SELL,
                        volume=1.0, price=1.0950),
    ]

    positions = group_deals_into_positions(deals)

    # Only the two CLOSED positions (100, 400) appear; 401 (still open) excluded
    assert len(positions) == 2
    by_id = {p["mt5_position_id"]: p for p in positions}
    assert set(by_id.keys()) == {100, 400}

    partial = by_id[100]
    assert partial["direction"] == "BUY"
    assert partial["pair"] == "EURUSD"
    assert partial["volume"] == pytest.approx(1.0)
    assert partial["entry_price"] == pytest.approx(1.1000)
    # exit_price = (1.1050*0.5 + 1.1100*0.5) / 1.0
    assert partial["exit_price"] == pytest.approx(1.1075)
    assert partial["result"] == pytest.approx(75.0)

    reversal = by_id[400]
    assert reversal["direction"] == "BUY"  # from the opening deal, not the closing one
    assert reversal["exit_price"] == pytest.approx(1.0950)
    assert reversal["result"] == pytest.approx(-50.0)


def test_group_deals_into_positions_still_open_excluded():
    """A position with only an opening deal (no closing deal at all) is
    still open and must be excluded entirely from the output."""
    deals = [
        _position_deal(200, 1_700_000_000, DEAL_ENTRY_IN, deal_type=DEAL_TYPE_BUY,
                        volume=1.0, price=1.1000),
    ]

    assert group_deals_into_positions(deals) == []


def test_group_deals_into_positions_partial_only_still_excluded():
    """A position with a closing deal that only partially reduces volume
    (opening 1.0, closing 0.5) is still open in-progress — excluded."""
    deals = [
        _position_deal(300, 1_700_000_000, DEAL_ENTRY_IN, deal_type=DEAL_TYPE_SELL,
                        volume=1.0, price=1.2000),
        _position_deal(300, 1_700_003_600, DEAL_ENTRY_OUT, deal_type=DEAL_TYPE_SELL,
                        volume=0.5, price=1.1950, profit=25.0),
    ]

    assert group_deals_into_positions(deals) == []


def test_group_deals_into_positions_session_persisted():
    """The returned dict's session field matches compute_session(opened_at)."""
    deals = [
        _position_deal(500, int(datetime(2024, 6, 1, 13, 0, tzinfo=timezone.utc).timestamp()),
                        DEAL_ENTRY_IN, deal_type=DEAL_TYPE_BUY, volume=1.0, price=1.1000),
        _position_deal(500, int(datetime(2024, 6, 1, 14, 0, tzinfo=timezone.utc).timestamp()),
                        DEAL_ENTRY_OUT, deal_type=DEAL_TYPE_BUY, volume=1.0, price=1.1050,
                        profit=50.0),
    ]

    positions = group_deals_into_positions(deals)

    assert len(positions) == 1
    assert positions[0]["session"] == "NY"  # opened_at hour=13 -> NY (overlap wins)


def test_zero_closed_trades_yields_no_journal_positions():
    """The lone BALANCE deal must not be grouped into a journal trade — balance-
    operation deals never become positions."""
    # Real MT5 balance deals carry position_id 0 and no closing leg.
    balance_deal = {
        "time": 1_700_000_000,
        "type": DEAL_TYPE_BALANCE,
        "entry": DEAL_ENTRY_IN,
        "profit": 10_000.0,
        "commission": 0.0,
        "swap": 0.0,
        "position_id": 0,
        "symbol": "",
        "volume": 0.0,
        "price": 0.0,
    }

    assert group_deals_into_positions([balance_deal]) == []


# ---------------------------------------------------------------------------
# Four named regression cases (Task 3) — the donor's own history proves these matter
# ---------------------------------------------------------------------------

def test_regression_scale_in_entry_price_is_volume_weighted_not_first_leg():
    """1. A position built from a scale-in (two opening legs at different prices) and a
    single close — assert the entry price equals the volume-weighted average, not the
    first leg's price."""
    deals = [
        _position_deal(600, 1_700_000_000, DEAL_ENTRY_IN, deal_type=DEAL_TYPE_BUY,
                        volume=1.0, price=1.1000),
        _position_deal(600, 1_700_001_800, DEAL_ENTRY_IN, deal_type=DEAL_TYPE_BUY,
                        volume=1.0, price=1.1200),
        _position_deal(600, 1_700_003_600, DEAL_ENTRY_OUT, deal_type=DEAL_TYPE_BUY,
                        volume=2.0, price=1.1300, profit=100.0),
    ]

    positions = group_deals_into_positions(deals)

    assert len(positions) == 1
    position = positions[0]
    # volume-weighted average of (1.1000*1.0 + 1.1200*1.0) / 2.0 = 1.1100
    # NOT the first leg's price (1.1000)
    assert position["entry_price"] == pytest.approx(1.1100)
    assert position["entry_price"] != pytest.approx(1.1000)
    assert position["volume"] == pytest.approx(2.0)


def test_regression_partial_close_strictly_less_volume_not_emitted():
    """2. A partially-closed position where closing volume is strictly less than opening
    volume — assert it is NOT emitted."""
    deals = [
        _position_deal(700, 1_700_000_000, DEAL_ENTRY_IN, deal_type=DEAL_TYPE_SELL,
                        volume=2.0, price=1.3000),
        _position_deal(700, 1_700_003_600, DEAL_ENTRY_OUT, deal_type=DEAL_TYPE_SELL,
                        volume=1.0, price=1.2950, profit=50.0),
    ]

    assert group_deals_into_positions(deals) == []


def test_regression_balance_only_deal_list_zero_positions_and_summed_deposit_base():
    """3. A deal list containing only balance operations — assert zero positions and a
    deposit_base equal to the summed deposits."""
    plain_balances = [
        _balance(1000, 1000.0),
        _balance(2000, 500.0),
        _balance(3000, -200.0),
    ]
    result = compute_deposit_adjusted_return(plain_balances)
    assert result["deposit_base"] == pytest.approx(1300.0)  # 1000 + 500 - 200

    # Real MT5 balance deals also carry position_id=0/symbol=""/volume=0.0/price=0.0 (the
    # widened shape group_deals_into_positions expects) — assert none of them become
    # journal positions either.
    widened_balances = [
        {**_balance(1000, 1000.0), "position_id": 0, "symbol": "", "volume": 0.0, "price": 0.0},
        {**_balance(2000, 500.0), "position_id": 0, "symbol": "", "volume": 0.0, "price": 0.0},
        {**_balance(3000, -200.0), "position_id": 0, "symbol": "", "volume": 0.0, "price": 0.0},
    ]
    assert group_deals_into_positions(widened_balances) == []


def test_regression_short_position_closed_by_buy_deal_direction_is_sell():
    """4. A short position whose closing deal type is BUY — assert the emitted direction is
    SELL, proving direction came from the opening deal."""
    deals = [
        _position_deal(800, 1_700_000_000, DEAL_ENTRY_IN, deal_type=DEAL_TYPE_SELL,
                        volume=1.0, price=1.2000),
        _position_deal(800, 1_700_003_600, DEAL_ENTRY_OUT, deal_type=DEAL_TYPE_BUY,
                        volume=1.0, price=1.1950, profit=50.0),
    ]

    positions = group_deals_into_positions(deals)

    assert len(positions) == 1
    assert positions[0]["direction"] == "SELL"
