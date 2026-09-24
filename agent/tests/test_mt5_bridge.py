"""
Unit tests for agent/mt5_bridge.py.

None of these tests touch a real MetaTrader5 terminal or the real MetaTrader5 pip
package. Whether this machine has them is irrelevant to every test below:
availability is always injected, never read. Tests that need "MT5 available and
returned X" behaviour monkeypatch the module's own `MT5_AVAILABLE` flag and its
module-level `mt5` reference directly with a fake stand-in object (the same
monkeypatch.setattr(module, attr, fake) technique agent/tests/test_terminal_discovery.py
already uses for `winreg`) — this is what the plan's "using the conftest stub module"
note refers to: `stub_mt5_module` (agent/tests/conftest.py) installs a bare fake
`MetaTrader5` module into `sys.modules` for any test that additionally wants a
real-shaped import to succeed; the tests below go one step further and monkeypatch
`agent.mt5_bridge.mt5` directly, since `mt5_bridge` already captured its own `mt5`
reference (or lack thereof) at import time, before any per-test fixture can run.
"""
from __future__ import annotations

import pathlib
import types
from datetime import datetime, timezone

import pytest

from agent import mt5_bridge, terminal_discovery


class _FakeOrder:
    """Stand-in for the mt5.TradeOrder namedtuple's fields this module reads."""

    def __init__(self, time_setup: int, sl: float, tp: float) -> None:
        self.time_setup = time_setup
        self.sl = sl
        self.tp = tp


class _FakeDeal:
    """Stand-in for the mt5.TradeDeal namedtuple's fields get_history_deals() reads."""

    def __init__(
        self,
        time: int,
        type: int,
        entry: int,
        profit: float,
        commission: float,
        swap: float,
        position_id: int,
        symbol: str,
        volume: float,
        price: float,
    ) -> None:
        self.time = time
        self.type = type
        self.entry = entry
        self.profit = profit
        self.commission = commission
        self.swap = swap
        self.position_id = position_id
        self.symbol = symbol
        self.volume = volume
        self.price = price


# ---------------------------------------------------------------------------
# Degradation when MetaTrader5 is absent — INJECTED, never read off the machine
# ---------------------------------------------------------------------------
#
# These tests used to assert `mt5_bridge.MT5_AVAILABLE is False` directly. That is
# a property of the machine the tests happened to run on, not a property of the
# program: on a Linux sandbox there is no win_amd64 wheel, so the import fails and
# the flag is False — but on windows-latest MetaTrader5 installs from
# requirements.txt exactly as intended, the flag is True, and `last_error_tuple()`
# returns a real MT5 code such as (-10004, 'No IPC connection') because the
# package IS present and only the terminal is not. Release run #2 failed on all
# three of them, and the old test name ("..._on_this_machine") admitted the defect
# outright.
#
# The degradation path is worth testing and is kept. It is now INJECTED via the
# fixture below — the same monkeypatch technique this file already uses to
# simulate MT5 being available — so it exercises the same branch on both platforms
# and asserts the program's behaviour instead of the environment's.
#
# Do not "fix" a future failure here by expecting True on Windows, and do not skip
# these when MetaTrader5 is importable: that would stop testing degradation
# precisely on the machines where the program actually runs.

@pytest.fixture
def mt5_unavailable(monkeypatch):
    """Force the no-MetaTrader5 branch regardless of what this machine has."""
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", False)
    monkeypatch.setattr(mt5_bridge, "mt5", None)


def test_module_is_importable_whether_or_not_metatrader5_is_present():
    # The load-bearing proof is that importing this module at the top of this file
    # did not raise on a machine that may have no MetaTrader5 wheel at all. What
    # the flag's VALUE is depends on the machine and is deliberately not asserted.
    assert isinstance(mt5_bridge.MT5_AVAILABLE, bool)
    assert mt5_bridge.MT5_AVAILABLE or mt5_bridge.mt5 is None


