"""
agent/mt5_bridge.py — the ONLY module in this package that imports MetaTrader5.

A stripped PORT of `mt5-service/services/mt5_bridge.py` (the donor stays in the tree
untouched as a working reference — PHASE-LOCAL-SYNC-SPEC.md §9). Unlike the donor,
this module is SYNCHRONOUS: the donor's async/await + `_run()`/`_mt5_lock` machinery
exists to serialise many concurrent asyncio tasks sharing one FastAPI event loop and
one always-on terminal across many users. This program has exactly one user driving
one terminal, walking one account at a time from a single-threaded loop (or a single
background worker thread, in the GUI a later plan adds) — there is no concurrent
caller to serialise against, so no lock of any kind is needed here.

Brought across from the donor, verbatim in rule where noted below:
- The `try: import MetaTrader5 as mt5 / except ImportError` guard, exposing
  `MT5_AVAILABLE` — this is how this module (and every test in
  `agent/tests/test_mt5_bridge.py`) stays importable and testable on a machine with
  no MetaTrader5 terminal and no MetaTrader5 pip package installed.
- `AUTH_ERROR_CODES` / `TIMEOUT_ERROR_CODES` / `IPC_UNAVAILABLE_ERROR_CODES` — DERIVED
  from `agent/error_codes.py`'s `MT5_ERROR_CODES` table below, never redeclared as
  integer literals, so a later probe that edits that one table automatically updates
  these sets too and this file never drifts out of sync with it.
- `get_history_deals()` — near-verbatim, same plain-dict shape, same keys.
- `get_initial_stop_loss()`, renamed `get_initial_stop_and_take_profit()` and
  returning an `(sl, tp)` tuple — VERBATIM in rule: sort ALL orders by `time_setup`
  and take the earliest, of ANY order type, never filtered to market types 0/1 (see
  that function's own docstring for why this rule exists and what it fixed).
- `detect_broker_utc_offset_seconds()` and its `UtcOffsetResult` shape.
- `login_account()`'s credential-handling discipline — `password` is never logged,
  never included in an exception message, never written to a file.
- `current_logged_in_account()` — called immediately after `initialize_terminal()`,
  this is 39-CONTEXT.md D-23's input: if a session was already active when this
  program attached, the caller pins a persistent notice for the rest of the run.
- `initialize_terminal(path)` / `shutdown_terminal()` — the path comes from
  `agent.terminal_discovery.find_terminal_path()`, never a config file or an
  environment variable (39-CONTEXT.md §8 / D-09 §12.2).

Deliberately NOT brought across, and why (39-PATTERNS.md's explicit exclusion list):
- The two-lock design (`_mt5_lock` + the per-account chain lock). Those exist
  because the donor's terminal is shared across many users' concurrent login
  chains on one always-on VPS process. This program has one user and one terminal
  and runs its accounts one at a time — there is nothing to serialise.
- `restart_terminal()` and its process-kill call. It would kill the user's OWN
  terminal, which they may be actively using for something else entirely — an
  absolute prohibition for a program that runs on someone else's computer, not
  a company-owned VPS.
- The IPC self-heal escalation chain built around that restart (login_account()'s
  re-init-and-retry-then-restart ladder). All of it exists to keep a shared,
  always-on terminal alive across many accounts; this program just reports one
  account's failure and moves on to the next one (the per-account try/except
  philosophy a later plan, agent/sync.py, ports separately).
- The shared-VPS keeper/import that restores a fixed account on that always-on
  terminal — a concept that only makes sense when a single terminal process is
  kept alive indefinitely for many users; irrelevant to a program a user starts,
  runs once, and closes.
- The local encrypted credential store and any privileged database writer. This
  program receives the investor password once per sync run, in memory only, from
  the accounts-list response body (already decrypted server-side) — it holds no
  privileged database credential of any kind and imports no database client
  library at all.
- The conventional fixed terminal path as anything but a documented, LAST-RESORT
  fallback inside `terminal_discovery.find_terminal_path()` itself — never the
  primary discovery mechanism here, and never hardcoded a second time in this file.

SECURITY: `login_account()` NEVER logs the `password` argument.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, NamedTuple, Optional

from agent import terminal_discovery
from agent.error_codes import ErrorCategory, MT5_ERROR_CODES
from agent.terminal_discovery import TerminalNotFoundError

# ---------------------------------------------------------------------------
# Import MetaTrader5 — the ONE place in this package where it appears. An
# ImportError is suppressed so this module (and everything that imports it) stays
# importable and unit-testable on a machine with no Windows MT5 installation —
# this repo's own dev/CI machine included.
# ---------------------------------------------------------------------------
try:
    import MetaTrader5 as mt5  # type: ignore[import-untyped]

    MT5_AVAILABLE: bool = True
except ImportError:  # no MetaTrader5 pip package on this machine
    mt5 = None  # type: ignore[assignment]
    MT5_AVAILABLE: bool = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error-code constant sets — DERIVED from agent/error_codes.py's MT5_ERROR_CODES
# table, never redeclared as literals. Editing that one table (e.g. the second
# developer's probe in plan 39-17) automatically keeps these sets correct; there is
# no second place to remember to update.
# ---------------------------------------------------------------------------
def _codes_with_category(category: ErrorCategory) -> frozenset[int]:
    return frozenset(
        code for code, info in MT5_ERROR_CODES.items() if info.category is category
    )


AUTH_ERROR_CODES: frozenset[int] = _codes_with_category(ErrorCategory.AUTH_FAILED)
TIMEOUT_ERROR_CODES: frozenset[int] = _codes_with_category(ErrorCategory.TIMEOUT)
# Connection-class only — deliberately EXCLUDES the algo-trading-disabled codes,
# which now get their own category and their own outcome branch (39-CONTEXT.md D-09
# §12.1), unlike the donor's own IPC_UNAVAILABLE_ERROR_CODES, which lumped both
# together.
IPC_UNAVAILABLE_ERROR_CODES: frozenset[int] = _codes_with_category(
    ErrorCategory.CONNECTION_ERROR
)


# ---------------------------------------------------------------------------
# Terminal lifecycle
# ---------------------------------------------------------------------------

def initialize_terminal(path: Optional[str] = None) -> bool:
    """
    Attach to (or launch) the MT5 terminal.

    `path` should be the `terminal64.exe` path `terminal_discovery.find_terminal_path()`
    already located — pass it explicitly, or leave it `None` to let this function
    call `find_terminal_path()` itself. Either way, as soon as no path is available
    at all, this raises `TerminalNotFoundError` — the one caller that converts
    `find_terminal_path()`'s own "return None, never raise" contract into the
    exception the GUI (a later plan) catches for its blocking-but-non-crashing
    notice («MetaTrader 5 не найден на этом компьютере», 39-CONTEXT.md D-09 §12.2).

    There is deliberately no configuration option and no environment-variable
    override for this path — see `terminal_discovery.py`'s own module docstring for
    why. Do not add one back here "for convenience".

    Returns True on success, False if MetaTrader5 is unavailable in this
    environment or the terminal itself failed to initialise — this mirrors the
    donor's own bool contract and never raises for THAT failure mode, only for the
    "no path found anywhere" one above.
    """
    if path is None:
        path = terminal_discovery.find_terminal_path()
    if path is None:
        raise TerminalNotFoundError(
            "MetaTrader 5 не найден на этом компьютере"
        )

    if not MT5_AVAILABLE:
        logger.warning("MetaTrader5 library not available — terminal not initialised")
        return False

    ok: bool = bool(mt5.initialize(path=path))
    if ok:
        logger.info("MT5 terminal initialised at %s", path)
    else:
        logger.error("mt5.initialize(path=%s) failed: %s", path, mt5.last_error())
    return ok


def shutdown_terminal() -> None:
    """Disconnect from the MT5 terminal. Call once when this program exits."""
    if not MT5_AVAILABLE:
        return
    mt5.shutdown()
    logger.info("MT5 terminal shut down")


def current_logged_in_account() -> Optional[dict[str, Any]]:
    """
    Return whatever account is logged into the terminal RIGHT NOW, as
    `{"login": int, "server": str}`, or `None` if MT5 is unavailable or nothing is
    currently logged in.

    Call this IMMEDIATELY after `initialize_terminal()`, before this program logs
    into anything itself — this is 39-CONTEXT.md D-23's input. If a session was
    already active at that moment, the caller (agent/main.py, a later plan) pins a
    persistent notice for the rest of the run: the user may have been working in
    that account themselves.

    DELIBERATELY NARROW, like the donor's own `get_current_login()` — this must
    NEVER be widened to include balance/equity/margin/profit. Money never leaves
    the owner's own private view (project-wide rule).
    """
    if not MT5_AVAILABLE:
        return None

    info = mt5.account_info()
    if info is None:
        return None

    return {"login": int(info.login), "server": str(info.server)}


# ---------------------------------------------------------------------------
# Account operations
# ---------------------------------------------------------------------------

def login_account(login: int, password: str, server: str) -> bool:
    """
    Switch the MT5 terminal to the given account, via `mt5.login()` only — never
    calls `mt5.initialize()` again for a normal account switch (re-initialising on
    every switch risks broker rate-limiting / IP-ban behaviour for no benefit).

    One IPC-class self-heal only, no further escalation: if the first attempt fails
    with a code in `IPC_UNAVAILABLE_ERROR_CODES`, the terminal is re-initialised
    ONCE and the login retried exactly once. Any other failure (e.g. a wrong
    password) never retries — retrying a bad credential against a broker risks a
    rate-limit / IP-ban, exactly why this never re-initialises for that case. There
    is no `restart_terminal()` escalation beyond this one retry (see this module's
    own header for why that machinery was deliberately not ported).

    SECURITY: `password` is NEVER logged, NEVER included in an exception message,
    and NEVER written to any file.

    Returns True on successful login, False otherwise. Call `last_error_tuple()`
    (or `agent.errors.classify_login_error()` against its code) after a False
    return to get the error category.
    """
    if not MT5_AVAILABLE:
        logger.warning("MT5 not available — login_account() returning False")
        return False

    # Intentionally does NOT log `password`.
    logger.debug("login_account: login=%d server=%s", login, server)

    ok: bool = bool(mt5.login(login, password=password, server=server))
    if ok:
        logger.info("Logged in: login=%d server=%s", login, server)
        return True

    err = mt5.last_error()
    logger.warning(
        "mt5.login failed for login=%d server=%s error=%s", login, server, err
    )

    code = err[0] if err else None
    if code not in IPC_UNAVAILABLE_ERROR_CODES:
        return False

    logger.warning(
        "login_account: IPC-class error %s for login=%d — re-initialising terminal "
        "and retrying once (no further escalation — see module header)",
        err, login,
    )
    reinit_ok: bool = bool(mt5.initialize())
    if not reinit_ok:
        logger.error(
            "login_account: terminal re-initialise failed after IPC error: %s",
            mt5.last_error(),
        )
        return False

    retry_ok: bool = bool(mt5.login(login, password=password, server=server))
    if retry_ok:
        logger.info("Logged in on retry: login=%d server=%s", login, server)
    else:
        logger.warning(
            "mt5.login retry failed for login=%d server=%s error=%s — giving up "
            "for this run (no restart-terminal escalation)",
            login, server, mt5.last_error(),
        )
    return retry_ok


def get_account_info() -> Optional[Any]:
    """
    Return the `mt5.account_info()` namedtuple (or `None` on error/unavailability).

    Exposes `trade_mode`, `equity`, `balance`, `profit` and other fields
    (mql5.com/en/docs/python_metatrader5/mt5accountinfo_py) — callers must respect
    the project-wide money rule themselves; this function is a raw passthrough.
    """
    if not MT5_AVAILABLE:
        return None
    return mt5.account_info()


def last_error_tuple() -> Optional[tuple[int, str]]:
    """
    Return the raw `mt5.last_error()` tuple, or `None` if MT5 is unavailable.

    Callers pass `result[0]` (the integer code) to
    `agent.errors.classify_login_error()` — this function never interprets the
    tuple itself, it only exposes it.
    """
    if not MT5_AVAILABLE:
        return None
    return mt5.last_error()


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def get_history_deals(from_dt: datetime, to_dt: datetime) -> list[dict]:
    """
    Pull all closed deals between `from_dt` and `to_dt`.

    Returns a list of plain dicts with keys:
        time        (int)   — Unix seconds in BROKER-LOCAL time (NOT UTC). Normalise
                              with `agent.positions.normalize_to_utc()` after calling
                              `detect_broker_utc_offset_seconds()`.
        type        (int)   — DEAL_TYPE: 0=buy, 1=sell, 2=balance_op
        entry       (int)   — DEAL_ENTRY: 0=IN, 1=OUT (closed trade)
        profit      (float)
        commission  (float)
        swap        (float)
        position_id (int)   — the round-turn position this deal belongs to;
                              `agent.positions.group_deals_into_positions()` groups
                              deals by this field into one journal trade per CLOSED
                              position.
        symbol      (str)   — traded pair, e.g. "EURUSD".
        volume      (float) — deal volume in lots.
        price       (float) — deal execution price.

    Plain dicts keep every consumer of this data MT5-free and unit-testable, mirroring
    the donor's own contract (mt5-service/services/mt5_bridge.py:776-830).
    """
    if not MT5_AVAILABLE:
        return []

    raw = mt5.history_deals_get(from_dt, to_dt)
    if raw is None:
        logger.error("history_deals_get returned None: %s", mt5.last_error())
        return []

    result: list[dict] = []
    for deal in raw:
        result.append({
            "time": int(deal.time),
            "type": int(deal.type),
            "entry": int(deal.entry),
            "profit": float(deal.profit),
            "commission": float(deal.commission),
            "swap": float(deal.swap),
            "position_id": int(deal.position_id),
            "symbol": str(deal.symbol),
            "volume": float(deal.volume),
            "price": float(deal.price),
        })
    return result


def get_initial_stop_and_take_profit(
    position_id: int,
) -> tuple[Optional[float], Optional[float]]:
    """
    Return `(initial_stop_loss, take_profit)` off the order that OPENED
    `position_id`, or `(None, None)` if no such order can be found (or MT5 is
    unavailable). Both values come off the SAME `mt5.history_orders_get(position=...)`
    record, in ONE IPC call — there is no second call for `tp`.

    CORRECTED RULE, ported VERBATIM from the donor's `get_initial_stop_loss()`
    (mt5-service/services/mt5_bridge.py:866-910, live-verified against account
    541183920): the opening order is the EARLIEST order by `time_setup`, of ANY
    order type — NOT filtered to market BUY/SELL (type 0/1). MT5 preserves a
    pending order's original type (buy-limit / sell-limit / buy-stop / sell-stop)
    through triggering rather than rewriting it to 0/1 on fill; 17 of 18 trades on
    that live account were pending-order-originated, so a type-in-(0,1) filter
    silently found no stop for nearly the whole account. Sorting ALL orders by
    `time_setup` and taking the earliest is correct regardless of type — this
    function applies NO order-type filter of any kind.

    MT5 uses `0.0` as its sentinel for "no stop/target was ever set" on BOTH `sl`
    and `tp` — mapped to `None` here for both, never stored as a literal `0.0`, so a
    real zero-distance value is never confused with "none set" downstream.

    No live probe has confirmed the `tp` sentinel the way the donor's own
    31-03-SUMMARY.md confirmed the `sl` one on a real account — this is inferred by
    symmetry with `sl` (both are 0.0-defaulted numeric fields on the same MT5
    `TradeOrder` record) and is an explicit candidate for the second developer's
    live pass in plan 39-17, not a confirmed fact.
    """
    if not MT5_AVAILABLE:
        return None, None

    raw = mt5.history_orders_get(position=position_id)
    if not raw:
        err = mt5.last_error()
        if err and err[0] != 0:  # error code 0 = no error, just no orders for this position
            logger.warning(
                "history_orders_get returned nothing for position_id=%d: %s",
                position_id, err,
            )
        return None, None

    orders = sorted(raw, key=lambda o: o.time_setup)
    earliest = orders[0]
    sl = float(earliest.sl)
    tp = float(earliest.tp)
    return (sl if sl != 0.0 else None, tp if tp != 0.0 else None)


# ---------------------------------------------------------------------------
# UTC offset detection
# ---------------------------------------------------------------------------

class UtcOffsetResult(NamedTuple):
    """Result of `detect_broker_utc_offset_seconds()`.

    `offset_seconds` is ALWAYS an int (0 on any failure — a usable fallback for
    downstream normalisation). `detected` distinguishes a broker GENUINELY on UTC
    (a real, entirely normal offset of 0) from a FAILED detection — callers must
    branch on `detected`, never assume `offset_seconds` alone means "detection
    succeeded".
    """

    offset_seconds: int
    detected: bool


def detect_broker_utc_offset_seconds() -> UtcOffsetResult:
    """
    Detect the UTC offset used by the broker's server for deal timestamps.

    `mt5.history_deals_get` returns times in BROKER-SERVER local time, not UTC.

    Method (ported from the donor, mt5-service/services/mt5_bridge.py:934-985):
        1. Fetch the most recent EURUSD tick from the broker.
        2. Compare its reported time to `datetime.now(UTC)`.
        3. Round to the nearest hour to absorb subsecond / network noise.

    Returns `UtcOffsetResult(0, False)` when the EURUSD tick could not be fetched
    (MT5 unavailable, or `mt5.symbol_info_tick()` itself returned `None`) — a FAILED
    detection. `detected` is True only when a real tick was compared against
    wall-clock UTC, including the entirely normal case where the resulting offset
    itself rounds to zero (a broker genuinely on UTC) — that case must never be
    confused with a failed detection that merely fell back to 0.
    """
    if not MT5_AVAILABLE:
        return UtcOffsetResult(0, False)

    tick = mt5.symbol_info_tick("EURUSD")
    if tick is None:
        logger.error(
            "detect_broker_utc_offset_seconds: EURUSD tick is None — UTC OFFSET "
            "DETECTION FAILED. Falling back to offset=0, but session and period "
            "buckets WILL BE WRONG if the broker is not actually on UTC."
        )
        return UtcOffsetResult(0, False)

    broker_time: int = int(tick.time)
    utc_now: int = int(datetime.now(timezone.utc).timestamp())
    offset: int = round((broker_time - utc_now) / 3600) * 3600
    logger.debug(
        "UTC offset detected: broker_time=%d utc_now=%d offset=%d",
        broker_time, utc_now, offset,
    )
    return UtcOffsetResult(offset, True)
