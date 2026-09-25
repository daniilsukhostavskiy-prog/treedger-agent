"""
agent/tests/test_autostart.py — behavioural and structural tests for agent/autostart.py.

`subprocess.run` is monkeypatched module-wide to a recorder that captures each
invocation's argument LIST (never a rendered string) and returns canned scheduler
output. Structural claims (parameter-less
signatures, no `shell=True`) are decided by `ast`, matching this project's
established grep-is-not-proof convention: a text search finds the words used to
describe a prohibition, not what the code actually does.
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
    invocation's argument list (asserting it IS a list, never a shell string) AND its
    keyword arguments, and returns either a caller-supplied canned response or a
    generic success response.
    """

    def __init__(self) -> None:
        self.calls: "list[list[str]]" = []
        self.kwargs: "list[dict[str, object]]" = []
        self._queued_responses: "list[types.SimpleNamespace]" = []

    def queue_response(self, response: types.SimpleNamespace) -> None:
        self._queued_responses.append(response)

    def __call__(self, args: object, **kwargs: object) -> types.SimpleNamespace:
        assert isinstance(args, list), "subprocess.run must be called with an argument list"
        assert "shell" not in kwargs, "subprocess.run must never be called with shell="
        self.calls.append(list(args))  # type: ignore[arg-type]
        self.kwargs.append(dict(kwargs))
        if self._queued_responses:
            return self._queued_responses.pop(0)
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


def _task_xml(command: str, arguments: str = autostart.MINIMIZED_FLAG) -> str:
    """The shape `schtasks /Query /TN ... /XML` really prints: a UTF-16 declaration
    over single-byte text, `\\r\\r\\n` line endings, the task namespace, and a quoted
    Command (Task Scheduler stores the /TR quotes)."""
    args_line = f"      <Arguments>{arguments}</Arguments>\r\r\n" if arguments else ""
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\r\r\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\r\r\n'
        "  <Actions Context=\"Author\">\r\r\n"
        "    <Exec>\r\r\n"
        f"      <Command>\"{command}\"</Command>\r\r\n"
        f"{args_line}"
        "    </Exec>\r\r\n"
        "  </Actions>\r\r\n"
        "</Task>\r\r\n"
    )


def _query_success_response(command: str, arguments: str = autostart.MINIMIZED_FLAG) -> types.SimpleNamespace:
    """A canned `schtasks /Query ... /XML` success response (ASCII-safe bytes)."""
    return types.SimpleNamespace(
        returncode=0, stdout=_task_xml(command, arguments).encode("ascii"), stderr=b""
    )


def _query_not_found_response() -> types.SimpleNamespace:
    """A canned `schtasks /Query` failure response — the task does not exist. The
    stderr text is localized in real life, which is why only the returncode counts."""
    return types.SimpleNamespace(
        returncode=1,
        stdout=b"",
        stderr="ОШИБКА: Не удается найти указанный файл.\r\n".encode("cp866"),
    )


# ---------------------------------------------------------------------------
# own_executable_path()
# ---------------------------------------------------------------------------


def test_own_executable_path_returns_frozen_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_exe = r"C:\Users\someone\AppData\Local\Programs\Treedger\Treedger.exe"
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
    recorder.queue_response(_query_success_response(r"C:\path\Treedger\Treedger.exe"))
    monkeypatch.setattr(autostart.subprocess, "run", recorder)

    assert autostart.query_task_command() == r'"C:\path\Treedger\Treedger.exe" --minimized'


