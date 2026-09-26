"""
agent/tests/test_audio_mute.py — the test coverage for `agent/audio_mute.py`
(quick 260926-ieo). Every behavioural test here uses a FAKE backend and a fake
terminal-pid provider — no test ever touches real Core Audio (see
`agent/tests/conftest.py`'s `_never_touch_real_core_audio` autouse guard, which fails
loudly if `MuteController` ever falls back to its real default backend factory).
"""
from __future__ import annotations

import ast
import contextlib
import logging
import pathlib
import uuid

import pytest

from agent import audio_mute

_MODULE_PATH = pathlib.Path(__file__).resolve().parent.parent / "audio_mute.py"

# Captured at collection time, BEFORE the autouse conftest guard
# (`_never_touch_real_core_audio`) monkeypatches `audio_mute._default_backend_factory`
# for every test — the one test below that exercises the REAL off-Windows degradation
# path needs the real function back, deliberately bypassing that guard for itself only.
_REAL_DEFAULT_BACKEND_FACTORY = audio_mute._default_backend_factory


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSession:
    """A duck-typed stand-in for `agent.audio_mute._Session` — pid/instance_id plus
    `get_mute()`/`set_mute()`. Unlike the real `_Session`, this fake does NOT swallow
    exceptions internally — a test that wants to simulate "the real session raised
    while `MuteController` was walking the snapshot" configures `raise_on_get_mute`."""

    def __init__(
        self,
        pid: int,
        instance_id: str,
        *,
        muted: bool = False,
        fail_get_mute: bool = False,
        fail_set_mute: bool = False,
        raise_on_get_mute: "Exception | None" = None,
    ) -> None:
        self.pid = pid
        self.instance_id = instance_id
        self._muted = muted
        self._fail_get_mute = fail_get_mute
        self._fail_set_mute = fail_set_mute
        self._raise_on_get_mute = raise_on_get_mute
        self.calls: "list[str]" = []

    def get_mute(self):
        self.calls.append("get_mute")
        if self._raise_on_get_mute is not None:
            raise self._raise_on_get_mute
        if self._fail_get_mute:
            return None
        return self._muted

    def set_mute(self, muted: bool) -> bool:
        self.calls.append(f"set_mute({muted})")
        if self._fail_set_mute:
            return False
        self._muted = muted
        return True


class _FakeBackend:
    """Records whether its context manager's body ran to completion (`exit_count`
    increments in `finally`, so it is proven to increment even when the body of the
    `with` block raises)."""

    def __init__(self, sessions: "list[_FakeSession]") -> None:
        self.sessions = sessions
        self.enter_count = 0
        self.exit_count = 0

    @contextlib.contextmanager
    def snapshot(self):
        self.enter_count += 1
        try:
            yield self.sessions
        finally:
            self.exit_count += 1


def _backend_factory(backend: "_FakeBackend"):
    return lambda: backend


# ---------------------------------------------------------------------------
# apply()
# ---------------------------------------------------------------------------


def test_apply_mutes_only_terminal_pid_sessions_and_reports_counts() -> None:
    terminal = _FakeSession(100, "term-1", muted=False)
    browser = _FakeSession(200, "browser-1", muted=False)
    system = _FakeSession(0, "system-sounds", muted=False)
    backend = _FakeBackend([terminal, browser, system])

    controller = audio_mute.MuteController(
        backend_factory=_backend_factory(backend), terminal_pids=lambda: {100}
    )
    result = controller.apply(adopt_already_muted=False)

    assert "set_mute(True)" in terminal.calls
    assert not any(call.startswith("set_mute") for call in browser.calls)
    assert not any(call.startswith("set_mute") for call in system.calls)
    assert result.terminal_sessions_seen == 1
    assert result.newly_muted == 1
    assert backend.exit_count == 1


def test_apply_twice_on_already_muted_session_makes_no_second_set_mute_call() -> None:
    terminal = _FakeSession(100, "term-1", muted=False)
    backend = _FakeBackend([terminal])
    controller = audio_mute.MuteController(
        backend_factory=_backend_factory(backend), terminal_pids=lambda: {100}
    )

    first = controller.apply(adopt_already_muted=False)
    calls_after_first = list(terminal.calls)
    second = controller.apply(adopt_already_muted=False)

    assert first.newly_muted == 1
    assert second.newly_muted == 0
    assert terminal.calls == calls_after_first  # no new set_mute/get_mute call at all


def test_apply_leaves_already_user_muted_session_alone_and_restore_never_unmutes_it() -> None:
    terminal = _FakeSession(100, "term-1", muted=True)
    backend = _FakeBackend([terminal])
    controller = audio_mute.MuteController(
        backend_factory=_backend_factory(backend), terminal_pids=lambda: {100}
    )

    result = controller.apply(adopt_already_muted=False)
    assert result.left_alone_user_muted == 1
    assert not any(call.startswith("set_mute") for call in terminal.calls)

    calls_before_restore = list(terminal.calls)
    restore_result = controller.restore()
    assert restore_result.unmuted == 0
    assert terminal.calls == calls_before_restore  # never touched

    restore_with_adopt = controller.restore(adopt_already_muted=True)
    assert restore_with_adopt.unmuted == 0
    assert terminal.calls == calls_before_restore  # still never touched — it is "theirs"


