"""
agent/tests/test_single_instance.py — behavioural tests for agent/single_instance.py.

Every test here injects a fake `kernel32`/`user32` surface via `monkeypatch` rather than
touching the real Windows API, mirroring `agent/terminal_discovery.py`'s testing
convention for `winreg`. See each fake class's own docstring for exactly what it does
NOT cover — real kernel mutex semantics and real Windows foreground-lock behaviour are
both verified on real hardware at the human checkpoint in plan 40-15, not here.
"""
from __future__ import annotations

import ast
import ctypes
import pathlib

import pytest

from agent import single_instance


class _FakeKernel32:
    """
    A stand-in for the real `ctypes.WinDLL("kernel32", ...)` handle, injected via
    monkeypatch so `acquire_single_instance_lock()`'s own branching logic (first-owner
    vs. already-exists) is genuinely exercised, on any platform this test suite runs on.

    Does NOT cover: real kernel mutex semantics — session-namespace visibility, or the
    automatic release of a real mutex handle when the owning OS process terminates.
    Those are verified on real hardware at the human checkpoint in plan 40-15.
    """

    def __init__(self) -> None:
        self._already_exists = False
        self.current_thread_id = 111

    def CreateMutexW(self, _security_attributes: object, _initial_owner: object, _name: str) -> int:
        if self._already_exists:
            ctypes.set_last_error(single_instance._ERROR_ALREADY_EXISTS)
        else:
            ctypes.set_last_error(0)
            self._already_exists = True
        return 4242  # arbitrary non-zero fake handle

    def GetCurrentThreadId(self) -> int:
        return self.current_thread_id


class _FakeUser32:
    """
    A stand-in for the real `ctypes.WinDLL("user32", ...)` handle, injected via
    monkeypatch.

    By default (`tray_window_found=False`) `FindWindowW(TRAY_WINDOW_CLASS, None)`
    returns 0, so `raise_existing_window()` falls through to the title-based path this
    class already exercised before quick 260926-ieo added the tray-window path in
    front of it — every pre-existing test in this file therefore keeps exercising
    exactly the title-based fallback, unchanged.

    Does NOT cover: real Windows foreground-lock behaviour — whether
    `SetForegroundWindow` actually succeeds from a background process on a live,
    interactive desktop session (LIKELY but not proven in
    this sandbox). That still needs confirming on a real machine with a real
    interactive desktop session.
    """

    def __init__(
        self,
        *,
        window_found: bool = True,
        foreground_thread_id: int = 222,
        attach_succeeds: bool = True,
        set_foreground_succeeds: bool = True,
        tray_window_found: bool = False,
        tray_hwnd: int = 7777,
        register_message_succeeds: bool = True,
        allow_set_foreground_raises: bool = False,
        post_message_succeeds: bool = True,
        window_thread_pid: int = 4321,
    ) -> None:
        self.window_found = window_found
        self.foreground_thread_id = foreground_thread_id
        self.attach_succeeds = attach_succeeds
        self.set_foreground_succeeds = set_foreground_succeeds
        self.tray_window_found = tray_window_found
        self.tray_hwnd = tray_hwnd
        self.register_message_succeeds = register_message_succeeds
        self.allow_set_foreground_raises = allow_set_foreground_raises
        self.post_message_succeeds = post_message_succeeds
        self.window_thread_pid = window_thread_pid
        self.attach_calls: list[tuple[int, int, bool]] = []
        self.register_window_message_calls: list[str] = []
        self.post_message_calls: list[tuple[int, int]] = []
        self.allow_set_foreground_calls: list[int] = []

    def FindWindowW(self, class_name: object, title: object) -> int:
        if class_name is not None:
            # The tray-window lookup (quick 260926-ieo): by class name, no title.
            return self.tray_hwnd if self.tray_window_found else 0
        assert title == single_instance.WINDOW_TITLE
        return 9999 if self.window_found else 0

    def RegisterWindowMessageW(self, name: str) -> int:
        self.register_window_message_calls.append(name)
        return 555 if self.register_message_succeeds else 0

    def GetWindowThreadProcessId(self, _hwnd: int, pid_out: object) -> int:
        if pid_out is not None:
            # `ctypes.byref(wintypes.DWORD(...))` on the real path — write through it
            # exactly like the real Win32 call would.
            pid_out._obj.value = self.window_thread_pid
        return self.foreground_thread_id

    def AllowSetForegroundWindow(self, pid: int) -> bool:
        if self.allow_set_foreground_raises:
            raise OSError("simulated AllowSetForegroundWindow failure")
        self.allow_set_foreground_calls.append(pid)
        return True

    def PostMessageW(self, hwnd: int, msg_id: int, _wparam: int, _lparam: int) -> bool:
        self.post_message_calls.append((hwnd, msg_id))
        return self.post_message_succeeds

    def GetForegroundWindow(self) -> int:
        return 8888

    def AttachThreadInput(self, current_id: int, target_id: int, attach: bool) -> bool:
        self.attach_calls.append((current_id, target_id, attach))
        return self.attach_succeeds

    def ShowWindow(self, _hwnd: int, _cmd: int) -> bool:
        return True

    def SetForegroundWindow(self, _hwnd: int) -> bool:
        return self.set_foreground_succeeds


# ---------------------------------------------------------------------------
# acquire_single_instance_lock()
# ---------------------------------------------------------------------------


def test_acquire_returns_true_on_first_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(single_instance, "_kernel32", _FakeKernel32())
    assert single_instance.acquire_single_instance_lock() is True


def test_acquire_returns_false_on_second_call_while_first_handle_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeKernel32()
    monkeypatch.setattr(single_instance, "_kernel32", fake)

    first = single_instance.acquire_single_instance_lock()
    second = single_instance.acquire_single_instance_lock()

    assert first is True
    assert second is False