def test_every_call_degrades_gracefully_when_mt5_unavailable(mt5_unavailable):
    assert mt5_bridge.get_account_info() is None
    assert mt5_bridge.current_logged_in_account() is None
    assert mt5_bridge.last_error_tuple() is None
    assert mt5_bridge.login_account(123, "pw", "Server") is False
    assert mt5_bridge.get_history_deals(
        datetime.now(timezone.utc), datetime.now(timezone.utc)
    ) == []
    assert mt5_bridge.get_initial_stop_and_take_profit(1) == (None, None)
    result = mt5_bridge.detect_broker_utc_offset_seconds()
    assert result.offset_seconds == 0
    assert result.detected is False
    # shutdown_terminal() must not raise even with nothing to shut down.
    mt5_bridge.shutdown_terminal()


# ---------------------------------------------------------------------------
# Constant sets derive from agent/error_codes.py — never redeclared as literals
# ---------------------------------------------------------------------------

def test_constant_sets_derive_from_error_codes_table():
    from agent.error_codes import MT5_ERROR_CODES, ErrorCategory

    assert mt5_bridge.AUTH_ERROR_CODES == frozenset(
        code
        for code, info in MT5_ERROR_CODES.items()
        if info.category is ErrorCategory.AUTH_FAILED
    )
    assert mt5_bridge.TIMEOUT_ERROR_CODES == frozenset(
        code
        for code, info in MT5_ERROR_CODES.items()
        if info.category is ErrorCategory.TIMEOUT
    )
    assert mt5_bridge.IPC_UNAVAILABLE_ERROR_CODES == frozenset(
        code
        for code, info in MT5_ERROR_CODES.items()
        if info.category is ErrorCategory.CONNECTION_ERROR
    )
    # The algo-trading-disabled codes must NOT leak into the connection-class set —
    # this is the one deliberate divergence from the donor's own grouping.
    assert mt5_bridge.IPC_UNAVAILABLE_ERROR_CODES.isdisjoint(
        frozenset(
            code
            for code, info in MT5_ERROR_CODES.items()
            if info.category is ErrorCategory.ALGOTRADING_DISABLED
        )
    )


# ---------------------------------------------------------------------------
# initialize_terminal()
# ---------------------------------------------------------------------------

def test_initialize_terminal_raises_when_no_path_found_anywhere(monkeypatch):
    monkeypatch.setattr(terminal_discovery, "find_terminal_path", lambda: None)
    with pytest.raises(terminal_discovery.TerminalNotFoundError):
        mt5_bridge.initialize_terminal()


def test_initialize_terminal_returns_false_when_mt5_unavailable_even_with_a_path(
    mt5_unavailable,
):
    assert mt5_bridge.initialize_terminal(path=r"C:\fake\terminal64.exe") is False


# ---------------------------------------------------------------------------
# get_initial_stop_and_take_profit() — the load-bearing correctness rule
# ---------------------------------------------------------------------------

def test_picks_earliest_order_by_time_setup_regardless_of_a_later_order_existing(
    monkeypatch,
):
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    fake_mt5 = types.SimpleNamespace(
        history_orders_get=lambda position: [
            _FakeOrder(time_setup=200, sl=1.20000, tp=1.30000),  # later order
            _FakeOrder(time_setup=100, sl=1.05000, tp=1.15000),  # earliest — must win
        ],
        last_error=lambda: (0, "no error"),
    )
    monkeypatch.setattr(mt5_bridge, "mt5", fake_mt5)

    sl, tp = mt5_bridge.get_initial_stop_and_take_profit(555)
    assert sl == 1.05000
    assert tp == 1.15000


def test_no_order_type_filter_applied_no_matter_what_the_orders_look_like(monkeypatch):
    """
    The correctness-critical rule this function exists to enforce: no type-in-(0,1)
    filter of any kind. Since neither the implementation nor this test ever inspects
    an order "type" field at all, an earliest PENDING-style order (conceptually) and
    a later MARKET-style order both flow through identically — only time_setup
    ordering decides.
    """
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    fake_mt5 = types.SimpleNamespace(
        history_orders_get=lambda position: [
            _FakeOrder(time_setup=50, sl=0.99000, tp=1.01000),
        ],
        last_error=lambda: (0, "no error"),
    )
    monkeypatch.setattr(mt5_bridge, "mt5", fake_mt5)

    sl, tp = mt5_bridge.get_initial_stop_and_take_profit(1)
    assert sl == 0.99000
    assert tp == 1.01000


