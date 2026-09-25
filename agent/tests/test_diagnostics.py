"""
agent/tests/test_diagnostics.py — `agent/diagnostics.py`: the rotating file log, the
startup banner, the uncaught-exception hooks, and the REDACTION proof the brief
requires — a DEBUG-level log of a full mocked sync run (real `agent.sync`, real
`agent.mt5_bridge` driving a fake `mt5` namespace, a fake HTTP client) and of a pairing
attempt never contains the investor password, the bearer token or the pairing code.
"""
from __future__ import annotations

import logging
import queue
import sys
import threading
import types
from pathlib import Path

import pytest

from agent import api_client, config_store, diagnostics, main, mt5_bridge, sync
from agent import terminal_discovery, terminal_process

_PASSWORD = "inv-SECRET-pw-91c2"
_TOKEN = "rotated-SECRET-token-7f3a"
_PAIRING_CODE = "PAIR-SECRET-5521"
_PAIRED_TOKEN = "paired-SECRET-token-3b1d"


@pytest.fixture()
def log_file(tmp_path: Path):
    """Configure logging into tmp_path at DEBUG; tear it all down afterwards."""
    root = logging.getLogger()
    previous_level = root.level
    diagnostics.remove_configured_handlers()
    path = diagnostics.configure_logging(level=logging.DEBUG, directory=tmp_path)
    assert path is not None
    yield path
    diagnostics.remove_configured_handlers()
    root.setLevel(previous_level)


def _flushed_text(path: Path) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return path.read_text(encoding="utf-8")


