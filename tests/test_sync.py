"""
agent/tests/test_sync.py — every `<behavior>` bullet for `agent/sync.py`
(39-14-PLAN.md Task 2), plus the structural self-audit of the whole `agent/` folder
(Task 3).

`agent.mt5_bridge`'s module-level functions are monkeypatched directly (the same
technique `agent/tests/test_mt5_bridge.py` already uses for other module-level
symbols) so none of this touches a real MetaTrader5 terminal or the real MetaTrader5
pip package — this machine has neither. `agent.api_client.ApiClient` is replaced by a
small recording fake, so no network is ever touched either.
"""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from agent import api_client as api_client_module
from agent import errors, mt5_bridge, sync


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeAccountInfo:
    def __init__(self, equity: float = 1100.0, currency: str = "USD", trade_mode: int = 2) -> None:
        self.equity = equity
        self.currency = currency
        self.trade_mode = trade_mode


class _FakeApiClient:
    """Records every call; never touches the network."""

    def __init__(self) -> None:
        self.posted_trades: list[dict[str, Any]] = []
        self.posted_statuses: list[dict[str, Any]] = []
        self.fetch_accounts_result: Optional[Any] = None
        self.token_saved_at_call_index: Optional[int] = None

    def fetch_accounts(self):
        return self.fetch_accounts_result

    def post_trades(self, *, account_id, deposit_base, currency, is_demo, trades) -> dict:
        self.posted_trades.append(
            {
                "account_id": account_id,
                "deposit_base": deposit_base,
                "currency": currency,
                "is_demo": is_demo,
                "trades": trades,
            }
        )
        return {"inserted": len(trades), "updated": 0, "skipped": 0}

    def post_account_status(self, *, account_id, outcome, is_demo, stats=None, equity_points=None) -> None:
        self.posted_statuses.append(
            {
                "account_id": account_id,
                "outcome": outcome,
                "is_demo": is_demo,
                "stats": stats,
                "equity_points": equity_points,
            }
        )


class _Reporter:
    def __init__(self) -> None:
        self.events: list[sync.AccountProgress] = []

    def __call__(self, progress: sync.AccountProgress) -> None:
        self.events.append(progress)


def _account(account_id: str = "acc-1", mt_login: str = "12345", broker_server: str = "Broker-Server") -> dict:
    return {
        "accountId": account_id,
        "mtLogin": mt_login,
        "brokerServer": broker_server,
        "investorPassword": "super-secret-password",
    }


def _deal(
    *,
    time: int,
    type_: int = 0,
    entry: int,
    profit: float = 0.0,
    commission: float = 0.0,
    swap: float = 0.0,
    position_id: int = 555,
    symbol: str = "EURUSD",
    volume: float = 1.0,
    price: float = 1.1,
) -> dict:
    return {
        "time": time,
        "type": type_,
        "entry": entry,
        "profit": profit,
        "commission": commission,
        "swap": swap,
        "position_id": position_id,
        "symbol": symbol,
        "volume": volume,
        "price": price,
    }


@pytest.fixture()
def stub_bridge(monkeypatch: pytest.MonkeyPatch):
    """
    Installs a working default stub for every `agent.mt5_bridge` function `sync.py`
    calls, returning a small namespace the test can further tweak per-case.
    """

    class _Stub:
        def __init__(self) -> None:
            self.login_ok = True
            self.last_error: Optional[tuple[int, str]] = None
            self.deals: list[dict] = []
            self.account_info: Optional[_FakeAccountInfo] = _FakeAccountInfo()
            self.utc_offset_seconds = 0
            self.utc_offset_detected = True
            self.stop_take_profit_by_position: dict[int, tuple] = {}
            self.stop_take_profit_raises_for: set[int] = set()
            self.login_calls: list[tuple] = []
            self.pre_existing_session: Optional[dict] = None
            self.initialize_ok = True

        def login_account(self, login, password, server):
            self.login_calls.append((login, password, server))
            return self.login_ok

        def last_error_tuple(self):
            return self.last_error

        def get_history_deals(self, from_dt, to_dt):
            return self.deals

        def get_account_info(self):
            return self.account_info

        def detect_broker_utc_offset_seconds(self):
            return mt5_bridge.UtcOffsetResult(self.utc_offset_seconds, self.utc_offset_detected)

        def get_initial_stop_and_take_profit(self, position_id):
            if position_id in self.stop_take_profit_raises_for:
                raise RuntimeError("stop/target read failed")
            return self.stop_take_profit_by_position.get(position_id, (None, None))

        def initialize_terminal(self, path=None):
            return self.initialize_ok

        def current_logged_in_account(self):
            return self.pre_existing_session

    stub = _Stub()
    monkeypatch.setattr(mt5_bridge, "login_account", stub.login_account)
    monkeypatch.setattr(mt5_bridge, "last_error_tuple", stub.last_error_tuple)
    monkeypatch.setattr(mt5_bridge, "get_history_deals", stub.get_history_deals)
    monkeypatch.setattr(mt5_bridge, "get_account_info", stub.get_account_info)
    monkeypatch.setattr(mt5_bridge, "detect_broker_utc_offset_seconds", stub.detect_broker_utc_offset_seconds)
    monkeypatch.setattr(mt5_bridge, "get_initial_stop_and_take_profit", stub.get_initial_stop_and_take_profit)
    monkeypatch.setattr(mt5_bridge, "initialize_terminal", stub.initialize_terminal)
    monkeypatch.setattr(mt5_bridge, "current_logged_in_account", stub.current_logged_in_account)
    return stub


