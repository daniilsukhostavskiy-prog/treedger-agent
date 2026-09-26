"""
agent/audio_mute.py — mutes the MetaTrader 5 terminal's own Windows sounds, and
nothing else's (quick 260926-ieo).

WHAT THIS DOES
--------------------------------------------------------------------------
Exactly one thing: it walks the render (playback) audio endpoints' PER-APPLICATION
sessions — the same list Windows' own volume mixer shows — finds the sessions that
belong to a running `terminal64.exe` process (by pid, from
`agent/terminal_process.py`'s own Toolhelp-based list — never re-implemented here),
and mutes or unmutes exactly those sessions through Core Audio's
`ISimpleAudioVolume.SetMute`.

WHAT THIS NEVER DOES
--------------------------------------------------------------------------
It never touches the master/endpoint output volume (the single system-wide speaker
level), never touches any other process's audio session, never opens, reads or writes
any MetaTrader 5 file or setting, never ends, restarts or closes any process, and never
shows, hides, minimises or otherwise moves any window, MetaTrader 5's included. Its only
effect, ever, is one boolean (muted/not muted) on a handful of per-application volume-
mixer entries that already exist only because a `terminal64.exe` process is running.

WHY RE-APPLY PERIODICALLY (not once)
--------------------------------------------------------------------------
A per-application Core Audio session is created lazily by Windows the first time that
process actually plays a sound — a `terminal64.exe` that has not yet made a sound has no
session to mute yet, and every account-switch sound it might play in the future creates
one. This module is therefore called on a short, cheap timer
(`agent/main.py`'s `_on_mute_tick`) rather than once at start: `FIRST_APPLY_DELAY_SECONDS`
shortly after start/attach, then `REAPPLY_INTERVAL_SECONDS` while idle and the shorter
`REAPPLY_INTERVAL_DURING_SYNC_SECONDS` while a sync is running (sync is exactly when MT5 is
most likely to play a connect/switch sound). The session enumerator Core Audio hands back
is a point-in-time snapshot, not a live view, which is the other reason this module
re-enumerates on every call rather than caching anything.

WHO OWNS A MUTE
--------------------------------------------------------------------------
Windows remembers a per-application volume/mute setting for an "application identity"
across that application's own process lifetimes (not merely for the lifetime of one
session object) — a terminal started AFTER this program has already been running may
therefore come up already muted, purely from that memory, with no session this program
itself ever muted. `MuteController` keeps two in-memory sets, keyed by `(pid,
instance_id)`: sessions it muted itself ("ours" — always safe to unmute later) and
sessions it found already muted the first time it looked, with `adopt_already_muted=False`
("theirs" — the user's own choice, NEVER unmuted by this program, ever). The persisted
`config_store.mt5_mute_pending` marker exists for exactly the ambiguous case above: "a mute
this program applied may still be in effect in Windows' per-app memory, even though this
fresh process has no record of muting it itself" — `agent/main.py` passes
`adopt_already_muted=True` while that marker is set, which lets `apply()`/`restore()`
correctly claim (and, on restore, undo) a mute this program is responsible for even
across a crash or a full logoff/logon, without ever touching a session this program has
no reason to believe it caused.

THREADING
--------------------------------------------------------------------------
Every public method here is called ONLY from the Tk main thread, through
`agent/main.py`'s `root.after` timer (`_on_mute_tick`) — never from a background thread.
COM itself is initialised and uninitialised once PER SNAPSHOT (`CoInitializeEx`/
`CoUninitialize` inside `_WindowsCoreAudioBackend.snapshot()`), on whichever thread calls
it, because Tk may already have initialised COM on the main thread in some other apartment
mode; `CoInitializeEx(COINIT_APARTMENTTHREADED)` returning `RPC_E_CHANGED_MODE` (COM was
already initialised in a different mode on this thread) is treated as "fine, proceed" —
Core Audio itself works from either apartment — and in that one case `CoUninitialize` is
deliberately NOT called, so this module never ends an apartment mode it did not start.

OFF-WINDOWS
--------------------------------------------------------------------------
Every function and method degrades to an all-zero, no-exception result off Windows (or
wherever `ctypes.WinDLL("ole32", ...)` itself is unavailable) — mirroring
`agent/single_instance.py` and `agent/terminal_process.py`'s identical pattern — so this
whole module stays importable and testable on this repository's own non-Windows dev/CI
environment.
"""
from __future__ import annotations