def test_configure_logging_creates_the_file_and_is_idempotent(log_file: Path, tmp_path: Path) -> None:
    assert log_file == tmp_path / diagnostics.LOG_FILENAME
    again = diagnostics.configure_logging(level=logging.DEBUG, directory=tmp_path)
    assert again == log_file

    file_handlers = [
        h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    assert file_handlers[0].maxBytes == diagnostics.LOG_MAX_BYTES
    assert file_handlers[0].backupCount == diagnostics.LOG_BACKUP_COUNT
    assert log_file.exists()


def test_log_dir_lives_under_the_config_store_app_data_folder(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert diagnostics.log_dir() == config_store.app_data_dir() / "logs"
    assert diagnostics.log_dir() == tmp_path / "TreedgerAgent" / "logs"


def test_banner_records_version_and_frozen_flags(log_file: Path) -> None:
    diagnostics.log_startup_banner()
    text = _flushed_text(log_file)
    assert f"build={api_client.AGENT_BUILD_VERSION}" in text
    assert "frozen=" in text
    assert "compiled=" in text
    assert "executable=" in text


def test_sys_excepthook_writes_a_critical_record_with_traceback(log_file: Path, monkeypatch) -> None:
    monkeypatch.setattr(diagnostics, "_hooks_installed", False)
    monkeypatch.setattr(sys, "excepthook", lambda *_a: None)
    monkeypatch.setattr(threading, "excepthook", lambda _args: None)
    diagnostics.install_exception_hooks()

    try:
        raise ValueError("boom-main-thread")
    except ValueError:
        sys.excepthook(*sys.exc_info())

    text = _flushed_text(log_file)
    assert "CRITICAL" in text
    assert "boom-main-thread" in text
    assert "Traceback" in text


def test_threading_excepthook_writes_a_critical_record(log_file: Path, monkeypatch) -> None:
    monkeypatch.setattr(diagnostics, "_hooks_installed", False)
    monkeypatch.setattr(sys, "excepthook", lambda *_a: None)
    monkeypatch.setattr(threading, "excepthook", lambda _args: None)
    diagnostics.install_exception_hooks()

    try:
        raise RuntimeError("boom-worker-thread")
    except RuntimeError:
        exc_type, exc_value, exc_tb = sys.exc_info()
    threading.excepthook(
        types.SimpleNamespace(
            exc_type=exc_type, exc_value=exc_value, exc_traceback=exc_tb,
            thread=threading.current_thread(),
        )
    )

    text = _flushed_text(log_file)
    assert "CRITICAL" in text
    assert "boom-worker-thread" in text


def test_tk_callback_exception_is_logged(log_file: Path) -> None:
    try:
        raise KeyError("boom-tk-callback")
    except KeyError:
        diagnostics.log_tk_callback_exception(*sys.exc_info())
    text = _flushed_text(log_file)
    assert "CRITICAL" in text
    assert "boom-tk-callback" in text


# ---------------------------------------------------------------------------
# REDACTION — the brief's required test
# ---------------------------------------------------------------------------

class _FakeClient:
    def fetch_accounts(self):
        return api_client.AccountsFetchResult(
            token=_TOKEN,
            accounts=[
                {
                    "accountId": "acc-1",
                    "mtLogin": "541183920",
                    "brokerServer": "Broker-Live",
                    "investorPassword": _PASSWORD,
                }
            ],
        )

    def post_trades(self, **_kwargs):
        return {"inserted": 0, "updated": 0, "skipped": 0}

    def post_account_status(self, **_kwargs):
        return None


def _fake_mt5() -> types.SimpleNamespace:
    account = types.SimpleNamespace(
        login=541183920, server="Broker-Live", equity=1000.0, currency="USD", trade_mode=2
    )
    return types.SimpleNamespace(
        initialize=lambda *a, **k: True,
        last_error=lambda: (1, "Success"),
        terminal_info=lambda: types.SimpleNamespace(
            name="MetaTrader 5", company="Broker", build=4755, path=r"C:\MT5", connected=True,
            community_balance=0.0,
        ),
        account_info=lambda: account,
        login=lambda *a, **k: True,
        history_deals_get=lambda *a, **k: [],
        history_orders_get=lambda *a, **k: [],
        symbol_info_tick=lambda *_a: types.SimpleNamespace(time=0),
        shutdown=lambda: None,
    )


def test_full_mocked_sync_at_debug_never_logs_password_or_token(log_file: Path, monkeypatch) -> None:
    monkeypatch.setattr(mt5_bridge, "MT5_AVAILABLE", True)
    monkeypatch.setattr(mt5_bridge, "mt5", _fake_mt5())
    monkeypatch.setattr(mt5_bridge, "_abandoned_call", None)
    monkeypatch.setattr(terminal_discovery, "find_terminal_path", lambda: r"C:\MT5\terminal64.exe")
    monkeypatch.setattr(
        terminal_process,
        "list_terminal_processes",
        lambda: [terminal_process.RunningTerminal(1, r"C:\MT5\terminal64.exe", False, "token")],
    )
    monkeypatch.setattr(terminal_process, "current_process_elevated", lambda: False)
    saved: "list[str]" = []
    monkeypatch.setattr(config_store, "save_token", saved.append)

    summary = sync.run_sync(_FakeClient(), lambda _progress: None)

    assert summary.total == 1
    assert saved == [_TOKEN]
    text = _flushed_text(log_file)
    assert text.strip()
    assert "initialize" in text
    assert _PASSWORD not in text
    assert _TOKEN not in text


def test_pairing_attempt_never_logs_code_or_token(log_file: Path, monkeypatch) -> None:
    class _FakeApiClient:
        def __init__(self, base_url, token=None):
            self.base_url = base_url

        def redeem_pairing_code(self, code):
            assert code == _PAIRING_CODE
            return _PAIRED_TOKEN

    class _Var:
        def __init__(self, value: str) -> None:
            self._value = value

        def get(self) -> str:
            return self._value

    monkeypatch.setattr(api_client, "ApiClient", _FakeApiClient)
    monkeypatch.setattr(config_store, "save_base_url", lambda _url: None)
    monkeypatch.setattr(config_store, "save_token", lambda _token: None)

    window = main.AgentWindow.__new__(main.AgentWindow)
    window._queue = queue.Queue()
    window._base_url_var = _Var("https://treedger.example")
    window._code_var = _Var(_PAIRING_CODE)
    dispatched: "list[object]" = []
    window._dispatch = dispatched.append  # type: ignore[method-assign]

    window._on_pair_clicked()

    assert dispatched and dispatched[-1].__class__.__name__ == "PairingSucceededEvent"
    text = _flushed_text(log_file)
    assert "pairing attempt" in text
    assert _PAIRING_CODE not in text
    assert _PAIRED_TOKEN not in text