# ---------------------------------------------------------------------------
# Step 0 — defensive validation before MT5 is touched
# ---------------------------------------------------------------------------

def test_malformed_mt_login_fails_only_this_account_without_touching_mt5(stub_bridge) -> None:
    account = _account(mt_login="not-a-number")
    client = _FakeApiClient()
    reporter = _Reporter()

    outcome = sync.sync_one_account(client, account, reporter)

    assert outcome == errors.OUTCOME_INTERNAL
    assert stub_bridge.login_calls == []  # MT5 was never touched
    assert client.posted_statuses[0]["outcome"] == errors.OUTCOME_INTERNAL
    assert client.posted_statuses[0]["stats"] is None
    assert client.posted_statuses[0]["equity_points"] is None


def test_malformed_broker_server_fails_only_this_account_without_touching_mt5(stub_bridge) -> None:
    account = _account(broker_server="")
    client = _FakeApiClient()
    reporter = _Reporter()

    outcome = sync.sync_one_account(client, account, reporter)

    assert outcome == errors.OUTCOME_INTERNAL
    assert stub_bridge.login_calls == []


# ---------------------------------------------------------------------------
# Login failure classification
# ---------------------------------------------------------------------------

def test_login_failure_classifies_outcome_reports_and_returns_without_raising(stub_bridge) -> None:
    stub_bridge.login_ok = False
    stub_bridge.last_error = (-2, "AUTH_FAILED")  # -2 is a real MQL5 auth-failed code
    client = _FakeApiClient()
    reporter = _Reporter()

    outcome = sync.sync_one_account(client, _account(), reporter)

    assert outcome != errors.OUTCOME_OK
    failed_events = [e for e in reporter.events if e.phase == sync.PHASE_FAILED]
    assert len(failed_events) == 1
    assert failed_events[0].outcome == outcome
    assert client.posted_statuses[0]["outcome"] == outcome
    assert client.posted_statuses[0]["stats"] is None
    assert client.posted_statuses[0]["equity_points"] is None


def test_login_failure_never_reaches_broker_closed_from_a_first_failure(stub_bridge) -> None:
    """
    agent/errors.py's own heuristic requires a prior successful sync AND several
    consecutive failures before it can ever say broker_closed — this call site always
    passes has_synced_successfully_before=False (see sync.py's own header), so a
    single auth failure must classify as plain auth_failed, never broker_closed.
    """
    stub_bridge.login_ok = False
    stub_bridge.last_error = (-2, "AUTH_FAILED")
    client = _FakeApiClient()
    reporter = _Reporter()

    outcome = sync.sync_one_account(client, _account(), reporter)

    assert outcome == errors.OUTCOME_AUTH_FAILED


# ---------------------------------------------------------------------------
# Whole-account exception handling — criterion 8's structural half
# ---------------------------------------------------------------------------

def test_exception_anywhere_inside_one_account_is_caught_classified_internal_never_reraised(
    stub_bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*args, **kwargs):
        raise RuntimeError("simulated IPC explosion")

    monkeypatch.setattr(mt5_bridge, "get_history_deals", _raise)
    client = _FakeApiClient()
    reporter = _Reporter()

    outcome = sync.sync_one_account(client, _account(), reporter)  # must not raise

    assert outcome == errors.OUTCOME_INTERNAL
    assert client.posted_statuses[0]["outcome"] == errors.OUTCOME_INTERNAL
    assert client.posted_statuses[0]["stats"] is None
    assert client.posted_statuses[0]["equity_points"] is None


