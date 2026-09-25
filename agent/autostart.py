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

Every call goes through `_run_schtasks()`, which also passes
`creationflags=CREATE_NO_WINDOW`: schtasks.exe is a console program, and the released
windowed (console-less) build would otherwise flash a black console window on every
start (the path self-check) and every checkbox click.

WHY THE QUERY READS XML, NOT THE LIST FORMAT
--------------------------------------------------------------------------
`schtasks /Query /FO LIST /V` labels its lines in the Windows display language — the
"Task To Run:" line this module used to look for is translated on a Russian or
Ukrainian Windows, so the old parse silently answered "no task" there and the path
repair never ran. `schtasks /Query /TN TreedgerAgent /XML` emits the task definition
itself, whose element names (`Exec`, `Command`, `Arguments`) are never translated.
Verified on a real machine: despite the declaration `encoding="UTF-16"`, the text
schtasks writes into a pipe is SINGLE-BYTE in the console (OEM) code page (437 on
English Windows, 866 on Russian) with `\\r\\r\\n` line endings — so the bytes are
decoded with the OEM code page and the declaration is stripped before parsing (see
`_decode_schtasks_output` / `_parse_task_xml`). A missing task is recognised by the
return code alone, never by its localized error text.

Known limitation (logged, harmless): a path character that the OEM code page cannot
represent comes back from schtasks as "?", so the registered command never compares
equal to this program's own path — the checkbox then reads OFF and a silent `/Change`
repair is attempted on start.

THE CHECKBOX SHOWS ONLY A VERIFIED STATE
--------------------------------------------------------------------------
`task_is_registered_for_this_exe()` is true only when the task exists AND points at
THIS executable. A task left over from somewhere else (another copy, another folder)
does not make the checkbox read as ticked; the start-up self-check repairs a stale path
first, and only then is the checkbox's state read.
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from typing import Optional, Union

logger = logging.getLogger(__name__)

TASK_NAME = "TreedgerAgent"
"""The one scheduled task this whole program ever creates, queries, changes, or deletes."""

MINIMIZED_FLAG = "--minimized"
"""The single command-line flag `agent/main.py` recognises when
launched from Task Scheduler — it affects ONLY the window's initial state (minimised vs.
normal) and nothing else. Defined here, next to `TASK_NAME`, since this module is the one
place that writes it into the scheduled task's command line; `agent/main.py` imports it
from here rather than repeating the literal string."""

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
"""Passed as `creationflags` to EVERY schtasks call: schtasks.exe is a console program,
and a windowed (console-less) parent would otherwise flash a black console window for
each call. 0x08000000 on Windows; 0 elsewhere (the attribute does not exist there)."""

_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)


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


# ---------------------------------------------------------------------------
# Running schtasks, and reading its output
# ---------------------------------------------------------------------------


def _oem_codepage() -> Optional[int]:
    """The console (OEM) code page schtasks writes in — 437 on English Windows, 866 on
    Russian — via kernel32.GetOEMCP. None off Windows or on any error."""
    try:
        return int(ctypes.WinDLL("kernel32").GetOEMCP())
    except (AttributeError, OSError):
        return None


