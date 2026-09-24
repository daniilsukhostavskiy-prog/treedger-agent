"""
Deposit-adjusted statistics engine for the Treedger local MT5 sync agent.

VERBATIM PORT from `mt5-service/services/stats_compute.py` (the donor stays in the tree
untouched as a working donor — PHASE-LOCAL-SYNC-SPEC.md §9). This is a transcription, not
a redesign: no formula improved, no field renamed, no comparison changed, no boundary
adjusted. Transcription drift — not absence — is the failure mode
`agent/tests/test_positions.py` exists to catch (39-03-PLAN.md Task 2).

The one intentional, test-driven naming change: the donor's private `_VOLUME_EPSILON` is
exposed here as the public `VOLUME_EPSILON`, since the ported test suite imports it
directly. The value is identical.

All functions accept plain deal dicts (no MetaTrader5 import) so they are unit-testable
without the MT5 terminal, and this file itself imports nothing beyond the standard
library — no MetaTrader5, no network, no filesystem, no environment read.

Deal dict keys:
  time       (int)   — Unix seconds in broker-local time (normalize separately)
  type       (int)   — DEAL_TYPE_BALANCE=2 for deposits/withdrawals; 0/1 for trades
  entry      (int)   — DEAL_ENTRY_OUT=1 for closed (OUT) trades
  profit     (float)
  commission (float)
  swap       (float)

Equity values are ALWAYS in % — never money amounts (project constraint + MT5-04).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Constants (mirror DEAL_TYPE and DEAL_ENTRY enums from the MT5 C++ API)
# ---------------------------------------------------------------------------
DEAL_TYPE_BALANCE: int = 2   # balance operation (deposit / withdrawal)
DEAL_TYPE_BUY: int = 0       # buy trade deal
DEAL_TYPE_SELL: int = 1      # sell trade deal
DEAL_ENTRY_IN: int = 0       # opening leg (IN)
DEAL_ENTRY_OUT: int = 1      # closed trade (OUT)

# D-04 locked session windows (UTC) — see compute_session() below.
VOLUME_EPSILON: float = 1e-9

# Number of seconds in common look-back windows
_SECONDS_1M: int = 30 * 86400
_SECONDS_3M: int = 90 * 86400
_SECONDS_6M: int = 180 * 86400
_SECONDS_1Y: int = 365 * 86400


# ---------------------------------------------------------------------------
# compute_deposit_adjusted_return
# ---------------------------------------------------------------------------

def compute_deposit_adjusted_return(deals: list[dict]) -> dict:
    """
    Separate balance operations (DEAL_TYPE=2) from trading P&L.
    Track a running deposit base that adjusts for deposits and withdrawals.

    Returns:
        return_all       (float) — total return % against deposit_base
        deposit_base     (float) — net deposits (positive deposited minus withdrawals)
        running_balance  (float) — current account balance after all deals
        first_trade_at   (int|None) — Unix ts of first closed trade
        closed_trades    (list[dict]) — [{time, pnl, running_balance}]
    """
    running_balance = 0.0
    deposit_base = 0.0
    first_trade_at = None
    closed_trades: list[dict] = []

    for deal in sorted(deals, key=lambda d: d["time"]):
        if deal["type"] == DEAL_TYPE_BALANCE:
            # Deposits raise the base; withdrawals lower it
            deposit_base += deal["profit"]
            running_balance += deal["profit"]
        elif deal["entry"] == DEAL_ENTRY_OUT:
            pnl = deal["profit"] + deal["commission"] + deal["swap"]
            running_balance += pnl
            if first_trade_at is None:
                first_trade_at = deal["time"]
            closed_trades.append({
                "time": deal["time"],
                "pnl": pnl,
                "running_balance": running_balance,
            })

    if deposit_base <= 0:
        return_all = 0.0
    else:
        return_all = (running_balance - deposit_base) / deposit_base * 100.0

    return {
        "return_all": round(return_all, 2),
        "deposit_base": deposit_base,
        "running_balance": running_balance,
        "first_trade_at": first_trade_at,
        "closed_trades": closed_trades,
    }


# ---------------------------------------------------------------------------
# compute_period_returns
# ---------------------------------------------------------------------------

def compute_period_returns(
    closed_trades: list[dict],
    deposit_base: float,
    now_utc: Optional[datetime] = None,
) -> dict:
    """
    Compute return % for 1-month, 3-month, 6-month, 1-year windows.
    Each window sums pnl for trades within the window / deposit_base * 100.

    Args:
        closed_trades: list of {time (Unix int), pnl, running_balance}
        deposit_base:  net deposit amount (denominator)
        now_utc:       anchor for window calculation (defaults to datetime.now(UTC))
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    now_ts = now_utc.timestamp()

    if deposit_base <= 0:
        return {"return_1m": 0.0, "return_3m": 0.0, "return_6m": 0.0, "return_1y": 0.0}

    def _sum_window(seconds: int) -> float:
        cutoff = now_ts - seconds
        return sum(t["pnl"] for t in closed_trades if t["time"] >= cutoff)

    return {
        "return_1m": round(_sum_window(_SECONDS_1M) / deposit_base * 100.0, 2),
        "return_3m": round(_sum_window(_SECONDS_3M) / deposit_base * 100.0, 2),
        "return_6m": round(_sum_window(_SECONDS_6M) / deposit_base * 100.0, 2),
        "return_1y": round(_sum_window(_SECONDS_1Y) / deposit_base * 100.0, 2),
    }


