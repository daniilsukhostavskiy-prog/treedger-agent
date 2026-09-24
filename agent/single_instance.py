"""
agent/single_instance.py — a per-user, per-session named-mutex guard so a second copy
of this program launched while the first is still running does not corrupt another
account's history in the same MT5 terminal (40-CONTEXT.md D-25).

WHY THIS EXISTS (D-25)
--------------------------------------------------------------------------
Before this phase's autostart feature, two running copies of this program were rare.
After it, they become everyday: the program sits minimised in the taskbar, the person
does not see it, and clicks the shortcut again. The cost of doing nothing here is NOT a
crash — it is silent data corruption. If two processes each drive the same physical MT5
terminal and switch its logged-in account under each other, the first process can end up
reading and uploading the SECOND process's account history under the first account's
identity. No error is ever raised anywhere in that sequence — the trades simply land
against the wrong account. `agent/main.py`'s `_sync_in_flight` flag cannot help here: it
is a per-process Python bool, and a second OS process starts with its own fresh one.

The contract for a second instance is therefore NOT "exit silently" — the person clicked
the shortcut, saw nothing happen, and will click it again, thinking it did not register.
The second instance must bring the FIRST instance's window to the front, so the click
visibly did something, and only THEN exit with code 0. That exit-0 wiring itself lands
in `agent/main.py` in a later plan (40-10) — this module only provides the two
primitives `main.py` calls before doing anything else.

WHY A NAMED MUTEX, NOT A LOCK FILE
--------------------------------------------------------------------------
A Windows kernel mutex is released automatically by the OS the moment the owning
process terminates, for ANY reason — clean exit, crash, or a forced kill from Task
Manager. A PID or lock file on disk has no such guarantee: if the process that created
it dies abnormally, the file survives, and every later launch would see it and refuse to
start — forever, unless the program grows a "maybe this file is stale" heuristic. This
project has already refused that exact class of heuristic elsewhere (39-CONTEXT.md
D-29..D-36 refuse to infer `broker_closed` from a failure count) — a stale-lock guess is
one more decision this program is not equipped to make reliably, so it does not try. The
mutex sidesteps the whole class of problem: nothing to go stale.

WHY `Local\\` NOT `Global\\`
--------------------------------------------------------------------------
`Global\\` names live in a machine-wide kernel object namespace and, on some Windows
configurations, require the `SeCreateGlobalPrivilege` right to create — the wrong scope
for a program that deliberately never asks for administrator rights (D-13). `Local\\`
(the per-session namespace) is visible only within the caller's own Terminal
Services/logon session, which is exactly right: this program only needs to detect a
second copy the SAME logged-in user just launched, never a copy started by a different
Windows user on the same machine — a scenario this program's threat model does not
otherwise consider either (see the accepted limitation below).

ACCEPTED LIMITATION (T-40-04-05, disposition: accept)
--------------------------------------------------------------------------
`MUTEX_NAME` is a fixed, guessable string. A malicious local process running as the same
Windows user could pre-create a mutex with this exact name before this program ever
launches, which would make `acquire_single_instance_lock()` always report "another
instance is already running" and block this program from starting at all. For this
program's threat model — a single-digit user base, and no adversarial local-process
scenario modelled anywhere else in this phase's threat register either — this is an
accepted, low-severity, documented limitation, not something to engineer around with
e.g. a randomized or per-install mutex name (which would then need to be persisted
somewhere, reopening exactly the "where do we safely store one more value" question
`agent/config_store.py`'s own docstring already tries to minimise).
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Optional

try:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
except AttributeError:
    # Non-Windows platform (this program is Windows-only per README.md, but its unit
    # tests must still import and run anywhere, including this repo's own non-Windows
    # dev/CI environments — mirrors agent/terminal_discovery.py's identical pattern for
    # `winreg`). `ctypes.WinDLL` itself does not exist off Windows, so merely accessing
    # the attribute raises AttributeError before any call is even attempted — never a
    # crash at import time.
    _kernel32 = None  # type: ignore[assignment]
    _user32 = None  # type: ignore[assignment]

if _kernel32 is not None:
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _kernel32.GetCurrentThreadId.argtypes = []

if _user32 is not None:
    _user32.FindWindowW.restype = wintypes.HWND
    _user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPDWORD]
    _user32.AttachThreadInput.restype = wintypes.BOOL
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _user32.ShowWindow.restype = wintypes.BOOL
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]

MUTEX_NAME = r"Local\TreedgerAgent-SingleInstance"
"""The kernel object name this program checks and creates. Per-session (`Local\\`), not
machine-wide (`Global\\`) — see the module docstring's "WHY `Local\\` NOT `Global\\`"
section. Fixed and guessable by design; see "ACCEPTED LIMITATION" above for why that is
an accepted low-severity limitation rather than a gap to close."""

WINDOW_TITLE = "Treedger — синхронизация MT5"
"""The exact title `agent/main.py`'s `AgentWindow.__init__` assigns via
`self.root.title(...)` (agent/main.py:76) — copied from that file, not retyped from
memory, so `raise_existing_window()` keeps matching it even if this file is edited
without also updating main.py (a mismatch here would make the raise silently stop
working, degrading to the documented `False` path rather than crashing)."""

_ERROR_ALREADY_EXISTS = 183
_SW_RESTORE = 9

# Retained for the process lifetime; see the module docstring's "WHY A NAMED MUTEX, NOT
# A LOCK FILE" section for why nothing in this module ever explicitly closes it. Typed
# as a plain Optional[int] (rather than wintypes.HANDLE) so a monkeypatched, non-Windows
# test double can also assign a plain int here.
_mutex_handle: Optional[int] = None


def acquire_single_instance_lock() -> bool:
    """
    Returns True the first time this is called in a fresh process — this process now
    owns `MUTEX_NAME` and is the only instance. Returns False if another process
    already owns it (i.e. `CreateMutexW` reports `ERROR_ALREADY_EXISTS`).

    The returned handle is stored at module scope and is NEVER explicitly closed by
    this module — deliberately: the Windows kernel releases a mutex handle
    automatically when the owning process terminates, by any means (clean exit, crash,
    a forced kill from Task Manager), and that automatic release is the entire reason a
    mutex was chosen over a lock file (see module docstring). Adding a "close on exit"
    code path would invite a later error-path variant that closes it EARLY, silently
    reopening the exact cross-process race this module exists to prevent — so the
    simplest correct thing is: acquire once, never release, let the OS handle the rest.
    """
    global _mutex_handle
    if _kernel32 is None:
        # Off Windows there is no OS mutex primitive to check against. Every caller in
        # a non-Windows test/dev run degrades to "we are the only instance" — a defined
        # value, never a raise, matching this module's documented off-Windows contract.
        return True

    ctypes.set_last_error(0)
    handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    _mutex_handle = handle
    return ctypes.get_last_error() != _ERROR_ALREADY_EXISTS


def raise_existing_window() -> bool:
    """
    Best-effort: find the first instance's window by its exact title and bring it to
    the foreground. Returns True only when a window handle was found AND the
    foreground call itself reported success. NEVER raises — every Windows API failure
    along this path degrades to False, because a second instance that cannot raise the
    first instance's window must still exit cleanly; this function's only job is a
    best effort, and its caller (agent/main.py, plan 40-10) exits 0 regardless of the
    return value.

    Honest caveat (40-RESEARCH.md Pattern 2 / assumption A3 — LIKELY, not proven in
    this sandbox): Windows' foreground-lock restriction means a background process
    calling `SetForegroundWindow` on an unrelated window is not guaranteed to succeed,
    even with the `AttachThreadInput` sequence below. This is verified on real
    hardware at the human checkpoint in plan 40-15. If it is observed to fail there,
    `FlashWindow` (flashing the taskbar icon) is the documented, strictly weaker but
    more reliable fallback — deliberately not implemented here yet, since it should
    not be added speculatively ahead of that empirical result.
    """
    if _kernel32 is None or _user32 is None:
        return False

    try:
        hwnd = _user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            return False

        # AttachThreadInput sequence (the documented workaround for the foreground-lock
        # restriction described above): attach this thread's input queue to the CURRENT
        # foreground window's thread, so Windows treats the SetForegroundWindow call
        # below as if it came from the already-focused thread, then detach again.
        foreground_hwnd = _user32.GetForegroundWindow()
        current_thread_id = _kernel32.GetCurrentThreadId()
        target_thread_id = (
            _user32.GetWindowThreadProcessId(foreground_hwnd, None) if foreground_hwnd else 0
        )

        attached = False
        if target_thread_id and target_thread_id != current_thread_id:
            attached = bool(_user32.AttachThreadInput(current_thread_id, target_thread_id, True))

        try:
            _user32.ShowWindow(hwnd, _SW_RESTORE)
            success = bool(_user32.SetForegroundWindow(hwnd))
        finally:
            if attached:
                _user32.AttachThreadInput(current_thread_id, target_thread_id, False)

        return success
    except Exception:
        # Every Windows API failure along this best-effort path degrades to False — see
        # function docstring. This function must never raise.
        return False