def test_zero_sentinel_maps_to_none_on_both_fields(monkeypatch):
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    fake_mt5 = types.SimpleNamespace(
        history_orders_get=lambda position: [_FakeOrder(time_setup=1, sl=0.0, tp=0.0)],
        last_error=lambda: (0, "no error"),
    )
    monkeypatch.setattr(mt5_bridge, "mt5", fake_mt5)

    assert mt5_bridge.get_initial_stop_and_take_profit(1) == (None, None)


def test_near_zero_value_is_not_confused_with_the_sentinel(monkeypatch):
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    fake_mt5 = types.SimpleNamespace(
        history_orders_get=lambda position: [
            _FakeOrder(time_setup=1, sl=0.00001, tp=0.0)
        ],
        last_error=lambda: (0, "no error"),
    )
    monkeypatch.setattr(mt5_bridge, "mt5", fake_mt5)

    sl, tp = mt5_bridge.get_initial_stop_and_take_profit(1)
    assert sl == 0.00001
    assert tp is None


def test_none_orders_result_yields_none_none_without_raising(monkeypatch):
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    fake_mt5 = types.SimpleNamespace(
        history_orders_get=lambda position: None,
        last_error=lambda: (0, "no error"),
    )
    monkeypatch.setattr(mt5_bridge, "mt5", fake_mt5)

    assert mt5_bridge.get_initial_stop_and_take_profit(1) == (None, None)


def test_empty_orders_list_yields_none_none_without_raising(monkeypatch):
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    fake_mt5 = types.SimpleNamespace(
        history_orders_get=lambda position: [],
        last_error=lambda: (0, "no error"),
    )
    monkeypatch.setattr(mt5_bridge, "mt5", fake_mt5)

    assert mt5_bridge.get_initial_stop_and_take_profit(1) == (None, None)


def test_one_ipc_call_only_for_both_sl_and_tp(monkeypatch):
    """One history_orders_get() call must produce BOTH fields — no second IPC call."""
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    call_count = {"n": 0}

    def _history_orders_get(position):
        call_count["n"] += 1
        return [_FakeOrder(time_setup=1, sl=1.1, tp=1.2)]

    fake_mt5 = types.SimpleNamespace(
        history_orders_get=_history_orders_get,
        last_error=lambda: (0, "no error"),
    )
    monkeypatch.setattr(mt5_bridge, "mt5", fake_mt5)

    sl, tp = mt5_bridge.get_initial_stop_and_take_profit(1)
    assert (sl, tp) == (1.1, 1.2)
    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# get_history_deals()
# ---------------------------------------------------------------------------

def test_get_history_deals_produces_every_documented_key(monkeypatch):
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    deal = _FakeDeal(
        time=1_700_000_000,
        type=0,
        entry=1,
        profit=12.5,
        commission=-1.0,
        swap=-0.5,
        position_id=999,
        symbol="EURUSD",
        volume=0.1,
        price=1.2345,
    )
    fake_mt5 = types.SimpleNamespace(
        history_deals_get=lambda from_dt, to_dt: [deal],
        last_error=lambda: (0, "no error"),
    )
    monkeypatch.setattr(mt5_bridge, "mt5", fake_mt5)

    deals = mt5_bridge.get_history_deals(
        datetime.now(timezone.utc), datetime.now(timezone.utc)
    )
    assert deals == [
        {
            "time": 1_700_000_000,
            "type": 0,
            "entry": 1,
            "profit": 12.5,
            "commission": -1.0,
            "swap": -0.5,
            "position_id": 999,
            "symbol": "EURUSD",
            "volume": 0.1,
            "price": 1.2345,
        }
    ]