# ---------------------------------------------------------------------------
# build_equity_points
# ---------------------------------------------------------------------------

def build_equity_points(
    closed_trades: list[dict],
    deposit_base: float,
    floating_equity_pct: Optional[float] = None,
) -> list[dict]:
    """
    Build equity curve as a list of {ts, equity_pct} dicts.
    One point per closed trade (at close time), using running_balance from the trade.
    Optionally appends a floating-P&L snapshot point at the current moment.

    equity_pct = (running_balance_at_trade - deposit_base) / deposit_base * 100
    Values are % only — never money amounts.
    """
    if deposit_base <= 0:
        return []

    points: list[dict] = []
    for trade in sorted(closed_trades, key=lambda t: t["time"]):
        equity_pct = (trade["running_balance"] - deposit_base) / deposit_base * 100.0
        points.append({
            "ts": trade["time"],
            "equity_pct": round(equity_pct, 4),
        })

    if floating_equity_pct is not None:
        # Append floating P&L as the rightmost point (MT5-04)
        last_ts = points[-1]["ts"] if points else 0
        now_ts = max(int(datetime.now(timezone.utc).timestamp()), last_ts)
        points.append({
            "ts": now_ts,
            "equity_pct": round(float(floating_equity_pct), 4),
        })

    return points


# ---------------------------------------------------------------------------
# normalize_to_utc
# ---------------------------------------------------------------------------

def normalize_to_utc(broker_unix_ts: int, offset_seconds: int) -> datetime:
    """
    Convert a broker-local Unix timestamp to a UTC datetime.

    MT5 history_deals_get returns times in broker-server local time (commonly
    UTC+2/+3 for CIS brokers), NOT UTC. Subtract the offset to get true UTC.

    Args:
        broker_unix_ts:  Unix seconds as reported by the MT5 terminal
        offset_seconds:  broker UTC offset in seconds (e.g. 7200 for UTC+2)
    Returns:
        datetime in UTC timezone
    """
    utc_ts = broker_unix_ts - offset_seconds
    return datetime.fromtimestamp(utc_ts, tz=timezone.utc)


# ---------------------------------------------------------------------------
# compute_aggregate_stats
# ---------------------------------------------------------------------------

