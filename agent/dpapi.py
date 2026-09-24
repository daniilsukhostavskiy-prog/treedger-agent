"""
agent/dpapi.py — the ONLY module in this program that knows Windows DPAPI exists.

Exposes exactly `protect(plaintext: bytes) -> bytes`, `unprotect(ciphertext: bytes)
-> bytes`, and `DpapiUnavailableError`. Implemented with standard-library `ctypes`
against `crypt32.dll`/`kernel32.dll` — `CryptProtectData`/`CryptUnprotectData` with
`CRYPTPROTECT_UI_FORBIDDEN`, never anything else. No optional entropy parameter:
DPAPI's own per-user session-key binding is the whole mechanism — a second secret is
not needed on top of it. Adds zero new entries to `agent/requirements.txt` — every
symbol used here is standard library.

WHY NO NON-WINDOWS BRANCH EXISTS — READ BEFORE ADDING ONE
----------------------------------------------------------------------------------
Without DPAPI, this program refuses to store the token and says so, rather than
storing it some other way. There is no XOR, no baked-key AES, no "obfuscation," no
plaintext fallback — none of that is encryption, and pretending it is would be worse
than admitting the token can't be stored on this platform: **a mechanism that creates
a feeling of protection without providing it is worse than no mechanism** — the same
principle behind this program's own honesty about not self-verifying its executable
(see `agent/README.md`'s "What this program does not do" section).

This file DOES import successfully on a non-Windows machine (the two functions below
are still defined) — that import guard exists ONLY so the cross-platform part of the
test suite (and this repository's own non-Windows sandbox, matching
`config_store.py`'s own documented assumption) can still import `agent.dpapi` without
crashing collection. Calling `protect()`/`unprotect()` off Windows raises
`DpapiUnavailableError` immediately — never returns, never falls back, never encodes
anything. The absence of a return-value fallback anywhere in this module is proven by
the folder's own `ast`-based structural audit (`TestAgentFolderStructuralAudit` in
`agent/tests/test_sync.py`), not by grepping this file for a word.

THIS MODULE IS ALLOWED — REQUIRED — TO RAISE
----------------------------------------------------------------------------------
Every other module in `agent/` (most visibly `config_store.py`, whose own docstring
states loaders "return `None` from every loader rather than raising") is written so a
failure at that layer degrades quietly to "nothing stored" rather than crashing the
whole program. This module is the deliberate, single exception to that rule: a
silent failure HERE means a token got stored in a way nobody checked, which is a
worse outcome than the program crashing loudly during development. `protect()` and
`unprotect()` raise on every failure path — `OSError` when the underlying Win32 call
fails, `DpapiUnavailableError` off Windows. Neither function ever returns a "best
effort" or partial result. The caller (`config_store.py`) is where a `dpapi` failure
gets translated into the rest of the program's usual "return None" convention — that
translation does not happen inside this file.

KNOWN DPAPI LIMITS — READ BEFORE PROMISING A USER ANYTHING ABOUT RECOVERY
----------------------------------------------------------------------------------
A token encrypted here becomes PERMANENTLY undecryptable if an administrator RESETS
(not the user's own self-service password change) the Windows account password —
DPAPI's master key is itself wrapped with the user's own credential material, and an
admin reset does not have that material to re-wrap it with. This is documented DPAPI
behaviour, not a bug in this module, and there is nothing to "fix" here: the recovery
path is the same one already designed for any broken/discarded token — the user
re-pairs. User-facing copy must never promise this "can't happen."

Related, rarer edge case: under a ROAMING Windows profile, DPAPI-protected data is
bound per-*user*, not strictly per-*machine* — it can decrypt on a different machine
on the same domain. Most consumer Windows installs (this program's expected
environment) are not domain-joined/roaming, so this is a low-probability edge case,
not a design flaw — state it accurately rather than overclaiming "bound to this one
computer."
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DpapiUnavailableError(Exception):
    """Raised by protect()/unprotect() on any platform where DPAPI does not exist."""


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_from_bytes(data: bytes) -> DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _require_windows() -> None:
    if not sys.platform.startswith("win"):
        raise DpapiUnavailableError(
            "Windows DPAPI is not available on this platform "
            f"(sys.platform={sys.platform!r}); refusing to store or read a token."
        )


def protect(plaintext: bytes) -> bytes:
    """
    Encrypt `plaintext` for the current Windows user via `CryptProtectData`. Raises
    `DpapiUnavailableError` off Windows, `OSError` if the Win32 call itself fails.
    Never returns anything but real DPAPI ciphertext.
    """
    _require_windows()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    in_blob = _blob_from_bytes(plaintext)
    out_blob = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(f"CryptProtectData failed: {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def unprotect(ciphertext: bytes) -> bytes:
    """
    Decrypt `ciphertext` previously produced by `protect()`. Raises
    `DpapiUnavailableError` off Windows, `OSError` if the Win32 call itself fails
    (including when `ciphertext` is not valid DPAPI ciphertext at all). Never returns
    a guess.
    """
    _require_windows()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    in_blob = _blob_from_bytes(ciphertext)
    out_blob = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(f"CryptUnprotectData failed: {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
