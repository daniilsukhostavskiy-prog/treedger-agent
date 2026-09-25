"""
Shared pytest fixtures for the agent/ unit tests.

All tests in this package run WITHOUT a real MetaTrader5 terminal and WITHOUT the
MetaTrader5 pip package installed — this mirrors the donor's own
`try: import MetaTrader5 / except ImportError` guard philosophy
(mt5-service/services/mt5_bridge.py), so agent/ modules that import MetaTrader5 stay
importable and testable on any machine.

Deliberately NOT ported from mt5-service/tests/conftest.py: the Fernet-key fixture and
the anchor-enabled fixture. Neither concept exists in this folder — the agent has no
local encrypted credential store and no shared-terminal anchor/keeper logic (see
`agent/mt5_bridge.py`'s own module docstring for the full port-exclusion list).
"""
from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture()
def stub_mt5_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """
    Install a minimal stub `MetaTrader5` module into sys.modules for tests that want to
    exercise bridge-style code paths without a real terminal or the real pip package
    installed. Not used by agent/tests/test_positions.py (which is pure-Python and never
    imports MetaTrader5 at all) — used by agent/tests/test_mt5_bridge.py instead.
    """
    stub = types.ModuleType("MetaTrader5")
    monkeypatch.setitem(sys.modules, "MetaTrader5", stub)
    return stub


def _refuse_real_terminal_launch(*_args, **_kwargs):
    raise AssertionError("tests must never launch a real MetaTrader terminal")


@pytest.fixture(autouse=True)
def _never_launch_a_real_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    AUTOUSE guard: `agent.sync.run_sync` now starts MetaTrader 5 itself when none is
    running, and a developer's machine may well have a real MT5 installed. Every test
    therefore gets a process launcher that FAILS the test loudly instead of starting
    anything. Tests that exercise the launch path override `_popen` with their own
    recorder.
    """
    from agent import terminal_process

    monkeypatch.setattr(terminal_process, "_popen", _refuse_real_terminal_launch)


class _RegistryAccessForbidden:
    """Stands in for `winreg` inside agent/autostart.py: ANY attribute access fails the
    test. A developer's machine has a real HKCU Run key (and possibly a real Treedger
    autostart value) — no test may read or write it."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(
            f"tests must never touch the real registry (winreg.{name}) — swap "
            "agent.autostart._winreg for a fake"
        )


@pytest.fixture(autouse=True)
def _never_touch_the_real_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    AUTOUSE guard (quick 260925-qhs): autostart is now a per-user HKCU Run value.
    Every test gets a registry handle that fails loudly on any access; tests that
    exercise autostart.py swap in their own in-memory fake
    (agent/tests/test_autostart.py).
    """
    from agent import autostart

    monkeypatch.setattr(autostart, "_winreg", _RegistryAccessForbidden())
