"""
agent/tests/test_terminal_process.py — `agent/terminal_process.py`.

No test here ever starts a real process: the launch test replaces the module's
`_popen` indirection with a recorder (and the autouse guard in conftest.py makes any
other path fail loudly). The Windows-only checks only assert return TYPES of the
read-only enumeration functions, never what happens to be running on the machine.
"""
from __future__ import annotations

import os
import sys

import pytest

from agent import terminal_process

_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"


class _PopenRecorder:
    def __init__(self) -> None:
        self.args: "list[object]" = []
        self.kwargs: "dict[str, object]" = {}

    def __call__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs

        class _Proc:
            pid = 31337

        return _Proc()


def test_launch_uses_an_argument_list_no_shell_and_the_install_folder(monkeypatch) -> None:
    recorder = _PopenRecorder()
    monkeypatch.setattr(terminal_process, "_popen", recorder)

    pid = terminal_process.launch_terminal_minimized(_PATH)

    assert pid == 31337
    assert recorder.args == [_PATH]
    assert "shell" not in recorder.kwargs
    assert recorder.kwargs["cwd"] == os.path.dirname(_PATH)


@pytest.mark.skipif(sys.platform != "win32", reason="STARTUPINFO exists only on Windows")
def test_launch_requests_minimized_without_focus_on_windows(monkeypatch) -> None:
    recorder = _PopenRecorder()
    monkeypatch.setattr(terminal_process, "_popen", recorder)

    terminal_process.launch_terminal_minimized(_PATH)

    si = recorder.kwargs["startupinfo"]
    assert si.dwFlags & terminal_process.STARTF_USESHOWWINDOW
    assert si.wShowWindow == 7  # SW_SHOWMINNOACTIVE


def test_launch_propagates_oserror(monkeypatch) -> None:
    def _refuse(*_a, **_k):
        raise OSError("refused")

    monkeypatch.setattr(terminal_process, "_popen", _refuse)
    with pytest.raises(OSError):
        terminal_process.launch_terminal_minimized(_PATH)


def test_select_terminal_pids_matches_only_terminal64_case_insensitively() -> None:
    entries = [
        (1, "terminal64.exe"),
        (2, "TERMINAL64.EXE"),
        (3, "terminal.exe"),
        (4, "metaeditor64.exe"),
        (5, "myterminal64.exe"),
        (6, "Terminal64.exe"),
        (7, ""),
    ]
    assert terminal_process._select_terminal_pids(entries) == [1, 2, 6]


@pytest.mark.parametrize(
    ("token_elevated", "error", "self_elevated", "expected"),
    [
        (True, 0, False, (True, "token")),
        (False, 0, False, (False, "token")),
        (True, 0, True, (True, "token")),
        (None, 5, False, (True, "access_denied_inference")),
        (None, 5, True, (None, "unknown")),
        (None, 5, None, (None, "unknown")),
        (None, 87, False, (None, "unknown")),
        (None, 0, False, (None, "unknown")),
    ],
)
def test_classify_elevation_table(token_elevated, error, self_elevated, expected) -> None:
    assert terminal_process._classify_elevation(token_elevated, error, self_elevated) == expected


@pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp32 is Windows-only")
def test_enumeration_returns_a_list_and_never_raises_on_windows() -> None:
    result = terminal_process.list_terminal_processes()
    assert isinstance(result, list)
    for item in result:
        assert isinstance(item, terminal_process.RunningTerminal)
    assert isinstance(terminal_process.current_process_elevated(), bool)


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows degradation path")
def test_enumeration_degrades_to_unknown_off_windows() -> None:
    assert terminal_process.list_terminal_processes() is None
    assert terminal_process.current_process_elevated() is None
