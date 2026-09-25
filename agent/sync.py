"""
agent/sync.py — the per-account sync pipeline, PORTED (stripped) from
`mt5-service/scheduler/daily_sync.py::sync_one_account` (the donor stays in the tree
untouched).

WHAT IS PORTED, AND WHY
-------------------------------------------------------------------------
- Step 0's defensive validation of the login and the broker-server string, BEFORE MT5
  is ever touched. The donor's own comment explains why in production terms: a
  malformed account row once crashed an entire nightly nightly-sync loop. The same
  reasoning holds here for a loop over one user's own accounts — one bad row must fail
  only itself.
- Steps 3-6: pull the FULL trade history (this program has no per-account cutoff
  concept at all — see the note below), read account info, detect the broker's UTC
  offset and normalise every deal's timestamp with it, then compute the
  deposit-adjusted return and the equity curve. The donor's CORE-05 rule is carried
  verbatim: the floating-equity percentage is computed against the EXACT SAME
  deposit base passed into the equity-point builder for the closed-trade points — if
  the two ever used different bases, the live (rightmost) point would sit on a
  different denominator than the rest of the curve and the curve would visibly
  disagree with itself.
- Step 7's shape: group deals into closed positions via `agent/positions.py`, then
  hand them to `agent/api_client.py`'s `post_trades` — this program has no database
  client of any kind, so there is no writer call to make here at all.
- Step 9's shape, FOLDED FORWARD rather than run as a second pass: the initial
  stop-loss/take-profit read happens for each position BEFORE that position is
  POSTed, in the same loop — so it arrives on the insert itself, never as a later
  backfill. The donor's per-row try/except AND its whole-step try/except are both
  kept, so a single stop/target read failure can never abort the account.
- The whole-account try/except that never re-raises — this is this phase's readiness
  criterion 8's structural half. Directly ported from the donor's own documented
  failure philosophy: on failure, this program reports what happened and moves on to
  the next account; it never crashes the run.

WHAT IS DELIBERATELY NOT PORTED, AND WHY
-------------------------------------------------------------------------
- The per-account cutoff column and its filter step. This program neither reads nor
  writes that column at all — every sync pulls the account's
  complete broker history, unconditionally, with no truncation.
- The chain-lock acquisition around the donor's login-to-write chain. That lock exists
  because the donor's ONE terminal process is shared across many users' concurrent
  login chains on an always-on VPS. This program has exactly one user, one terminal,
  and walks its accounts strictly one at a time from a single loop — there is nothing
  concurrent to serialise against.
- The encrypted local credential-store load. This program never has an encrypted local
  credential store at all; it receives the investor password once per sync run, in
  memory only, from the `/api/agent/accounts` response body (already decrypted
  server-side) and never writes it anywhere.
- Every database-writer call. Replaced end to end by HTTP POSTs through
  `agent/api_client.py` — this program imports no database client library of any
  kind.
- The fleet loop, the auto-deactivate thresholds, the sync-run bookkeeping table and
  everything about a persistent run log. All of that is VPS-only observability with no
  equivalent here; the server's own per-account state tracking (driven by the outcome
  this module reports) is what replaces it.

THIS PROGRAM NEVER PRODUCES A `broker_closed` OUTCOME, BY CONSTRUCTION
-------------------------------------------------------------------------
`agent/errors.py::classify_outcome()` can only ever return one of six outcomes —
`ok`, `auth_failed`, `server_unavailable`, `timeout`, `algotrading_disabled`, or
`internal` — and `broker_closed` is not one of them; the type this program's own
`ErrorCategory` enum uses has no such member at all, so no return value from this
call site can ever carry that meaning. `classify_outcome()` still accepts
`consecutive_auth_failures`/`has_synced_successfully_before` as parameters, always
called here with `consecutive_auth_failures=0, has_synced_successfully_before=False`,
but they are accepted-and-ignored — a leftover shape from a sustained-failure
heuristic that was deliberately deleted outright, not merely left unfed (see
`agent/errors.py`'s own comment on that branch for the full reasoning). This is a
real, intentional scoping decision, not an oversight: the server already implements
an equivalent sustained-failure safety net independently
(`src/lib/api/agent/ingest.server.ts`'s `AUTH_FAILED_DEACTIVATE_THRESHOLD`, which
deactivates an account after repeated `auth_failed` reports via its own
`disconnect_reason='sync_failure'` path) — a different value, on a different column,
but the same protective shape, applied server-side where cross-run state already
lives. The only path to `broker_closed` at all is the owner's own explicit action in
their own browser session — never inferred here, and never accepted here even if an
agent token tried to report it (the server refuses that outcome from an agent token
as a security boundary).
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from agent import (
    api_client,
    config_store,
    errors,
    mt5_bridge,
    positions,
    terminal_discovery,
    terminal_process,
)
from agent.terminal_discovery import TerminalNotFoundError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Progress reporting — a plain, GUI-free dataclass. No tkinter import anywhere in
# this module, so the whole pipeline stays testable headlessly (agent/main.py, plan
# 39-16, is the one place that ever renders these).
# ---------------------------------------------------------------------------

PHASE_TERMINAL_CHECK = "terminal_check"
PHASE_LOGIN = "login"
PHASE_READING = "reading"
PHASE_SENDING = "sending"
PHASE_DONE = "done"
PHASE_FAILED = "failed"


@dataclass
class AccountProgress:
    """
    One progress update, posted through the `report` callback every caller of this
    module supplies. `account_id`/`mt_login` are `None` only for the one
    `PHASE_TERMINAL_CHECK` event `run_sync` reports once per run, before any account
    is touched.
    """

    account_id: Optional[str]
    mt_login: Optional[str]
    phase: str
    trades_sent: int = 0
    outcome: Optional[str] = None
    error_reason: Optional[str] = None
    # Set only on the one PHASE_TERMINAL_CHECK event, to `{"login": int, "server":
    # str}` when a session was ALREADY active in the terminal at startup, or
    # `None` when it was not. The GUI pins its persistent notice off this field.
    pre_existing_session: Optional[dict] = None


@dataclass
class RunSummary:
    """What `run_sync` hands back once every account has been walked."""

    total: int = 0
    succeeded: int = 0
    failed: int = 0


ReportFn = Callable[[AccountProgress], None]

# ---------------------------------------------------------------------------
# Run stages — the window's stage list («Поиск терминала», «Запуск терминала»,
# «Подключение к терминалу», «Получение списка счетов»). Reported through the
# optional `report_stage` callback; `agent/ui_state.py` mirrors these strings.
# ---------------------------------------------------------------------------

STAGE_FIND_TERMINAL = "find_terminal"
STAGE_LAUNCH_TERMINAL = "launch_terminal"
STAGE_CONNECT = "connect"
STAGE_FETCH_ACCOUNTS = "fetch_accounts"

STAGE_IN_PROGRESS = "in_progress"
STAGE_DONE = "done"
STAGE_FAILED = "failed"


@dataclass
class StageProgress:
    """One stage update. `detail` is the found path, «запущен свёрнутым», or the
    account count; `elapsed_seconds` is the live connect counter."""

    stage: str
    status: str
    detail: Optional[str] = None
    elapsed_seconds: Optional[int] = None


StageFn = Callable[[StageProgress], None]


class SyncCancelledError(Exception):
    """
    The person pressed «Отменить». Raised by `run_sync` at the next step boundary —
    never in the middle of a step: a blocking MT5 call cannot be interrupted (only the
    connect watchdog bounds it), so cancel takes effect only between steps.
    """


class SyncAbortedError(Exception):
    """
    Raised by `run_sync` when the MT5 terminal itself could not be initialised (as
    opposed to `agent.terminal_discovery.TerminalNotFoundError`, which
    `mt5_bridge.initialize_terminal()` raises directly and lets propagate unchanged —
    that is the "no terminal installed at all" case the GUI's own no-terminal screen
    catches). This is the DISTINCT "a path was found but the terminal itself refused
    to start" case.
    """


class TerminalLaunchError(SyncAbortedError):
    """
    No terminal64.exe was running, so this program tried to start the discovered one
    (minimized, see `agent/terminal_process.py`) and Windows refused (`OSError`).
    A subclass of `SyncAbortedError` so any caller that only knows the parent still
    treats it as "the run could not reach a terminal".
    """


# ---------------------------------------------------------------------------
# Wire-contract payload builders
# ---------------------------------------------------------------------------

def _iso_utc(ts: Any) -> Optional[str]:
    """
    Render a UTC Unix-seconds int, or an already-UTC `datetime`, as the
    `z.iso.datetime()`-compatible string the wire contract requires (seconds
    precision, a literal trailing `Z`, no other timezone offset form). Returns `None`
    for `None` input, unchanged.
    """
    if ts is None:
        return None
    if isinstance(ts, datetime):
        dt = ts
    else:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_account_stats_payload(stats: dict) -> dict:
    """
    Map `agent.positions.compute_aggregate_stats()`'s snake_case output onto
    `agentAccountStatsSchema`'s exact camelCase field names
    (`src/lib/api/agent/contract.ts`) — nothing computed here, only renamed and, for
    `firstTradeAt`, reformatted from a Unix-seconds int to an ISO datetime string.
    """
    return {
        "returnAll": stats["return_all"],
        "return1m": stats["return_1m"],
        "return3m": stats["return_3m"],
        "return6m": stats["return_6m"],
        "return1y": stats["return_1y"],
        "maxDrawdown": stats["max_drawdown"],
        "winRate": stats["win_rate"],
        "profitFactor": stats["profit_factor"],
        "avgRr": stats["avg_rr"],
        "totalTrades": stats["total_trades"],
        "tradingPeriod": stats["trading_period"],
        "firstTradeAt": _iso_utc(stats["first_trade_at"]),
    }


def _build_equity_points_payload(equity_points: list[dict]) -> list[dict]:
    """Map `agent.positions.build_equity_points()`'s output onto `agentEquityPointSchema`."""
    return [
        {"ts": _iso_utc(point["ts"]), "equityPct": point["equity_pct"]}
        for point in equity_points
    ]