def compute_aggregate_stats(
    deals: list[dict],
    account_equity: float,
    now_utc: Optional[datetime] = None,
) -> dict:
    """
    Compute the full statistics payload for an account.

    Returns a dict matching StatsResult fields:
        return_all, return_1m, return_3m, return_6m, return_1y,
        max_drawdown, win_rate, profit_factor, avg_rr,
        total_trades, trading_period, first_trade_at
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    base = compute_deposit_adjusted_return(deals)
    deposit_base = base["deposit_base"]
    closed_trades = base["closed_trades"]
    first_trade_at = base["first_trade_at"]

    period_returns = compute_period_returns(closed_trades, deposit_base, now_utc=now_utc)

    # ---- max drawdown (peak-to-trough % on running_balance) ---
    peak = deposit_base
    max_drawdown = 0.0
    if deposit_base > 0:
        for trade in closed_trades:
            rb = trade["running_balance"]
            if rb > peak:
                peak = rb
            dd = (peak - rb) / deposit_base * 100.0
            if dd > max_drawdown:
                max_drawdown = dd

    # ---- win rate / profit factor / avg RR --------------------
    wins = [t for t in closed_trades if t["pnl"] > 0]
    losses = [t for t in closed_trades if t["pnl"] < 0]
    total_trades = len(closed_trades)
    win_rate = (len(wins) / total_trades * 100.0) if total_trades > 0 else 0.0

    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    # profit_factor MUST stay finite. It is written straight into
    # account_stats.profit_factor (`numeric not null default 0`, migration 0004)
    # by supabase_writer.upsert_account_stats(), and float("inf") cannot survive
    # that write: httpx encodes request bodies with allow_nan=False (raises
    # ValueError "Out of range float values are not JSON compliant"), and the raw
    # wire form would be the bare token `Infinity`, which is not valid JSON
    # (RFC 8259) and is rejected by PostgREST's parser. Either way the write
    # throws, verify.py's outer except swallows it, and the job fails as
    # "timeout" — an opaque failure with no relation to the real cause.
    #
    # gross_loss == 0 covers two cases, and BOTH resolve to 0.0 here:
    #   - no closed trades at all (a freshly funded account) — the ratio is
    #     undefined and 0.0 is also the column's own default;
    #   - wins but no losses — the true ratio is infinite/undefined.
    # The frontend's equivalent fallback (src/lib/utils/statistics.ts:267)
    # substitutes grossWin itself, but ITS grossWin is denominated in % (summed
    # trades.result), whereas gross_profit here is an absolute money amount in
    # account currency. account_stats feeds the GDPR export, so substituting it
    # would push real money into a column documented as a dimensionless ratio —
    # hence 0.0 rather than a literal port of that fallback. No UI reads this
    # column; the statistics the owner actually sees are recomputed client-side
    # from `trades`, where the %-based convention applies correctly.
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 0.0

    # avg_rr: average(win_pnl) / average(|loss_pnl|) — simple reward-to-risk ratio
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    avg_rr = (avg_win / avg_loss) if avg_loss > 0 else 0.0

    # ---- trading period (days from first_trade_at → now) ------
    if first_trade_at is not None:
        first_dt = datetime.fromtimestamp(first_trade_at, tz=timezone.utc)
        trading_period = max(0, (now_utc - first_dt).days)
    else:
        trading_period = 0

    return {
        "return_all": base["return_all"],
        "return_1m": period_returns["return_1m"],
        "return_3m": period_returns["return_3m"],
        "return_6m": period_returns["return_6m"],
        "return_1y": period_returns["return_1y"],
        "max_drawdown": round(max_drawdown, 2),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 4),
        "avg_rr": round(avg_rr, 4),
        "total_trades": total_trades,
        "trading_period": trading_period,
        "first_trade_at": first_trade_at,
    }


# ---------------------------------------------------------------------------
# compute_session
# ---------------------------------------------------------------------------

def compute_session(opened_at_utc: datetime) -> Optional[str]:
    """
    Classify a trade's open time into a session chip (JOURNAL-02 / D-04).

    Windows are LOCKED (owner decision, CONTEXT.md D-04), all boundaries in UTC:
        Asia:      23:00-06:00
        Frankfurt: 06:00-07:00
        London:    07:00-12:00
        NY:        12:00-20:00  — NY WINS the London/NY overlap (12:00-15:00)
        20:00-23:00 -> None (no session, no chip)

    CRITICAL: NY must be checked FIRST in the if-chain. Both the naive "NY"
    window (12:00-20:00) and the naive "London" window (07:00-12:00) do NOT
    actually overlap on paper (12 is the shared boundary), but D-04's overlap
    language plus RESEARCH.md Pattern 2 make explicit that any ambiguity in
    the 12:00-15:00 band must resolve to NY — checking NY before London is
    the entire correctness argument here, not just the boundary values.
    """
    hour = opened_at_utc.hour
    if 12 <= hour < 20:
        return "NY"
    if 7 <= hour < 12:
        return "London"
    if 6 <= hour < 7:
        return "Frankfurt"
    if hour >= 23 or hour < 6:
        return "Asia"
    return None  # 20:00-23:00 UTC — no session (D-04)


# ---------------------------------------------------------------------------
# group_deals_into_positions
# ---------------------------------------------------------------------------

def group_deals_into_positions(deals: list[dict]) -> list[dict]:
    """
    Group MT5 deals by `position_id` into one dict per fully-CLOSED position
    (JOURNAL-01 / D-02 round-turn mapping).

    Input deals must already carry the Phase 5 fields added to
    mt5_bridge.get_history_deals() (position_id, symbol, volume, price) and
    must already have `time` normalized to UTC Unix seconds (see
    normalize_to_utc / routers/verify.py's `deals_normalised` pattern) —
    this function does no timezone conversion of its own beyond building the
    output datetimes from those already-UTC seconds.

    Grouping rules (Claude's Discretion, RESEARCH.md Pattern 1 + CONTEXT.md
    "Per-position aggregation edge cases"):
      - Deals are grouped strictly by `position_id` — this is MT5's own
        round-turn identity and is the only grouping key used.
      - Within a group, deals with entry == DEAL_ENTRY_IN (0) are "opening
        legs"; deals with entry == DEAL_ENTRY_OUT (1) are "closing legs".
        A position with multiple opening legs (scaled-in entry) has its
        entry_price volume-weighted across all opening legs; a position with
        multiple closing legs (partial closes) has its exit_price
        volume-weighted across all closing legs.
      - A position is considered CLOSED only when the summed closing volume
        equals the summed opening volume (within a small float epsilon) —
        i.e. every lot that was opened has since been closed out, whether in
        one closing deal or several partial closes. A position with no
        closing deals at all, or with closing volume strictly less than
        opening volume (an in-progress partial close), is STILL OPEN and is
        excluded from the output entirely (its floating P&L is already
        reflected in the equity curve, not the journal — D-02).
      - Reversals: MT5 represents a reversal as a close of the original
        position_id plus the open of a brand-new position_id in the opposite
        direction (not a single row that mutates position_id in place) — so
        a reversal naturally produces one CLOSED group (the original
        position, fully closed) and one STILL-OPEN group (the new reversed
        position, excluded here until a later sync closes it too). No special
        casing is needed beyond the volume-balance check above.
      - direction is taken from the FIRST opening deal's `type` (0=BUY,
        1=SELL) — never from a closing deal, and never re-derived from net
        volume signs.

    Returns one dict per closed position with keys:
        mt5_position_id (int), pair (str), direction ("BUY"|"SELL"),
        opened_at (datetime, UTC) — first deal time in the group,
        closed_at (datetime, UTC) — last deal time in the group,
        entry_price (float) — volume-weighted over opening legs,
        exit_price (float)  — volume-weighted over closing legs,
        volume (float)      — total round-turn volume (summed opening volume),
        result (float)      — summed profit across every deal in the group,
        commission (float)  — summed across every deal in the group,
        swap (float)        — summed across every deal in the group,
        session (str|None)  — compute_session(opened_at) (D-04).
    """
    groups: dict[int, list[dict]] = {}
    for deal in deals:
        groups.setdefault(deal["position_id"], []).append(deal)

    positions: list[dict] = []
    for position_id, group_deals in groups.items():
        ordered = sorted(group_deals, key=lambda d: d["time"])

        opening_legs = [d for d in ordered if d["entry"] == DEAL_ENTRY_IN]
        closing_legs = [d for d in ordered if d["entry"] == DEAL_ENTRY_OUT]

        if not closing_legs:
            continue  # still open — excluded from the journal (D-02)

        opening_volume = sum(d["volume"] for d in opening_legs)
        closing_volume = sum(d["volume"] for d in closing_legs)
        if closing_volume + VOLUME_EPSILON < opening_volume:
            continue  # partial close in progress — still open, excluded

        # direction from the opening deal (never a closing deal)
        direction_source = opening_legs[0] if opening_legs else ordered[0]
        direction = "SELL" if direction_source["type"] == DEAL_TYPE_SELL else "BUY"

        def _volume_weighted_price(legs: list[dict]) -> float:
            total_volume = sum(d["volume"] for d in legs)
            if total_volume <= 0:
                return legs[0]["price"] if legs else 0.0
            return sum(d["price"] * d["volume"] for d in legs) / total_volume

        entry_price = _volume_weighted_price(opening_legs) if opening_legs else ordered[0]["price"]
        exit_price = _volume_weighted_price(closing_legs)

        opened_at = datetime.fromtimestamp(ordered[0]["time"], tz=timezone.utc)
        closed_at = datetime.fromtimestamp(ordered[-1]["time"], tz=timezone.utc)

        positions.append({
            "mt5_position_id": position_id,
            "pair": ordered[0]["symbol"],
            "direction": direction,
            "opened_at": opened_at,
            "closed_at": closed_at,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "volume": opening_volume if opening_volume > 0 else closing_volume,
            "result": sum(d["profit"] for d in ordered),
            "commission": sum(d["commission"] for d in ordered),
            "swap": sum(d["swap"] for d in ordered),
            "session": compute_session(opened_at),
        })

    return positions
