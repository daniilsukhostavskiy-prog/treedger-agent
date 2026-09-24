"""
agent/terminal_discovery.py — locates the installed MetaTrader 5 terminal executable
on the current Windows machine.

THERE IS DELIBERATELY NO CONFIGURATION OPTION AND NO ENVIRONMENT-VARIABLE OVERRIDE for
this path. Do not add one back "for convenience" if a
future change makes this feel awkward. The donor service
(mt5-service/services/mt5_bridge.py:267-271, `MT5_TERMINAL_PATH_ENV` +
`_DEFAULT_MT5_TERMINAL_PATH`) reads an env var with a hardcoded fallback default — that
is safe there ONLY because that program runs on a company-owned Windows VPS whose
install layout the team itself controls end-to-end. This program runs on an arbitrary
end user's own computer: trusting an environment variable or a config file for
"which .exe do I launch" would let a stale or tampered setting silently point this
program at a different executable than the one the user actually installed. The
Windows registry — specifically the same "Add/Remove Programs" (Uninstall) entries
every installer writes — is the one source of truth this module trusts instead.

Registry technique: no single documented
MetaQuotes registry key was found for terminal discovery, and a broker-rebranded
(white-label) MetaTrader 5 build — e.g. "FTMO MetaTrader 5", "RoboForex MetaTrader 5" —
installs under its OWN product name in its OWN folder, producing its own Uninstall
entry. The only reliable technique is the generic Windows one: enumerate every
Uninstall-registry subkey, read its `DisplayName`, and match case-insensitively against
"MetaTrader 5" as a substring (so both the official build and every rebrand match).

Enumeration deliberately covers SIX hive/view combinations, not the minimum four
(HKLM/HKCU x native/WOW6432Node), by using both of the two independent techniques for
reaching the 32-bit-vs-64-bit-redirected registry view at once, for both hives:
  - the explicit `KEY_WOW64_64KEY` / `KEY_WOW64_32KEY` access flags (the modern
    technique `winreg`/the Win32 API itself recommend for reliably reaching a SPECIFIC
    view regardless of whether this Python interpreter itself is 32- or 64-bit), AND
  - the older, still-common convention of manually splicing a literal "WOW6432Node"
    path segment into the subkey path.
This is deliberate over-coverage, not a bug: since no single documented key exists,
belt-and-suspenders costs nothing (duplicate hits across combinations are harmless —
`find_terminal_path()` only needs the first candidate whose executable exists on disk)
and avoids a false "MT5 not found" on some Windows build where only one of the two
techniques happens to work.

Never raises. A missing registry key, a missing value, a permissions error on one
hive/view, or running on a non-Windows platform at all (winreg unavailable) all just
mean fewer candidates for that combination — the scan continues and, in the worst
case, `find_terminal_path()` returns `None`. The caller decides what "not found" means;
this module never raises `TerminalNotFoundError` itself (see below).
"""
from __future__ import annotations

import os
from typing import Optional, TypedDict

try:
    import winreg  # type: ignore[import-not-found]
except ImportError:
    # Non-Windows platform (this program is Windows-only per README.md, but the unit
    # tests for this module must still run anywhere, including this repo's own CI). A
    # missing winreg degrades every registry lookup below to "zero hits" — never a
    # crash.
    winreg = None  # type: ignore[assignment]


class TerminalNotFoundError(Exception):
    """
    Raised when no usable MetaTrader 5 terminal executable can be located on this
    machine — neither via the registry nor at the conventional fallback path.

    Defined in this module (not in agent/mt5_bridge.py) so agent/main.py's GUI can
    import and catch this one stable symbol without depending on the MT5 bridge module
    at all. `find_terminal_path()` itself never raises this — it only ever returns
    `None`. It is `agent/mt5_bridge.py`'s `initialize_terminal(path)`
    that raises it, for the GUI to catch and show its blocking-but-non-crashing notice:
    «MetaTrader 5 не найден на этом компьютере».
    """


class RegistryCandidate(TypedDict):
    """One Windows Uninstall-registry entry whose DisplayName matched "MetaTrader 5"."""

    hive: str
    view: str
    display_name: str
    install_location: str
    exe_path: Optional[str]
    exists: bool


# The executable name every MetaTrader 5 build (official or white-label) ships as.
_TERMINAL_EXE_NAME = "terminal64.exe"

# Case-insensitive substring every known MetaTrader 5 build's DisplayName carries,
# including broker-rebranded builds.
_DISPLAY_NAME_MARKER = "metatrader 5"

# The two Uninstall-registry subkey path shapes: native, and the older
# manually-spliced WOW6432Node convention (see module docstring).
_UNINSTALL_SUBKEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
_UNINSTALL_SUBKEY_WOW6432 = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"

# ---------------------------------------------------------------------------
# Final, documented fallback ONLY — never the primary discovery mechanism.
#
# This is the same literal path mt5-service/services/mt5_bridge.py hardcodes as its
# own *default* (line 271, `_DEFAULT_MT5_TERMINAL_PATH`) — safe there only because
# that program runs on a company-owned VPS whose install layout the team controls.
# Here it is tried only after every registry candidate above has failed, as a last
# resort for the common case where a non-rebranded MetaTrader 5 sits at its
# conventional path but, for whatever reason (a stripped-down installer, a manually
# copied install), never wrote a normal Uninstall registry entry at all.
# ---------------------------------------------------------------------------
_CONVENTIONAL_FALLBACK_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"