def _build_trade_payload(position: dict, initial_stop_loss: Optional[float], take_profit: Optional[float]) -> dict:
    """
    Map one `agent.positions.group_deals_into_positions()` position dict onto
    `agentTradeSchema`. `position["result"]` (the position's summed money P&L) is sent
    as the wire contract's `profit` field — deliberately never as `result`: the
    contract has no such field, because the server alone computes the immutable
    `result` percentage column from `profit` and the account's own deposit base.
    """
    return {
        "mt5PositionId": position["mt5_position_id"],
        "pair": position["pair"],
        "direction": position["direction"],
        "openedAt": _iso_utc(position["opened_at"]),
        "closedAt": _iso_utc(position["closed_at"]),
        "entryPrice": position["entry_price"],
        "exitPrice": position["exit_price"],
        "volume": position["volume"],
        "profit": position["result"],
        "commission": position["commission"],
        "swap": position["swap"],
        "session": position["session"],
        "initialStopLoss": initial_stop_loss,
        "takeProfit": take_profit,
    }


def _outcome_for_login_failure(error_code: Optional[int]) -> str:
    """
    Classify a failed `mt5_bridge.login_account()` attempt into one of the seven wire
    outcomes, via `agent.errors.classify_outcome()`. See this module's own header for
    why `consecutive_auth_failures`/`has_synced_successfully_before` are always passed
    as `0`/`False` from this call site — that is a deliberate scoping decision, not an
    oversight.
    """
    return errors.classify_outcome(
        login_succeeded=False,
        error_code=error_code,
        consecutive_auth_failures=0,
        has_synced_successfully_before=False,
    )