import contextlib
import ctypes
import logging
import uuid
from typing import Callable, NamedTuple, Optional

from agent import terminal_process

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cadence constants (agent/main.py schedules `_on_mute_tick` from these — never a
# repeated literal).
# ---------------------------------------------------------------------------
FIRST_APPLY_DELAY_SECONDS: int = 1
REAPPLY_INTERVAL_SECONDS: int = 15
REAPPLY_INTERVAL_DURING_SYNC_SECONDS: int = 2

try:
    _ole32 = ctypes.WinDLL("ole32", use_last_error=True)
except AttributeError:
    # Non-Windows platform: `ctypes.WinDLL` itself does not exist. Mirrors
    # agent/single_instance.py and agent/terminal_process.py's identical pattern.
    _ole32 = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Well-known CLSID/IID strings (Windows SDK headers) — built into real `_GUID`
# structures by `_guid()` below, once, at import time.
# ---------------------------------------------------------------------------
_CLSID_MMDEVICE_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_IMMDEVICE_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_IAUDIO_SESSION_MANAGER2 = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
_IID_IAUDIO_SESSION_CONTROL2 = "{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}"
_IID_ISIMPLE_AUDIO_VOLUME = "{87CE5498-68D6-44E5-9215-6DA47EF883D8}"

_E_RENDER = 0
_DEVICE_STATE_ACTIVE = 0x1
_CLSCTX_ALL = 0x17
_COINIT_APARTMENTTHREADED = 0x2
# HRESULT for "COM was already initialised on this thread in a different apartment
# mode" — proceed without calling CoUninitialize in that one case (see module
# docstring's THREADING section).
_RPC_E_CHANGED_MODE = -2147417850

# Vtable slot numbers — named for the method actually called, per the interface's own
# declaration order in the Windows SDK headers (IUnknown occupies slots 0-2 on every
# COM interface: QueryInterface, AddRef, Release).
_SLOT_QUERY_INTERFACE = 0
_SLOT_RELEASE = 2
_SLOT_ENUM_AUDIO_ENDPOINTS = 3  # IMMDeviceEnumerator::EnumAudioEndpoints
_SLOT_COLLECTION_GET_COUNT = 3  # IMMDeviceCollection::GetCount
_SLOT_COLLECTION_ITEM = 4  # IMMDeviceCollection::Item
_SLOT_DEVICE_ACTIVATE = 3  # IMMDevice::Activate
_SLOT_SESSION_MANAGER_GET_SESSION_ENUMERATOR = 5  # IAudioSessionManager2::GetSessionEnumerator
_SLOT_ENUM_SESSIONS_GET_COUNT = 3  # IAudioSessionEnumerator::GetCount
_SLOT_ENUM_SESSIONS_GET_SESSION = 4  # IAudioSessionEnumerator::GetSession
_SLOT_SESSION_CONTROL2_GET_SESSION_INSTANCE_IDENTIFIER = 13  # IAudioSessionControl2
_SLOT_SESSION_CONTROL2_GET_PROCESS_ID = 14  # IAudioSessionControl2
# Deliberately named ONLY for the two methods this module ever calls on
# ISimpleAudioVolume — the neighbouring volume-LEVEL slots (GetMasterVolume /
# SetMasterVolume) have no constant here and are never called (see module docstring's
# "WHAT THIS NEVER DOES").
_SIMPLE_VOLUME_SET_MUTE = 5
_SIMPLE_VOLUME_GET_MUTE = 6


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    """A `_GUID` built from a standard `{8-4-4-4-12}` string, byte-for-byte identical
    to `uuid.UUID(text).bytes_le` — the Windows `GUID` struct's own in-memory layout
    (Data1/Data2/Data3 little-endian, Data4 as raw bytes) matches `bytes_le` exactly."""
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


if _ole32 is not None:
    _ole32.CoInitializeEx.restype = ctypes.c_long
    _ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _ole32.CoUninitialize.restype = None
    _ole32.CoUninitialize.argtypes = []
    _ole32.CoCreateInstance.restype = ctypes.c_long
    _ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _ole32.CoTaskMemFree.restype = None
    _ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]


