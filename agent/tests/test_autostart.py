"""
agent/tests/test_autostart.py — behavioural and structural tests for agent/autostart.py.

`subprocess.run` is monkeypatched module-wide to a recorder that captures each
invocation's argument LIST (never a rendered string) and returns canned scheduler
output, per the plan's own testing instructions. Structural claims (parameter-less
signatures, no `shell=True`) are decided by `ast`, matching this project's
grep-is-not-proof convention (40-CONTEXT.md D-16).
"""
from __future__ import annotations

import ast
import pathlib
import sys
import types

import pytest

from agent import autostart


class _RunRecorder:
    """
    A stand-in for `subprocess.run`, injected via monkeypatch. Captures every
    invocation's argument list (asserting it IS a list, never a shell string) and
    returns either a caller-supplied canned response or a generic success response.
    """

    def __init__(self) -> None:
        self.calls: "list[list[str]]" = []
        self._queued_responses: "list[types.SimpleNamespace]" = []

    def queue_response(self, response: types.SimpleNamespace) -> None:
        self._queued_responses.append(response)

    def __call__(self, args: object, **kwargs: object) -> types.SimpleNamespace:
        assert isinstance(args, list), "subprocess.run must be called with an argument list"
        assert "shell" not in kwargs, "subprocess.run must never be called with shell="
        self.calls.append(list(args))  # type: ignore[arg-type]
        if self._queued_responses:
            return self._queued_responses.pop(0)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def _query_success_response(command: str) -> types.SimpleNamespace:
    """A canned `schtasks /Query ... /FO LIST /V` success response carrying `command`."""
    stdout = (
        "Folder: \\\n"
        "HostName:                             DESKTOP\n"
        f"TaskName:                             \\{autostart.TASK_NAME}\n"
        f"Task To Run:                          {command}\n"
        "Run As User:                          DESKTOP\\user\n"
    )
    return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _query_not_found_response() -> types.SimpleNamespace:
    """A canned `schtasks /Query` failure response — the task does not exist."""
    return types.SimpleNamespace(
        returncode=1,
        stdout="",
        stderr="ERROR: The system cannot find the file specified.\n",
    )


# ---------------------------------------------------------------------------
# own_executable_path()
# ---------------------------------------------------------------------------


def test_own_executable_path_returns_frozen_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_exe = r"C:\Users\someone\AppData\Local\Programs\TreedgerAgent\main.dist\main.exe"
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


# ---------------------------------------------------------------------------
# query_task_command() / task_exists()
# ---------------------------------------------------------------------------


