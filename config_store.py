"""
agent/config_store.py — the ONLY place this program persists anything to disk.

Deliberately trivial, per 39-PATTERNS.md's own note not to over-engineer this: one JSON
file, exactly two keys, standard-library `json` only. This module is not a general
key-value store and must never grow a third key.

WHY ONLY A TOKEN AND A BASE URL EVER GO IN HERE — READ BEFORE ADDING A THIRD KEY
----------------------------------------------------------------------------------
The investor password is never written to this file (or anywhere else). It arrives
fresh, in memory only, on every `/api/agent/accounts` response (decrypted server-side)
and is discarded the moment this program's process exits — a program that never stores
the password on disk cannot leak it from disk. Do not "cache" the password here, an
account id, a login, a broker-server name, or anything else about the trader's accounts
— none of that belongs in a program-level config file, and every one of those values is
re-fetched from the server on the next run anyway.

The file lives under the current user's own per-user application-data directory —
never beside the program itself and never under a Program Files-style path — so this
program never needs, and never asks for, administrator rights (PHASE-LOCAL-SYNC-SPEC.md
§8 / SYNC-LOCAL-AGENT-HANDOFF.md §5.7).

Reads and writes use the standard `json` module only. Never `pickle`, never `yaml` of
any kind. A missing or corrupt config file is treated as "nothing stored yet" — this
module returns `None` from every loader rather than raising, so a damaged file just
sends the caller back to the pairing screen instead of crashing the program.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

_APP_DIR_NAME = "TreedgerAgent"
_CONFIG_FILENAME = "config.json"

# The exact two keys this file is ever allowed to hold. Nothing else, ever.
_KEY_TOKEN = "token"
_KEY_BASE_URL = "base_url"


def _app_data_dir() -> Path:
    """
    The current user's own per-user application-data directory — never a path beside
    this program's own source, never anywhere under Program Files. `%APPDATA%` is the
    standard per-user, no-admin-rights-required location on Windows (this program's one
    supported platform this phase, PHASE-LOCAL-SYNC-SPEC.md §9); a dotfile under the
    user's home directory is the fallback for any other platform this test suite runs
    on (e.g. this repository's own non-Windows dev/CI environment).
    """
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / _APP_DIR_NAME
    return Path.home() / f".{_APP_DIR_NAME.lower()}"


def config_path() -> Path:
    """The one file this whole module ever reads or writes."""
    return _app_data_dir() / _CONFIG_FILENAME


def _read_config() -> dict:
    """
    Read the config file and return it as a dict, or `{}` on ANY failure — missing
    file, unreadable file, or a file that is not valid JSON. Never raises. `{}` is
    treated identically to "the file has never been written."
    """
    path = config_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_config(data: dict) -> None:
    """
    Write `data` as the whole config file, standard `json` only. Writes to a temporary
    file in the same directory first, then atomically replaces the real file — so a
    crash or a killed process mid-write can never leave a half-written, corrupt config
    file behind (which `_read_config()` would otherwise have to treat as "nothing
    stored," silently losing a token that was actually still good).
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle)
    tmp_path.replace(path)


def load_token() -> Optional[str]:
    """The stored rotating token, or `None` if none is stored (or the file is corrupt/missing)."""
    data = _read_config()
    token = data.get(_KEY_TOKEN)
    return token if isinstance(token, str) and token else None


def save_token(token: str) -> None:
    """
    Persist `token`, replacing whatever was stored before. Callers (agent/sync.py) MUST
    call this immediately on receiving a rotated token from `/api/agent/accounts` — a
    token received but never stored is exactly the dropped-response lockout the D-26
    grace window exists to survive.
    """
    data = _read_config()
    data[_KEY_TOKEN] = token
    _write_config(data)


def clear_token() -> None:
    """Remove the stored token (e.g. on a 401 — the GUI sends the user back to pairing)."""
    data = _read_config()
    data.pop(_KEY_TOKEN, None)
    _write_config(data)


def load_base_url() -> Optional[str]:
    """The stored server base URL, or `None` if none is stored (or the file is corrupt/missing)."""
    data = _read_config()
    url = data.get(_KEY_BASE_URL)
    return url if isinstance(url, str) and url else None


def save_base_url(url: str) -> None:
    """Persist the server base URL, replacing whatever was stored before."""
    data = _read_config()
    data[_KEY_BASE_URL] = url
    _write_config(data)
