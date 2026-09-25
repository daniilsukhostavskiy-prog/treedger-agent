"""
agent/tests/test_sync.py — the test coverage for `agent/sync.py`,
plus the structural self-audit of the whole `agent/` folder.

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
from agent import errors, mt5_bridge, sync, terminal_discovery, terminal_process

_FAKE_TERMINAL_PATH = r"C:\Fake\MetaTrader 5\terminal64.exe"


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

        def initialize_terminal(self, path=None, **_kwargs):
            self.initialize_calls.append(path)
            self.initialize_kwargs.append(dict(_kwargs))
            self.call_order.append(("initialize", path))
            on_tick = _kwargs.get("on_tick")
            if on_tick is not None:
                for seconds in self.ticks:
                    on_tick(seconds)
            if self.initialize_raises is not None:
                raise self.initialize_raises
            return self.initialize_ok

        def current_logged_in_account(self):
            return self.pre_existing_session

        def last_connect_error_code(self):
            return self.connect_error_code

        def terminal_trade_allowed(self):
            return self.trade_allowed

        def find_terminal_path(self):
            return self.terminal_path

        def list_terminal_processes(self):
            return self.running

        def current_process_elevated(self):
            return self.self_elevated

        def launch_terminal_minimized(self, path):
            self.launch_calls.append(path)
            self.call_order.append(("launch", path))
            if self.launch_raises is not None:
                raise self.launch_raises
            return 4242

    stub = _Stub()
    # Terminal discovery / process stubs — sync.run_sync now calls these itself, and a
    # developer machine may have a REAL MetaTrader 5 installed and running. Default:
    # one non-elevated terminal already running at the fake path, so nothing launches.
    stub.terminal_path = _FAKE_TERMINAL_PATH
    stub.running = [
        terminal_process.RunningTerminal(
            pid=1111, exe_path=_FAKE_TERMINAL_PATH, elevated=False, elevation_source="token"
        )
    ]
    stub.self_elevated = False
    stub.launch_calls = []
    stub.launch_raises = None
    stub.initialize_calls = []
    stub.initialize_kwargs = []
    stub.initialize_raises = None
    stub.connect_error_code = None
    stub.trade_allowed = True
    stub.call_order = []
    stub.ticks = []
    monkeypatch.setattr(terminal_discovery, "find_terminal_path", stub.find_terminal_path)
    monkeypatch.setattr(terminal_process, "list_terminal_processes", stub.list_terminal_processes)
    monkeypatch.setattr(terminal_process, "current_process_elevated", stub.current_process_elevated)
    monkeypatch.setattr(terminal_process, "launch_terminal_minimized", stub.launch_terminal_minimized)
    monkeypatch.setattr(mt5_bridge, "login_account", stub.login_account)
    monkeypatch.setattr(mt5_bridge, "last_error_tuple", stub.last_error_tuple)
    monkeypatch.setattr(mt5_bridge, "get_history_deals", stub.get_history_deals)
    monkeypatch.setattr(mt5_bridge, "get_account_info", stub.get_account_info)
    monkeypatch.setattr(mt5_bridge, "detect_broker_utc_offset_seconds", stub.detect_broker_utc_offset_seconds)
    monkeypatch.setattr(mt5_bridge, "get_initial_stop_and_take_profit", stub.get_initial_stop_and_take_profit)
    monkeypatch.setattr(mt5_bridge, "initialize_terminal", stub.initialize_terminal)
    monkeypatch.setattr(mt5_bridge, "current_logged_in_account", stub.current_logged_in_account)
    monkeypatch.setattr(mt5_bridge, "last_connect_error_code", stub.last_connect_error_code)
    monkeypatch.setattr(mt5_bridge, "terminal_trade_allowed", stub.terminal_trade_allowed)
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
# 260925-k6y — find -> launch-if-needed -> connect
# ---------------------------------------------------------------------------

def _empty_fetch_client() -> _FakeApiClient:
    client = _FakeApiClient()
    client.fetch_accounts_result = api_client_module.AccountsFetchResult(token="tok", accounts=[])
    return client


def test_no_running_terminal_launches_discovered_path_before_initialize(stub_bridge) -> None:
    stub_bridge.running = []

    sync.run_sync(_empty_fetch_client(), _Reporter())

    assert stub_bridge.launch_calls == [_FAKE_TERMINAL_PATH]
    assert stub_bridge.call_order[0] == ("launch", _FAKE_TERMINAL_PATH)
    assert stub_bridge.call_order[1] == ("initialize", _FAKE_TERMINAL_PATH)


def test_running_terminal_is_never_launched_again(stub_bridge) -> None:
    sync.run_sync(_empty_fetch_client(), _Reporter())

    assert stub_bridge.launch_calls == []
    assert stub_bridge.initialize_calls == [_FAKE_TERMINAL_PATH]


def test_unknown_process_list_launches_nothing(stub_bridge) -> None:
    stub_bridge.running = None

    sync.run_sync(_empty_fetch_client(), _Reporter())

    assert stub_bridge.launch_calls == []


def test_launch_oserror_raises_terminal_launch_error_and_never_initializes(stub_bridge) -> None:
    stub_bridge.running = []
    stub_bridge.launch_raises = OSError("access denied")

    with pytest.raises(sync.TerminalLaunchError):
        sync.run_sync(_empty_fetch_client(), _Reporter())

    assert stub_bridge.initialize_calls == []
    assert issubclass(sync.TerminalLaunchError, sync.SyncAbortedError)


def test_unresponsive_with_elevated_terminal_sets_elevation_mismatch(stub_bridge) -> None:
    stub_bridge.running = [
        terminal_process.RunningTerminal(
            pid=7, exe_path=_FAKE_TERMINAL_PATH, elevated=True, elevation_source="access_denied_inference"
        )
    ]
    stub_bridge.self_elevated = False
    stub_bridge.initialize_raises = mt5_bridge.TerminalUnresponsiveError("hung", waited_seconds=90)

    with pytest.raises(mt5_bridge.TerminalUnresponsiveError) as info:
        sync.run_sync(_empty_fetch_client(), _Reporter())

    assert info.value.elevation_mismatch is True
    assert info.value.waited_seconds == 90


def test_unresponsive_without_elevated_terminal_has_no_elevation_mismatch(stub_bridge) -> None:
    stub_bridge.initialize_raises = mt5_bridge.TerminalUnresponsiveError("hung", waited_seconds=90)

    with pytest.raises(mt5_bridge.TerminalUnresponsiveError) as info:
        sync.run_sync(_empty_fetch_client(), _Reporter())

    assert info.value.elevation_mismatch is False


def test_no_terminal_found_raises_and_launches_nothing(stub_bridge) -> None:
    stub_bridge.terminal_path = None
    stub_bridge.running = []

    with pytest.raises(terminal_discovery.TerminalNotFoundError):
        sync.run_sync(_empty_fetch_client(), _Reporter())

    assert stub_bridge.launch_calls == []
    assert stub_bridge.initialize_calls == []


# ---------------------------------------------------------------------------
# 260925-k6y Task 2 — stage reporting and between-steps cancel
# ---------------------------------------------------------------------------

class _StageRecorder:
    """Records stage updates AND account progress into one ordered timeline."""

    def __init__(self) -> None:
        self.timeline: "list[tuple[str, Any]]" = []

    def stage(self, progress: "sync.StageProgress") -> None:
        self.timeline.append(("stage", (progress.stage, progress.status, progress.detail, progress.elapsed_seconds)))

    def report(self, progress: "sync.AccountProgress") -> None:
        self.timeline.append(("account", (progress.account_id, progress.phase)))

    def stages(self) -> "list[tuple[str, str]]":
        return [(s, st) for kind, (s, st, *_rest) in self.timeline if kind == "stage"]


def _two_account_client() -> _FakeApiClient:
    client = _FakeApiClient()
    client.fetch_accounts_result = api_client_module.AccountsFetchResult(
        token="tok", accounts=[_account(account_id="acc-1"), _account(account_id="acc-2")]
    )
    return client


def test_no_launch_run_reports_stages_in_order_before_first_login(stub_bridge) -> None:
    rec = _StageRecorder()

    sync.run_sync(_two_account_client(), rec.report, report_stage=rec.stage)

    expected = [
        (sync.STAGE_FIND_TERMINAL, sync.STAGE_IN_PROGRESS),
        (sync.STAGE_FIND_TERMINAL, sync.STAGE_DONE),
        (sync.STAGE_CONNECT, sync.STAGE_IN_PROGRESS),
        (sync.STAGE_CONNECT, sync.STAGE_DONE),
        (sync.STAGE_FETCH_ACCOUNTS, sync.STAGE_IN_PROGRESS),
        (sync.STAGE_FETCH_ACCOUNTS, sync.STAGE_DONE),
    ]
    assert rec.stages() == expected
    details = {(v[0], v[1]): v[2] for kind, v in rec.timeline if kind == "stage"}
    assert details[(sync.STAGE_FIND_TERMINAL, sync.STAGE_DONE)] == _FAKE_TERMINAL_PATH
    assert details[(sync.STAGE_FETCH_ACCOUNTS, sync.STAGE_DONE)] == "2"

    fetch_done_index = rec.timeline.index(
        ("stage", (sync.STAGE_FETCH_ACCOUNTS, sync.STAGE_DONE, "2", None))
    )
    first_login_index = next(
        i for i, (kind, value) in enumerate(rec.timeline)
        if kind == "account" and value[1] == sync.PHASE_LOGIN
    )
    assert fetch_done_index < first_login_index


def test_launch_run_also_reports_the_launch_stage(stub_bridge) -> None:
    stub_bridge.running = []
    rec = _StageRecorder()

    sync.run_sync(_empty_fetch_client(), rec.report, report_stage=rec.stage)

    stages = rec.stages()
    assert (sync.STAGE_LAUNCH_TERMINAL, sync.STAGE_IN_PROGRESS) in stages
    assert (sync.STAGE_LAUNCH_TERMINAL, sync.STAGE_DONE) in stages
    assert stages.index((sync.STAGE_LAUNCH_TERMINAL, sync.STAGE_DONE)) < stages.index(
        (sync.STAGE_CONNECT, sync.STAGE_IN_PROGRESS)
    )


def test_connect_tick_surfaces_as_elapsed_seconds(stub_bridge) -> None:
    stub_bridge.ticks = [3]
    rec = _StageRecorder()

    sync.run_sync(_empty_fetch_client(), rec.report, report_stage=rec.stage)

    assert ("stage", (sync.STAGE_CONNECT, sync.STAGE_IN_PROGRESS, None, 3)) in rec.timeline


def test_failed_connect_reports_connect_failed_before_raising(stub_bridge) -> None:
    stub_bridge.initialize_ok = False
    rec = _StageRecorder()

    with pytest.raises(sync.SyncAbortedError):
        sync.run_sync(_empty_fetch_client(), rec.report, report_stage=rec.stage)

    assert rec.stages()[-1] == (sync.STAGE_CONNECT, sync.STAGE_FAILED)


# ---------------------------------------------------------------------------
# quick 260925-qhs — cold-start budget, «Алготрейдинг» connect hint, trade_allowed
# ---------------------------------------------------------------------------

def test_launched_terminal_gets_the_cold_start_connect_budget(stub_bridge) -> None:
    stub_bridge.running = []

    sync.run_sync(_empty_fetch_client(), _Reporter())

    assert stub_bridge.initialize_kwargs[0].get("budget_seconds") == (
        mt5_bridge.COLD_START_CONNECT_BUDGET_SECONDS
    )


def test_already_running_terminal_passes_no_budget_keyword(stub_bridge) -> None:
    sync.run_sync(_empty_fetch_client(), _Reporter())

    assert "budget_seconds" not in stub_bridge.initialize_kwargs[0]


def test_launched_terminal_still_passes_on_tick_with_the_budget(stub_bridge) -> None:
    stub_bridge.running = []
    rec = _StageRecorder()

    sync.run_sync(_empty_fetch_client(), rec.report, report_stage=rec.stage)

    kwargs = stub_bridge.initialize_kwargs[0]
    assert callable(kwargs.get("on_tick"))
    assert kwargs.get("budget_seconds") == mt5_bridge.COLD_START_CONNECT_BUDGET_SECONDS


def test_connect_failure_with_algo_hint_code_raises_algo_trading_suspected(stub_bridge) -> None:
    stub_bridge.initialize_ok = False
    stub_bridge.connect_error_code = -10005
    rec = _StageRecorder()

    with pytest.raises(sync.AlgoTradingSuspectedError) as info:
        sync.run_sync(_empty_fetch_client(), rec.report, report_stage=rec.stage)

    assert info.value.error_code == -10005
    assert isinstance(info.value, sync.SyncAbortedError)
    assert rec.stages()[-1] == (sync.STAGE_CONNECT, sync.STAGE_FAILED)


def test_connect_failure_with_minus_6_raises_plain_sync_aborted(stub_bridge) -> None:
    stub_bridge.initialize_ok = False
    stub_bridge.connect_error_code = -6

    with pytest.raises(sync.SyncAbortedError) as info:
        sync.run_sync(_empty_fetch_client(), _Reporter())

    assert not isinstance(info.value, sync.AlgoTradingSuspectedError)


def test_connect_failure_with_unknown_code_raises_plain_sync_aborted(stub_bridge) -> None:
    stub_bridge.initialize_ok = False
    stub_bridge.connect_error_code = None

    with pytest.raises(sync.SyncAbortedError) as info:
        sync.run_sync(_empty_fetch_client(), _Reporter())

    assert not isinstance(info.value, sync.AlgoTradingSuspectedError)


def test_trade_not_allowed_is_reported_on_terminal_check_and_run_continues(stub_bridge) -> None:
    stub_bridge.trade_allowed = False
    reporter = _Reporter()

    summary = sync.run_sync(_two_account_client(), reporter)

    terminal_check = [e for e in reporter.events if e.phase == sync.PHASE_TERMINAL_CHECK]
    assert len(terminal_check) == 1
    assert terminal_check[0].algo_trading_allowed is False
    assert summary.total == 2
    walked = {e.account_id for e in reporter.events if e.phase == sync.PHASE_LOGIN}
    assert walked == {"acc-1", "acc-2"}


def test_trade_allowed_true_and_unknown_are_forwarded_verbatim(stub_bridge) -> None:
    for value in (True, None):
        stub_bridge.trade_allowed = value
        reporter = _Reporter()
        sync.run_sync(_empty_fetch_client(), reporter)
        terminal_check = [e for e in reporter.events if e.phase == sync.PHASE_TERMINAL_CHECK]
        assert terminal_check[0].algo_trading_allowed is value


def test_account_progress_algo_field_is_last_and_defaults_to_none() -> None:
    import dataclasses

    fields = dataclasses.fields(sync.AccountProgress)
    assert fields[-1].name == "algo_trading_allowed"
    assert sync.AccountProgress(account_id=None, mt_login=None, phase="x").algo_trading_allowed is None


def test_cancel_during_fetch_raises_after_token_saved_with_no_logins(
    stub_bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from agent import config_store

    saved: "list[str]" = []
    monkeypatch.setattr(config_store, "save_token", saved.append)
    cancel = threading.Event()
    client = _two_account_client()
    original_fetch = client.fetch_accounts

    def _fetch_then_cancel():
        cancel.set()
        return original_fetch()

    client.fetch_accounts = _fetch_then_cancel  # type: ignore[method-assign]

    with pytest.raises(sync.SyncCancelledError):
        sync.run_sync(client, _Reporter(), cancel_event=cancel)

    assert saved == ["tok"]
    assert stub_bridge.login_calls == []


def test_cancel_when_first_account_done_leaves_second_unprocessed(stub_bridge) -> None:
    import threading

    cancel = threading.Event()
    reporter = _Reporter()

    def _report(progress):
        reporter(progress)
        if progress.phase == sync.PHASE_DONE:
            cancel.set()

    with pytest.raises(sync.SyncCancelledError):
        sync.run_sync(_two_account_client(), _report, cancel_event=cancel)

    assert len(stub_bridge.login_calls) == 1
    assert not any(e.account_id == "acc-2" for e in reporter.events)


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
# Task 3 — structural self-audit of the whole agent/ folder, plus 40-13's five
# additional claims and its ast-based rewrite of three pre-existing
# text-search siblings that shared the exact same defect as
# `test_no_process_kill_call` (see that method's own docstring below for the
# concrete history).
# ---------------------------------------------------------------------------


class _FunctionScopeVisitor(ast.NodeVisitor):
    """
    Shared base for every visitor below that needs to know not just THAT a
    construct exists, but WHICH function it lives inside. Tracks the
    innermost enclosing `FunctionDef`/`AsyncFunctionDef` node as a stack;
    `None` means module scope.
    """

    def __init__(self) -> None:
        self._stack: "list[ast.AST]" = []

    def _current_function(self) -> "Optional[ast.AST]":
        return self._stack[-1] if self._stack else None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._stack.append(node)
        self.generic_visit(node)
        self._stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._stack.append(node)
        self.generic_visit(node)
        self._stack.pop()


def _function_label(func_node: "Optional[ast.AST]") -> str:
    return getattr(func_node, "name", None) or "<module scope>"


def _find_top_level_function(tree: ast.Module, name: str) -> "Optional[ast.AST]":
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _docstring_constant_ids(tree: ast.Module) -> "set[int]":
    """
    `id()` of every string `Constant` node that is an actual module/class/
    function docstring — the bare-string `Expr` that is the FIRST statement
    of its enclosing body. Used so a check can ignore prose that names a
    forbidden construct in order to explain the rule against it, without
    ignoring the SAME literal string used as real code elsewhere (e.g. a list
    element passed to `subprocess.run`).
    """
    ids: "set[int]" = set()
    containers: "list[ast.AST]" = [tree]
    containers.extend(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    for container in containers:
        body = getattr(container, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


# ---- order-sending MT5 API surface (converted from a text search) ---------

_ORDER_SENDING_ATTRS = {"order_send", "order_check", "order_calc_margin", "order_calc_profit"}


def _order_sending_offenders(files: "list[pathlib.Path]") -> "list[str]":
    offenders: "list[str]" = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in _ORDER_SENDING_ATTRS:
                offenders.append(f"{path}:{node.lineno} references .{node.attr}(...)")
    return offenders


# ---- TLS verification bypass (converted from a text search) ---------------


def _is_false_constant(node: "Optional[ast.AST]") -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _assign_targets(node: "ast.AST") -> "list[ast.AST]":
    if isinstance(node, ast.Assign):
        return node.targets
    if isinstance(node, ast.AnnAssign) and node.target is not None:
        return [node.target]
    return []


def _target_name(node: "ast.AST") -> "Optional[str]":
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _tls_bypass_offenders(files: "list[pathlib.Path]") -> "list[str]":
    offenders: "list[str]" = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "verify" and _is_false_constant(kw.value):
                        offenders.append(f"{path}:{node.lineno} passes verify=False")
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and _is_false_constant(
                getattr(node, "value", None)
            ):
                for target in _assign_targets(node):
                    if _target_name(target) == "verify":
                        offenders.append(f"{path}:{node.lineno} assigns verify = False")
    return offenders


# ---- process-kill call (the defect 40-13 exists to fix) -------------------

# "terminate"/"TerminateProcess" added by quick task 260925-k6y: the program now STARTS
# a process (the MT5 terminal, when none is running), so ending one — including the
# one it started — must stay structurally impossible, not merely unconventional.
_KILL_CALL_ATTRS = {"kill", "pthread_kill", "terminate", "TerminateProcess"}
_KILL_STRING_SUBSTRING = "taskkill"


def _process_kill_offenders(files: "list[pathlib.Path]") -> "list[str]":
    offenders: "list[str]" = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstring_ids = _docstring_constant_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                attr = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else (func.id if isinstance(func, ast.Name) else None)
                )
                if attr in _KILL_CALL_ATTRS:
                    offenders.append(
                        f"{path}:{node.lineno} calls a process-kill function ({attr}(...))"
                    )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstring_ids:
                    continue
                if _KILL_STRING_SUBSTRING in node.value.lower():
                    offenders.append(
                        f"{path}:{node.lineno} string constant contains "
                        f"{_KILL_STRING_SUBSTRING!r}"
                    )
    return offenders


# ---- no call ever passes a `shell` keyword ----------------------------------


def _shell_keyword_offenders(files: "list[pathlib.Path]") -> "list[str]":
    """Every `Call` node that passes a `shell=` keyword at all (any value) — the
    program starts processes only from argument lists."""
    offenders: "list[str]" = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and any(kw.arg == "shell" for kw in node.keywords):
                offenders.append(f"{path}:{node.lineno} passes a shell= keyword")
    return offenders


# ---- the stored token is never read as plaintext ---------------------------

_TOKEN_KEY_IDENTIFIER = "_KEY_TOKEN"


def _is_token_key_reference(node: "Optional[ast.AST]") -> bool:
    return isinstance(node, ast.Name) and node.id == _TOKEN_KEY_IDENTIFIER


class _TokenKeyReadVisitor(_FunctionScopeVisitor):
    """
    Every READ (never a write) of the token config key: a `Load`-context
    subscript, or a `.get(...)` call, keyed by the `_KEY_TOKEN` identifier —
    deliberately never the bare literal `"token"`, which `agent/api_client.py`
    also uses for an unrelated HTTP response field. Matching only the
    identifier is what keeps this check from false-positiving on that
    unrelated, legitimate code — the same word, two unrelated meanings, and
    only the actual reference disambiguates them.
    """

    def __init__(self) -> None:
        super().__init__()
        self.reads: "list[tuple[ast.AST, Optional[ast.AST]]]" = []

    def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802
        key = node.slice
        if isinstance(node.ctx, ast.Load) and _is_token_key_reference(key):
            self.reads.append((node, self._current_function()))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and node.args
            and _is_token_key_reference(node.args[0])
        ):
            self.reads.append((node, self._current_function()))
        self.generic_visit(node)


def _assigned_name_for(func_node: "Optional[ast.AST]", read_node: ast.AST) -> "Optional[str]":
    """The bare name a read expression is directly assigned to, e.g. `raw` in
    `raw = data.get(_KEY_TOKEN)` — `None` if the read is not a direct,
    single-target assignment (the only shape the real loader uses)."""
    if func_node is None:
        return None
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assign) and node.value is read_node:
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                return node.targets[0].id
    return None


def _first_decrypt_call_lineno(func_node: "Optional[ast.AST]") -> "Optional[int]":
    """The earliest line, if any, on which `func_node` calls something whose
    attribute name is `unprotect` (the one decrypt entry point)."""
    if func_node is None:
        return None
    linenos = [
        node.lineno
        for node in ast.walk(func_node)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "unprotect"
    ]
    return min(linenos) if linenos else None


def _returns_raw_before_decrypting(
    func_node: "Optional[ast.AST]", read_node: ast.AST, assigned_name: "Optional[str]"
) -> bool:
    """True when `func_node` returns the read expression itself, or the bare
    name it was assigned to, at or before the first decrypt call — or when no
    decrypt call exists in the function at all."""
    if func_node is None:
        return False
    decrypt_lineno = _first_decrypt_call_lineno(func_node)
    if decrypt_lineno is None:
        return True
    for node in ast.walk(func_node):
        if isinstance(node, ast.Return) and node.value is not None:
            is_raw_directly = node.value is read_node
            is_raw_by_name = (
                assigned_name is not None
                and isinstance(node.value, ast.Name)
                and node.value.id == assigned_name
            )
            if (is_raw_directly or is_raw_by_name) and node.lineno <= decrypt_lineno:
                return True
    return False


def _plaintext_token_offenders(files: "list[pathlib.Path]") -> "list[str]":
    offenders: "list[str]" = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _TokenKeyReadVisitor()
        visitor.visit(tree)
        for read_node, func_node in visitor.reads:
            permitted_site = (
                path.name == "config_store.py" and _function_label(func_node) == "load_token"
            )
            if not permitted_site:
                offenders.append(
                    f"{path}:{read_node.lineno} reads the token config key outside "
                    f"config_store.load_token() (in {_function_label(func_node)})"
                )
                continue
            assigned_name = _assigned_name_for(func_node, read_node)
            if _returns_raw_before_decrypting(func_node, read_node, assigned_name):
                offenders.append(
                    f"{path}:{read_node.lineno} in load_token() reaches a return without "
                    "first passing the value through dpapi.unprotect(...)"
                )
    return offenders


# ---- no non-Windows branch ever hands back a value -------------------------


def _references_platform(test_node: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Attribute) and node.attr == "platform" for node in ast.walk(test_node)
    )


def _branch_returns_or_yields(branch: "list[ast.stmt]") -> "list[ast.AST]":
    offenders: "list[ast.AST]" = []
    for stmt in branch:
        for node in ast.walk(stmt):
            if isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom)):
                offenders.append(node)
    return offenders


def _dpapi_fallback_offenders(files: "list[pathlib.Path]") -> "list[str]":
    offenders: "list[str]" = []
    for path in files:
        if path.name != "dpapi.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and _references_platform(node.test):
                for branch_node in _branch_returns_or_yields(node.body) + _branch_returns_or_yields(
                    node.orelse
                ):
                    offenders.append(
                        f"{path}:{branch_node.lineno} a platform-examining `if` "
                        f"(line {node.lineno}) returns/yields a value instead of only "
                        "ever raising"
                    )
    return offenders


# ---- the command line never sources a credential ---------------------------

_PERMITTED_ARGV_SITES = {
    ("main.py", "main"),
    ("autostart.py", "own_executable_path"),
}


class _ArgvReferenceVisitor(_FunctionScopeVisitor):
    """Every `sys.argv` reference (an `Attribute` node: `value=Name('sys')`,
    `attr='argv'`), tagged with its enclosing function."""

    def __init__(self) -> None:
        super().__init__()
        self.refs: "list[tuple[ast.AST, Optional[ast.AST]]]" = []

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        if node.attr == "argv" and isinstance(node.value, ast.Name) and node.value.id == "sys":
            self.refs.append((node, self._current_function()))
        self.generic_visit(node)


def _contains_subscript(node: ast.AST) -> "Optional[ast.Subscript]":
    for sub in ast.walk(node):
        if isinstance(sub, ast.Subscript):
            return sub
    return None


def _argv_offenders(files: "list[pathlib.Path]") -> "list[str]":
    offenders: "list[str]" = []
    for path in files:
        # `agent/packaging/` is CI/build tooling (e.g. `assert_dpapi_executed.py`, a
        # standalone junit-xml report checker invoked as `python
        # packaging/assert_dpapi_executed.py <path>`) — it runs on a build server, is
        # never imported by the `agent` package, and never ships inside the built
        # executable. Its own `sys.argv` usage is a plain CLI-script argument, not a
        # credential path into this PROGRAM, so it is out of scope for this claim —
        # unlike `tests`, which `_agent_source_files()` itself already excludes for
        # every check, `packaging` still needs scanning by the other eleven claims
        # (e.g. no eval/exec), so the exclusion is scoped to this one check only.
        if "packaging" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        visitor = _ArgvReferenceVisitor()
        visitor.visit(tree)
        for ref_node, func_node in visitor.refs:
            site = (path.name, _function_label(func_node))
            if site not in _PERMITTED_ARGV_SITES:
                offenders.append(
                    f"{path}:{ref_node.lineno} references sys.argv outside a permitted site "
                    f"(in {_function_label(func_node)})"
                )

        if path.name != "main.py":
            continue
        parse_argv = _find_top_level_function(tree, "parse_argv")
        if parse_argv is None:
            continue
        for node in ast.walk(parse_argv):
            if isinstance(node, ast.Return) and node.value is not None:
                leak = _contains_subscript(node.value)
                if leak is not None:
                    offenders.append(
                        f"{path}:{leak.lineno} parse_argv()'s return value indexes into an "
                        "argument list directly instead of only testing membership"
                    )
    return offenders


# ---- the autostart registry write has exactly one home ---------------------
#
# quick 260925-qhs: autostart is a per-user HKCU Run value, no longer a Task
# Scheduler entry. The claim keeps its shape: the one thing that can put this program
# into Windows autostart (a registry SetValueEx) lives in exactly one function, and the
# one function allowed to CREATE an autostart entry has exactly one caller — the
# checkbox handler.

_AUTOSTART_ENABLE_FUNC = "enable_autostart"
_AUTOSTART_ENABLE_HOME = ("main.py", "_on_autostart_toggled")
_REGISTRY_WRITE_ATTR = "SetValueEx"
_REGISTRY_WRITE_HOME = ("autostart.py", "_write_own_command")


class _AttributeCallVisitor(_FunctionScopeVisitor):
    """Every call whose target is an `Attribute` named `attr` (e.g.
    `autostart.enable_autostart(...)`, `_winreg.SetValueEx(...)`), tagged with its
    enclosing function. A docstring or comment naming the same word is not a Call node
    and is therefore invisible here."""

    def __init__(self, attr: str) -> None:
        super().__init__()
        self._attr = attr
        self.calls: "list[tuple[ast.AST, Optional[ast.AST]]]" = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == self._attr:
            self.calls.append((node, self._current_function()))
        self.generic_visit(node)


def _attribute_call_sites(
    files: "list[pathlib.Path]", attr: str
) -> "list[tuple[pathlib.Path, ast.AST, Optional[ast.AST]]]":
    sites: "list[tuple[pathlib.Path, ast.AST, Optional[ast.AST]]]" = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _AttributeCallVisitor(attr)
        visitor.visit(tree)
        for call_node, func_node in visitor.calls:
            sites.append((path, call_node, func_node))
    return sites


def _single_home_offenders(
    files: "list[pathlib.Path]", attr: str, home: "tuple[str, str]", what: str
) -> "list[str]":
    sites = _attribute_call_sites(files, attr)
    if len(sites) != 1:
        return [
            f"expected exactly one {what} ({attr}(...)) call site, found {len(sites)}: "
            + ", ".join(f"{p}:{n.lineno} in {_function_label(f)}" for p, n, f in sites)
        ]
    path, node, func = sites[0]
    if (path.name, _function_label(func)) != home:
        return [
            f"{path}:{node.lineno} the only {what} ({attr}(...)) lives in "
            f"{path.name}:{_function_label(func)}, expected {home[0]}:{home[1]}"
        ]
    return []


def _autostart_write_offenders(files: "list[pathlib.Path]") -> "list[str]":
    return _single_home_offenders(
        files, _AUTOSTART_ENABLE_FUNC, _AUTOSTART_ENABLE_HOME, "autostart enable"
    ) + _single_home_offenders(
        files, _REGISTRY_WRITE_ATTR, _REGISTRY_WRITE_HOME, "registry write"
    )


# ---- the single-instance lock is always the first thing that happens ------

_FORBIDDEN_PRE_LOCK_MODULES = {"config_store", "terminal_discovery", "mt5_bridge"}


def _resolves_to_lock_acquire(call_node: ast.Call) -> bool:
    func = call_node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "acquire_single_instance_lock"
        and isinstance(func.value, ast.Name)
        and func.value.id == "single_instance"
    )


def _statement_index(func_node: ast.AST, call_node: ast.Call) -> "Optional[int]":
    for index, stmt in enumerate(func_node.body):  # type: ignore[attr-defined]
        for node in ast.walk(stmt):
            if node is call_node:
                return index
    return None


def _lock_ordering_offenders(files: "list[pathlib.Path]") -> "list[str]":
    offenders: "list[str]" = []
    for path in files:
        if path.name != "main.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        entry = _find_top_level_function(tree, "main")
        if entry is None:
            offenders.append(f"{path}: no top-level main() function found")
            continue

        positioned: "list[tuple[tuple[int, int, int], ast.Call]]" = []
        for node in ast.walk(entry):
            if isinstance(node, ast.Call):
                stmt_index = _statement_index(entry, node)
                if stmt_index is not None:
                    positioned.append(((stmt_index, node.lineno, node.col_offset), node))
        positioned.sort(key=lambda item: item[0])

        if not positioned:
            offenders.append(f"{path}: main() makes no calls at all")
            continue

        _first_pos, first_call = positioned[0]
        if not _resolves_to_lock_acquire(first_call):
            offenders.append(
                f"{path}:{first_call.lineno} the first call main() makes is not "
                "single_instance.acquire_single_instance_lock(...)"
            )
    return offenders


class TestAgentFolderStructuralAudit:
    """
    The machine-readable form of README.md's own "verifiable in the source, not just
    claimed here" promises. Each assertion below is one of those claims, made
    automatic rather than merely written down — a future PR that adds any of these
    constructs must fail THIS test before it can ever ship.

    Every claim below is decided by walking the actual `ast` of the source — never a
    raw substring/line grep — because a text search matches the WORDS a rule uses to
    STATE a prohibition as readily as a VIOLATION of it. Two concrete examples proved
    this the hard way: a plain-text grep for "supabase" would false-positive on
    `agent/positions.py`'s own pre-existing, unrelated code comment that names a donor
    module elsewhere in the repository by way of explaining a design decision inherited
    from it; and an earlier, line-based version of the process-kill check below matched
    the literal words "taskkill" and ".kill(" inside a docstring in
    `agent/autostart.py` that was explaining, in prose, why that module has nothing to
    do with killing a process. Both are the same failure mode: the artifact getting
    bent to satisfy the check instead of the check being made to read what the code
    actually DOES. The thirteen claims this class enforces:

    1. No listening/serving construct is imported anywhere (`socket`,
       `socketserver`, `http.server`).
    2. No dynamic-execution construct is called anywhere (`eval`, `exec`).
    3. No unsafe deserialization construct is imported or invoked anywhere
       (`pickle`, `marshal`, an unguarded `yaml.load`/`yaml.unsafe_load`).
    4. No order-placing MT5 API surface is ever referenced
       (`order_send`/`order_check`/`order_calc_margin`/`order_calc_profit`).
    5. No database client is imported, and no `service_role`-named identifier
       appears, anywhere in this folder.
    6. No TLS certificate-verification bypass (`verify=False`, or an
       assignment of `False` to a variable/attribute named `verify`) exists
       anywhere.
    7. No call that ends another process (`.kill(...)`, `os.kill(...)`,
       `signal.pthread_kill(...)`, `.terminate(...)`, `TerminateProcess(...)`, or
       the literal `taskkill` string outside a docstring) exists anywhere — not
       even for the MT5 terminal this program itself may have started.
    8. The persisted authentication token is read from disk in exactly one
       place, and that read's value always passes through the DPAPI decrypt
       call before it can be returned — never handed back as plaintext.
    9. The DPAPI module may refuse to operate on a non-Windows platform, but
       neither branch of a platform check may ever return or yield a value —
       only raise.
    10. The command line is read to build the window's own launch options in
        exactly two narrow, documented, non-credential shapes; nothing else
        in the tree reads it, and the launch-options builder itself never
        extracts a raw command-line element into a string field.
    11. The registry write that puts this program into Windows autostart
        (`SetValueEx`) appears in exactly one function in the whole tree
        (`autostart._write_own_command`), and the one function allowed to create
        an autostart value (`autostart.enable_autostart`) has exactly one caller,
        the checkbox handler (quick 260925-qhs: HKCU Run value, no longer a Task
        Scheduler entry).
    12. The single-instance lock is the very first call the program's entry
        point makes — before the command line is parsed, before the
        configuration file is touched, and before the GUI toolkit's own root
        window is constructed.
    13. No call anywhere passes a `shell` keyword — every process this program
        starts (the MT5 terminal) is started from an argument list.
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

    def test_no_order_sending_mt5_api_surface(self, tmp_path: pathlib.Path) -> None:
        """
        AST-based (converted from a line-based text search in this same plan that fixed
        the process-kill check below): flags an actual `Attribute` node named for one of
        MT5's four order-placing API calls, never a prose mention in a comment or
        docstring — this module never sends an order, and a text search over these exact
        identifier strings would trip on a docstring that quotes one of them to explain
        why it is forbidden. An attribute name simply cannot appear as an `Attribute`
        AST node from inside a string constant, so no explicit docstring exclusion is
        even needed here — unlike the process-kill check, which must also catch a bare
        string literal.
        """
        offenders = _order_sending_offenders(self._agent_source_files())
        assert not offenders, f"order-sending MT5 API surface found: {offenders}"

        violation_dir = tmp_path / "violation"
        violation_dir.mkdir()
        violation = violation_dir / "evil.py"
        violation.write_text(
            "import MetaTrader5 as mt5\n\n"
            "def place_order():\n"
            "    return mt5.order_send({})\n",
            encoding="utf-8",
        )
        assert _order_sending_offenders([violation]), "a real order_send(...) call must be reported"

        safe_dir = tmp_path / "safe"
        safe_dir.mkdir()
        safe = safe_dir / "safe.py"
        safe.write_text(
            '"""This module never calls order_send, order_check, order_calc_margin or '
            'order_calc_profit."""\n'
            "def noop():\n"
            "    return None\n",
            encoding="utf-8",
        )
        assert not _order_sending_offenders([safe]), "a docstring mention must not be reported"

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
        # expected to appear in legitimate prose in this folder, unlike "supabase") —
        # deliberately left as a text search: the whole point of this specific
        # sub-check is that even a PROSE mention of `service_role` in this folder is
        # itself suspicious enough to want visibility, the opposite of the
        # docstring-false-positive problem the rest of this class guards against.
        service_role_offenders: list[str] = []
        for path in self._agent_source_files():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if "service_role" in line.lower():
                    service_role_offenders.append(f"{path}:{lineno}")
        assert not service_role_offenders, f"service_role reference found: {service_role_offenders}"

    def test_no_tls_verification_bypass(self, tmp_path: pathlib.Path) -> None:
        """
        AST-based (converted from a line-based text search in this same plan that fixed
        the process-kill check below): flags an actual `verify=False` call keyword or a
        `verify = False` assignment, never a prose mention — the previous version
        matched any non-comment line containing the words "verify", "=" and "false"
        together, which a docstring instructing readers never to add `verify=False`
        (exactly the kind of sentence `agent/api_client.py`'s own TLS docstring already
        contains, one keyword short of tripping it) would itself have matched.
        """
        offenders = _tls_bypass_offenders(self._agent_source_files())
        assert not offenders, f"TLS verification bypass found: {offenders}"

        violation_dir = tmp_path / "violation"
        violation_dir.mkdir()
        violation = violation_dir / "evil.py"
        violation.write_text(
            "import requests\n\n"
            "def fetch():\n"
            "    return requests.get('https://example.com', verify=False)\n",
            encoding="utf-8",
        )
        assert _tls_bypass_offenders([violation]), "a real verify=False call must be reported"

        safe_dir = tmp_path / "safe"
        safe_dir.mkdir()
        safe = safe_dir / "safe.py"
        safe.write_text(
            '"""Never pass verify=False to a request in this file — that would disable '
            'TLS certificate verification."""\n'
            "def fetch():\n"
            "    return None\n",
            encoding="utf-8",
        )
        assert not _tls_bypass_offenders([safe]), "a docstring mention must not be reported"

    def test_no_process_kill_call(self, tmp_path: pathlib.Path) -> None:
        """
        The specific defect this plan exists to fix. The previous version of this check
        grepped every non-`#`-comment line for the literal substrings "taskkill",
        ".kill(", "os.kill" and "signal.pthread_kill" — and it matched those same
        substrings inside a docstring in `agent/autostart.py` that was explaining, in
        prose, why that module has nothing to do with killing a process. That is the
        general failure mode this whole class exists to avoid: a forbidden-construct
        list must be able to CONTAIN the forbidden words, so a text search matches the
        rule's own documentation as readily as a violation of it. This version instead
        walks actual `Call` nodes whose target attribute/name is `kill` or
        `pthread_kill` (covers `proc.kill()`, `os.kill(...)`,
        `signal.pthread_kill(...)`, and `Process(pid).kill()` alike), plus actual
        string-`Constant` nodes containing "taskkill" that are NOT a module/class/
        function docstring — proven below to still catch a real kill call while
        leaving a docstring's own prose alone.
        """
        offenders = _process_kill_offenders(self._agent_source_files())
        assert not offenders, f"process-kill call found: {offenders}"

        violation_dir = tmp_path / "violation"
        violation_dir.mkdir()
        violation = violation_dir / "evil.py"
        violation.write_text(
            "import os\n\n"
            "def stop_other_process(pid):\n"
            "    os.kill(pid, 9)\n",
            encoding="utf-8",
        )
        assert _process_kill_offenders([violation]), "a real os.kill(...) call must be reported"

        safe_dir = tmp_path / "safe"
        safe_dir.mkdir()
        safe = safe_dir / "safe.py"
        safe.write_text(
            '"""\n'
            "This module never issues taskkill, never calls .kill( on anything, never\n"
            "calls os.kill and never calls signal.pthread_kill — it only manages Windows\n"
            "Task Scheduler entries, never a running process.\n"
            '"""\n'
            "def noop():\n"
            "    return None\n",
            encoding="utf-8",
        )
        assert not _process_kill_offenders([safe]), "a docstring mention must not be reported"

        terminate_dir = tmp_path / "terminate"
        terminate_dir.mkdir()
        terminate = terminate_dir / "stopper.py"
        terminate.write_text(
            "def stop(proc):\n"
            "    proc.terminate()\n",
            encoding="utf-8",
        )
        assert _process_kill_offenders([terminate]), "a real .terminate() call must be reported"

    def test_no_shell_keyword_anywhere(self, tmp_path: pathlib.Path) -> None:
        """Claim 13: no `Call` in the tree passes `shell=` (any value)."""
        offenders = _shell_keyword_offenders(self._agent_source_files())
        assert not offenders, f"shell= keyword found: {offenders}"

        violation = tmp_path / "shelly.py"
        violation.write_text(
            "import subprocess\n\n"
            "def run(cmd):\n"
            "    subprocess.run(cmd, shell=True)\n",
            encoding="utf-8",
        )
        assert _shell_keyword_offenders([violation]), "a shell=True call must be reported"

    # -----------------------------------------------------------------------
    # 40-13 — five new claims this phase makes about the source.
    # -----------------------------------------------------------------------

    def test_no_plaintext_token_read_path(self, tmp_path: pathlib.Path) -> None:
        """
        The stored authentication token is ciphertext on disk; the ONLY place this
        program ever reads the config file's `token` key is `config_store.load_token()`,
        and that read's value must pass through `dpapi.unprotect(...)` before
        `load_token()` can return it. This is a DATAFLOW claim — "does the value that
        was just read reach a `return` without being decrypted first?" — that no amount
        of careful prose or grepping for the word "token" could ever decide, and
        "token" itself is the wrong word to search for regardless: `agent/api_client.py`
        legitimately reads an entirely UNRELATED `"token"` field out of an HTTP response
        body, so only matching the `_KEY_TOKEN` identifier — never the bare literal —
        keeps this check from false-positiving on that unrelated code. Only walking the
        actual `Assign`/`Call`/`Return` shape around the read answers the real question.
        """
        offenders = _plaintext_token_offenders(self._agent_source_files())
        assert not offenders, f"plaintext token read path found: {offenders}"

        outside_reader_dir = tmp_path / "outside_reader"
        outside_reader_dir.mkdir()
        outside_reader = outside_reader_dir / "leaky.py"
        outside_reader.write_text(
            "_KEY_TOKEN = 'token'\n\n"
            "def read_it(data):\n"
            "    return data.get(_KEY_TOKEN)\n",
            encoding="utf-8",
        )
        assert _plaintext_token_offenders([outside_reader]), (
            "a token-key read outside config_store.load_token() must be reported"
        )

        plaintext_loader_dir = tmp_path / "plaintext_loader"
        plaintext_loader_dir.mkdir()
        plaintext_loader = plaintext_loader_dir / "config_store.py"
        plaintext_loader.write_text(
            "_KEY_TOKEN = 'token'\n\n"
            "def load_token():\n"
            "    data = {}\n"
            "    raw = data.get(_KEY_TOKEN)\n"
            "    return raw\n",
            encoding="utf-8",
        )
        assert _plaintext_token_offenders([plaintext_loader]), (
            "a load_token() that returns the raw value without decrypting it must be reported"
        )

    def test_no_non_windows_dpapi_fallback(self, tmp_path: pathlib.Path) -> None:
        """
        `agent/dpapi.py` may refuse to store/read a token off Windows, but it may never
        HAND BACK a value on that path — no XOR, no baked-key AES, no "obfuscation"
        masquerading as encryption. The module's own deferred-check shape (the platform
        test lives INSIDE a function body, never at import time, so the module still
        imports cleanly on a non-Windows dev/CI machine) is fine and permitted; what is
        checked here is only whether a platform-examining `if`'s branches ever reach a
        `return`/`yield` at all. Deciding "does this branch hand back a value" requires
        walking the branch's actual statements — a text search for the word "platform"
        or "Windows" would say nothing about what the branch DOES.
        """
        offenders = _dpapi_fallback_offenders(self._agent_source_files())
        assert not offenders, f"non-Windows DPAPI fallback found: {offenders}"

        violation_dir = tmp_path / "violation"
        violation_dir.mkdir()
        violation = violation_dir / "dpapi.py"
        violation.write_text(
            "import sys\n\n"
            "def protect(plaintext):\n"
            "    if not sys.platform.startswith('win'):\n"
            "        return plaintext\n"
            "    return b'real-ciphertext'\n",
            encoding="utf-8",
        )
        assert _dpapi_fallback_offenders([violation]), (
            "a non-Windows branch that returns a value must be reported"
        )

        safe_dir = tmp_path / "safe"
        safe_dir.mkdir()
        safe = safe_dir / "dpapi.py"
        safe.write_text(
            "import sys\n\n"
            "class DpapiUnavailableError(Exception):\n"
            "    pass\n\n"
            "def protect(plaintext):\n"
            "    if not sys.platform.startswith('win'):\n"
            "        raise DpapiUnavailableError('no DPAPI here')\n"
            "    return b'real-ciphertext'\n",
            encoding="utf-8",
        )
        assert not _dpapi_fallback_offenders([safe]), (
            "a non-Windows branch that only raises must not be reported"
        )

    def test_no_argv_sourced_credential(self, tmp_path: pathlib.Path) -> None:
        """
        Neither the token, nor the base URL, nor any other parameter this program needs
        is ever sourced from `sys.argv` — its only source is the config file
        (`agent/main.py`'s own `WindowLaunchOptions` docstring states this constraint
        explicitly: passing a secret through argv would put it on the process command
        line, visible to any other process on the machine that can enumerate command
        lines). `sys.argv` is legitimately referenced in exactly two places in this
        runtime PROGRAM (the `agent` package plus its own entry point), for two
        narrow, non-credential purposes: `main()` passes `sys.argv[1:]` into
        `parse_argv()` (which this check further constrains to return only a value
        built from boolean literals/comparisons, never an indexed element), and
        `agent/autostart.py`'s `own_executable_path()` reads `sys.argv[0]` — the
        running process's OWN path, never a user-supplied parameter. A third
        reference anywhere in the `agent` package, or a `parse_argv()` that indexes
        into the argument list instead of only testing membership, is an offender.
        `agent/packaging/` is excluded from this specific claim: it is CI/build
        tooling that runs on a build server, is never imported by the `agent`
        package, and never ships inside the built executable — its own CLI-script
        argument handling is not a credential path into the program this claim is
        about, and it stays in scope for the other eleven claims. Neither "how many
        places" nor "does this expression index into a list" is a question a text
        search over the word "argv" can answer.
        """
        offenders = _argv_offenders(self._agent_source_files())
        assert not offenders, f"argv-sourced credential path found: {offenders}"

        third_site_dir = tmp_path / "third_site"
        third_site_dir.mkdir()
        third_site = third_site_dir / "leaky.py"
        third_site.write_text(
            "import sys\n\n"
            "def read_server_url():\n"
            "    return sys.argv[1]\n",
            encoding="utf-8",
        )
        assert _argv_offenders([third_site]), "a third sys.argv reference must be reported"

        leaky_parse_dir = tmp_path / "leaky_parse"
        leaky_parse_dir.mkdir()
        leaky_parse = leaky_parse_dir / "main.py"
        leaky_parse.write_text(
            "import sys\n\n"
            "class WindowLaunchOptions:\n"
            "    def __init__(self, base_url=None):\n"
            "        self.base_url = base_url\n\n"
            "def parse_argv(argv):\n"
            "    return WindowLaunchOptions(base_url=argv[0])\n\n"
            "def main():\n"
            "    parse_argv(sys.argv[1:])\n",
            encoding="utf-8",
        )
        assert _argv_offenders([leaky_parse]), (
            "a parse_argv() that indexes into argv to build a string field must be reported"
        )

    def test_autostart_write_has_exactly_one_home(self, tmp_path: pathlib.Path) -> None:
        """
        Claim 11 (rewritten by quick 260925-qhs for the HKCU Run-value mechanism). The
        registry write that puts this program into Windows autostart (`SetValueEx`)
        appears exactly once in the whole tree, inside `agent/autostart.py`'s
        `_write_own_command()`, and `autostart.enable_autostart()` — the only function
        allowed to CREATE an autostart value — is called from exactly one place, the
        checkbox handler `_on_autostart_toggled` in `agent/main.py`, never from the
        silent startup self-check. A program that can write itself into Windows
        autostart from more than one code path behaves like malware; this makes that
        structurally impossible rather than merely conventional. Both are collected as
        `Call` NODES, never a text match, so a comment or docstring that names them (to
        explain this very rule, as this docstring does) is invisible — proven below.
        """
        offenders = _autostart_write_offenders(self._agent_source_files())
        assert not offenders, f"autostart single-home claim violated: {offenders}"

        def _write(name: str, filename: str, body: str) -> pathlib.Path:
            folder = tmp_path / name
            folder.mkdir()
            path = folder / filename
            path.write_text(body, encoding="utf-8")
            return path

        real_caller = _write(
            "real_caller",
            "main.py",
            "from agent import autostart\n\n"
            "def _on_autostart_toggled():\n"
            "    autostart.enable_autostart()\n",
        )
        real_writer = _write(
            "real_writer",
            "autostart.py",
            "_winreg = None\n\n"
            "def _write_own_command():\n"
            "    _winreg.SetValueEx(None, 'TreedgerAgent', 0, 1, 'x')\n",
        )
        assert not _autostart_write_offenders([real_caller, real_writer])

        second_caller = _write(
            "second_caller",
            "rogue.py",
            "from agent import autostart\n\n"
            "def self_register():\n"
            "    autostart.enable_autostart()\n",
        )
        assert _autostart_write_offenders([second_caller, real_caller, real_writer]), (
            "a second autostart.enable_autostart() call site must be reported"
        )

        startup_caller = _write(
            "startup_caller",
            "main.py",
            "from agent import autostart\n\n"
            "def __init__():\n"
            "    autostart.enable_autostart()\n",
        )
        assert _autostart_write_offenders([startup_caller, real_writer]), (
            "enable_autostart() called from anywhere but the checkbox handler must be reported"
        )

        second_writer = _write(
            "second_writer",
            "autostart.py",
            "_winreg = None\n\n"
            "def _write_own_command():\n"
            "    _winreg.SetValueEx(None, 'TreedgerAgent', 0, 1, 'x')\n\n"
            "def repair_autostart_path():\n"
            "    _winreg.SetValueEx(None, 'TreedgerAgent', 0, 1, 'y')\n",
        )
        assert _autostart_write_offenders([real_caller, second_writer]), (
            "a second SetValueEx call must be reported"
        )

        prose_only = _write(
            "prose_only",
            "autostart.py",
            '"""Only _write_own_command() calls SetValueEx; enable_autostart() is called '
            'only by the checkbox handler."""\n'
            "# SetValueEx enable_autostart\n"
            "_winreg = None\n\n"
            "def _write_own_command():\n"
            "    _winreg.SetValueEx(None, 'TreedgerAgent', 0, 1, 'x')\n",
        )
        assert not _autostart_write_offenders([real_caller, prose_only]), (
            "a docstring/comment naming the rule must not be counted"
        )

    def test_single_instance_lock_precedes_any_work(self, tmp_path: pathlib.Path) -> None:
        """
        `agent/main.py`'s `main()` acquires the single-instance lock BEFORE any other
        call it makes — before `parse_argv`, before the Tk root is constructed, and
        therefore before `AgentWindow.__init__` ever reaches `config_store.load_token()`
        or `terminal_discovery.find_terminal_path()`. A second process that gets far
        enough to touch either the config file or the MT5 terminal can corrupt another
        account's history silently, with no error raised anywhere in that sequence — so
        the lock must gate every one of those calls, not merely run alongside them. This
        is a statement-ORDER property within the parsed function body: the check sorts
        every `Call` node `main()` makes by (top-level-statement index, line, column)
        and asserts the very first one resolves to
        `single_instance.acquire_single_instance_lock(...)` — an ordering question no
        text search can even pose, let alone answer.
        """
        offenders = _lock_ordering_offenders(self._agent_source_files())
        assert not offenders, f"single-instance lock does not precede other work: {offenders}"

        violating_dir = tmp_path / "violating"
        violating_dir.mkdir()
        violating_main = violating_dir / "main.py"
        violating_main.write_text(
            "from agent import config_store, single_instance\n\n"
            "def main():\n"
            "    token = config_store.load_token()\n"
            "    if not single_instance.acquire_single_instance_lock():\n"
            "        return\n"
            "    return token\n",
            encoding="utf-8",
        )
        assert _lock_ordering_offenders([violating_main]), (
            "a config_store call reached before the lock must be reported"
        )
