"""
agent/tests/test_autostart.py — behavioural and structural tests for agent/autostart.py,
the per-user HKCU Run-value autostart (quick 260925-qhs, replacing the Task Scheduler
entry whose any-user ONLOGON trigger failed with "Access is denied").

The real registry is NEVER touched: conftest.py's autouse guard replaces
`autostart._winreg` with an object that fails the test on any access, and every test
here that needs a registry swaps in `_FakeWinreg`, an in-memory stand-in with the same
small API surface autostart.py uses. Structural claims (parameter-less signatures, no
subprocess import) are decided by `ast`, never by a text search.
"""
from __future__ import annotations

import ast
import pathlib
import sys
from typing import Any, Optional

import pytest

from agent import autostart

_OWN_EXE = r"C:\Program Files\Treedger\Treedger.exe"
_OWN_COMMAND = f'"{_OWN_EXE}" --minimized'


class _FakeKey:
    def __init__(self, registry: "_FakeWinreg", path: str, access: int) -> None:
        self.registry = registry
        self.path = path
        self.access = access

    def __enter__(self) -> "_FakeKey":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _FakeWinreg:
    """In-memory HKCU: {key_path: {value_name: (value, type)}}. The Run key always
    exists; StartupApproved\\Run exists only when a test creates it."""

    HKEY_CURRENT_USER = object()
    KEY_READ = 0x20019
    KEY_SET_VALUE = 0x0002
    REG_SZ = 1
    REG_BINARY = 3

    def __init__(self) -> None:
        self.keys: "dict[str, dict[str, tuple[Any, int]]]" = {autostart.RUN_KEY_PATH: {}}
        self.writes: "list[tuple[str, str, int, Any]]" = []
        self.deletes: "list[tuple[str, str]]" = []
        self.opened: "list[tuple[str, int]]" = []
        self.fail_writes = False

    # ---- test helpers ----------------------------------------------------
    def set_run(self, value: Any, value_type: int = REG_SZ) -> None:
        self.keys[autostart.RUN_KEY_PATH][autostart.RUN_VALUE_NAME] = (value, value_type)

    def set_approved(self, value: Any) -> None:
        self.keys.setdefault(autostart.STARTUP_APPROVED_KEY_PATH, {})[autostart.RUN_VALUE_NAME] = (
            value,
            self.REG_BINARY,
        )

    def run_value(self) -> Optional[Any]:
        entry = self.keys[autostart.RUN_KEY_PATH].get(autostart.RUN_VALUE_NAME)
        return None if entry is None else entry[0]

    def approved_present(self) -> bool:
        return autostart.RUN_VALUE_NAME in self.keys.get(autostart.STARTUP_APPROVED_KEY_PATH, {})

    # ---- the winreg API autostart.py uses --------------------------------
    def OpenKeyEx(self, root: object, path: str, reserved: int = 0, access: int = KEY_READ) -> _FakeKey:  # noqa: N802
        assert root is self.HKEY_CURRENT_USER, "only HKCU may ever be opened"
        self.opened.append((path, access))
        if path not in self.keys:
            raise FileNotFoundError(path)
        return _FakeKey(self, path, access)

    def QueryValueEx(self, key: _FakeKey, name: str) -> "tuple[Any, int]":  # noqa: N802
        values = self.keys[key.path]
        if name not in values:
            raise FileNotFoundError(name)
        return values[name]

    def SetValueEx(self, key: _FakeKey, name: str, reserved: int, value_type: int, value: Any) -> None:  # noqa: N802
        assert key.access & self.KEY_SET_VALUE, "a write needs KEY_SET_VALUE access"
        if self.fail_writes:
            raise PermissionError("denied")
        self.writes.append((key.path, name, value_type, value))
        self.keys[key.path][name] = (value, value_type)

    def DeleteValue(self, key: _FakeKey, name: str) -> None:  # noqa: N802
        assert key.access & self.KEY_SET_VALUE, "a delete needs KEY_SET_VALUE access"
        values = self.keys[key.path]
        if name not in values:
            raise FileNotFoundError(name)
        self.deletes.append((key.path, name))
        del values[name]


