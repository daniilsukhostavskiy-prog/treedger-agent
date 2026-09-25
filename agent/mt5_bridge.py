"""
agent/mt5_bridge.py — the ONLY module in this package that imports MetaTrader5.

A stripped PORT of `mt5-service/services/mt5_bridge.py` (the donor stays in the tree
untouched as a working reference). Unlike the donor,
this module is SYNCHRONOUS: the donor's async/await + `_run()`/`_mt5_lock` machinery
exists to serialise many concurrent asyncio tasks sharing one FastAPI event loop and
one always-on terminal across many users. This program has exactly one user driving
one terminal, walking one account at a time from a single-threaded loop (or a single
background worker thread, in the GUI `agent/main.py` provides) — there is no concurrent
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
- `current_logged_in_account()` — called immediately after `initialize_terminal()`:
  if a session was already active when this
  program attached, the caller pins a persistent notice for the rest of the run.
- `initialize_terminal(path)` / `shutdown_terminal()` — the path comes from
  `agent.terminal_discovery.find_terminal_path()`, never a config file or an
  environment variable — there is no configuration option to point this program
  at a path manually, by design.

Deliberately NOT brought across, and why:
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

THE ONE EXCEPTION TO "SYNCHRONOUS": THE CONNECT WATCHDOG
- `mt5.initialize()` is a blocking C call that can hang for as long as Windows keeps a
  modal dialog (e.g. an "untrusted publisher" prompt) in front of the terminal it is
  starting, or when the terminal runs elevated and this program does not. A live run
  sat on «Проверка терминала…» forever because of that. So every `mt5.initialize()` in
  `initialize_terminal()` now runs on a daemon helper thread joined against ONE
  wall-clock deadline (`CONNECT_BUDGET_SECONDS`). Past the deadline the caller gets
  `TerminalUnresponsiveError` and the window becomes usable again.
- A blocking C call CANNOT be interrupted from Python. The helper thread is therefore
  ABANDONED, not stopped, and the MetaTrader5 library's state is suspect for as long as
  it lives. Rule: while it lives, no second MT5 call is ever made — a new connect
  attempt raises `TerminalUnresponsiveError(still_blocked_from_previous=True)`,
  `shutdown_terminal()` skips `mt5.shutdown()`, and `last_error_tuple()` returns None.
- Connect order is attach-first: `mt5.initialize(timeout=...)` WITHOUT a path, and only
  when that returns False, `mt5.initialize(<discovered path>, timeout=...)`. Each
  attempt's variant, result, `last_error`, and elapsed seconds are logged, plus a narrow
  whitelist of `terminal_info()` fields (never the money-bearing ones).

SECURITY: `login_account()` NEVER logs the `password` argument.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, NamedTuple, Optional

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
# table, never redeclared as literals. Editing that one table (e.g. once the second
# developer's own live probe reports back) automatically keeps these sets correct; there is
# no second place to remember to update.
# ---------------------------------------------------------------------------
def _codes_with_category(category: ErrorCategory) -> frozenset[int]:
    return frozenset(
        code for code, info in MT5_ERROR_CODES.items() if info.category is category
    )


AUTH_ERROR_CODES: frozenset[int] = _codes_with_category(ErrorCategory.AUTH_FAILED)
TIMEOUT_ERROR_CODES: frozenset[int] = _codes_with_category(ErrorCategory.TIMEOUT)
# Connection-class only — deliberately EXCLUDES the algo-trading-disabled codes,
# which now get their own category and their own outcome branch, unlike the donor's
# own IPC_UNAVAILABLE_ERROR_CODES, which lumped both
# together.
IPC_UNAVAILABLE_ERROR_CODES: frozenset[int] = _codes_with_category(
    ErrorCategory.CONNECTION_ERROR
)
# Codes that, when mt5.initialize() ITSELF fails with them, most often mean
# «Алготрейдинг» is off (quick 260925-qhs): every row flagged
# `suggests_algotrading_off_at_connect` (-10005, observed live 2026-09-25) plus every
# ALGOTRADING_DISABLED row. Derived, never literal — a user-facing HINT only.
ALGOTRADING_CONNECT_HINT_CODES: frozenset[int] = frozenset(
    code
    for code, info in MT5_ERROR_CODES.items()
    if info.suggests_algotrading_off_at_connect
    or info.category is ErrorCategory.ALGOTRADING_DISABLED
)


# ---------------------------------------------------------------------------
# Connect watchdog — see the module header's "THE ONE EXCEPTION" section.
# ---------------------------------------------------------------------------

# Whole-connect wall-clock budget, both initialize variants together.
CONNECT_BUDGET_SECONDS: float = 90.0
# The whole-connect budget used INSTEAD when this very run started the terminal
# (quick 260925-qhs): a cold terminal start at Windows logon competes with every
# other startup app, and 90 s was sized for a terminal that was already running.
# Attempt A keeps its short attach timeout; attempt B gets the rest of this budget.
# No fixed pre-attach sleep is added — without IPC there is no readiness signal to
# wait for. Passed explicitly by agent/sync.py via `budget_seconds=`.
COLD_START_CONNECT_BUDGET_SECONDS: float = 150.0
# The attach-without-path attempt's own library timeout (ms). Short, because a
# running, reachable terminal answers an attach in well under a second.
ATTACH_TIMEOUT_MS: int = 20_000
# How often the watchdog wakes to report elapsed time and check the deadline.
WATCHDOG_TICK_SECONDS: float = 1.0

# The helper thread of a timed-out mt5.initialize(), if one is still alive. Only
# ever written by _call_with_watchdog() (set) and initialize_terminal() (cleared once
# the thread has finished on its own).
_abandoned_call: Optional[threading.Thread] = None

# The `mt5.last_error()` tuple read when the most recent initialize_terminal() attempt
# failed (None after a success). Reset at the start of every initialize_terminal()
# call and written ONLY there, from the one last_error() read each failed attempt
# already makes for its log line — never a second library call.
_last_connect_error: Optional[Any] = None


class TerminalUnresponsiveError(Exception):
    """
    The MT5 terminal did not answer within `CONNECT_BUDGET_SECONDS`, or a previous
    attempt's abandoned call is still blocked inside the library
    (`still_blocked_from_previous=True`). `elevation_mismatch` is set by
    `agent/sync.py` when a running terminal is elevated and this program is not —
    the likeliest reason an attach can never succeed.
    """

    def __init__(
        self,
        message: str,
        *,
        waited_seconds: int = 0,
        still_blocked_from_previous: bool = False,
        elevation_mismatch: bool = False,
    ) -> None:
        super().__init__(message)
        self.waited_seconds = waited_seconds
        self.still_blocked_from_previous = still_blocked_from_previous
        self.elevation_mismatch = elevation_mismatch


def is_previous_call_still_blocked() -> bool:
    """True while a timed-out mt5.initialize() helper thread is still alive."""
    return _abandoned_call is not None and _abandoned_call.is_alive()


def _call_with_watchdog(
    fn: Callable[[], Any],
    *,
    deadline: float,
    label: str,
    on_tick: Optional[Callable[[int], None]],
    started_at: float,
) -> Any:
    """
    Run `fn` on a daemon thread named "mt5-<label>" and join it in
    `WATCHDOG_TICK_SECONDS` slices against the monotonic `deadline`. After each slice
    in which it is still running, `on_tick(elapsed_whole_seconds)` is called (the
    window's live counter). Past the deadline the thread is ABANDONED — a blocking C
    call cannot be interrupted, so it is not stopped, only left behind and remembered
    in `_abandoned_call` so that no later MT5 call runs concurrently with it — and
    `TerminalUnresponsiveError` is raised. An exception raised by `fn` itself is
    re-raised on the caller's thread unchanged.
    """
    global _abandoned_call
    box: "dict[str, Any]" = {}

    def _runner() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 — handed back to the caller below
            box["error"] = exc

    helper = threading.Thread(target=_runner, name=f"mt5-{label}", daemon=True)
    helper.start()
    while True:
        remaining = deadline - time.monotonic()
        helper.join(max(0.0, min(WATCHDOG_TICK_SECONDS, remaining)))
        if not helper.is_alive():
            break
        if on_tick is not None:
            try:
                on_tick(int(time.monotonic() - started_at))
            except Exception:  # noqa: BLE001 — a progress callback must never break the watchdog
                logger.exception("connect watchdog: on_tick callback failed")
        if time.monotonic() >= deadline:
            _abandoned_call = helper
            waited = int(time.monotonic() - started_at)
            logger.error(
                "connect watchdog: %s did not return within %d s — helper thread "
                "abandoned (a blocking MT5 call cannot be interrupted); no further MT5 "
                "call will be made while it is alive",
                label, waited,
            )
            raise TerminalUnresponsiveError(
                f"MetaTrader 5 terminal did not respond within {waited} s",
                waited_seconds=waited,
            )
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _log_terminal_info() -> None:
    """Log a narrow whitelist of terminal_info() fields. NEVER the whole namedtuple:
    it carries `community_balance`, and money never goes into this log."""
    try:
        info = mt5.terminal_info()
    except Exception as exc:  # noqa: BLE001 — diagnostics only
        logger.warning("terminal_info() raised %s", exc.__class__.__name__)
        return
    if info is None:
        logger.warning("terminal_info() returned None: %s", mt5.last_error())
        return
    logger.info(
        "terminal_info: name=%s company=%s build=%s path=%s connected=%s trade_allowed=%s",
        getattr(info, "name", None),
        getattr(info, "company", None),
        getattr(info, "build", None),
        getattr(info, "path", None),
        getattr(info, "connected", None),
        # «Алготрейдинг» switch state — a flag, not money.
        getattr(info, "trade_allowed", None),
    )


# ---------------------------------------------------------------------------
# Terminal lifecycle
# ---------------------------------------------------------------------------

def initialize_terminal(
    path: Optional[str] = None,
    *,
    on_tick: Optional[Callable[[int], None]] = None,
    budget_seconds: Optional[float] = None,
) -> bool:
    """
    Attach to (or launch) the MT5 terminal, never for longer than
    `CONNECT_BUDGET_SECONDS` in total — or `budget_seconds` when given (agent/sync.py
    passes `COLD_START_CONNECT_BUDGET_SECONDS` only when that run itself started the
    terminal). After a False return, `last_connect_error_code()` gives the failing
    attempt's MT5 error code.

    `path` should be the `terminal64.exe` path `terminal_discovery.find_terminal_path()`
    already located — pass it explicitly, or leave it `None` to let this function
    call `find_terminal_path()` itself. Either way, as soon as no path is available
    at all, this raises `TerminalNotFoundError` — the one caller that converts
    `find_terminal_path()`'s own "return None, never raise" contract into the
    exception `agent/main.py`'s GUI catches for its blocking-but-non-crashing
    notice («MetaTrader 5 не найден на этом компьютере»).

    There is deliberately no configuration option and no environment-variable
    override for this path — see `terminal_discovery.py`'s own module docstring for
    why. Do not add one back here "for convenience".

    Order: (A) `mt5.initialize(timeout=...)` with NO path — attaches to a terminal
    that is already running; (B) only if A returned False, `mt5.initialize(path,
    timeout=...)` with the path as the unnamed first parameter, as the official docs
    define it. Both run under the connect watchdog; `on_tick(elapsed_seconds)` is
    called about once a second while either is in progress.

    Returns True on success, False if MetaTrader5 is unavailable in this environment
    or both attempts returned False. Raises `TerminalUnresponsiveError` when the
    budget runs out, or immediately when a previous attempt's abandoned call is still
    blocked in the library.
    """
    global _abandoned_call, _last_connect_error

    if path is None:
        path = terminal_discovery.find_terminal_path()
    if path is None:
        raise TerminalNotFoundError(
            "MetaTrader 5 не найден на этом компьютере"
        )

    if not MT5_AVAILABLE:
        logger.warning("MetaTrader5 library not available — terminal not initialised")
        return False

    if _abandoned_call is not None:
        if _abandoned_call.is_alive():
            logger.error(
                "initialize_terminal refused: the previous attempt's mt5.initialize() "
                "is still blocked (thread %s)", _abandoned_call.name,
            )
            raise TerminalUnresponsiveError(
                "a previous MetaTrader 5 connect attempt is still blocked",
                still_blocked_from_previous=True,
            )
        logger.info("the previously abandoned mt5.initialize() call has since finished; continuing")
        _abandoned_call = None

    _last_connect_error = None
    budget = CONNECT_BUDGET_SECONDS if budget_seconds is None else float(budget_seconds)
    logger.info("connect: whole-connect budget %.0f s", budget)
    started_at = time.monotonic()
    deadline = started_at + budget

    def _remaining_ms() -> int:
        return int((deadline - time.monotonic()) * 1000)

    # Attempt A — attach to an already-running terminal, no path.
    timeout_a = max(1, min(ATTACH_TIMEOUT_MS, _remaining_ms()))
    logger.info("connect attempt A: mt5.initialize(timeout=%d) without a path", timeout_a)
    ok_a = bool(
        _call_with_watchdog(
            lambda: mt5.initialize(timeout=timeout_a),
            deadline=deadline, label="initialize-attach", on_tick=on_tick, started_at=started_at,
        )
    )
    logger.info("connect attempt A returned %s after %.1f s", ok_a, time.monotonic() - started_at)
    if ok_a:
        _log_terminal_info()
        return True
    _last_connect_error = mt5.last_error()
    logger.warning("connect attempt A last_error=%s", _last_connect_error)

    if deadline - time.monotonic() < 1.0:
        logger.error(
            "connect: no budget left for attempt B after %.1f s", time.monotonic() - started_at
        )
        return False

    # Attempt B — the discovered path, POSITIONAL (the docs' unnamed first parameter).
    timeout_b = max(1000, _remaining_ms() - 2000)
    logger.info("connect attempt B: mt5.initialize(%s, timeout=%d)", path, timeout_b)
    ok_b = bool(
        _call_with_watchdog(
            lambda: mt5.initialize(path, timeout=timeout_b),
            deadline=deadline, label="initialize-path", on_tick=on_tick, started_at=started_at,
        )
    )
    logger.info("connect attempt B returned %s after %.1f s", ok_b, time.monotonic() - started_at)
    if ok_b:
        _last_connect_error = None
        _log_terminal_info()
        return True
    _last_connect_error = mt5.last_error()
    logger.error("connect attempt B last_error=%s — terminal not initialised", _last_connect_error)
    return False


def last_connect_error_code() -> Optional[int]:
    """
    The MT5 error code of the attempt that made the most recent `initialize_terminal()`
    return False — None after a successful connect, when the code is not an int, or
    while an abandoned `mt5.initialize()` is still blocked (its outcome is unknown and
    nothing about the library's state is trustworthy until it returns). Reads the tuple
    captured during that call; never calls into the library itself.
    """
    if is_previous_call_still_blocked():
        return None
    err = _last_connect_error
    if not err:
        return None
    try:
        code = err[0]
    except (TypeError, IndexError, KeyError):
        return None
    # bool is an int subclass; a True/False "code" is not a code.
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return code


def terminal_trade_allowed() -> Optional[bool]:
    """
    Whether «Алготрейдинг» (Algo Trading) is switched on in the attached terminal —
    `terminal_info().trade_allowed`. None when MT5 is unavailable, when an abandoned
    call is still blocked (no library call is made then), when `terminal_info()`
    returns None or raises, or when the attribute is missing. Never raises.
    """
    if not MT5_AVAILABLE:
        return None
    if is_previous_call_still_blocked():
        return None
    try:
        info = mt5.terminal_info()
    except Exception as exc:  # noqa: BLE001 — a diagnostic read must never break a run
        logger.warning("terminal_trade_allowed: terminal_info() raised %s", exc.__class__.__name__)
        return None
    if info is None:
        return None
    value = getattr(info, "trade_allowed", None)
    if value is None:
        return None
    return bool(value)


def shutdown_terminal() -> None:
    """
    Disconnect from the MT5 terminal (IPC only — the terminal process itself keeps
    running). Call once when this program exits. Skipped while a timed-out
    `mt5.initialize()` is still blocked: calling into the library concurrently with
    that abandoned call is exactly what the watchdog rule forbids.
    """
    if not MT5_AVAILABLE:
        return
    if is_previous_call_still_blocked():
        logger.warning("shutdown_terminal skipped: an abandoned mt5.initialize() is still blocked")
        return
    mt5.shutdown()
    logger.info("MT5 terminal shut down")


def current_logged_in_account() -> Optional[dict[str, Any]]:
    """
    Return whatever account is logged into the terminal RIGHT NOW, as
    `{"login": int, "server": str}`, or `None` if MT5 is unavailable or nothing is
    currently logged in.

    Call this IMMEDIATELY after `initialize_terminal()`, before this program logs
    into anything itself. If a session was
    already active at that moment, the caller (`agent/main.py`) pins a
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
    # Deliberately NOT wrapped in the connect watchdog: this only ever runs after a
    # successful attach earlier in the same run, against a terminal that is already
    # running and answering. Wrapping it would make TerminalUnresponsiveError escape
    # from here into sync_one_account(), whose never-raise catch-all would then have to
    # re-raise it — a contract change out of scope for the hang fix (recorded as a
    # residual risk in quick task 260925-k6y's SUMMARY).
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
    if is_previous_call_still_blocked():
        # Never call into the library alongside an abandoned blocking call.
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

    No live probe has confirmed the `tp` sentinel the way a prior live test on a real
    account confirmed the `sl` one — this is inferred by
    symmetry with `sl` (both are 0.0-defaulted numeric fields on the same MT5
    `TradeOrder` record) and is an explicit candidate for the second developer's
    own live pass, not a confirmed fact.
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
