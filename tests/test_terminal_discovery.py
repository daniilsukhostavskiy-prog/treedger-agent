"""
Unit tests for agent/terminal_discovery.py.

Every test fakes the Windows registry entirely via monkeypatch — none of them touch a
real registry, so this suite is deterministic regardless of what is actually installed
on the machine running it (including a real Windows box that happens to have a real,
differently-shaped MetaTrader 5 install, or no MetaTrader 5 install at all).

Only real disk I/O this file does: writing a stub `terminal64.exe` file under
pytest's own `tmp_path`, so `os.path.isfile()` — genuinely exercised, not mocked — is
what decides whether a candidate "exists".
"""
from __future__ import annotations

import os

import pytest

from agent import terminal_discovery
from agent.terminal_discovery import (
    TerminalNotFoundError,
    enumerate_registry_candidates,
    find_terminal_path,
)


class _FakeKey:
    """Stand-in for the opaque handle winreg.OpenKeyEx() returns."""

    def __init__(self, combo_id: tuple) -> None:
        self.combo_id = combo_id

    def Close(self) -> None:  # noqa: N802 - matches winreg's PyHKEY.Close() API
        pass


def _install_fake_registry(monkeypatch: pytest.MonkeyPatch, combos: list[dict]) -> None:
    """
    Patches terminal_discovery's four registry primitives with an in-memory fake, so
    no test in this file ever calls the real `winreg` module.

    `combos` is a list of dicts, each shaped either:
      {"id": (hive, path), "denied": True}
      {"id": (hive, path), "entries": {subkey_name: {"DisplayName": ..., "InstallLocation": ...}}}
    """
    by_id = {c["id"]: c for c in combos}

    def fake_combinations():
        return [(c["id"][0], c["id"][0], c["id"][1], 0, f"view-{c['id']}") for c in combos]

    def fake_open_key(hive, subkey_path, access):
        combo = by_id[(hive, subkey_path)]
        if combo.get("denied"):
            raise PermissionError("access denied (fake)")
        return _FakeKey((hive, subkey_path))

    def fake_enum_subkey_names(key):
        combo = by_id[key.combo_id]
        return list(combo.get("entries", {}).keys())

    def fake_read_string_value(key, subkey_name, value_name):
        combo = by_id[key.combo_id]
        return combo.get("entries", {}).get(subkey_name, {}).get(value_name)

    monkeypatch.setattr(terminal_discovery, "_registry_combinations", fake_combinations)
    monkeypatch.setattr(terminal_discovery, "_open_key", fake_open_key)
    monkeypatch.setattr(terminal_discovery, "_enum_subkey_names", fake_enum_subkey_names)
    monkeypatch.setattr(terminal_discovery, "_read_string_value", fake_read_string_value)


def test_collects_every_match_including_broker_branded(tmp_path, monkeypatch):
    official_dir = tmp_path / "official"
    official_dir.mkdir()
    (official_dir / "terminal64.exe").write_text("stub")

    branded_dir = tmp_path / "branded"
    branded_dir.mkdir()
    (branded_dir / "terminal64.exe").write_text("stub")

    combos = [
        {
            "id": ("HKLM", "uninstall1"),
            "entries": {
                "MetaQuotes.MT5": {
                    "DisplayName": "MetaTrader 5",
                    "InstallLocation": str(official_dir),
                },
                "SomeOtherApp": {
                    "DisplayName": "Totally Unrelated App",
                    "InstallLocation": str(tmp_path),
                },
            },
        },
        {
            "id": ("HKCU", "uninstall2"),
            "entries": {
                "FTMO.MT5": {
                    "DisplayName": "FTMO MetaTrader 5",
                    "InstallLocation": str(branded_dir),
                },
            },
        },
    ]
    _install_fake_registry(monkeypatch, combos)

    candidates = enumerate_registry_candidates()
    display_names = {c["display_name"] for c in candidates}
    # Every MetaTrader-5-matching entry is collected, INCLUDING the broker-branded
    # one — the unrelated app never appears at all.
    assert display_names == {"MetaTrader 5", "FTMO MetaTrader 5"}
    assert all(c["exists"] for c in candidates)

    assert find_terminal_path() == str(official_dir / "terminal64.exe")


def test_skips_hit_with_missing_executable(tmp_path, monkeypatch):
    missing_dir = tmp_path / "missing"
    missing_dir.mkdir()  # deliberately no terminal64.exe written here

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "terminal64.exe").write_text("stub")

    combos = [
        {
            "id": ("HKLM", "uninstall1"),
            "entries": {
                "Broken.MT5": {
                    "DisplayName": "MetaTrader 5",
                    "InstallLocation": str(missing_dir),
                },
            },
        },
        {
            "id": ("HKCU", "uninstall2"),
            "entries": {
                "Real.MT5": {
                    "DisplayName": "MetaTrader 5",
                    "InstallLocation": str(real_dir),
                },
            },
        },
    ]
    _install_fake_registry(monkeypatch, combos)

    candidates = enumerate_registry_candidates()
    existence_flags = {c["exists"] for c in candidates}
    assert existence_flags == {False, True}

    # find_terminal_path() skips the hit whose exe is missing on disk and returns
    # the one that actually exists.
    assert find_terminal_path() == str(real_dir / "terminal64.exe")


def test_permission_error_on_one_hive_does_not_abort_scan(tmp_path, monkeypatch):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "terminal64.exe").write_text("stub")

    combos = [
        {"id": ("HKLM", "uninstall1"), "denied": True},
        {
            "id": ("HKCU", "uninstall2"),
            "entries": {
                "Real.MT5": {
                    "DisplayName": "MetaTrader 5",
                    "InstallLocation": str(real_dir),
                },
            },
        },
    ]
    _install_fake_registry(monkeypatch, combos)

    # The denied HKLM combination raises PermissionError (an OSError subclass) from
    # _open_key — the scan must not abort; the HKCU combination is still enumerated.
    candidates = enumerate_registry_candidates()
    assert len(candidates) == 1
    assert candidates[0]["display_name"] == "MetaTrader 5"
    assert find_terminal_path() == str(real_dir / "terminal64.exe")


def test_non_windows_platform_returns_none_without_raising(monkeypatch):
    monkeypatch.setattr(terminal_discovery, "winreg", None)
    # Deterministic regardless of what's actually on the machine running this test.
    monkeypatch.setattr(os.path, "isfile", lambda path: False)

    assert enumerate_registry_candidates() == []
    assert find_terminal_path() is None


def test_terminal_not_found_error_is_an_exception_subclass():
    assert issubclass(TerminalNotFoundError, Exception)


def test_find_terminal_path_falls_back_to_conventional_path_when_registry_empty(
    tmp_path, monkeypatch
):
    _install_fake_registry(monkeypatch, [])  # no registry hits at all

    fallback = tmp_path / "terminal64.exe"
    fallback.write_text("stub")
    monkeypatch.setattr(terminal_discovery, "_CONVENTIONAL_FALLBACK_PATH", str(fallback))

    assert find_terminal_path() == str(fallback)


def test_find_terminal_path_returns_none_when_nothing_found(tmp_path, monkeypatch):
    _install_fake_registry(monkeypatch, [])
    monkeypatch.setattr(
        terminal_discovery, "_CONVENTIONAL_FALLBACK_PATH", str(tmp_path / "does-not-exist.exe")
    )

    assert find_terminal_path() is None