@pytest.fixture()
def reg(monkeypatch: pytest.MonkeyPatch) -> _FakeWinreg:
    fake = _FakeWinreg()
    monkeypatch.setattr(autostart, "_winreg", fake)
    monkeypatch.setattr(autostart, "own_executable_path", lambda: _OWN_EXE)
    return fake


# ---------------------------------------------------------------------------
# own_executable_path() — unchanged
# ---------------------------------------------------------------------------


def test_own_executable_path_returns_frozen_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_exe = r"C:\Program Files\Treedger\Treedger.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", fake_exe)

    assert autostart.own_executable_path() == autostart.os.path.realpath(fake_exe)


def test_own_executable_path_falls_back_to_argv0_when_not_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    fake_argv0 = r"C:\repo\agent\main.py"
    monkeypatch.setattr(sys, "argv", [fake_argv0])

    assert autostart.own_executable_path() == autostart.os.path.realpath(fake_argv0)


def test_build_command_line_quotes_the_path_and_appends_the_flag() -> None:
    assert autostart._build_command_line(_OWN_EXE) == _OWN_COMMAND


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_registered_command_reads_the_run_value(reg: _FakeWinreg) -> None:
    assert autostart.registered_command() is None
    reg.set_run(_OWN_COMMAND)
    assert autostart.registered_command() == _OWN_COMMAND


def test_registered_command_ignores_a_non_string_value(reg: _FakeWinreg) -> None:
    reg.set_run(b"\x01\x02", _FakeWinreg.REG_BINARY)
    assert autostart.registered_command() is None


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (_OWN_COMMAND, _OWN_EXE),
        (r'"C:\x\y.exe"', r"C:\x\y.exe"),
        (r"C:\x\y.exe --minimized", None),
        ('"unterminated', None),
        ("", None),
    ],
)
def test_registered_exe_is_the_first_quoted_text(command: str, expected: Optional[str]) -> None:
    assert autostart._registered_exe(command) == expected


def test_enabled_only_for_this_exe_and_not_disabled_in_task_manager(reg: _FakeWinreg) -> None:
    assert autostart.autostart_enabled_for_this_exe() is False  # no value

    reg.set_run(_OWN_COMMAND)
    assert autostart.autostart_enabled_for_this_exe() is True  # no StartupApproved key

    reg.set_run(_OWN_COMMAND.upper())
    assert autostart.autostart_enabled_for_this_exe() is True  # case-insensitive

    reg.set_approved(bytes([0x02]) + bytes(11))
    assert autostart.autostart_enabled_for_this_exe() is True  # even first byte = enabled

    reg.set_approved(bytes([0x03]) + bytes(11))
    assert autostart.disabled_in_startup_apps() is True
    assert autostart.autostart_enabled_for_this_exe() is False  # odd = disabled in Task Manager


def test_a_different_exe_reads_as_not_enabled(reg: _FakeWinreg) -> None:
    reg.set_run(r'"D:\portable\Treedger\Treedger.exe" --minimized')
    assert autostart.autostart_enabled_for_this_exe() is False


def test_disabled_in_startup_apps_ignores_non_bytes_and_empty(reg: _FakeWinreg) -> None:
    assert autostart.disabled_in_startup_apps() is False  # key absent
    reg.set_approved("03")
    assert autostart.disabled_in_startup_apps() is False
    reg.set_approved(b"")
    assert autostart.disabled_in_startup_apps() is False


def test_winreg_unavailable_reads_as_not_enabled_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "_winreg", None)
    assert autostart.registered_command() is None
    assert autostart.autostart_enabled_for_this_exe() is False
    assert autostart.disabled_in_startup_apps() is False
    assert isinstance(autostart.repair_autostart_path(), str)
    autostart.enable_autostart()
    autostart.disable_autostart()


# ---------------------------------------------------------------------------
# enable / disable
# ---------------------------------------------------------------------------


def test_enable_writes_exactly_once_and_clears_the_task_manager_marker(reg: _FakeWinreg) -> None:
    reg.set_approved(bytes([0x03]) + bytes(11))

    autostart.enable_autostart()

    assert reg.writes == [(autostart.RUN_KEY_PATH, "TreedgerAgent", _FakeWinreg.REG_SZ, _OWN_COMMAND)]
    assert not reg.approved_present()
    assert autostart.autostart_enabled_for_this_exe() is True