def test_query_task_command_returns_none_when_no_task(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _RunRecorder()
    recorder.queue_response(_query_not_found_response())
    monkeypatch.setattr(autostart.subprocess, "run", recorder)

    assert autostart.query_task_command() is None


def test_task_exists_true_and_false(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _RunRecorder()
    recorder.queue_response(_query_success_response(r"C:\path\Treedger\Treedger.exe"))
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
    fake_exe = r"C:\Users\someone\AppData\Local\Programs\Treedger\Treedger.exe"
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
    fake_exe = r"C:\Users\someone\AppData\Local\Programs\Treedger\Treedger.exe"
    monkeypatch.setattr(autostart, "own_executable_path", lambda: fake_exe)
    stale_command = r'"D:\old\location\Treedger\Treedger.exe" --minimized'
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
    fake_exe = r"C:\Users\someone\AppData\Local\Programs\Treedger\Treedger.exe"
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


# ---------------------------------------------------------------------------
# 260925-k6y — locale-independent XML query, decoding, truthful checkbox,
# CREATE_NO_WINDOW on every call.
# ---------------------------------------------------------------------------


def test_decode_utf16le_with_bom() -> None:
    text = "<Task>Привет</Task>"
    raw = b"\xff\xfe" + text.encode("utf-16-le")
    assert autostart._decode_schtasks_output(raw) == text


def test_decode_bytes_with_nuls_as_utf16le() -> None:
    text = "<Command>C:\\x\\Treedger.exe</Command>"
    assert autostart._decode_schtasks_output(text.encode("utf-16-le")) == text


def test_decode_cp866_bytes_with_the_oem_codepage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "_oem_codepage", lambda: 866)
    path = "C:\\Users\\Даниил\\Treedger\\Treedger.exe"
    assert autostart._decode_schtasks_output(path.encode("cp866")) == path


def test_decode_str_passes_through_and_none_is_empty() -> None:
    assert autostart._decode_schtasks_output("already text") == "already text"
    assert autostart._decode_schtasks_output(None) == ""


def test_parse_task_xml_namespaced_with_declaration_and_crcrlf() -> None:
    xml_text = _task_xml(r"C:\x y\Treedger.exe")
    assert "\r\r\n" in xml_text and 'encoding="UTF-16"' in xml_text

    assert autostart._parse_task_xml(xml_text) == (r"C:\x y\Treedger.exe", "--minimized")


def test_parse_task_xml_without_exec_command_is_none() -> None:
    xml_text = (
        '<?xml version="1.0" encoding="UTF-16"?>\r\r\n'
        '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        "<Actions><ComHandler><ClassId>{x}</ClassId></ComHandler></Actions></Task>"
    )
    assert autostart._parse_task_xml(xml_text) is None
    assert autostart._parse_task_xml("not xml at all <") is None
    assert autostart._parse_task_xml("") is None


def test_query_task_command_from_canned_xml(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _RunRecorder()
    recorder.queue_response(_query_success_response(r"C:\x y\Treedger.exe"))
    monkeypatch.setattr(autostart.subprocess, "run", recorder)

    assert autostart.query_task_command() == r'"C:\x y\Treedger.exe" --minimized'
    assert recorder.calls[0] == ["schtasks", "/Query", "/TN", autostart.TASK_NAME, "/XML"]


def test_query_task_command_none_on_returncode_1(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _RunRecorder()
    recorder.queue_response(_query_not_found_response())
    monkeypatch.setattr(autostart.subprocess, "run", recorder)
    assert autostart.query_task_command() is None


def test_query_task_command_without_arguments_omits_them(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _RunRecorder()
    recorder.queue_response(_query_success_response(r"C:\x\Treedger.exe", arguments=""))
    monkeypatch.setattr(autostart.subprocess, "run", recorder)
    assert autostart.query_task_command() == r'"C:\x\Treedger.exe"'


def test_registered_for_this_exe_true_only_for_own_path(monkeypatch: pytest.MonkeyPatch) -> None:
    own = r"C:\Users\someone\Treedger\Treedger.exe"
    monkeypatch.setattr(autostart, "own_executable_path", lambda: own)

    monkeypatch.setattr(autostart, "query_task_command", lambda: f'"{own.upper()}" --MINIMIZED')
    assert autostart.task_is_registered_for_this_exe() is True

    monkeypatch.setattr(
        autostart, "query_task_command", lambda: r'"D:\vps\main.exe" --minimized'
    )
    assert autostart.task_is_registered_for_this_exe() is False

    monkeypatch.setattr(autostart, "query_task_command", lambda: None)
    assert autostart.task_is_registered_for_this_exe() is False


def test_repair_compares_case_insensitively(monkeypatch: pytest.MonkeyPatch) -> None:
    own = r"C:\Users\someone\Treedger\Treedger.exe"
    monkeypatch.setattr(autostart, "own_executable_path", lambda: own)
    monkeypatch.setattr(autostart, "query_task_command", lambda: f'"{own.lower()}" --minimized')
    recorder = _RunRecorder()
    monkeypatch.setattr(autostart.subprocess, "run", recorder)

    assert "already correct" in autostart.repair_task_path()
    assert recorder.calls == []


def test_every_schtasks_call_carries_create_no_window(monkeypatch: pytest.MonkeyPatch) -> None:
    own = r"C:\Users\someone\Treedger\Treedger.exe"
    monkeypatch.setattr(autostart, "own_executable_path", lambda: own)
    recorder = _RunRecorder()
    recorder.queue_response(_query_success_response(r"D:\stale\Treedger.exe"))
    monkeypatch.setattr(autostart.subprocess, "run", recorder)

    autostart.repair_task_path()  # query + change
    autostart.create_task()
    autostart.remove_task()

    verbs = [call[1] for call in recorder.calls]
    assert verbs == ["/Query", "/Change", "/Create", "/Delete"]
    for kwargs in recorder.kwargs:
        assert kwargs.get("creationflags") == autostart._CREATE_NO_WINDOW
        assert "shell" not in kwargs
        assert kwargs.get("capture_output") is True
        assert kwargs.get("check") is False
    if sys.platform == "win32":
        assert autostart._CREATE_NO_WINDOW == 0x08000000