def test_apply_adopts_already_muted_session_when_requested_and_restore_unmutes_it() -> None:
    terminal = _FakeSession(100, "term-1", muted=True)
    backend = _FakeBackend([terminal])
    controller = audio_mute.MuteController(
        backend_factory=_backend_factory(backend), terminal_pids=lambda: {100}
    )

    result = controller.apply(adopt_already_muted=True)
    assert result.adopted == 1
    assert controller.has_recorded() is True

    restore_result = controller.restore()
    assert restore_result.unmuted == 1
    assert terminal._muted is False


def test_terminal_pid_provider_returning_none_touches_nothing() -> None:
    backend = _FakeBackend([_FakeSession(100, "term-1", muted=False)])
    backend_factory_calls: "list[None]" = []

    def _factory():
        backend_factory_calls.append(None)
        return backend

    controller = audio_mute.MuteController(backend_factory=_factory, terminal_pids=lambda: None)

    assert controller.apply(adopt_already_muted=False) == audio_mute.ApplyResult()
    assert controller.restore() == audio_mute.RestoreResult()
    assert backend_factory_calls == []  # short-circuited before ever touching the backend


def test_backend_factory_none_returns_all_zero() -> None:
    controller = audio_mute.MuteController(backend_factory=lambda: None, terminal_pids=lambda: {100})

    assert controller.apply(adopt_already_muted=False) == audio_mute.ApplyResult()
    assert controller.restore() == audio_mute.RestoreResult()


def test_backend_factory_raising_oserror_returns_all_zero_without_exception() -> None:
    def _raiser():
        raise OSError("simulated backend construction failure")

    controller = audio_mute.MuteController(backend_factory=_raiser, terminal_pids=lambda: {100})

    assert controller.apply(adopt_already_muted=False) == audio_mute.ApplyResult()
    assert controller.restore() == audio_mute.RestoreResult()


def test_snapshot_raising_mid_iteration_still_releases_and_does_not_raise() -> None:
    exploding = _FakeSession(100, "term-1", raise_on_get_mute=RuntimeError("boom"))
    backend = _FakeBackend([exploding])
    controller = audio_mute.MuteController(
        backend_factory=_backend_factory(backend), terminal_pids=lambda: {100}
    )

    result = controller.apply(adopt_already_muted=False)

    assert result == audio_mute.ApplyResult()  # all-zero — the plan permits either
    assert backend.exit_count == 1  # the context manager's own finally still ran


def test_get_mute_returning_none_counts_as_error_and_leaves_session_untouched() -> None:
    terminal = _FakeSession(100, "term-1", fail_get_mute=True)
    backend = _FakeBackend([terminal])
    controller = audio_mute.MuteController(
        backend_factory=_backend_factory(backend), terminal_pids=lambda: {100}
    )

    result = controller.apply(adopt_already_muted=False)

    assert result.errors == 1
    assert not any(call.startswith("set_mute") for call in terminal.calls)
    assert controller.has_recorded() is False


def test_same_failure_repeated_twice_logs_warning_only_once(caplog: pytest.LogCaptureFixture) -> None:
    def _raiser():
        raise OSError("same failure every time")

    controller = audio_mute.MuteController(backend_factory=_raiser, terminal_pids=lambda: {100})

    with caplog.at_level(logging.DEBUG, logger="agent.audio_mute"):
        controller.apply(adopt_already_muted=False)
        controller.apply(adopt_already_muted=False)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# restore()
# ---------------------------------------------------------------------------


def test_restore_set_mute_failure_keeps_session_recorded_and_counts_error() -> None:
    terminal = _FakeSession(100, "term-1", muted=False)
    backend = _FakeBackend([terminal])
    controller = audio_mute.MuteController(
        backend_factory=_backend_factory(backend), terminal_pids=lambda: {100}
    )
    controller.apply(adopt_already_muted=False)
    assert controller.has_recorded() is True

    terminal._fail_set_mute = True
    result = controller.restore()

    assert result.errors == 1
    assert result.unmuted == 0
    assert controller.has_recorded() is True  # still recorded — will retry next time


def test_restore_drops_stale_record_when_session_no_longer_present() -> None:
    terminal = _FakeSession(100, "term-1", muted=False)
    backend = _FakeBackend([terminal])
    pid_holder = {"pids": {100}}
    controller = audio_mute.MuteController(
        backend_factory=_backend_factory(backend), terminal_pids=lambda: pid_holder["pids"]
    )
    controller.apply(adopt_already_muted=False)
    assert controller.has_recorded() is True

    pid_holder["pids"] = set()  # the terminal pid no longer exists
    result = controller.restore()

    assert result.unmuted == 0
    assert controller.has_recorded() is False  # dropped, not left dangling forever


