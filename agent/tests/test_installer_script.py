"""
agent/tests/test_installer_script.py — parity between the Inno Setup script
(packaging/installer/treedger.iss) and the program it installs (quick 260925-qhs).

ISCC is not needed: the .iss is parsed as plain text, section by section. What is
checked is exactly what would silently break if the two sides drifted apart:

  - AppMutex is the program's own single-instance mutex (so Setup/Uninstall refuse to
    run while the program is open);
  - the autostart value the installer writes is byte-for-byte the one
    agent/autostart.py writes and reads (value name, quoted exe path, --minimized) —
    otherwise the in-app checkbox would read OFF after an installer-enabled autostart;
  - the install is per-machine (Program Files, admin) with no per-user override —
    D-13's revocation depends on it;
  - both optional tasks are unchecked by default (autostart is opt-in only);
  - the finish-page launch runs NON-elevated;
  - the uninstaller removes the Run value even when the in-app checkbox created it.

The .iss lives at agent/packaging/installer/treedger.iss in this repository and at
packaging/installer/treedger.iss in the public treedger-agent repository (the agent
package moves whole, packaging/ moves to the root). The test finds either and FAILS —
never skips — when neither exists.
"""
from __future__ import annotations

import pathlib
import re
from typing import Optional

import pytest

from agent import autostart, single_instance

_AGENT_DIR = pathlib.Path(autostart.__file__).resolve().parent
_CANDIDATES = (
    _AGENT_DIR / "packaging" / "installer" / "treedger.iss",
    _AGENT_DIR.parent / "packaging" / "installer" / "treedger.iss",
)
_PROGRAM_FILES_APP = r"C:\Program Files\Treedger"


def _iss_path() -> pathlib.Path:
    for candidate in _CANDIDATES:
        if candidate.is_file():
            return candidate
    pytest.fail(
        "treedger.iss not found at any of: " + ", ".join(str(c) for c in _CANDIDATES)
    )
    raise AssertionError("unreachable")


def _sections() -> "dict[str, list[str]]":
    """Section name (lower-case, no brackets) → its non-empty, non-comment lines."""
    text = _iss_path().read_text(encoding="utf-8-sig")
    sections: "dict[str, list[str]]" = {}
    current: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        header = re.fullmatch(r"\[([A-Za-z]+)\]", line)
        if header:
            current = header.group(1).lower()
            sections.setdefault(current, [])
            continue
        if current is None or not line:
            continue
        if current != "code" and (line.startswith(";") or line.startswith("#")):
            continue
        sections[current].append(line if current != "code" else raw)
    return sections


def _setup_directives() -> "dict[str, str]":
    directives: "dict[str, str]" = {}
    for line in _sections().get("setup", []):
        if "=" in line:
            key, value = line.split("=", 1)
            directives[key.strip().lower()] = value.strip()
    return directives


def _unquote(value: str) -> str:
    """An Inno parameter value: surrounding quotes stripped, "" → "."""
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('""', '"')
    return value


def _entry(line: str) -> "dict[str, str]":
    """Parse `Name: "x"; Flags: a b` into {'name': 'x', 'flags': 'a b'} — `;` inside a
    quoted value is not a separator."""
    parts: "list[str]" = []
    buf = ""
    in_quotes = False
    for ch in line:
        if ch == '"':
            in_quotes = not in_quotes
        if ch == ";" and not in_quotes:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    entry: "dict[str, str]" = {}
    for part in parts:
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        entry[key.strip().lower()] = _unquote(value)
    return entry


def _entries(section: str) -> "list[dict[str, str]]":
    return [_entry(line) for line in _sections().get(section, [])]


def _flags(entry: "dict[str, str]") -> "set[str]":
    return set(entry.get("flags", "").lower().split())


def _code() -> str:
    return "\n".join(_sections().get("code", []))


def test_iss_file_exists_at_a_known_location() -> None:
    assert _iss_path().is_file()


def test_iss_is_utf8_with_bom_so_cyrillic_messages_survive() -> None:
    assert _iss_path().read_bytes().startswith(b"\xef\xbb\xbf")


def test_app_mutex_is_the_programs_single_instance_mutex() -> None:
    expected = single_instance.MUTEX_NAME
    assert expected.startswith("Local\\")
    assert _setup_directives()["appmutex"] == expected[len("Local\\"):]


def test_per_machine_admin_install_into_program_files_without_override() -> None:
    directives = _setup_directives()
    assert directives["privilegesrequired"].lower() == "admin"
    assert "privilegesrequiredoverridesallowed" not in directives
    assert directives["defaultdirname"].lower().startswith("{autopf}")
    assert directives.get("appid")
    assert directives.get("closeapplications", "").lower() == "no"


def test_both_optional_tasks_are_unchecked_by_default() -> None:
    tasks = {entry.get("name"): entry for entry in _entries("tasks")}
    assert set(tasks) >= {"autostart", "desktopicon"}
    for name in ("autostart", "desktopicon"):
        assert "unchecked" in _flags(tasks[name]), f"task {name} must be unchecked by default"


def test_registry_run_value_matches_what_the_program_writes() -> None:
    run_entries = [
        e for e in _entries("registry")
        if e.get("subkey", "").lower() == autostart.RUN_KEY_PATH.lower()
    ]
    assert len(run_entries) == 1
    entry = run_entries[0]
    assert entry.get("root", "").lower() == "hkcu"
    assert entry.get("valuetype", "").lower() == "string"
    assert entry.get("valuename") == autostart.RUN_VALUE_NAME
    assert entry.get("tasks") == "autostart"
    assert "uninsdeletevalue" in _flags(entry)
    installed = entry["valuedata"].replace("{app}", _PROGRAM_FILES_APP)
    assert installed == autostart._build_command_line(_PROGRAM_FILES_APP + r"\Treedger.exe")


def test_finish_page_launch_is_not_elevated() -> None:
    launches = [e for e in _entries("run") if e.get("filename", "").lower() == r"{app}\treedger.exe"]
    assert len(launches) == 1
    assert {"postinstall", "nowait", "skipifsilent", "runasoriginaluser"} <= _flags(launches[0])


def test_uninstall_step_deletes_the_run_value() -> None:
    code = _code()
    match = re.search(
        r"procedure\s+CurUninstallStepChanged\b(.*?)\nend;", code, re.IGNORECASE | re.DOTALL
    )
    assert match, "the [Code] section needs a CurUninstallStepChanged procedure"
    body = match.group(1)
    run_key = re.escape(autostart.RUN_KEY_PATH)
    pattern = (
        r"RegDeleteValue\(\s*HKEY_CURRENT_USER\s*,\s*'" + run_key + r"'\s*,\s*'"
        + re.escape(autostart.RUN_VALUE_NAME) + r"'\s*\)"
    )
    assert re.search(pattern, body), "the uninstaller must delete the Run value itself"
    approved_key = re.escape(autostart.STARTUP_APPROVED_KEY_PATH)
    assert re.search(r"RegDeleteValue\(\s*HKEY_CURRENT_USER\s*,\s*'" + approved_key, body)


def test_the_installer_never_closes_other_programs() -> None:
    code = _code().lower()
    assert "taskkill" not in code
    assert "terminateprocess" not in code