def test_query_task_command_returns_registered_command(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _RunRecorder()
    recorder.queue_response(_query_success_response(r'"C:\path\main.exe" --minimized'))
    monkeypatch.setattr(autostart.subprocess, "run", recorder)

    assert autostart.query_task_command() == r'"C:\path\main.exe" --minimized'


def test_query_task_command_returns_none_when_no_task(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _RunRecorder()
    recorder.queue_response(_query_not_found_response())
    monkeypatch.setattr(autostart.subprocess, "run", recorder)

    assert autostart.query_task_command() is None


def test_task_exists_true_and_false(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _RunRecorder()
    recorder.queue_response(_query_success_response(r'"C:\path\main.exe" --minimized'))
    monkeypatch.setattr(autostart.subprocess, "run", recorder)
    assert autostart.task_exists() is True

    recorder2 = _RunRecorder()
    recorder2.queue_response(_query_not_found_response())
    monkeypatch.setattr(autostart.subprocess, "run", recorder2)
    assert autostart.task_exists() is False


# ---------------------------------------------------------------------------
# repair_task_path()
# ---------------------------------------------------------------------------


def test_repair_issues_no_command_when_no_task_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The never-create case: `query_task_command()` (the only primitive that could reach
    `subprocess.run`) is bypassed directly so the recorder can prove the true claim —
    `repair_task_path()` issues NOTHING AT ALL, not even a query, once it has already
    learned no task exists.
    """
    monkeypatch.setattr(autostart, "query_task_command", lambda: None)
    recorder = _RunRecorder()
    monkeypatch.setattr(autostart.subprocess, "run", recorder)

    result = autostart.repair_task_path()

    assert recorder.calls == []
    assert "no" in result.lower()


def test_repair_issues_no_change_when_path_already_correct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_exe = r"C:\Users\someone\AppData\Local\Programs\TreedgerAgent\main.dist\main.exe"
    monkeypatch.setattr(autostart, "own_executable_path", lambda: fake_exe)
    current_command = f'"{fake_exe}" {autostart.MINIMIZED_FLAG}'
    monkeypatch.setattr(autostart, "query_task_command", lambda: current_command)
    recorder = _RunRecorder()
    monkeypatch.setattr(autostart.subprocess, "run", recorder)

    result = autostart.repair_task_path()

    assert recorder.calls == []
    assert "already correct" in result.lower()


def test_repair_issues_exactly_one_change_when_path_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_exe = r"C:\Users\someone\AppData\Local\Programs\TreedgerAgent\main.dist\main.exe"
    monkeypatch.setattr(autostart, "own_executable_path", lambda: fake_exe)
    stale_command = r'"D:\old\location\main.exe" --minimized'
    monkeypatch.setattr(autostart, "query_task_command", lambda: stale_command)
    recorder = _RunRecorder()
    monkeypatch.setattr(autostart.subprocess, "run", recorder)

    result = autostart.repair_task_path()

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call[0] == "schtasks"
    assert "/Change" in call
    assert autostart.TASK_NAME in call
    tr_index = call.index("/TR") + 1
    assert fake_exe in call[tr_index]
    assert autostart.MINIMIZED_FLAG in call[tr_index]
    assert "repaired" in result.lower()


def test_repair_task_path_accepts_no_parameters() -> None:
    with pytest.raises(TypeError):
        autostart.repair_task_path(object())  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# create_task()
# ---------------------------------------------------------------------------


def test_create_task_issues_exactly_one_create_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_exe = r"C:\Users\someone\AppData\Local\Programs\TreedgerAgent\main.dist\main.exe"
    monkeypatch.setattr(autostart, "own_executable_path", lambda: fake_exe)
    recorder = _RunRecorder()
    monkeypatch.setattr(autostart.subprocess, "run", recorder)

    autostart.create_task()

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call[0] == "schtasks"
    assert "/Create" in call
    assert autostart.TASK_NAME in call
    tr_index = call.index("/TR") + 1
    assert fake_exe in call[tr_index]
    assert autostart.MINIMIZED_FLAG in call[tr_index]


def test_create_task_accepts_no_parameters() -> None:
    with pytest.raises(TypeError):
        autostart.create_task(object())  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# remove_task()
# ---------------------------------------------------------------------------


def test_remove_task_issues_exactly_one_delete_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _RunRecorder()
    monkeypatch.setattr(autostart.subprocess, "run", recorder)

    autostart.remove_task()

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call[0] == "schtasks"
    assert "/Delete" in call
    assert autostart.TASK_NAME in call


# ---------------------------------------------------------------------------
# Structural: signatures and no shell=True (also re-asserted by the plan's own
# verify command against the real source file — these tests give a readable failure
# inside the normal pytest run too).
# ---------------------------------------------------------------------------


def test_structural_functions_exist_and_writer_functions_are_parameterless() -> None:
    source_path = pathlib.Path(__file__).resolve().parent.parent / "autostart.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    for name in (
        "own_executable_path",
        "task_exists",
        "query_task_command",
        "repair_task_path",
        "create_task",
        "remove_task",
    ):
        assert name in functions, f"missing {name}"

    for name in ("create_task", "repair_task_path"):
        args = functions[name].args
        assert not (
            args.args or args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg
        ), f"{name} must take no parameters"


def test_structural_no_shell_true_anywhere() -> None:
    source_path = pathlib.Path(__file__).resolve().parent.parent / "autostart.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "shell":
                    offenders.append(f"{source_path}:{node.lineno}")
    assert not offenders, f"a subprocess call passes shell=: {offenders}"