def test_restore_with_adopt_unmutes_unclassified_muted_session_but_never_the_users() -> None:
    theirs = _FakeSession(100, "term-theirs", muted=True)
    unclassified = _FakeSession(100, "term-new", muted=True)
    backend = _FakeBackend([theirs])
    controller = audio_mute.MuteController(
        backend_factory=_backend_factory(backend), terminal_pids=lambda: {100}
    )
    controller.apply(adopt_already_muted=False)  # classifies `theirs` as the user's own

    backend.sessions.append(unclassified)  # a new process instance appears, already muted
    result = controller.restore(adopt_already_muted=True)

    assert unclassified._muted is False  # adopted-and-unmuted
    assert theirs._muted is True  # the user's own mute is untouched
    assert result.unmuted == 1


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_guid_round_trips_against_uuid_bytes_le() -> None:
    sample = "BCDE0395-E52F-467C-8E3D-C4579291692E"
    guid = audio_mute._guid(sample)
    raw = audio_mute.ctypes.string_at(audio_mute.ctypes.addressof(guid), audio_mute.ctypes.sizeof(guid))
    assert raw == uuid.UUID(sample).bytes_le


def test_session_key_distinguishes_same_pid_different_instance() -> None:
    key_a = audio_mute._session_key(100, "instance-a")
    key_b = audio_mute._session_key(100, "instance-b")
    assert key_a != key_b


# ---------------------------------------------------------------------------
# Structural (AST) audits — never text greps, see module docstring precedent in
# agent/tests/test_sync.py::TestAgentFolderStructuralAudit.
# ---------------------------------------------------------------------------


def _tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"), filename=str(_MODULE_PATH))


def _docstring_constant_ids(tree: ast.Module) -> "set[int]":
    """`id()` of every string `Constant` that is an actual module/class/function
    docstring — copied from `agent/tests/test_sync.py`'s identical helper so a check
    can ignore prose naming a forbidden construct without ignoring the same literal
    used as real code elsewhere."""
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


def test_module_imports_no_subprocess_socket_or_winreg() -> None:
    tree = _tree()
    forbidden = {"subprocess", "socket", "winreg"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden, f"forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden, f"forbidden import: {node.module}"


def test_no_forbidden_process_or_window_calls() -> None:
    tree = _tree()
    forbidden_names = {
        "kill",
        "terminate",
        "TerminateProcess",
        "ExitProcess",
        "ShowWindow",
        "SetWindowPos",
        "MoveWindow",
        "CloseWindow",
        "EnumWindows",
        "FindWindowW",
        "open",
        "write_text",
        "write_bytes",
        "WritePrivateProfileStringW",
    }
    offenders: "list[str]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else (func.id if isinstance(func, ast.Name) else None)
            )
            if name in forbidden_names:
                offenders.append(f"{node.lineno}: calls {name}(...)")
    assert not offenders, offenders


def test_no_endpoint_or_master_volume_identifiers_or_strings() -> None:
    tree = _tree()
    docstring_ids = _docstring_constant_ids(tree)
    banned = ("endpointvolume", "mastervolume", "5cdf2c82")
    offenders: "list[str]" = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        if name and any(sub in name.lower() for sub in banned):
            offenders.append(f"{getattr(node, 'lineno', '?')}: identifier {name!r}")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_ids
        ):
            lowered = node.value.lower()
            if any(sub in lowered for sub in banned):
                offenders.append(f"{node.lineno}: string constant {node.value!r}")
    assert not offenders, offenders


def test_no_non_docstring_string_constant_ends_with_dot_ini() -> None:
    tree = _tree()
    docstring_ids = _docstring_constant_ids(tree)
    offenders: "list[str]" = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_ids
            and node.value.endswith(".ini")
        ):
            offenders.append(f"{node.lineno}: {node.value!r}")
    assert not offenders, offenders


def test_off_windows_or_missing_handle_degrades_to_all_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_default_backend_factory()` itself, with the module's WinDLL handle forced to
    `None`, must return `None` without raising — proving the off-Windows degradation
    path independently of the autouse conftest guard (which replaces this same
    function with a `pytest.fail` trap for every OTHER test in this suite). Restores
    the REAL function for this one test only, via `_REAL_DEFAULT_BACKEND_FACTORY`
    captured at collection time, before calling it directly."""
    monkeypatch.setattr(audio_mute, "_default_backend_factory", _REAL_DEFAULT_BACKEND_FACTORY)
    monkeypatch.setattr(audio_mute, "_ole32", None)

    assert audio_mute._default_backend_factory() is None
