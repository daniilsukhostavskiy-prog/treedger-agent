"""
agent/autostart.py — «Запускать вместе с Windows» as ONE per-user value under
HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run, and the self-check that keeps it
pointed at a file that exists (quick 260925-qhs).

WHY A RUN VALUE, NOT A TASK SCHEDULER ENTRY
--------------------------------------------------------------------------
Until quick 260925-qhs this module registered a Task Scheduler task with
`schtasks /Create ... /SC ONLOGON` and no user. A logon trigger without a user means
"at logon of ANY user", which needs administrator rights — every standard user got
"ERROR: Access is denied", so the checkbox never worked. Even a successful
schtasks-created task would have broken the unattended chain silently: its default
execution time limit (72 h) ends the program after three days, and its default
"start only on AC power" condition starts nothing on a laptop running on battery.

A value under the per-user Run key has none of those problems: it needs no
administrator rights (HKCU), it is per-user by construction, it has no time limit and
no power condition, it needs no console process (no window flash, no OEM-code-page
output to parse), the installer can write and remove the very same value
declaratively, and the user can see it in Task Manager → «Автозагрузка приложений».
Trust implication, stated honestly: any process running as the same user can rewrite
an HKCU Run value — exactly as it could already create its own Run value or its own
per-user task. The asset worth protecting is the executable itself, which the
installer now puts in admin-only Program Files.

THE STRUCTURAL SPLIT
--------------------------------------------------------------------------
- `_write_own_command()` is the ONE registry write in this whole program (the only
  `SetValueEx` call, tree-wide — asserted by `ast` in agent/tests/test_sync.py).
- `enable_autostart()` is the only function allowed to CREATE the value, and it is
  called from exactly one place: the checkbox handler in agent/main.py (also
  `ast`-asserted). A program that writes itself into autostart from more than one
  code path behaves like malware; this makes that impossible by construction.
- `repair_autostart_path()` — run on EVERY start — never creates a value. It rewrites
  an EXISTING value only when the executable that value names no longer exists on
  disk (the folder moved; Windows would launch nothing, silently). It never re-points
  a value that names ANOTHER existing copy: a portable copy started once after an
  install must not move autostart out of Program Files into a user-writable folder —
  the very re-execution point the installer exists to remove (Phase 40 D-22 as
  narrowed by quick 260925-qhs).

ONLY THE PROCESS'S OWN ACTUAL PATH IS EVER WRITTEN
--------------------------------------------------------------------------
No function here takes a path parameter. The value written always comes from
`own_executable_path()` — the running process itself — never from config.json, never
from the command line (beyond the process's own `argv[0]`), never from a server.

CORRECTION IS SILENT
--------------------------------------------------------------------------
`repair_autostart_path()` returns a short status string for the LOG only — never a
dialog: the person made no mistake, so there is nothing to tell them.

NO ADMIN RIGHTS, EVER
--------------------------------------------------------------------------
Only HKEY_CURRENT_USER is ever opened. Every public function catches `OSError`, logs
it and returns — nothing here ever raises into the window.

THE CHECKBOX SHOWS ONLY A VERIFIED STATE
--------------------------------------------------------------------------
`autostart_enabled_for_this_exe()` is true only when the Run value equals THIS
executable's own command AND the user has not switched it off in Task Manager's
«Автозагрузка приложений» (that switch lives in
...\\Explorer\\StartupApproved\\Run as a binary value whose first byte is odd when
disabled). A value left by another copy does not tick the box.

SHARED WITH THE INSTALLER
--------------------------------------------------------------------------
`packaging/installer/treedger.iss` writes the same value name (`RUN_VALUE_NAME`) with
the same command format (`_build_command_line`) for its own unchecked «Запускать
вместе с Windows» task, and its uninstaller deletes it — kept in step by
agent/tests/test_installer_script.py.

LEGACY TASK
--------------------------------------------------------------------------
This module no longer touches Task Scheduler at all and starts no process. The
installer deletes a leftover `TreedgerAgent` scheduled task from the old mechanism.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

try:
    import winreg as _winreg_module
except ImportError:  # not Windows — every function below degrades to a logged no-op
    _winreg_module = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# The registry handle every function goes through — a module global so tests can swap
# it for an in-memory fake (agent/tests/conftest.py forbids the real one in tests).
_winreg: Any = _winreg_module

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
"""The per-user Run key. It always exists on Windows, so it is opened, never created."""

STARTUP_APPROVED_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
"""Where Task Manager's «Автозагрузка приложений» on/off switch is stored."""

RUN_VALUE_NAME = "TreedgerAgent"
"""The one Run value this program (and its installer) ever writes, reads or deletes."""

MINIMIZED_FLAG = "--minimized"
"""The single command-line flag `agent/main.py` recognises — it affects ONLY the
window's initial state (minimised vs. normal). Defined here because this module is the
one place that writes it into the autostart command; agent/main.py imports it."""


def own_executable_path() -> str:
    """
    The absolute, real path of the CURRENTLY RUNNING process's own executable — the
    Nuitka `--standalone` build's own `.exe` path when frozen, or the real entry-point
    file's path when running from source (`python -m agent.main`).

    This is the ONLY path `_write_own_command()` ever writes — see the module
    docstring for why no function here accepts a path parameter.
    """
    if getattr(sys, "frozen", False):
        # Nuitka's --standalone build sets `sys.frozen = True`; when frozen,
        # `sys.executable` IS the built `.exe`'s own path.
        return os.path.realpath(sys.executable)
    # Running from source: `sys.argv[0]` is the entry-point file that was launched —
    # still derived purely from the running process, never from external input.
    return os.path.realpath(sys.argv[0])