def _decode_schtasks_output(raw: Union[bytes, str, None]) -> str:
    """
    Decode schtasks output bytes. Order: (1) a UTF-16 BOM or any NUL byte means
    UTF-16 (little-endian unless a BOM says otherwise); (2) the OEM code page schtasks
    actually uses for piped output; (3) UTF-8 with replacement. A `str` passes through
    unchanged, `None` becomes "". Never raises.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if b"\x00" in raw:
        return raw.decode("utf-16-le", errors="replace")
    codepage = _oem_codepage()
    if codepage:
        try:
            return raw.decode(f"cp{codepage}")
        except (LookupError, UnicodeDecodeError):
            pass
    return raw.decode("utf-8", errors="replace")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_task_xml(xml_text: str) -> "Optional[tuple[str, str]]":
    """
    `(command, arguments)` from a Task Scheduler task XML — the first
    `Actions/Exec/Command` and its sibling `Arguments` ("" when absent), matched by
    LOCAL element name so the task namespace does not matter. Surrounding quotes are
    stripped from the command. The XML declaration is removed first: schtasks declares
    `encoding="UTF-16"` even though the text it pipes is single-byte OEM, and
    ElementTree refuses a str that carries an encoding declaration. None when the XML
    does not parse or has no Exec/Command. (stdlib ElementTree resolves no external
    entities; the input is local OS tool output, not network data.)
    """
    body = _XML_DECLARATION.sub("", xml_text.replace("\r", ""), count=1).strip()
    if not body:
        return None
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None
    for element in root.iter():
        if _local_name(element.tag) != "Exec":
            continue
        command: Optional[str] = None
        arguments = ""
        for child in element:
            name = _local_name(child.tag)
            if name == "Command":
                command = (child.text or "").strip()
            elif name == "Arguments":
                arguments = (child.text or "").strip()
        if command:
            if len(command) >= 2 and command[0] == '"' and command[-1] == '"':
                command = command[1:-1]
            return command, arguments
    return None


def _run_schtasks(args: "list[str]") -> "subprocess.CompletedProcess[bytes]":
    """
    The ONE place `subprocess.run` is called: an argument list (never a shell string,
    never `shell=`), captured bytes output, `check=False`, and `CREATE_NO_WINDOW`. The
    caller builds the verb list — in particular `/Create` exists only inside
    `create_task()` (see the module docstring's structural split).
    """
    return subprocess.run(
        ["schtasks", *args],
        capture_output=True,
        check=False,
        creationflags=_CREATE_NO_WINDOW,
    )


def _log_result(verb: str, result: "subprocess.CompletedProcess[bytes]") -> None:
    stderr = _decode_schtasks_output(getattr(result, "stderr", b"")).strip()
    if result.returncode == 0:
        logger.info("schtasks %s: ok", verb)
    else:
        logger.warning("schtasks %s: returncode=%s stderr=%s", verb, result.returncode, stderr)


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def query_task_command() -> Optional[str]:
    """
    Returns the command Task Scheduler currently has registered for `TASK_NAME` as
    `'"<command>" <arguments>'` (arguments omitted when empty), or `None` when there is
    no such task. Built from `schtasks /Query /TN TreedgerAgent /XML`, which is
    locale-independent (the old LIST format's "Task To Run:" label is translated on a
    Russian/Ukrainian Windows, so the old parse silently reported "no task" there).
    Decided on the returncode only, never on (localized) text. Never raises.
    """
    try:
        result = _run_schtasks(["/Query", "/TN", TASK_NAME, "/XML"])
    except OSError as exc:
        logger.warning("schtasks /Query failed to run: %s", exc)
        return None
    if result.returncode != 0:
        return None
    parsed = _parse_task_xml(_decode_schtasks_output(result.stdout))
    if parsed is None:
        logger.warning("schtasks /Query returned XML without an Exec/Command")
        return None
    command, arguments = parsed
    return f'"{command}" {arguments}' if arguments else f'"{command}"'


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


def _same_command(a: str, b: str) -> bool:
    """Windows paths are case-insensitive; compare the registered command that way."""
    return a.strip().casefold() == b.strip().casefold()


def task_is_registered_for_this_exe() -> bool:
    """
    True ONLY when a `TASK_NAME` task exists AND its command is exactly this process's
    own `_build_command_line(own_executable_path())` (case-insensitively). This — not
    `task_exists()` — is what the autostart checkbox shows: a task pointing at some
    other file does not autostart THIS program, so the box must not read as ticked.
    """
    current = query_task_command()
    if current is None:
        return False
    return _same_command(current, _build_command_line(own_executable_path()))


# ---------------------------------------------------------------------------
# Repair / create / remove
# ---------------------------------------------------------------------------


def repair_task_path() -> str:
    """
    The self-check `agent/main.py` runs on EVERY start. Compares the
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
    if _same_command(current_command, desired_command):
        return "scheduled task path already correct; no change issued"

    try:
        result = _run_schtasks(["/Change", "/TN", TASK_NAME, "/TR", desired_command])
    except OSError as exc:
        logger.warning("schtasks /Change failed to run: %s", exc)
        return "scheduled task path repair failed to run"
    _log_result("/Change", result)
    return "scheduled task path repaired"


def create_task() -> None:
    """
    Creates the `ONLOGON` scheduled task for the CURRENT user, pointed at this process's
    own real path. This is the ONLY function in the entire `agent/` package that issues
    the scheduler's `/Create` verb (see module docstring) — reachable only from the
    explicit autostart opt-in checkbox handler in `agent/main.py`, never from the
    startup self-check.

    Omits `/RU` (defaults to the calling user) and never requests `/RL HIGHEST` — see
    the module docstring's "NO ADMIN RIGHTS, EVER" section.
    """
    command_line = _build_command_line(own_executable_path())
    try:
        result = _run_schtasks(
            ["/Create", "/TN", TASK_NAME, "/TR", command_line, "/SC", "ONLOGON"]
        )
    except OSError as exc:
        logger.warning("schtasks create failed to run: %s", exc)
        return
    _log_result("create", result)


def remove_task() -> None:
    """
    Deletes `TASK_NAME` if it exists. `/F` suppresses `schtasks /Delete`'s normal
    interactive confirmation prompt — this call always runs non-interactively (from the
    autostart checkbox handler in `agent/main.py`), and there is no console attached to
    answer a prompt on.
    """
    try:
        result = _run_schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    except OSError as exc:
        logger.warning("schtasks /Delete failed to run: %s", exc)
        return
    _log_result("/Delete", result)