# ---------------------------------------------------------------------------
# raise_existing_window() — title-based fallback (no tray window found, i.e.
# `tray_window_found=False`, the `_FakeUser32` default). These tests all predate quick
# 260926-ieo's tray-window path and keep their original, unchanged assertions.
# ---------------------------------------------------------------------------


def test_raise_existing_window_returns_false_when_window_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(single_instance, "_kernel32", _FakeKernel32())
    monkeypatch.setattr(single_instance, "_user32", _FakeUser32(window_found=False))

    assert single_instance.raise_existing_window() is False


def test_raise_existing_window_returns_false_when_foreground_call_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(single_instance, "_kernel32", _FakeKernel32())
    monkeypatch.setattr(
        single_instance, "_user32", _FakeUser32(set_foreground_succeeds=False)
    )

    assert single_instance.raise_existing_window() is False


def test_raise_existing_window_returns_true_on_full_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(single_instance, "_kernel32", _FakeKernel32())
    fake_user32 = _FakeUser32()
    monkeypatch.setattr(single_instance, "_user32", fake_user32)

    assert single_instance.raise_existing_window() is True
    # The AttachThreadInput sequence documented in the module docstring was exercised.
    assert fake_user32.attach_calls == [(111, 222, True), (111, 222, False)]


def test_raise_existing_window_never_raises_on_unexpected_api_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ExplodingUser32:
        def FindWindowW(self, _class_name: object, _title: str) -> int:
            raise OSError("simulated Windows API failure")

    monkeypatch.setattr(single_instance, "_kernel32", _FakeKernel32())
    monkeypatch.setattr(single_instance, "_user32", _ExplodingUser32())

    assert single_instance.raise_existing_window() is False


# ---------------------------------------------------------------------------
# raise_existing_window() — tray-window path (quick 260926-ieo)
# ---------------------------------------------------------------------------


def test_raise_existing_window_uses_tray_window_when_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(single_instance, "_kernel32", _FakeKernel32())
    fake_user32 = _FakeUser32(tray_window_found=True)
    monkeypatch.setattr(single_instance, "_user32", fake_user32)

    assert single_instance.raise_existing_window() is True
    assert fake_user32.register_window_message_calls == [single_instance.SHOW_WINDOW_MESSAGE_NAME]
    assert fake_user32.allow_set_foreground_calls == [fake_user32.window_thread_pid]
    assert fake_user32.post_message_calls == [(fake_user32.tray_hwnd, 555)]
    # The title-based path must NOT have run.
    assert fake_user32.attach_calls == []


def test_raise_existing_window_tray_path_returns_false_when_post_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(single_instance, "_kernel32", _FakeKernel32())
    fake_user32 = _FakeUser32(tray_window_found=True, post_message_succeeds=False)
    monkeypatch.setattr(single_instance, "_user32", fake_user32)

    assert single_instance.raise_existing_window() is False
    assert fake_user32.attach_calls == []  # never falls back once a tray window exists


def test_raise_existing_window_tray_path_returns_false_and_posts_nothing_when_register_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(single_instance, "_kernel32", _FakeKernel32())
    fake_user32 = _FakeUser32(tray_window_found=True, register_message_succeeds=False)
    monkeypatch.setattr(single_instance, "_user32", fake_user32)

    assert single_instance.raise_existing_window() is False
    assert fake_user32.post_message_calls == []


def test_raise_existing_window_tray_path_still_posts_when_allow_set_foreground_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(single_instance, "_kernel32", _FakeKernel32())
    fake_user32 = _FakeUser32(tray_window_found=True, allow_set_foreground_raises=True)
    monkeypatch.setattr(single_instance, "_user32", fake_user32)

    assert single_instance.raise_existing_window() is True
    assert fake_user32.post_message_calls == [(fake_user32.tray_hwnd, 555)]


# ---------------------------------------------------------------------------
# Off-Windows degradation
# ---------------------------------------------------------------------------


def test_off_windows_degrades_to_defined_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Off Windows (or wherever the WinDLL load itself failed at import time), both public
    functions must stay importable and callable without ever raising. The module
    already proved plain importability by being imported successfully at test-collection
    time above; this test proves the specific degraded return values the behavior spec
    requires for that state, regardless of which platform this suite happens to run on.
    """
    monkeypatch.setattr(single_instance, "_kernel32", None)
    monkeypatch.setattr(single_instance, "_user32", None)

    assert single_instance.acquire_single_instance_lock() is True
    assert single_instance.raise_existing_window() is False


# ---------------------------------------------------------------------------
# Structural: the module-level mutex handle is never explicitly closed
# ---------------------------------------------------------------------------


def test_mutex_handle_never_explicitly_closed() -> None:
    """
    An `ast` walk over agent/single_instance.py's own source, confirming no call whose
    function name resolves to a handle-closing name (`CloseHandle`) appears anywhere in
    the module — see the module docstring's "WHY A NAMED MUTEX, NOT A LOCK FILE" section
    for why the handle must be left open for the process lifetime.

    This is a source-level (static) guarantee, not a runtime behavioural assertion:
    there is no way to observe "the fake int handle was never closed" via the stubs
    above, since closing (or not closing) a plain Python int has no observable effect.
    A deliberately weak guarantee, for exactly that reason — this is the only
    mechanism available for this particular claim.
    """
    source_path = pathlib.Path(__file__).resolve().parent.parent / "single_instance.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

    forbidden_names = {"CloseHandle"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else (func.id if isinstance(func, ast.Name) else None)
            )
            if name in forbidden_names:
                offenders.append(f"{source_path}:{node.lineno} calls {name}(...)")

    assert not offenders, f"handle-closing call found: {offenders}"