def _build_command_line(exe_path: str) -> str:
    """
    The Run value's data: the executable path in double quotes (it may contain spaces,
    e.g. C:\\Program Files\\Treedger\\Treedger.exe) followed by `--minimized` as its own
    argument. The installer builds the identical string (parity-tested).
    """
    return f'"{exe_path}" {MINIMIZED_FLAG}'


def _same_command(a: str, b: str) -> bool:
    """Windows paths are case-insensitive; compare the registered command that way."""
    return a.strip().casefold() == b.strip().casefold()


def _registered_exe(command: str) -> Optional[str]:
    """The text inside the first pair of double quotes of a Run command, or None when
    the command has no complete quoted part (an unquoted or unparsable value)."""
    start = command.find('"')
    if start < 0:
        return None
    end = command.find('"', start + 1)
    if end < 0:
        return None
    exe = command[start + 1:end]
    return exe or None


def _read_value(key_path: str, name: str) -> Any:
    """The data of HKCU\\<key_path>\\<name>, or None when the key or value is absent,
    winreg is unavailable, or the read fails. Never raises."""
    if _winreg is None:
        return None
    try:
        with _winreg.OpenKeyEx(_winreg.HKEY_CURRENT_USER, key_path, 0, _winreg.KEY_READ) as key:
            value, _value_type = _winreg.QueryValueEx(key, name)
            return value
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("autostart: reading %s\\%s failed: %s", key_path, name, exc)
        return None


def _delete_value(key_path: str, name: str) -> None:
    """Delete HKCU\\<key_path>\\<name>; an absent key or value is not an error. Never
    raises."""
    if _winreg is None:
        return
    try:
        with _winreg.OpenKeyEx(_winreg.HKEY_CURRENT_USER, key_path, 0, _winreg.KEY_SET_VALUE) as key:
            _winreg.DeleteValue(key, name)
            logger.info("autostart: deleted %s\\%s", key_path, name)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("autostart: deleting %s\\%s failed: %s", key_path, name, exc)


def _write_own_command() -> bool:
    """
    THE one registry write in this program: `RUN_VALUE_NAME` = this process's own
    command, REG_SZ, under the per-user Run key (opened with KEY_SET_VALUE — the key
    always exists, so it is never created). Takes no parameters: only
    `own_executable_path()` is ever written. Returns whether the write succeeded.
    """
    if _winreg is None:
        logger.warning("autostart: winreg unavailable; nothing written")
        return False
    command = _build_command_line(own_executable_path())
    try:
        with _winreg.OpenKeyEx(_winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, _winreg.KEY_SET_VALUE) as key:
            _winreg.SetValueEx(key, RUN_VALUE_NAME, 0, _winreg.REG_SZ, command)
    except OSError as exc:
        logger.warning("autostart: writing the Run value failed: %s", exc)
        return False
    logger.info("autostart: Run value set to %s", command)
    return True


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def registered_command() -> Optional[str]:
    """The command currently stored in the `TreedgerAgent` Run value, or None when
    there is none (or it is not a string). Never raises."""
    value = _read_value(RUN_KEY_PATH, RUN_VALUE_NAME)
    return value if isinstance(value, str) else None


def disabled_in_startup_apps() -> bool:
    """True when the user switched this entry OFF in Task Manager → «Автозагрузка
    приложений»: the StartupApproved value is present, binary, and its first byte is
    odd. Absent, non-binary or empty → False. Never raises."""
    value = _read_value(STARTUP_APPROVED_KEY_PATH, RUN_VALUE_NAME)
    if not isinstance(value, (bytes, bytearray)) or len(value) == 0:
        return False
    return bool(value[0] & 1)


def autostart_enabled_for_this_exe() -> bool:
    """
    What the checkbox shows: True ONLY when the Run value equals this process's own
    command (case-insensitively) AND Task Manager has not disabled it. A value naming
    some other copy, a missing value, or an unavailable registry → False.
    """
    current = registered_command()
    if current is None:
        return False
    if not _same_command(current, _build_command_line(own_executable_path())):
        return False
    return not disabled_in_startup_apps()


# ---------------------------------------------------------------------------
# Repair / enable / disable
# ---------------------------------------------------------------------------


def repair_autostart_path() -> str:
    """
    The silent self-check agent/main.py runs on EVERY start. Never creates a value.
    Rewrites an existing value (via `_write_own_command()`) ONLY when the executable
    it names no longer exists on disk; an already-correct value, a value naming
    another existing copy, and an unparsable value are all left untouched. Returns a
    status string for the log only.
    """
    current = registered_command()
    if current is None:
        return "no autostart value; nothing to repair"
    if _same_command(current, _build_command_line(own_executable_path())):
        return "autostart value already points at this program; no change"
    exe = _registered_exe(current)
    if exe is None:
        return "autostart value is not a quoted path; left untouched"
    try:
        exists = os.path.exists(exe)
    except (OSError, ValueError):
        exists = True  # cannot tell — never rewrite on doubt
    if exists:
        return "autostart value names another existing copy; left untouched"
    if _write_own_command():
        return "autostart value named a missing file; repaired to this program"
    return "autostart value named a missing file; repair failed"


def enable_autostart() -> None:
    """
    The ONLY function allowed to create the autostart value — called solely by the
    checkbox handler in agent/main.py. Writes this program's own command (the one
    registry write) and removes a stale Task Manager "disabled" marker, since the
    person just asked for autostart explicitly. Never raises.
    """
    if _write_own_command():
        _delete_value(STARTUP_APPROVED_KEY_PATH, RUN_VALUE_NAME)


def disable_autostart() -> None:
    """Deletes the Run value and its Task Manager marker; either may be absent.
    Never raises."""
    _delete_value(RUN_KEY_PATH, RUN_VALUE_NAME)
    _delete_value(STARTUP_APPROVED_KEY_PATH, RUN_VALUE_NAME)