def test_get_history_deals_returns_empty_list_when_raw_is_none(monkeypatch):
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    fake_mt5 = types.SimpleNamespace(
        history_deals_get=lambda from_dt, to_dt: None,
        last_error=lambda: (1, "some error"),
    )
    monkeypatch.setattr(mt5_bridge, "mt5", fake_mt5)

    assert (
        mt5_bridge.get_history_deals(
            datetime.now(timezone.utc), datetime.now(timezone.utc)
        )
        == []
    )


# ---------------------------------------------------------------------------
# login_account() — credential-handling discipline + IPC self-heal, no escalation
# ---------------------------------------------------------------------------

def test_login_account_succeeds_on_first_attempt(monkeypatch):
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    fake_mt5 = types.SimpleNamespace(
        login=lambda login, password, server: True,
        last_error=lambda: (0, "no error"),
    )
    monkeypatch.setattr(mt5_bridge, "mt5", fake_mt5)

    assert mt5_bridge.login_account(123, "s3cret", "Broker-Server") is True


def test_login_account_never_retries_a_non_ipc_failure(monkeypatch):
    """A wrong-password-class failure (not in IPC_UNAVAILABLE_ERROR_CODES) must
    never trigger the re-init-and-retry path — retrying a bad credential risks a
    broker-side rate-limit/IP-ban."""
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    login_calls = {"n": 0}
    initialize_calls = {"n": 0}

    def _login(login, password, server):
        login_calls["n"] += 1
        return False

    def _initialize():
        initialize_calls["n"] += 1
        return True

    fake_mt5 = types.SimpleNamespace(
        login=_login,
        initialize=_initialize,
        last_error=lambda: (-10004, "AUTHORIZATION_ERROR"),
    )
    monkeypatch.setattr(mt5_bridge, "mt5", fake_mt5)

    assert mt5_bridge.login_account(123, "wrong", "Broker-Server") is False
    assert login_calls["n"] == 1
    assert initialize_calls["n"] == 0


def test_login_account_retries_exactly_once_on_ipc_class_failure(monkeypatch):
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    # Pick a code this test knows is connection-class via the real table, so the
    # test stays correct even if the table's exact membership is edited later.
    ipc_code = next(iter(mt5_bridge.IPC_UNAVAILABLE_ERROR_CODES))

    login_calls = {"n": 0}
    initialize_calls = {"n": 0}

    def _login(login, password, server):
        login_calls["n"] += 1
        return login_calls["n"] >= 2  # fails first, succeeds on the retry

    def _initialize():
        initialize_calls["n"] += 1
        return True

    fake_mt5 = types.SimpleNamespace(
        login=_login,
        initialize=_initialize,
        last_error=lambda: (ipc_code, "ipc unavailable"),
    )
    monkeypatch.setattr(mt5_bridge, "mt5", fake_mt5)

    assert mt5_bridge.login_account(123, "s3cret", "Broker-Server") is True
    assert login_calls["n"] == 2
    assert initialize_calls["n"] == 1


def test_login_account_gives_up_after_one_retry_no_restart_escalation(monkeypatch):
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    ipc_code = next(iter(mt5_bridge.IPC_UNAVAILABLE_ERROR_CODES))

    login_calls = {"n": 0}

    def _login(login, password, server):
        login_calls["n"] += 1
        return False  # never succeeds

    fake_mt5 = types.SimpleNamespace(
        login=_login,
        initialize=lambda: True,
        last_error=lambda: (ipc_code, "ipc unavailable"),
    )
    monkeypatch.setattr(mt5_bridge, "mt5", fake_mt5)

    assert mt5_bridge.login_account(123, "s3cret", "Broker-Server") is False
    # Exactly one initial attempt + one retry — no third attempt, no restart chain.
    assert login_calls["n"] == 2


def test_login_account_never_logs_the_password(monkeypatch, caplog):
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    fake_mt5 = types.SimpleNamespace(
        login=lambda login, password, server: True,
        last_error=lambda: (0, "no error"),
    )
    monkeypatch.setattr(mt5_bridge, "mt5", fake_mt5)

    secret = "sUp3r-s3cr3t-inv3st0r-pw"
    with caplog.at_level("DEBUG"):
        mt5_bridge.login_account(123, secret, "Broker-Server")

    for record in caplog.records:
        assert secret not in record.getMessage()