def _com_method(ptr: int, slot: int, argtypes: "list[object]"):
    """
    A callable bound to vtable slot `slot` of the COM interface pointer `ptr`
    (`ptr` itself must always be passed as this callable's first argument — it is
    the implicit `this`). Every COM method used in this module returns an `HRESULT`,
    read here as a plain signed `c_long` (never `ctypes.HRESULT`, which auto-raises on
    a failed value — this module checks every HRESULT itself and must never have one
    turned into a Python exception behind its back).
    """
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
    assert vtable is not None
    entry_address = vtable + slot * ctypes.sizeof(ctypes.c_void_p)
    entry = ctypes.cast(entry_address, ctypes.POINTER(ctypes.c_void_p)).contents.value
    func_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return func_type(entry)


def _release(ptr: int) -> None:
    """`IUnknown::Release()` on `ptr` — the one COM call every acquired pointer gets,
    exactly once, in `_WindowsCoreAudioBackend.snapshot()`'s cleanup."""
    _com_method(ptr, _SLOT_RELEASE, [])(ptr)


def _session_key(pid: int, instance_id: str) -> "tuple[int, str]":
    """The identity `MuteController` tracks a session by — a `(pid, instance_id)` pair
    distinguishes two sessions that happen to share a pid (a process can hold more than
    one audio session) with different instance ids."""
    return (pid, instance_id)


class ApplyResult(NamedTuple):
    terminal_sessions_seen: int = 0
    newly_muted: int = 0
    adopted: int = 0
    left_alone_user_muted: int = 0
    errors: int = 0


class RestoreResult(NamedTuple):
    terminal_sessions_seen: int = 0
    unmuted: int = 0
    errors: int = 0


class _Session:
    """One per-application Core Audio session, wrapping the two calls this module ever
    makes on it (`GetMute`/`SetMute`). `get_mute_fn`/`set_mute_fn` are plain closures so
    this class works identically for the real Windows backend and for a test fake."""

    def __init__(
        self,
        *,
        pid: int,
        instance_id: str,
        get_mute_fn: "Callable[[], Optional[bool]]",
        set_mute_fn: "Callable[[bool], bool]",
    ) -> None:
        self.pid = pid
        self.instance_id = instance_id
        self._get_mute_fn = get_mute_fn
        self._set_mute_fn = set_mute_fn

    def get_mute(self) -> Optional[bool]:
        try:
            return self._get_mute_fn()
        except Exception:  # noqa: BLE001 — a failed read is "unreadable", never a crash
            return None

    def set_mute(self, muted: bool) -> bool:
        try:
            return bool(self._set_mute_fn(muted))
        except Exception:  # noqa: BLE001 — a failed write is "not done", never a crash
            return False