def _is_demo_account(account_info: Any) -> Optional[bool]:
    """
    `trade_mode != 2` means demo/contest rather than a real funded account (mirrors
    the donor's own `job_store.py` convention). Returns `None` when `account_info` is
    unavailable at all, so the caller can send `isDemo: null` rather than guessing.
    """
    if account_info is None:
        return None
    trade_mode = getattr(account_info, "trade_mode", None)
    if trade_mode is None:
        return None
    return bool(trade_mode != 2)


# ---------------------------------------------------------------------------
# Per-account pipeline
# ---------------------------------------------------------------------------

_EPOCH_START = datetime(2000, 1, 1, tzinfo=timezone.utc)


def sync_one_account(client: api_client.ApiClient, account: dict, report: ReportFn) -> str:
    """
    Run the full sync pipeline for exactly one account and report its outcome.

    NEVER RAISES. Every failure mode — a malformed account row, a login failure, or
    any exception raised anywhere inside this function's own body — is caught,
    classified, reported through `report`, reported to the server via
    `client.post_account_status()`, and this function returns normally either way.
    This is readiness criterion 8's structural half: one account's failure can never
    stop `run_sync`'s loop over the rest.

    Returns the outcome string that was reported (one of the seven
    `AGENT_ACCOUNT_OUTCOMES` values).
    """
    account_id = account.get("accountId")
    mt_login_raw = account.get("mtLogin")
    broker_server = account.get("brokerServer")
    investor_password = account.get("investorPassword")

    def _fail(outcome: str, reason: str, mt_login_label: Optional[str]) -> str:
        logger.warning("account mt_login=%s failed: outcome=%s reason=%s", mt_login_label, outcome, reason)
        report(
            AccountProgress(
                account_id=account_id,
                mt_login=mt_login_label,
                phase=PHASE_FAILED,
                outcome=outcome,
                error_reason=reason,
            )
        )
        try:
            client.post_account_status(
                account_id=account_id,
                outcome=outcome,
                is_demo=None,
                stats=None,
                equity_points=None,
            )
        except Exception:  # noqa: BLE001 — reporting the failure must never itself raise
            pass
        return outcome

    try:
        # Step 0 — defensive validation BEFORE MT5 is ever touched. A malformed row
        # (e.g. a non-numeric login, or a missing broker-server string) fails only
        # this account, exactly mirroring the donor's own production-hardening fix.
        try:
            mt_login = int(mt_login_raw)
        except (TypeError, ValueError):
            return _fail(
                errors.OUTCOME_INTERNAL,
                "mt_login is not a valid integer",
                str(mt_login_raw) if mt_login_raw is not None else None,
            )

        if not isinstance(broker_server, str) or not broker_server.strip():
            return _fail(errors.OUTCOME_INTERNAL, "broker_server is missing or malformed", str(mt_login))

        logger.info("account mt_login=%d broker_server=%s: login", mt_login, broker_server)
        report(AccountProgress(account_id=account_id, mt_login=str(mt_login), phase=PHASE_LOGIN))

        login_ok = mt5_bridge.login_account(mt_login, investor_password, broker_server)
        if not login_ok:
            error_tuple = mt5_bridge.last_error_tuple()
            error_code = error_tuple[0] if error_tuple else None
            outcome = _outcome_for_login_failure(error_code)
            return _fail(outcome, f"login failed ({outcome})", str(mt_login))

        report(AccountProgress(account_id=account_id, mt_login=str(mt_login), phase=PHASE_READING))

        # Steps 3-4 — full history pull + UTC normalisation. This program has no
        # per-account cutoff of any kind: every sync pulls the account's complete
        # broker history, unconditionally (see this module's own header).
        now_utc = datetime.now(timezone.utc)
        deals_raw = mt5_bridge.get_history_deals(_EPOCH_START, now_utc)
        account_info = mt5_bridge.get_account_info()

        utc_offset_result = mt5_bridge.detect_broker_utc_offset_seconds()
        deals_normalised = [
            {
                **deal,
                "time": int(
                    positions.normalize_to_utc(deal["time"], utc_offset_result.offset_seconds).timestamp()
                ),
            }
            for deal in deals_raw
        ]

        # Steps 5-6 — deposit-adjusted return + equity curve. CORE-05: the floating
        # equity percentage MUST be computed against the SAME deposit base passed
        # into build_equity_points() below for the closed-trade points.
        base = positions.compute_deposit_adjusted_return(deals_normalised)
        deposit_base = base["deposit_base"]

        floating_equity_pct: Optional[float] = None
        if account_info is not None and deposit_base > 0:
            floating_equity_pct = (float(account_info.equity) - deposit_base) / deposit_base * 100.0

        equity_points_raw = positions.build_equity_points(
            closed_trades=base["closed_trades"],
            deposit_base=deposit_base,
            floating_equity_pct=floating_equity_pct,
        )

        # Step 7 — group into closed positions, fold the stop/target read FORWARD
        # into the same loop (never a second pass), then POST via the HTTP client —
        # this program has no database writer to call instead.
        grouped_positions = positions.group_deals_into_positions(deals_normalised)

        trades_payload: list[dict] = []
        for position in grouped_positions:
            try:
                initial_stop_loss, take_profit = mt5_bridge.get_initial_stop_and_take_profit(
                    position["mt5_position_id"]
                )
            except Exception:  # noqa: BLE001 — one row's stop/target read must never abort the account
                initial_stop_loss, take_profit = None, None
            trades_payload.append(_build_trade_payload(position, initial_stop_loss, take_profit))

        report(
            AccountProgress(
                account_id=account_id, mt_login=str(mt_login), phase=PHASE_SENDING, trades_sent=0
            )
        )

        currency = getattr(account_info, "currency", None) if account_info is not None else None
        is_demo = _is_demo_account(account_info)

        client.post_trades(
            account_id=account_id,
            deposit_base=deposit_base,
            currency=currency,
            is_demo=bool(is_demo),
            trades=trades_payload,
        )

        report(
            AccountProgress(
                account_id=account_id,
                mt_login=str(mt_login),
                phase=PHASE_SENDING,
                trades_sent=len(trades_payload),
            )
        )

        stats = positions.compute_aggregate_stats(
            deals=deals_normalised,
            account_equity=float(account_info.equity) if account_info is not None else 0.0,
            now_utc=now_utc,
        )

        client.post_account_status(
            account_id=account_id,
            outcome=errors.OUTCOME_OK,
            is_demo=is_demo,
            stats=build_account_stats_payload(stats),
            equity_points=_build_equity_points_payload(equity_points_raw),
        )

        report(
            AccountProgress(
                account_id=account_id,
                mt_login=str(mt_login),
                phase=PHASE_DONE,
                trades_sent=len(trades_payload),
                outcome=errors.OUTCOME_OK,
            )
        )
        logger.info("account mt_login=%d: ok, %d trade(s) sent", mt_login, len(trades_payload))
        return errors.OUTCOME_OK

    except Exception as exc:  # noqa: BLE001 — criterion 8: never let one account raise past this function
        mt_login_label = str(mt_login_raw) if mt_login_raw is not None else None
        # `str(exc)` here can never contain the investor password: no branch in this
        # function ever interpolates `investor_password` into an exception message,
        # and `mt5_bridge.login_account()`'s own docstring makes the identical
        # guarantee for every exception it raises or logs.
        return _fail(errors.OUTCOME_INTERNAL, str(exc) or exc.__class__.__name__, mt_login_label)


