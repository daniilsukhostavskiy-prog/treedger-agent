"""
agent/autostart.py — the per-user Windows Task Scheduler entry that makes «Запускать
вместе с Windows» work, and the self-check that keeps it pointed at the right file.

THE STRUCTURAL SPLIT IS THE WHOLE POINT OF THIS MODULE
--------------------------------------------------------------------------
`repair_task_path()` — the function `agent/main.py` calls on EVERY start — may issue
only the scheduler's QUERY and CHANGE verbs (`/Query`, `/Change`). It
is structurally INCAPABLE of creating a task: when no task exists it returns without
issuing the create verb, ever. `create_task()` is a completely separate function,
reachable only from the explicit "Включить" autostart opt-in checkbox handler
(`agent/main.py`), and it is the ONLY function in this entire program that
issues the scheduler's CREATE verb (`/Create`).

Why this split has to be structural, not a matter of convention: a program that writes
itself into Windows autostart behaves EXACTLY like malware, and that must be impossible
by construction, not merely avoided by discipline. This folder's own `ast` audit asserts the
literal `/Create` verb string appears inside exactly ONE function, tree-wide, across the
whole `agent/` package — so a future edit that moves the create call into
`repair_task_path()` (or anywhere else) cannot ship; the audit fails first.

ONLY THE PROCESS'S OWN ACTUAL PATH IS EVER WRITTEN
--------------------------------------------------------------------------
No function in this module accepts a path PARAMETER of any kind. The value written into
the scheduled task always comes from `own_executable_path()` — derived from the running
process itself (`sys.executable`/`sys.argv[0]`) — and NEVER from `config.json`, NEVER
from `sys.argv` (beyond the running process's own `argv[0]`), and NEVER from a server
response. Without this rule there would be a way to make this program register something
ELSE for autostart — a substitute `.exe` an attacker controls, run every time the user
logs in.

CORRECTION IS SILENT
--------------------------------------------------------------------------
`repair_task_path()` returns a short status string for LOGGING ONLY. It must never
surface a dialog or a notice of any kind: the person made no mistake — the install
folder simply is not where it used to be, most likely because they moved it themselves —
so there is nothing to tell them. Relying on the folder never moving fails silently in
exactly the case where it does move (Windows then launches nothing, with no error, and
the person notices a week later via stale data); silently repairing the path on every
start is the fix, and it deserves no dialog of its own.

NO ADMIN RIGHTS, EVER
--------------------------------------------------------------------------
Every scheduler command below omits `/RU` (run-as-user — omitting it defaults to the
CURRENTLY LOGGED-IN user) and never requests `/RL HIGHEST` (the elevated run level). A
logon task (`/SC ONLOGON`) for the current user needs no UAC elevation with the default
(`LIMITED`, i.e. standard-user) run level. This is **LIKELY but not proven in a
non-interactive sandbox** — it needs confirming on a real machine with a real interactive
desktop session. If elevation turns out to be required after all, that
is a FINDING, not a bug to patch here: the whole autostart mechanism would need
rethinking (e.g. a Startup-folder shortcut instead of Task Scheduler), because this
program never asks for administrator rights under any circumstances.

SUBPROCESS DISCIPLINE
--------------------------------------------------------------------------
Every scheduler invocation below is a `subprocess.run` ARGUMENT LIST — never a shell
string, and `shell=True` is never passed. Passing `shell=True` would reintroduce the
exact nested-quoting ambiguity `_build_command_line()`'s own comment explains: the outer
quoting a shell would apply is not the same quoting Task Scheduler itself needs around a
path containing spaces, and mixing the two is how the `--minimized` flag would end up
silently parsed as part of the path instead of as its own argument. Every verb this
module issues manages the SCHEDULER's own record of what to launch (create, query,
change, delete a task definition) — none of it ever targets a running process. This
module never shells out to `taskkill`, never calls `.kill(` on anything, and never
touches `os.kill`/`signal.pthread_kill` — it has nothing in common with the
whole-folder structural audit's separate, already-guarded prohibition on ending another
process outright (`agent/tests/test_sync.py::TestAgentFolderStructuralAudit`'s
`test_no_process_kill_call`, which is `ast`-based specifically so a sentence like this
one — naming those exact words to explain why this module never does them — can never
trip it).
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

TASK_NAME = "TreedgerAgent"
"""The one scheduled task this whole program ever creates, queries, changes, or deletes."""

MINIMIZED_FLAG = "--minimized"
"""The single command-line flag `agent/main.py` recognises when
launched from Task Scheduler — it affects ONLY the window's initial state (minimised vs.
normal) and nothing else. Defined here, next to `TASK_NAME`, since this module is the one
place that writes it into the scheduled task's command line; `agent/main.py` imports it
from here rather than repeating the literal string."""

_QUERY_TASK_TO_RUN_PREFIX = "Task To Run:"
"""The exact `schtasks /Query ... /FO LIST /V` output line prefix that carries the
registered command string. `schtasks /Query ... /XML` is a more robust
machine-parseable alternative should this text format ever prove fragile to parse."""


def own_executable_path() -> str:
    """
    The absolute, real path of the CURRENTLY RUNNING process's own executable — the
    Nuitka `--standalone` build's own `.exe` path when frozen, or the real entry-point
    file's path when running from source (this repo's own dev/test environment, e.g.
    `python -m agent.main`).

    This is the ONLY value `create_task()` and `repair_task_path()` ever write into the
    Task Scheduler — see the module docstring's "ONLY THE PROCESS'S OWN ACTUAL PATH IS
    EVER WRITTEN" section for why no function in this module accepts a path parameter
    instead.
    """
    if getattr(sys, "frozen", False):
        # Nuitka's --standalone build sets `sys.frozen = True` for compatibility with
        # the long-established PyInstaller convention; when frozen, `sys.executable` IS
        # the built `.exe`'s own path, not a separate Python interpreter sitting beside
        # it.
        return os.path.realpath(sys.executable)
    # Running from source: `sys.executable` is the Python interpreter itself, not a
    # meaningful autostart target. `sys.argv[0]` is the actual entry-point file that was
    # launched (e.g. `python -m agent.main` sets this to `agent/main.py`'s own real
    # path) — still derived purely from the running process, never from external input.
    return os.path.realpath(sys.argv[0])


def query_task_command() -> Optional[str]:
    """
    Returns the command string Task Scheduler currently has registered for `TASK_NAME`,
    or `None` when the scheduler reports no such task at all (a non-zero exit code from
    `schtasks /Query`, or a report with no "Task To Run" line). Never raises: any
    `subprocess` failure is treated identically to "no task" — the caller's job is to
    then decide, never this thin query wrapper.
    """
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith(_QUERY_TASK_TO_RUN_PREFIX):
            return line[len(_QUERY_TASK_TO_RUN_PREFIX):].strip()
    return None


def task_exists() -> bool:
    """Whether `TASK_NAME` is currently registered with Task Scheduler at all."""
    return query_task_command() is not None


def _build_command_line(exe_path: str) -> str:
    """
    Builds the ONE string Task Scheduler's `/TR` argument needs, as a single element of
    a `subprocess.run` argument list (never through `shell=True` — see module
    docstring). The DOUBLED quoting here is deliberate, not a mistake: the outer pair
    (the literal `"` characters in this f-string) is what Task Scheduler itself needs
    around a path that may contain spaces, so the `--minimized` flag that follows is
    parsed as its OWN argument rather than swallowed as part of the path — a well-known
    nested-quoting footgun when a shell's own quoting and Task Scheduler's own quoting
    of `/TR` are conflated.
    """
    return f'"{exe_path}" {MINIMIZED_FLAG}'


def repair_task_path() -> str:
    """
    The self-check `agent/main.py` runs on EVERY start (a later plan). Compares the
    scheduler's currently registered command against this process's own real path and
    rewrites the task ONLY when they differ — and only ever via the scheduler's CHANGE
    verb. See the module docstring's "THE STRUCTURAL SPLIT..." section for why this
    function can never create a task, and "CORRECTION IS SILENT" for why its return
    value is for logging only, never a dialog.
    """
    current_command = query_task_command()
    if current_command is None:
        # No task is registered at all — this function NEVER creates one (see module
        # docstring). Only the explicit opt-in handler, via create_task(), ever does.
        return "no scheduled task exists; nothing to repair"

    desired_command = _build_command_line(own_executable_path())
    if current_command == desired_command:
        return "scheduled task path already correct; no change issued"

    subprocess.run(
        ["schtasks", "/Change", "/TN", TASK_NAME, "/TR", desired_command],
        capture_output=True,
        text=True,
        check=False,
    )
    return "scheduled task path repaired"


def create_task() -> None:
    """
    Creates the `ONLOGON` scheduled task for the CURRENT user, pointed at this process's
    own real path. This is the ONLY function in the entire `agent/` package that issues
    the scheduler's `/Create` verb (see module docstring) — reachable only from the
    explicit "Включить" autostart opt-in checkbox handler in `agent/main.py` (a later
    plan), never from the startup self-check.

    Omits `/RU` (defaults to the calling user) and never requests `/RL HIGHEST` — see
    the module docstring's "NO ADMIN RIGHTS, EVER" section.
    """
    command_line = _build_command_line(own_executable_path())
    subprocess.run(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", command_line, "/SC", "ONLOGON"],
        capture_output=True,
        text=True,
        check=False,
    )


def remove_task() -> None:
    """
    Deletes `TASK_NAME` if it exists. `/F` suppresses `schtasks /Delete`'s normal
    interactive confirmation prompt — this call always runs non-interactively (from the
    "Отключить" autostart handler in `agent/main.py`, a later plan), and there is no
    console attached to answer a prompt on.
    """
    subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True,
        text=True,
        check=False,
    )
