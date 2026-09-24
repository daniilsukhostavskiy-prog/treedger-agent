"""
Shared pytest fixtures for the agent/ unit tests.

All tests in this package run WITHOUT a real MetaTrader5 terminal and WITHOUT the
MetaTrader5 pip package installed — this mirrors the donor's own
`try: import MetaTrader5 / except ImportError` guard philosophy
(mt5-service/services/mt5_bridge.py), so agent/ modules that import MetaTrader5 stay
importable and testable on any machine.

Deliberately NOT ported from mt5-service/tests/conftest.py: the Fernet-key fixture and
the anchor-enabled fixture. Neither concept exists in this folder — the agent has no
encrypted local credential store and no shared-terminal anchor/keeper logic (see
39-CONTEXT.md D-23 and the mt5_bridge.py port exclusions in 39-PATTERNS.md).
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
    imports MetaTrader5 at all) — reserved for later plans that port mt5_bridge.py.
    """
    stub = types.ModuleType("MetaTrader5")
    monkeypatch.setitem(sys.modules, "MetaTrader5", stub)
    return stub