# ---------------------------------------------------------------------------
# Whole-run orchestration
# ---------------------------------------------------------------------------

def run_sync(
    client: api_client.ApiClient,
    report: ReportFn,
    *,
    report_stage: Optional[StageFn] = None,
    cancel_event: Optional[threading.Event] = None,
) -> RunSummary:
    """
    Run one full sync: find the terminal, start it if none is running, connect,
    check for a pre-existing session, fetch this run's account list (persisting the
    rotated token IMMEDIATELY), then walk every account sequentially via
    `sync_one_account` — which never raises, so one account's failure can never stop
    the loop over the rest.

    `agent.terminal_discovery.TerminalNotFoundError` (raised here, before anything
    else, when no terminal is installed anywhere on this machine) is NOT caught here —
    it propagates to the caller, whose no-terminal screen is the one place that handles
    it. `SyncAbortedError` is raised instead for the DISTINCT case where a terminal path
    was found but the terminal itself failed to initialise.

    Before connecting: list running terminal64.exe processes, and — ONLY when that list
    is verifiably empty — start the discovered terminal minimized. A running terminal,
    or an enumeration that could not tell (`None`), launches nothing. A refused launch
    raises `TerminalLaunchError`. `mt5_bridge.TerminalUnresponsiveError` from the
    connect watchdog propagates, with `elevation_mismatch` set when a running terminal
    is elevated and this process is not.

    `report_stage` (optional) receives a `StageProgress` for each stage's start, end
    or failure, plus about one CONNECT tick per second while the connect is in
    progress. `cancel_event` (optional) is checked BETWEEN steps only — after finding
    the terminal, after launching it, after connecting, after fetching the account list
    (the rotated token is already saved by then), and before each account — and raises
    `SyncCancelledError`. A blocking MT5 call cannot be interrupted; only the connect
    watchdog bounds it, so «Отменить» takes effect after the current step. Both
    parameters are optional so every existing caller works unchanged.
    """

    def _stage(stage: str, status: str, detail: Optional[str] = None, elapsed: Optional[int] = None) -> None:
        if report_stage is not None:
            report_stage(StageProgress(stage, status, detail=detail, elapsed_seconds=elapsed))

    def _check_cancel(where: str) -> None:
        if cancel_event is not None and cancel_event.is_set():
            logger.info("run_sync: cancelled by the user after %s", where)
            raise SyncCancelledError(f"cancelled after {where}")

    # --- find ---------------------------------------------------------------
    _stage(STAGE_FIND_TERMINAL, STAGE_IN_PROGRESS)
    path = terminal_discovery.find_terminal_path()
    if path is None:
        _stage(STAGE_FIND_TERMINAL, STAGE_FAILED)
        raise TerminalNotFoundError("MetaTrader 5 не найден на этом компьютере")
    logger.info("run_sync: terminal path %s", path)
    _stage(STAGE_FIND_TERMINAL, STAGE_DONE, detail=path)
    _check_cancel("finding the terminal")

    running = terminal_process.list_terminal_processes()
    self_elevated = terminal_process.current_process_elevated()
    _log_running_terminals(running, path, self_elevated)

    # --- launch (only when verifiably none is running) ----------------------
    if running == []:
        _stage(STAGE_LAUNCH_TERMINAL, STAGE_IN_PROGRESS)
        try:
            pid = terminal_process.launch_terminal_minimized(path)
        except OSError as exc:
            logger.error("run_sync: starting MetaTrader 5 failed: %s", exc)
            _stage(STAGE_LAUNCH_TERMINAL, STAGE_FAILED)
            raise TerminalLaunchError("MetaTrader 5 could not be started") from exc
        logger.info("run_sync: no terminal64.exe was running; started pid=%d minimized", pid)
        _stage(STAGE_LAUNCH_TERMINAL, STAGE_DONE, detail="запущен свёрнутым")
        _check_cancel("starting the terminal")

    # --- connect --------------------------------------------------------------
    _stage(STAGE_CONNECT, STAGE_IN_PROGRESS, elapsed=0)
    on_tick: Optional[Callable[[int], None]] = None
    if report_stage is not None:
        def on_tick(seconds: int) -> None:
            report_stage(StageProgress(STAGE_CONNECT, STAGE_IN_PROGRESS, elapsed_seconds=seconds))

    try:
        if on_tick is not None:
            initialized = mt5_bridge.initialize_terminal(path, on_tick=on_tick)
        else:
            initialized = mt5_bridge.initialize_terminal(path)
    except mt5_bridge.TerminalUnresponsiveError as exc:
        exc.elevation_mismatch = self_elevated is False and any(
            t.elevated is True for t in (running or [])
        )
        logger.error(
            "run_sync: terminal unresponsive (waited=%s s, still_blocked=%s, "
            "elevation_mismatch=%s)",
            exc.waited_seconds, exc.still_blocked_from_previous, exc.elevation_mismatch,
        )
        _stage(STAGE_CONNECT, STAGE_FAILED)
        raise
    except Exception:
        _stage(STAGE_CONNECT, STAGE_FAILED)
        raise
    if not initialized:
        _stage(STAGE_CONNECT, STAGE_FAILED)
        raise SyncAbortedError("MetaTrader 5 terminal failed to initialize")
    _stage(STAGE_CONNECT, STAGE_DONE)
    _check_cancel("connecting to the terminal")

    # Read whatever account is ALREADY logged in, before this program logs
    # into anything itself. No login restore, no second terminal instance — only a
    # persistent notice for the GUI to pin for the rest of the run.
    pre_existing_session = mt5_bridge.current_logged_in_account()
    report(
        AccountProgress(
            account_id=None,
            mt_login=None,
            phase=PHASE_TERMINAL_CHECK,
            pre_existing_session=pre_existing_session,
        )
    )

    # --- fetch ----------------------------------------------------------------
    _stage(STAGE_FETCH_ACCOUNTS, STAGE_IN_PROGRESS)
    try:
        fetch_result = client.fetch_accounts()
    except Exception:
        _stage(STAGE_FETCH_ACCOUNTS, STAGE_FAILED)
        raise
    # Persist the rotated token IMMEDIATELY — the server has already rotated by the
    # time fetch_accounts() returns, and a crash between the two is exactly what the
    # server's grace window (see agent/api_client.py's AccountsFetchResult docstring)
    # exists to survive. This also happens BEFORE any cancel check, so a cancelled run
    # never loses the rotated token.
    config_store.save_token(fetch_result.token)
    # Counts only — never the account dicts (they carry the investor password).
    logger.info("run_sync: server returned %d account(s)", len(fetch_result.accounts))
    _stage(STAGE_FETCH_ACCOUNTS, STAGE_DONE, detail=str(len(fetch_result.accounts)))
    _check_cancel("fetching the account list")

    summary = RunSummary(total=len(fetch_result.accounts))
    for account in fetch_result.accounts:
        _check_cancel("the previous account")
        outcome = sync_one_account(client, account, report)
        if outcome == errors.OUTCOME_OK:
            summary.succeeded += 1
        else:
            summary.failed += 1

    logger.info(
        "run_sync: finished total=%d succeeded=%d failed=%d",
        summary.total, summary.succeeded, summary.failed,
    )
    return summary


def _log_running_terminals(
    running: "Optional[list[terminal_process.RunningTerminal]]",
    discovered_path: str,
    self_elevated: Optional[bool],
) -> None:
    """Diagnostics only: which terminal64.exe processes exist, whether each is the
    discovered install, and elevation on both sides (an elevated terminal cannot be
    attached to from a non-elevated process)."""
    if running is None:
        logger.warning("run_sync: could not enumerate processes (unknown) — nothing will be launched")
    elif not running:
        logger.info("run_sync: terminal64.exe already running: no")
    else:
        logger.info("run_sync: terminal64.exe already running: yes (%d)", len(running))
        wanted = os.path.normcase(os.path.normpath(discovered_path))
        for t in running:
            matches = (
                t.exe_path is not None
                and os.path.normcase(os.path.normpath(t.exe_path)) == wanted
            )
            logger.info(
                "run_sync: running terminal pid=%d exe_path=%s elevated=%s (%s) "
                "matches_discovered=%s",
                t.pid, t.exe_path, t.elevated, t.elevation_source, matches,
            )
    logger.info("run_sync: this process elevated=%s", self_elevated)