def _registry_combinations() -> list[tuple[int, str, str, int, str]]:
    """
    Returns (hive, hive_name, subkey_path, access_flags, view_label) tuples for all
    six hive/view combinations described in the module docstring. Empty on a
    non-Windows platform (winreg is None) — never raises.
    """
    if winreg is None:
        return []

    combos: list[tuple[int, str, str, int, str]] = []
    for hive, hive_name in (
        (winreg.HKEY_LOCAL_MACHINE, "HKLM"),
        (winreg.HKEY_CURRENT_USER, "HKCU"),
    ):
        combos.append(
            (hive, hive_name, _UNINSTALL_SUBKEY, winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
             f"{hive_name} 64-bit view")
        )
        combos.append(
            (hive, hive_name, _UNINSTALL_SUBKEY, winreg.KEY_READ | winreg.KEY_WOW64_32KEY,
             f"{hive_name} 32-bit view")
        )
        combos.append(
            (hive, hive_name, _UNINSTALL_SUBKEY_WOW6432, winreg.KEY_READ,
             f"{hive_name} WOW6432Node (explicit path)")
        )
    return combos


# ---------------------------------------------------------------------------
# Thin primitives around winreg — isolated into their own functions so the unit
# tests can substitute a fake in-memory registry without touching a real one.
# ---------------------------------------------------------------------------

def _open_key(hive: int, subkey_path: str, access: int):
    """Opens a registry key. Raises OSError (missing key, access denied, unsupported
    flag combination) — the caller decides how to handle that; this function never
    swallows it."""
    return winreg.OpenKeyEx(hive, subkey_path, 0, access)


def _enum_subkey_names(key) -> "list[str]":
    """
    Yields every direct child subkey name under `key`. Never raises: the first
    OSError from `winreg.EnumKey` (index out of range — no more subkeys, or the key
    vanished mid-scan) just ends the enumeration.
    """
    names: list[str] = []
    index = 0
    while True:
        try:
            names.append(winreg.EnumKey(key, index))
        except OSError:
            break
        index += 1
    return names


def _read_string_value(key, subkey_name: str, value_name: str) -> Optional[str]:
    """
    Reads one string value (e.g. "DisplayName", "InstallLocation") from a child
    subkey. Never raises: a missing subkey or a missing value both just return
    `None`.
    """
    try:
        with winreg.OpenKeyEx(key, subkey_name, 0, winreg.KEY_READ) as subkey:
            value, _ = winreg.QueryValueEx(subkey, value_name)
            return str(value)
    except OSError:
        return None


def enumerate_registry_candidates() -> list[RegistryCandidate]:
    """
    Enumerate every Windows Uninstall-registry entry whose DisplayName mentions
    "MetaTrader 5" (case-insensitive), across all six hive/view combinations (see
    module docstring).

    Never raises: a missing key, a missing value, a permissions error on one
    combination, or a non-Windows platform (winreg absent) all just contribute zero
    hits for that one combination — the scan continues with the rest.

    Returns EVERY match, not just the first — a broker-branded (white-label) build
    installs under its own product name in its own folder, and a user may have more
    than one MetaTrader 5 build installed.
    """
    candidates: list[RegistryCandidate] = []
    for hive, hive_name, subkey_path, access, view_label in _registry_combinations():
        try:
            key = _open_key(hive, subkey_path, access)
        except OSError:
            # Missing key, access denied, or an unsupported flag combination on this
            # Windows build — skip this one combination, keep scanning the rest.
            continue
        try:
            for subkey_name in _enum_subkey_names(key):
                display_name = _read_string_value(key, subkey_name, "DisplayName")
                if not display_name or _DISPLAY_NAME_MARKER not in display_name.lower():
                    continue
                install_location = _read_string_value(key, subkey_name, "InstallLocation") or ""
                exe_path = (
                    os.path.join(install_location, _TERMINAL_EXE_NAME)
                    if install_location
                    else None
                )
                exists = bool(exe_path) and os.path.isfile(exe_path)
                candidates.append(
                    RegistryCandidate(
                        hive=hive_name,
                        view=view_label,
                        display_name=display_name,
                        install_location=install_location,
                        exe_path=exe_path,
                        exists=exists,
                    )
                )
        finally:
            key.Close()
    return candidates


def find_terminal_path() -> Optional[str]:
    """
    Locate the installed MetaTrader 5 terminal executable on this machine.

    Tries the Windows registry FIRST via `enumerate_registry_candidates()`, returning
    the first candidate whose `terminal64.exe` actually exists on disk. Only if every
    registry candidate fails does it try the conventional install path as a final,
    documented fallback (see `_CONVENTIONAL_FALLBACK_PATH` above) — never as the
    primary mechanism, and never a hardcoded value that skips the registry check.

    Never raises. Returns `None` when nothing is found anywhere; the caller
    (`agent/mt5_bridge.py`'s `initialize_terminal()`, a later plan) is the one that
    raises `TerminalNotFoundError` for the GUI to show.
    """
    for candidate in enumerate_registry_candidates():
        if candidate["exists"]:
            return candidate["exe_path"]

    if os.path.isfile(_CONVENTIONAL_FALLBACK_PATH):
        return _CONVENTIONAL_FALLBACK_PATH

    return None