class _WindowsCoreAudioBackend:
    """
    The real Windows Core Audio walk: every render endpoint's every per-application
    session, this run. Never constructed by a unit test — see
    `agent/tests/conftest.py`'s `_never_touch_real_core_audio` autouse guard — only by
    `MuteController`'s real default backend factory, and exercised for real only by this
    quick task's own desktop smoke script.
    """

    @contextlib.contextmanager
    def snapshot(self):
        released: "list[int]" = []
        did_coinit = False
        sessions: "list[_Session]" = []
        try:
            hr_init = _ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
            # S_OK (0) or S_FALSE (1): we own this CoInitializeEx call and must balance
            # it. RPC_E_CHANGED_MODE (already initialised differently on this thread,
            # e.g. by Tk): proceed without ever calling CoUninitialize ourselves.
            did_coinit = hr_init in (0, 1)
            try:
                self._collect_sessions(sessions, released)
            except Exception:  # noqa: BLE001 — a partial snapshot beats none at all
                logger.debug("audio_mute: snapshot enumeration failed", exc_info=True)
            yield sessions
        finally:
            for ptr in reversed(released):
                try:
                    _release(ptr)
                except Exception:  # noqa: BLE001 — release must never raise past here
                    pass
            if did_coinit:
                try:
                    _ole32.CoUninitialize()
                except Exception:  # noqa: BLE001
                    pass

    def _collect_sessions(self, sessions: "list[_Session]", released: "list[int]") -> None:
        enumerator_ptr = ctypes.c_void_p()
        hr = _ole32.CoCreateInstance(
            ctypes.byref(_guid(_CLSID_MMDEVICE_ENUMERATOR)),
            None,
            _CLSCTX_ALL,
            ctypes.byref(_guid(_IID_IMMDEVICE_ENUMERATOR)),
            ctypes.byref(enumerator_ptr),
        )
        if hr < 0 or not enumerator_ptr.value:
            return
        released.append(enumerator_ptr.value)

        enum_endpoints = _com_method(
            enumerator_ptr.value,
            _SLOT_ENUM_AUDIO_ENDPOINTS,
            [ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)],
        )
        collection_ptr = ctypes.c_void_p()
        hr = enum_endpoints(enumerator_ptr.value, _E_RENDER, _DEVICE_STATE_ACTIVE, ctypes.byref(collection_ptr))
        if hr < 0 or not collection_ptr.value:
            return
        released.append(collection_ptr.value)

        get_count = _com_method(collection_ptr.value, _SLOT_COLLECTION_GET_COUNT, [ctypes.POINTER(ctypes.c_uint)])
        count = ctypes.c_uint(0)
        if get_count(collection_ptr.value, ctypes.byref(count)) < 0:
            return

        item = _com_method(
            collection_ptr.value, _SLOT_COLLECTION_ITEM, [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
        )
        for device_index in range(count.value):
            device_ptr = ctypes.c_void_p()
            if item(collection_ptr.value, device_index, ctypes.byref(device_ptr)) < 0 or not device_ptr.value:
                continue
            released.append(device_ptr.value)
            try:
                self._collect_device_sessions(device_ptr.value, sessions, released)
            except Exception:  # noqa: BLE001 — one bad device must not lose every other
                logger.debug("audio_mute: skipping one device after an error", exc_info=True)

    def _collect_device_sessions(
        self, device_ptr: int, sessions: "list[_Session]", released: "list[int]"
    ) -> None:
        activate = _com_method(
            device_ptr,
            _SLOT_DEVICE_ACTIVATE,
            [ctypes.POINTER(_GUID), ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
        )
        manager_ptr = ctypes.c_void_p()
        hr = activate(
            device_ptr,
            ctypes.byref(_guid(_IID_IAUDIO_SESSION_MANAGER2)),
            _CLSCTX_ALL,
            None,
            ctypes.byref(manager_ptr),
        )
        if hr < 0 or not manager_ptr.value:
            return
        released.append(manager_ptr.value)

        get_session_enum = _com_method(
            manager_ptr.value,
            _SLOT_SESSION_MANAGER_GET_SESSION_ENUMERATOR,
            [ctypes.POINTER(ctypes.c_void_p)],
        )
        session_enum_ptr = ctypes.c_void_p()
        if get_session_enum(manager_ptr.value, ctypes.byref(session_enum_ptr)) < 0 or not session_enum_ptr.value:
            return
        released.append(session_enum_ptr.value)

        get_count = _com_method(
            session_enum_ptr.value, _SLOT_ENUM_SESSIONS_GET_COUNT, [ctypes.POINTER(ctypes.c_int)]
        )
        count = ctypes.c_int(0)
        if get_count(session_enum_ptr.value, ctypes.byref(count)) < 0:
            return

        get_session = _com_method(
            session_enum_ptr.value,
            _SLOT_ENUM_SESSIONS_GET_SESSION,
            [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
        )
        for session_index in range(count.value):
            session_ptr = ctypes.c_void_p()
            if (
                get_session(session_enum_ptr.value, session_index, ctypes.byref(session_ptr)) < 0
                or not session_ptr.value
            ):
                continue
            released.append(session_ptr.value)
            try:
                self._describe_session(session_ptr.value, sessions, released)
            except Exception:  # noqa: BLE001 — one bad session must not lose every other
                logger.debug("audio_mute: skipping one session after an error", exc_info=True)

    def _describe_session(
        self, session_ptr: int, sessions: "list[_Session]", released: "list[int]"
    ) -> None:
        query_interface = _com_method(
            session_ptr, _SLOT_QUERY_INTERFACE, [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
        )

        control2_ptr = ctypes.c_void_p()
        hr = query_interface(
            session_ptr, ctypes.byref(_guid(_IID_IAUDIO_SESSION_CONTROL2)), ctypes.byref(control2_ptr)
        )
        if hr < 0 or not control2_ptr.value:
            return
        released.append(control2_ptr.value)

        get_process_id = _com_method(
            control2_ptr.value, _SLOT_SESSION_CONTROL2_GET_PROCESS_ID, [ctypes.POINTER(ctypes.c_uint)]
        )
        pid = ctypes.c_uint(0)
        if get_process_id(control2_ptr.value, ctypes.byref(pid)) != 0:
            # Any non-S_OK result — including the documented multi-process-session
            # case — means "unknown pid": skip, per the module docstring.
            return

        get_instance_id = _com_method(
            control2_ptr.value,
            _SLOT_SESSION_CONTROL2_GET_SESSION_INSTANCE_IDENTIFIER,
            [ctypes.POINTER(ctypes.c_wchar_p)],
        )
        raw_id = ctypes.c_wchar_p()
        if get_instance_id(control2_ptr.value, ctypes.byref(raw_id)) < 0 or not raw_id.value:
            return
        instance_id = raw_id.value
        # The string is CoTaskMem-allocated by Core Audio; free it once we have our own
        # copy (a plain Python str, already copied out by `.value` above).
        _ole32.CoTaskMemFree(ctypes.cast(raw_id, ctypes.c_void_p))

        volume_ptr = ctypes.c_void_p()
        hr = query_interface(session_ptr, ctypes.byref(_guid(_IID_ISIMPLE_AUDIO_VOLUME)), ctypes.byref(volume_ptr))
        if hr < 0 or not volume_ptr.value:
            return
        released.append(volume_ptr.value)

        get_mute = _com_method(volume_ptr.value, _SIMPLE_VOLUME_GET_MUTE, [ctypes.POINTER(ctypes.c_int)])
        set_mute = _com_method(volume_ptr.value, _SIMPLE_VOLUME_SET_MUTE, [ctypes.c_int, ctypes.POINTER(_GUID)])
        bound_volume_ptr = volume_ptr.value

        def _get_mute_fn(_get_mute=get_mute, _ptr=bound_volume_ptr) -> Optional[bool]:
            value = ctypes.c_int(0)
            if _get_mute(_ptr, ctypes.byref(value)) < 0:
                return None
            return bool(value.value)

        def _set_mute_fn(muted: bool, _set_mute=set_mute, _ptr=bound_volume_ptr) -> bool:
            return _set_mute(_ptr, 1 if muted else 0, None) >= 0

        sessions.append(
            _Session(pid=int(pid.value), instance_id=instance_id, get_mute_fn=_get_mute_fn, set_mute_fn=_set_mute_fn)
        )


def _default_backend_factory() -> "Optional[_WindowsCoreAudioBackend]":
    """The real backend, or `None` off Windows (or when `ole32`'s WinDLL handle could not
    be loaded at import time). Never raises."""
    if _ole32 is None:
        return None
    try:
        return _WindowsCoreAudioBackend()
    except Exception:  # noqa: BLE001 — constructing the backend must never raise
        return None


def _terminal_pids() -> "Optional[set[int]]":
    """
    The pids of every currently running `terminal64.exe`, reusing
    `agent/terminal_process.py`'s own Toolhelp-based, exact-basename-matched process
    list rather than re-implementing process enumeration here (see that module's own
    docstring). `None` means "could not tell" — every caller in this module treats that
    as "touch nothing this tick", never as "no terminal is running".
    """
    procs = terminal_process.list_terminal_processes()
    if procs is None:
        return None
    return {proc.pid for proc in procs}


class MuteController:
    """
    Mutes and restores `terminal64.exe`'s own per-application Core Audio sessions only —
    see the module docstring for the full behaviour and ownership rules. Every public
    method here NEVER raises: a failure anywhere inside `apply()`/`restore()` is caught,
    logged (at WARNING once per distinct failure, then at DEBUG for repeats of the exact
    same one, so a tick every 2 s never floods `agent.log`), and reported back as an
    all-zero result.
    """

    def __init__(
        self,
        *,
        backend_factory: "Optional[Callable[[], Optional[object]]]" = None,
        terminal_pids: "Optional[Callable[[], Optional[set[int]]]]" = None,
    ) -> None:
        self._backend_factory = backend_factory or _default_backend_factory
        self._terminal_pids_fn = terminal_pids or _terminal_pids
        # Keyed by `_session_key(pid, instance_id)`. `_ours` is always safe to unmute
        # later; `_theirs` (the user's own mute) is NEVER unmuted by this program.
        self._ours: "set[tuple[int, str]]" = set()
        self._theirs: "set[tuple[int, str]]" = set()
        self._last_logged_failure: Optional[str] = None

    def has_recorded(self) -> bool:
        """Whether this controller currently believes it owns at least one muted
        session — used by `agent/main.py` to decide whether the persisted
        `mt5_mute_pending` marker should stay set across a run."""
        return bool(self._ours)

    def apply(self, *, adopt_already_muted: bool) -> ApplyResult:
        try:
            pids = self._terminal_pids_fn()
            if pids is None:
                return ApplyResult()
            backend = self._backend_factory()
            if backend is None:
                return ApplyResult()

            seen = newly_muted = adopted = left_alone = errors = 0
            with backend.snapshot() as sessions:
                for session in sessions:
                    if session.pid not in pids:
                        continue
                    seen += 1
                    key = _session_key(session.pid, session.instance_id)
                    if key in self._ours or key in self._theirs:
                        continue
                    muted = session.get_mute()
                    if muted is None:
                        errors += 1
                        continue
                    if muted:
                        if adopt_already_muted:
                            self._ours.add(key)
                            adopted += 1
                        else:
                            self._theirs.add(key)
                            left_alone += 1
                        continue
                    if session.set_mute(True):
                        self._ours.add(key)
                        newly_muted += 1
                    else:
                        errors += 1

            self._log_result(newly_muted=newly_muted, adopted=adopted, unmuted=0)
            return ApplyResult(
                terminal_sessions_seen=seen,
                newly_muted=newly_muted,
                adopted=adopted,
                left_alone_user_muted=left_alone,
                errors=errors,
            )
        except Exception as exc:  # noqa: BLE001 — this method must never raise
            self._log_failure(exc)
            return ApplyResult()

    def restore(self, *, adopt_already_muted: bool = False) -> RestoreResult:
        try:
            pids = self._terminal_pids_fn()
            if pids is None:
                return RestoreResult()
            backend = self._backend_factory()
            if backend is None:
                return RestoreResult()

            seen = unmuted = errors = 0
            with backend.snapshot() as sessions:
                by_key = {}
                for session in sessions:
                    if session.pid not in pids:
                        continue
                    seen += 1
                    by_key[_session_key(session.pid, session.instance_id)] = session

                still_present_ours: "set[tuple[int, str]]" = set()
                for key in list(self._ours):
                    session = by_key.get(key)
                    if session is None:
                        # No longer appears in this snapshot — drop the stale record.
                        self._ours.discard(key)
                        continue
                    still_present_ours.add(key)
                    if session.set_mute(False):
                        unmuted += 1
                        self._ours.discard(key)
                    else:
                        errors += 1  # stays recorded — try again next time

                if adopt_already_muted:
                    for key, session in by_key.items():
                        if key in still_present_ours or key in self._theirs:
                            continue  # never unmute the user's own mute
                        muted = session.get_mute()
                        if muted is None:
                            errors += 1
                            continue
                        if muted and session.set_mute(False):
                            unmuted += 1
                        elif muted:
                            errors += 1

            self._log_result(newly_muted=0, adopted=0, unmuted=unmuted)
            return RestoreResult(terminal_sessions_seen=seen, unmuted=unmuted, errors=errors)
        except Exception as exc:  # noqa: BLE001 — this method must never raise
            self._log_failure(exc)
            return RestoreResult()

    def _log_result(self, *, newly_muted: int, adopted: int, unmuted: int) -> None:
        if newly_muted or adopted or unmuted:
            logger.info(
                "audio_mute: newly_muted=%d adopted=%d unmuted=%d", newly_muted, adopted, unmuted
            )
        else:
            logger.debug("audio_mute: no change this tick")

    def _log_failure(self, exc: Exception) -> None:
        description = f"{exc.__class__.__name__}: {exc}"
        if description != self._last_logged_failure:
            logger.warning("audio_mute: operation failed: %s", description)
            self._last_logged_failure = description
        else:
            logger.debug("audio_mute: operation failed again (already warned): %s", description)