def test_enable_without_a_marker_still_writes_exactly_once(reg: _FakeWinreg) -> None:
    autostart.enable_autostart()
    assert len(reg.writes) == 1
    assert reg.run_value() == _OWN_COMMAND


def test_enable_never_raises_on_a_registry_error(reg: _FakeWinreg) -> None:
    reg.fail_writes = True
    autostart.enable_autostart()
    assert reg.run_value() is None


def test_disable_deletes_both_values(reg: _FakeWinreg) -> None:
    reg.set_run(_OWN_COMMAND)
    reg.set_approved(bytes([0x03]) + bytes(11))

    autostart.disable_autostart()

    assert reg.run_value() is None
    assert not reg.approved_present()
    assert reg.writes == []


def test_disable_tolerates_absent_values(reg: _FakeWinreg) -> None:
    autostart.disable_autostart()  # neither value, StartupApproved key missing
    assert reg.deletes == []


# ---------------------------------------------------------------------------
# repair_autostart_path() — never creates; rewrites only a dead path
# ---------------------------------------------------------------------------


def test_repair_writes_nothing_when_no_value_exists(reg: _FakeWinreg) -> None:
    status = autostart.repair_autostart_path()
    assert reg.writes == []
    assert reg.run_value() is None
    assert isinstance(status, str) and status


def test_repair_writes_nothing_when_already_correct(reg: _FakeWinreg) -> None:
    reg.set_run(_OWN_COMMAND)
    autostart.repair_autostart_path()
    assert reg.writes == []


def test_repair_rewrites_a_value_whose_exe_no_longer_exists(reg: _FakeWinreg, tmp_path: pathlib.Path) -> None:
    gone = tmp_path / "moved-away" / "Treedger.exe"
    reg.set_run(f'"{gone}" --minimized')

    status = autostart.repair_autostart_path()

    assert reg.writes == [(autostart.RUN_KEY_PATH, "TreedgerAgent", _FakeWinreg.REG_SZ, _OWN_COMMAND)]
    assert "repaired" in status


def test_repair_never_repoints_another_existing_copy(reg: _FakeWinreg, tmp_path: pathlib.Path) -> None:
    other = tmp_path / "Treedger.exe"
    other.write_bytes(b"MZ")
    reg.set_run(f'"{other}" --minimized')

    autostart.repair_autostart_path()

    assert reg.writes == []
    assert reg.run_value() == f'"{other}" --minimized'


@pytest.mark.parametrize("value", [r"C:\no\quotes\Treedger.exe --minimized", '"', "garbage"])
def test_repair_leaves_an_unparsable_value_alone(reg: _FakeWinreg, value: str) -> None:
    reg.set_run(value)
    autostart.repair_autostart_path()
    assert reg.writes == []


def test_repair_never_raises_on_a_registry_error(reg: _FakeWinreg, tmp_path: pathlib.Path) -> None:
    reg.set_run(f'"{tmp_path / "gone.exe"}" --minimized')
    reg.fail_writes = True
    assert isinstance(autostart.repair_autostart_path(), str)


# ---------------------------------------------------------------------------
# Structural (ast)
# ---------------------------------------------------------------------------


def _autostart_tree() -> ast.Module:
    return ast.parse(pathlib.Path(autostart.__file__).read_text(encoding="utf-8"))


def test_writer_functions_take_no_parameters() -> None:
    tree = _autostart_tree()
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    for name in ("enable_autostart", "disable_autostart", "repair_autostart_path", "_write_own_command"):
        assert name in functions, f"{name} missing"
        args = functions[name].args
        total = len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs)
        assert total == 0 and args.vararg is None and args.kwarg is None, (
            f"{name} must take no parameters — only this process's own path is ever written"
        )


def test_autostart_imports_no_subprocess() -> None:
    tree = _autostart_tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] != "subprocess" for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "subprocess"


def test_set_value_is_called_only_inside_write_own_command() -> None:
    tree = _autostart_tree()
    homes: "list[str]" = []
    for function in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "SetValueEx":
                homes.append(function.name)
    assert homes == ["_write_own_command"]


def test_only_hkcu_is_ever_referenced() -> None:
    tree = _autostart_tree()
    roots = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr.startswith("HKEY_")
    }
    assert roots == {"HKEY_CURRENT_USER"}
