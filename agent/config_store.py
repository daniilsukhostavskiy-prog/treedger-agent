"""
agent/config_store.py — the ONLY place this program persists anything to disk.

Deliberately trivial, by design: one JSON
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
program never needs, and never asks for, administrator rights.

Reads and writes use the standard `json` module only. Never `pickle`, never `yaml` of
any kind. A missing or corrupt config file is treated as "nothing stored yet" — this
module returns `None` from every loader rather than raising, so a damaged file just
sends the caller back to the pairing screen instead of crashing the program.

THE `token` VALUE IS CIPHERTEXT, NOT A BARE STRING
----------------------------------------------------------------------------------
The two-key rule above is unchanged. What changed is the VALUE stored under the
`token` key: it is now `base64.b64encode(dpapi.protect(token.encode("utf-8")))`, never
the raw token string. `dpapi.py` (this program's one and only DPAPI-aware module) is
the encryption layer; `save_token`/`load_token` are its only two call sites in the
whole program.

WHY THE OLD PLAINTEXT TOKEN IS DISCARDED, NOT MIGRATED
----------------------------------------------------------------------------------
A Phase-39 install has a bare plaintext string under `token`. This module does NOT
detect that shape and re-encrypt it in place — that would require a permanent "if the
value is not encrypted, take it as-is" branch, which is exactly the hole encrypting
the token closes, made eternal: six months from now nobody remembers why the branch
exists, and it quietly becomes the plaintext path back. `load_token()` treats a
legacy plaintext value exactly like any other value that fails to decode as
base64-of-DPAPI-ciphertext — it returns `None`, same as a missing or corrupt file,
and the program shows the pairing screen. Cost of this choice right now is minimal:
one person re-pairs once. There is no branch anywhere in this module that recognises
or accepts an unencrypted token.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
from typing import Optional

from agent import dpapi

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
    supported platform); a dotfile under the
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
    """
    The stored rotating token, decrypted, or `None` if none is stored, the file is
    corrupt/missing, the stored value is not valid base64, decryption fails, or the
    decrypted bytes are not valid UTF-8. Every one of those cases lands in the SAME
    `None` path — there is no branch that distinguishes "legacy plaintext token" from
    any other unreadable value, by design (see the module docstring's "WHY THE OLD
    PLAINTEXT TOKEN IS DISCARDED" section).
    """
    data = _read_config()
    raw = data.get(_KEY_TOKEN)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ciphertext = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        plaintext = dpapi.unprotect(ciphertext)
    except (OSError, dpapi.DpapiUnavailableError):
        return None
    try:
        token = plaintext.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return token or None


def save_token(token: str) -> None:
    """
    Encrypt `token` via DPAPI and persist the ciphertext (base64-encoded, under the
    same `token` key), replacing whatever was stored before. Callers (agent/sync.py)
    MUST call this immediately on receiving a rotated token from `/api/agent/accounts`
    — a token received but never stored is exactly the dropped-response lockout the
    server's grace window (see `agent/api_client.py`'s `AccountsFetchResult` docstring)
    exists to survive.

    A `dpapi.DpapiUnavailableError` (or the underlying `OSError` from a failed Win32
    call) is allowed to propagate out of this function rather than being swallowed:
    "the program refuses to store the token and says so" (see `agent/dpapi.py`'s own
    docstring) lives at THIS
    boundary, not inside `dpapi.py` itself — this function must never fall back to
    storing the token in any other form.
    """
    ciphertext = dpapi.protect(token.encode("utf-8"))
    data = _read_config()
    data[_KEY_TOKEN] = base64.b64encode(ciphertext).decode("ascii")
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