def test_exception_message_never_contains_the_investor_password(stub_bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args, **kwargs):
        raise RuntimeError("simulated IPC explosion")

    monkeypatch.setattr(mt5_bridge, "get_history_deals", _raise)
    client = _FakeApiClient()
    reporter = _Reporter()
    account = _account()

    sync.sync_one_account(client, account, reporter)

    for event in reporter.events:
        if event.error_reason:
            assert account["investorPassword"] not in event.error_reason


def test_run_sync_over_three_accounts_where_the_middle_one_raises_still_processes_the_third(
    stub_bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE criterion-8 proof: one account's exception must never stop the loop over the rest."""
    accounts = [_account(account_id="acc-1"), _account(account_id="acc-2"), _account(account_id="acc-3")]
    client = _FakeApiClient()
    client.fetch_accounts_result = api_client_module.AccountsFetchResult(token="tok_new", accounts=accounts)

    def _login_that_blows_up_on_the_second_account(login, password, server):
        if login == 12345 and _login_that_blows_up_on_the_second_account.calls == 1:
            _login_that_blows_up_on_the_second_account.calls += 1
            raise RuntimeError("simulated crash mid-login")
        _login_that_blows_up_on_the_second_account.calls += 1
        return True

    _login_that_blows_up_on_the_second_account.calls = 0
    monkeypatch.setattr(mt5_bridge, "login_account", _login_that_blows_up_on_the_second_account)

    reporter = _Reporter()
    summary = sync.run_sync(client, reporter)

    assert summary.total == 3
    # account 1 (call 1) ok, account 2 (call 2) raises -> internal, account 3 (call 3) ok
    assert summary.succeeded == 2
    assert summary.failed == 1
    account_ids_seen = {status["account_id"] for status in client.posted_statuses}
    assert account_ids_seen == {"acc-1", "acc-2", "acc-3"}


def test_config_store_token_persisted_immediately_after_fetch_accounts(
    stub_bridge, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from agent import config_store

    monkeypatch.setenv("APPDATA", str(tmp_path))

    client = _FakeApiClient()
    client.fetch_accounts_result = api_client_module.AccountsFetchResult(token="tok_rotated", accounts=[])

    reporter = _Reporter()
    sync.run_sync(client, reporter)

    assert config_store.load_token() == "tok_rotated"


# ---------------------------------------------------------------------------
# UTC normalisation + CORE-05 (same deposit base for floating equity)
# ---------------------------------------------------------------------------

def test_deals_are_utc_normalised_before_grouping(stub_bridge) -> None:
    offset = 7200  # UTC+2
    broker_local_open = 1_700_000_000
    broker_local_close = 1_700_003_600
    stub_bridge.utc_offset_seconds = offset
    stub_bridge.deals = [
        _deal(time=broker_local_open, entry=0, position_id=999, price=1.1000),
        _deal(time=broker_local_close, entry=1, position_id=999, price=1.1050, profit=50.0),
    ]
    client = _FakeApiClient()
    reporter = _Reporter()

    sync.sync_one_account(client, _account(), reporter)

    assert len(client.posted_trades) == 1
    trade = client.posted_trades[0]["trades"][0]
    expected_opened = datetime.fromtimestamp(broker_local_open - offset, tz=timezone.utc)
    expected_closed = datetime.fromtimestamp(broker_local_close - offset, tz=timezone.utc)
    assert trade["openedAt"] == sync._iso_utc(expected_opened)
    assert trade["closedAt"] == sync._iso_utc(expected_closed)


def test_floating_equity_uses_the_same_deposit_base_as_closed_trades(stub_bridge) -> None:
    stub_bridge.deals = [
        _deal(time=1_700_000_000, type_=2, entry=0, profit=1000.0, position_id=0),  # deposit
        _deal(time=1_700_001_000, entry=0, position_id=111, price=1.10),
        _deal(time=1_700_002_000, entry=1, position_id=111, price=1.11, profit=100.0),
    ]
    # equity = deposit_base(1000) + closed pnl(100) + 50 floating = 1150
    stub_bridge.account_info = _FakeAccountInfo(equity=1150.0)
    client = _FakeApiClient()
    reporter = _Reporter()

    sync.sync_one_account(client, _account(), reporter)

    equity_points = client.posted_statuses[0]["equity_points"]
    assert equity_points is not None
    last_point = equity_points[-1]
    # deposit_base=1000, running_balance after close=1100, floating equity should be
    # (1150-1000)/1000*100 = 15.0 -- computed against the SAME 1000 deposit_base as
    # the closed-trade point immediately before it, not a separately derived value.
    assert last_point["equityPct"] == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# Stop/take-profit folded forward, never a second pass
# ---------------------------------------------------------------------------

def test_initial_stop_and_take_profit_read_once_and_attached_before_posting(stub_bridge) -> None:
    stub_bridge.deals = [
        _deal(time=1_700_000_000, entry=0, position_id=777, price=1.10),
        _deal(time=1_700_001_000, entry=1, position_id=777, price=1.12, profit=20.0),
    ]
    stub_bridge.stop_take_profit_by_position[777] = (1.05, 1.20)
    client = _FakeApiClient()
    reporter = _Reporter()

    sync.sync_one_account(client, _account(), reporter)

    trade = client.posted_trades[0]["trades"][0]
    assert trade["initialStopLoss"] == 1.05
    assert trade["takeProfit"] == 1.20
    # The value arrived ON the post — there was exactly one post_trades call, no
    # second call updating the row afterward.
    assert len(client.posted_trades) == 1


def test_a_failed_stop_take_profit_read_never_aborts_the_account(stub_bridge) -> None:
    stub_bridge.deals = [
        _deal(time=1_700_000_000, entry=0, position_id=888, price=1.10),
        _deal(time=1_700_001_000, entry=1, position_id=888, price=1.12, profit=20.0),
    ]
    stub_bridge.stop_take_profit_raises_for.add(888)
    client = _FakeApiClient()
    reporter = _Reporter()

    outcome = sync.sync_one_account(client, _account(), reporter)

    assert outcome == errors.OUTCOME_OK
    trade = client.posted_trades[0]["trades"][0]
    assert trade["initialStopLoss"] is None
    assert trade["takeProfit"] is None


def test_every_trade_posted_carries_a_non_null_mt5_position_id(stub_bridge) -> None:
    stub_bridge.deals = [
        _deal(time=1_700_000_000, entry=0, position_id=42, price=1.10),
        _deal(time=1_700_001_000, entry=1, position_id=42, price=1.12, profit=20.0),
    ]
    client = _FakeApiClient()
    reporter = _Reporter()

    sync.sync_one_account(client, _account(), reporter)

    trade = client.posted_trades[0]["trades"][0]
    assert trade["mt5PositionId"] is not None
    assert trade["mt5PositionId"] == 42


# ---------------------------------------------------------------------------
# Progress reporting, including failures
# ---------------------------------------------------------------------------

def test_run_sync_reports_progress_for_every_account_including_failures(
    stub_bridge, monkeypatch: pytest.MonkeyPatch
) -> None:

    accounts = [_account(account_id="acc-ok"), _account(account_id="acc-bad", mt_login="nope")]
    client = _FakeApiClient()
    client.fetch_accounts_result = api_client_module.AccountsFetchResult(token="tok", accounts=accounts)
    reporter = _Reporter()

    sync.run_sync(client, reporter)

    reported_account_ids = {e.account_id for e in reporter.events if e.account_id is not None}
    assert reported_account_ids == {"acc-ok", "acc-bad"}
    bad_events = [e for e in reporter.events if e.account_id == "acc-bad"]
    assert any(e.phase == sync.PHASE_FAILED for e in bad_events)


def test_run_sync_reports_a_terminal_check_event_before_any_account(stub_bridge, monkeypatch: pytest.MonkeyPatch) -> None:

    stub_bridge.pre_existing_session = {"login": 999, "server": "Some-Server"}
    client = _FakeApiClient()
    client.fetch_accounts_result = api_client_module.AccountsFetchResult(token="tok", accounts=[])
    reporter = _Reporter()

    sync.run_sync(client, reporter)

    assert reporter.events[0].phase == sync.PHASE_TERMINAL_CHECK
    assert reporter.events[0].pre_existing_session == {"login": 999, "server": "Some-Server"}


def test_run_sync_raises_sync_aborted_error_when_terminal_fails_to_initialize(
    stub_bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_bridge.initialize_ok = False
    client = _FakeApiClient()
    reporter = _Reporter()

    with pytest.raises(sync.SyncAbortedError):
        sync.run_sync(client, reporter)


# ---------------------------------------------------------------------------
# Success path: aggregates + equity points sent on ok, nulled on failure
# ---------------------------------------------------------------------------

def test_successful_account_sends_stats_and_equity_points_with_account_status(stub_bridge) -> None:
    stub_bridge.deals = [
        _deal(time=1_700_000_000, entry=0, position_id=1, price=1.10),
        _deal(time=1_700_001_000, entry=1, position_id=1, price=1.12, profit=20.0),
    ]
    client = _FakeApiClient()
    reporter = _Reporter()

    outcome = sync.sync_one_account(client, _account(), reporter)

    assert outcome == errors.OUTCOME_OK
    status = client.posted_statuses[0]
    assert status["outcome"] == errors.OUTCOME_OK
    assert status["stats"] is not None
    assert status["equity_points"] is not None
    assert "returnAll" in status["stats"]


def test_failed_account_sends_null_stats_and_equity_points(stub_bridge) -> None:
    stub_bridge.login_ok = False
    stub_bridge.last_error = (-2, "AUTH_FAILED")
    client = _FakeApiClient()
    reporter = _Reporter()

    sync.sync_one_account(client, _account(), reporter)

    status = client.posted_statuses[0]
    assert status["stats"] is None
    assert status["equity_points"] is None


# ---------------------------------------------------------------------------
# build_account_stats_payload — pure field-name mapping
# ---------------------------------------------------------------------------

def test_build_account_stats_payload_maps_field_names() -> None:
    stats = {
        "return_all": 1.0,
        "return_1m": 2.0,
        "return_3m": 3.0,
        "return_6m": 4.0,
        "return_1y": 5.0,
        "max_drawdown": 6.0,
        "win_rate": 7.0,
        "profit_factor": 8.0,
        "avg_rr": 9.0,
        "total_trades": 10,
        "trading_period": 11,
        "first_trade_at": 1_700_000_000,
    }

    payload = sync.build_account_stats_payload(stats)

    assert payload == {
        "returnAll": 1.0,
        "return1m": 2.0,
        "return3m": 3.0,
        "return6m": 4.0,
        "return1y": 5.0,
        "maxDrawdown": 6.0,
        "winRate": 7.0,
        "profitFactor": 8.0,
        "avgRr": 9.0,
        "totalTrades": 10,
        "tradingPeriod": 11,
        "firstTradeAt": sync._iso_utc(1_700_000_000),
    }


def test_build_account_stats_payload_handles_null_first_trade_at() -> None:
    stats = {
        "return_all": 0.0,
        "return_1m": 0.0,
        "return_3m": 0.0,
        "return_6m": 0.0,
        "return_1y": 0.0,
        "max_drawdown": 0.0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "avg_rr": 0.0,
        "total_trades": 0,
        "trading_period": 0,
        "first_trade_at": None,
    }

    payload = sync.build_account_stats_payload(stats)

    assert payload["firstTradeAt"] is None


# ---------------------------------------------------------------------------
# Task 3 — structural self-audit of the whole agent/ folder
# ---------------------------------------------------------------------------

class TestAgentFolderStructuralAudit:
    """
    The machine-readable form of README.md's own "verifiable in the source, not just
    claimed here" promises. Each assertion below is one of those claims, made
    automatic rather than merely written down — a future PR that adds any of these
    constructs must fail THIS test before it can ever ship.

    Import detection uses `ast` (never a raw substring grep) for the "no database
    client" claim specifically, because a plain-text grep for `supabase` would
    false-positive on `agent/positions.py`'s own pre-existing, unrelated code comment
    that names the DONOR module `supabase_writer.py` by way of explaining a design
    decision inherited from it — that comment is prose about another file in another
    part of the repository, not a database-client import in THIS one, and flagging it
    would be exactly the "gate matches prose, not behaviour" failure mode this
    project's own verification-integrity rule calls out. Every other claim below is
    checked with a plain, case-insensitive text search, since a legitimate PROSE
    match for e.g. "eval(" or "pickle" the developer intended to write literally is
    vanishingly unlikely and no such match exists in this folder as of this test's
    writing.
    """

    @staticmethod
    def _agent_source_files() -> list[pathlib.Path]:
        agent_dir = pathlib.Path(__file__).resolve().parent.parent
        return sorted(
            p
            for p in agent_dir.rglob("*.py")
            if "tests" not in p.relative_to(agent_dir).parts and "__pycache__" not in p.parts
        )

    def test_no_listening_or_serving_construct(self) -> None:
        forbidden_imports = {"socket", "socketserver", "http.server"}
        offenders: list[str] = []
        for path in self._agent_source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in forbidden_imports:
                            offenders.append(f"{path}:{node.lineno} imports {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module in forbidden_imports:
                    offenders.append(f"{path}:{node.lineno} imports from {node.module}")
        assert not offenders, f"listening/serving construct found: {offenders}"

    def test_no_self_modification_or_dynamic_execution(self) -> None:
        forbidden_calls = {"eval", "exec"}
        offenders: list[str] = []
        for path in self._agent_source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id in forbidden_calls:
                        offenders.append(f"{path}:{node.lineno} calls {node.func.id}(...)")
        assert not offenders, f"dynamic execution construct found: {offenders}"

    def test_no_unsafe_deserialization(self) -> None:
        forbidden_imports = {"pickle", "marshal"}
        offenders: list[str] = []
        for path in self._agent_source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in forbidden_imports:
                            offenders.append(f"{path}:{node.lineno} imports {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module in forbidden_imports:
                    offenders.append(f"{path}:{node.lineno} imports from {node.module}")
                # yaml.load(x) without a Loader= kwarg (or via yaml.unsafe_load) is
                # unsafe; yaml.safe_load(...) is fine and must not be flagged.
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Attribute) and func.attr in ("load", "unsafe_load"):
                        if isinstance(func.value, ast.Name) and func.value.id == "yaml":
                            has_safe_loader_kwarg = any(kw.arg == "Loader" for kw in node.keywords)
                            if func.attr == "unsafe_load" or not has_safe_loader_kwarg:
                                offenders.append(f"{path}:{node.lineno} unsafe yaml.{func.attr}(...)")
        assert not offenders, f"unsafe deserialization construct found: {offenders}"

    def test_no_order_sending_mt5_api_surface(self) -> None:
        forbidden_attrs = {"order_send", "order_check", "order_calc_margin", "order_calc_profit"}
        offenders: list[str] = []
        for path in self._agent_source_files():
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for attr in forbidden_attrs:
                    if attr in line:
                        offenders.append(f"{path}:{lineno} contains {attr!r}")
        assert not offenders, f"order-sending MT5 API surface found: {offenders}"

    def test_no_database_client(self) -> None:
        """
        AST-based (see class docstring): flags an actual `import supabase`/`import
        psycopg`-family statement, never a prose mention of a donor filename in a
        comment.
        """
        forbidden_modules = {"supabase", "psycopg", "psycopg2"}
        offenders: list[str] = []
        for path in self._agent_source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top_level = alias.name.split(".")[0]
                        if top_level in forbidden_modules:
                            offenders.append(f"{path}:{node.lineno} imports {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    top_level = node.module.split(".")[0]
                    if top_level in forbidden_modules:
                        offenders.append(f"{path}:{node.lineno} imports from {node.module}")
        assert not offenders, f"database client import found: {offenders}"

        # A `service_role`-named identifier is still checked as plain text (never
        # expected to appear in legitimate prose in this folder, unlike "supabase").
        service_role_offenders: list[str] = []
        for path in self._agent_source_files():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if "service_role" in line.lower():
                    service_role_offenders.append(f"{path}:{lineno}")
        assert not service_role_offenders, f"service_role reference found: {service_role_offenders}"

    def test_no_tls_verification_bypass(self) -> None:
        offenders: list[str] = []
        for path in self._agent_source_files():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "verify" in line and "=" in line and "false" in line.lower():
                    offenders.append(f"{path}:{lineno}: {line.strip()}")
        assert not offenders, f"TLS verification bypass found: {offenders}"

    def test_no_process_kill_call(self) -> None:
        forbidden_snippets = ("taskkill", ".kill(", "os.kill", "signal.pthread_kill")
        offenders: list[str] = []
        for path in self._agent_source_files():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for snippet in forbidden_snippets:
                    if snippet in line:
                        offenders.append(f"{path}:{lineno} contains {snippet!r}")
        assert not offenders, f"process-kill call found: {offenders}"