# ---------------------------------------------------------------------------
# current_logged_in_account()
# ---------------------------------------------------------------------------

def test_current_logged_in_account_is_narrow_login_and_server_only(monkeypatch):
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    fake_info = types.SimpleNamespace(
        login=555444333, server="Broker-Live01", balance=99999.0, equity=100000.0
    )
    fake_mt5 = types.SimpleNamespace(account_info=lambda: fake_info)
    monkeypatch.setattr(mt5_bridge, "mt5", fake_mt5)

    result = mt5_bridge.current_logged_in_account()
    assert result == {"login": 555444333, "server": "Broker-Live01"}
    assert "balance" not in result
    assert "equity" not in result


def test_current_logged_in_account_returns_none_when_nothing_logged_in(monkeypatch):
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    fake_mt5 = types.SimpleNamespace(account_info=lambda: None)
    monkeypatch.setattr(mt5_bridge, "mt5", fake_mt5)

    assert mt5_bridge.current_logged_in_account() is None


# ---------------------------------------------------------------------------
# detect_broker_utc_offset_seconds()
# ---------------------------------------------------------------------------

def test_detect_utc_offset_reports_detected_false_on_missing_tick(monkeypatch):
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    fake_mt5 = types.SimpleNamespace(symbol_info_tick=lambda symbol: None)
    monkeypatch.setattr(mt5_bridge, "mt5", fake_mt5)

    result = mt5_bridge.detect_broker_utc_offset_seconds()
    assert result.offset_seconds == 0
    assert result.detected is False


def test_detect_utc_offset_zero_offset_is_still_detected_true(monkeypatch):
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    now = datetime.now(timezone.utc)
    fake_tick = types.SimpleNamespace(time=int(now.timestamp()))
    fake_mt5 = types.SimpleNamespace(symbol_info_tick=lambda symbol: fake_tick)
    monkeypatch.setattr(mt5_bridge, "mt5", fake_mt5)

    result = mt5_bridge.detect_broker_utc_offset_seconds()
    assert result.offset_seconds == 0
    assert result.detected is True


# ---------------------------------------------------------------------------
# Regression guard: no test may read MT5 availability off the machine
# ---------------------------------------------------------------------------

def test_no_test_asserts_ambient_mt5_availability():
    """
    Enforce, rather than merely advise, the rule the comment above states.

    Release run #2 failed because three tests asserted `mt5_bridge.MT5_AVAILABLE
    is False` — true on a Linux sandbox with no win_amd64 wheel, false on
    windows-latest where MetaTrader5 installs normally. That pinned a property of
    the machine instead of a property of the program, and it cost a release run
    to discover because the suite had never run on Windows.

    This is `ast`-based on purpose. A text search for the flag name would match
    this docstring, the explanatory comment above, and the fixture that legitimately
    monkeypatches the flag — the same defect that made the old text-search
    `test_no_process_kill_call` trip over prose describing what it forbade. Only a
    real comparison node counts: `MT5_AVAILABLE is <constant>` inside an `assert`.
    Reading the flag is fine; asserting its ambient value is not.
    """
    import ast

    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        for sub in ast.walk(node.test):
            if not isinstance(sub, ast.Compare):
                continue
            reads_flag = any(
                isinstance(n, ast.Attribute) and n.attr == "MT5_AVAILABLE"
                for n in ast.walk(sub)
            )
            compares_to_constant = any(
                isinstance(c, ast.Constant) for c in sub.comparators
            )
            if reads_flag and compares_to_constant:
                offenders.append(f"line {sub.lineno}")

    assert not offenders, (
        "a test asserts the machine's ambient MetaTrader5 availability at "
        + ", ".join(offenders)
        + " — inject it with the mt5_unavailable fixture instead. This exact "
        "pattern failed release run #2 on windows-latest."
    )
