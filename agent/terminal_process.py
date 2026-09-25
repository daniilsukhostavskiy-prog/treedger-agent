"""
agent/terminal_process.py — "is MetaTrader 5 already running, and is it elevated?",
plus the one function that starts it when it is not.

THIS MODULE NEVER ENDS, RESTARTS OR CLOSES ANY PROCESS
--------------------------------------------------------------------------
Not the terminal it started itself, and not any other. It can list running
`terminal64.exe` processes (read-only snapshot), read their executable path and
elevation (read-only token query), and start the discovered terminal ONCE when none is
running. That is the whole surface. The terminal belongs to the person using this
computer — they may be trading in it — so this program never takes it away from them,
and closing this program's window only disconnects its own IPC session
(`agent/mt5_bridge.shutdown_terminal()`), never the terminal itself. The folder's
structural audit (`agent/tests/test_sync.py`) fails the build if a process-ending call
ever appears anywhere in this package.

HOW THE TERMINAL IS STARTED
--------------------------------------------------------------------------
`subprocess.Popen` with an ARGUMENT LIST containing only the executable path — no
shell, no extra arguments, never the "runas" verb, so it runs with this program's own
(normal) rights. The path comes only from `agent/terminal_discovery.find_terminal_path()`
(the Windows Uninstall registry, then the conventional install path) — never from a
config file, an environment variable or the command line. The window is requested
minimized and without focus (`STARTUPINFO.wShowWindow = SW_SHOWMINNOACTIVE`). That
request is BEST-EFFORT: MetaTrader 5 restores its own saved window placement on start,
and some builds ignore the show-window hint entirely, so the terminal may still appear
normally on some machines. Nothing here fights that.

Windows-only by nature; every function degrades (returns None / an empty answer) off
Windows so the unit tests import and run anywhere. The `ctypes.WinDLL`
try/except-AttributeError pattern is copied from `agent/single_instance.py`.
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
from ctypes import wintypes
from typing import NamedTuple, Optional

from agent import terminal_discovery

logger = logging.getLogger(__name__)

# Module-level indirection so tests can replace the process launcher. The autouse
# guard in agent/tests/conftest.py swaps this for a callable that FAILS the test, so no
# test can ever start a real MetaTrader terminal by accident.
_popen = subprocess.Popen

try:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
except AttributeError:
    # Non-Windows platform: ctypes.WinDLL does not exist at all.
    _kernel32 = None  # type: ignore[assignment]
    _advapi32 = None  # type: ignore[assignment]

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_CLASS = 20  # TOKEN_INFORMATION_CLASS.TokenElevation
ERROR_ACCESS_DENIED = 5
STARTF_USESHOWWINDOW = 0x00000001
SW_SHOWMINNOACTIVE = 7
_MAX_PATH_CHARS = 32768
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


if _kernel32 is not None:
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.GetCurrentProcess.argtypes = []

if _advapi32 is not None:
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]


class RunningTerminal(NamedTuple):
    """One running `terminal64.exe`. `exe_path`/`elevated` are `None` when Windows
    would not tell us; `elevation_source` says how `elevated` was decided:
    "token" (read directly), "access_denied_inference" (our non-elevated process was
    refused its token — which is what an elevated target looks like), or "unknown"."""

    pid: int
    exe_path: Optional[str]
    elevated: Optional[bool]
    elevation_source: str


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested directly, platform-independent)
# ---------------------------------------------------------------------------

def _select_terminal_pids(entries: "list[tuple[int, str]]") -> "list[int]":
    """PIDs whose executable basename is terminal64.exe, case-insensitively."""
    wanted = terminal_discovery._TERMINAL_EXE_NAME.lower()
    return [pid for pid, exe_name in entries if os.path.basename(exe_name or "").lower() == wanted]


def _classify_elevation(
    token_elevated: Optional[bool], open_token_error: int, self_elevated: Optional[bool]
) -> "tuple[Optional[bool], str]":
    if token_elevated is True:
        return True, "token"
    if token_elevated is False:
        return False, "token"
    if open_token_error == ERROR_ACCESS_DENIED and self_elevated is False:
        return True, "access_denied_inference"
    return None, "unknown"


# ---------------------------------------------------------------------------
# Windows queries
# ---------------------------------------------------------------------------

def _token_elevated(process_handle: int) -> "tuple[Optional[bool], int]":
    """(elevated, last_error) for a process handle. `elevated` is None when the
    token could not be opened or queried; `last_error` is the Win32 error then."""
    if _advapi32 is None:
        return None, 0
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(process_handle, TOKEN_QUERY, ctypes.byref(token)):
        return None, ctypes.get_last_error()
    try:
        elevation = wintypes.DWORD(0)
        returned = wintypes.DWORD(0)
        ok = _advapi32.GetTokenInformation(
            token, TOKEN_ELEVATION_CLASS, ctypes.byref(elevation),
            ctypes.sizeof(elevation), ctypes.byref(returned),
        )
        if not ok:
            return None, ctypes.get_last_error()
        return bool(elevation.value), 0
    finally:
        _kernel32.CloseHandle(token)


def current_process_elevated() -> Optional[bool]:
    """Whether THIS process runs elevated (as administrator). None off Windows or on error."""
    if _kernel32 is None or _advapi32 is None:
        return None
    try:
        # GetCurrentProcess() is a pseudo-handle; it is never closed.
        elevated, _err = _token_elevated(_kernel32.GetCurrentProcess())
        return elevated
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        return None


def _snapshot_entries() -> "Optional[list[tuple[int, str]]]":
    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        return None
    try:
        entries: "list[tuple[int, str]]" = []
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            entries.append((int(entry.th32ProcessID), entry.szExeFile))
            ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return entries
    finally:
        _kernel32.CloseHandle(snapshot)


def _describe_pid(pid: int, self_elevated: Optional[bool]) -> RunningTerminal:
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        elevated, source = _classify_elevation(None, err, self_elevated)
        return RunningTerminal(pid=pid, exe_path=None, elevated=elevated, elevation_source=source)
    try:
        exe_path: Optional[str] = None
        buf = ctypes.create_unicode_buffer(_MAX_PATH_CHARS)
        size = wintypes.DWORD(_MAX_PATH_CHARS)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            exe_path = buf.value
        token_elevated, err = _token_elevated(handle)
        elevated, source = _classify_elevation(token_elevated, err, self_elevated)
        return RunningTerminal(pid=pid, exe_path=exe_path, elevated=elevated, elevation_source=source)
    finally:
        _kernel32.CloseHandle(handle)


def list_terminal_processes() -> "Optional[list[RunningTerminal]]":
    """
    Every running terminal64.exe. `[]` means "verifiably none running"; `None` means
    "could not tell" (not Windows, or the process snapshot itself failed) — callers
    must treat `None` as "do not launch anything", never as "none running".
    Never raises.
    """
    if _kernel32 is None:
        return None
    try:
        entries = _snapshot_entries()
        if entries is None:
            return None
        self_elevated = current_process_elevated()
        return [_describe_pid(pid, self_elevated) for pid in _select_terminal_pids(entries)]
    except Exception:  # noqa: BLE001 — enumeration failure is "unknown", never a crash
        logger.exception("list_terminal_processes failed")
        return None


def launch_terminal_minimized(path: str) -> int:
    """
    Start the terminal at `path` minimized and without focus. Returns the new PID and
    drops the Popen object — this program never waits on, signals or ends the process
    it started. Raises OSError when Windows refuses to start it.
    """
    kwargs: "dict[str, object]" = {"cwd": os.path.dirname(path), "close_fds": True}
    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_cls is not None:
        si = startupinfo_cls()
        si.dwFlags |= STARTF_USESHOWWINDOW
        si.wShowWindow = SW_SHOWMINNOACTIVE
        kwargs["startupinfo"] = si
    proc = _popen([path], **kwargs)
    pid = int(proc.pid)
    logger.info("started MetaTrader 5 minimized: pid=%d path=%s", pid, path)
    return pid
