"""
agent/tray.py — a zero-dependency, ctypes-only Windows notification-area (tray) icon
(quick 260926-ieo).

WHY CTYPES, NOT PYSTRAY
--------------------------------------------------------------------------
`pystray` needs Pillow to build its icon image, and this program's release build
already takes about 23 minutes (`agent/packaging/40-BUILD-EVIDENCE.md`) — this quick
task's own budget rule is "no new dependency, ever", so a tray icon here is built the
same way `agent/single_instance.py` and `agent/terminal_process.py` already build their
own small slices of the Win32 API: raw `ctypes.WinDLL` calls, with explicit
`argtypes`/`restype` on every one of them.

THREADING
--------------------------------------------------------------------------
`TrayIcon` owns exactly one daemon thread: it registers a window class, creates one
hidden top-level window, and runs that window's own `GetMessageW` loop for as long as
the icon lives. Every `Shell_NotifyIconW` call happens ON THAT THREAD, from inside the
window procedure — never from whatever thread calls `set_tooltip`/`show_balloon`/
`set_sounds_muted`. Those public methods only enqueue an update (or store a plain bool
under a lock) and `PostMessageW` a private "wake" message to the hidden window, which the
tray thread then drains on its own. `command_sink` (the callable passed to `__init__`) is
therefore ALSO CALLED ON THE TRAY THREAD, from inside the window procedure — it must
never touch Tk directly; `agent/main.py`'s `tray_command_sink` only ever puts a plain
`_TrayCommand` onto `AgentWindow`'s own cross-thread queue, exactly like the sync
worker thread already does (see that module's own CROSS-THREAD DISCIPLINE section).

HIDDEN WINDOW
--------------------------------------------------------------------------
The tray needs a real window handle to receive its `Shell_NotifyIconW` callback message
and Explorer's own "TaskbarCreated" broadcast — but that window is created with
`WS_EX_TOOLWINDOW`, style `0`, and `ShowWindow` is never called on it, so it never
becomes visible and never gets a taskbar button. It is deliberately an ORDINARY
top-level window, never a message-only window (`HWND_MESSAGE`): a message-only window
cannot be found by `FindWindowW` and does not receive the "TaskbarCreated" broadcast at
all, and this module's whole cross-process "raise the window" feature
(`agent/single_instance.py`'s `raise_existing_window`) depends on `FindWindowW` finding
this exact window by its class name.

LOGON TIMING
--------------------------------------------------------------------------
Explorer, and therefore the taskbar notification area, is often not ready yet at the
moment a `Run`-value autostart program starts — `Shell_NotifyIconW(NIM_ADD)` commonly
fails during logon for exactly that reason. This module never treats one failed
`NIM_ADD` as fatal: it retries on a 2-second `WM_TIMER` for up to two minutes, and
re-adds the icon whenever it sees Explorer's own "TaskbarCreated" message (which fires
after an Explorer restart too, not only at logon). `start()` reports success as soon as
the hidden window exists — it does not wait for `NIM_ADD` to succeed — so a slow logon
never makes this program flash its window open before quietly retrying in the
background; only if `NIM_ADD` never succeeds at all does this module give up and tell
`agent/main.py` (via `CMD_TRAY_FAILED`) to show the window instead.

WHAT THIS NEVER DOES
--------------------------------------------------------------------------
This module never ends, restarts or closes any process (its own `stop()` method closes
only its OWN hidden window and message loop — never named "terminate", on purpose), and
it never shows, hides, moves or otherwise touches any OTHER program's window, the
MetaTrader 5 terminal's included.

OFF-WINDOWS
--------------------------------------------------------------------------
Every Win32 handle here degrades to `None` at import time off Windows (mirroring
`agent/single_instance.py`'s identical pattern), and `TrayIcon.start()` simply returns
`False` in that case — the pure helpers below (`clamp_text`, `menu_entries`,
`notify_action`) have no Windows dependency at all and run anywhere.

THE TRAY ICON'S OWN IMAGE (owner follow-up to quick 260926-ieo)
--------------------------------------------------------------------------
`--windows-icon-from-ico=agent/assets/treedger.ico` (added to `NUITKA_FLAGS` in
`agent/packaging/github-workflows/build-release.yml` and `probe-window.yml`) EMBEDS the
icon into the built `Treedger.exe`'s own resources — it does NOT ship
`agent/assets/treedger.ico` as a file next to the built executable, and
`--include-package=agent` carries Python MODULES only, never a `.ico` asset. A frozen
build therefore has no `agent/assets/treedger.ico` on disk to open at all, and must load
the icon from the exe's own embedded resource instead
(`ExtractIconExW(sys.executable, ...)`, exactly what a real installed copy's Explorer/
Start-menu/taskbar icon already comes from). Running from source (`python -m agent.main`,
no build, no embedded resource) is the opposite case: there the on-disk
`agent/assets/treedger.ico` is the only source, loaded via `LoadImageW(...,
LR_LOADFROMFILE)` at the small-icon size (`GetSystemMetrics(SM_CXSMICON)`). `_load_icon`
picks exactly one of these two paths, using the SAME `getattr(sys, "frozen", False)`
check `agent/autostart.py`'s `own_executable_path()` already uses to tell a Nuitka
`--standalone` build from a source run — never re-invented here. Either path falls back
to the generic `IDI_APPLICATION` icon on any failure, and every `HICON` this class
creates (extracted, file-loaded, or the fallback) is released via `DestroyIcon` in
`_cleanup()` when the tray stops.
"""
from __future__ import annotations

import ctypes
import logging
import pathlib
import queue
import sys
import threading
from ctypes import wintypes
from typing import Callable, NamedTuple, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public identifiers
# ---------------------------------------------------------------------------
TRAY_WINDOW_CLASS = "TreedgerAgentTrayWindow"
SHOW_WINDOW_MESSAGE_NAME = "TreedgerAgent.ShowWindow"

CMD_SYNC_NOW = "sync_now"
CMD_OPEN_WINDOW = "open_window"
CMD_OPEN_SITE = "open_site"
CMD_TOGGLE_SOUNDS = "toggle_sounds"
CMD_EXIT = "exit"
CMD_TRAY_FAILED = "tray_failed"

TOOLTIP_MAX_CHARS = 127
BALLOON_TITLE_MAX_CHARS = 63
BALLOON_TEXT_MAX_CHARS = 255

_SOUND_LABEL_MUTED = "Звуки MT5: выкл"
_SOUND_LABEL_UNMUTED = "Звуки MT5: вкл"
_LABEL_SYNC_NOW = "Синхронизировать сейчас"
_LABEL_OPEN_WINDOW = "Открыть окно"
_LABEL_OPEN_SITE = "Открыть Treedger"
_LABEL_EXIT = "Выход"

# ---------------------------------------------------------------------------
# Win32 constants — this module's own private copy; never shared with
# agent/single_instance.py or agent/terminal_process.py's WinDLL instances.
# ---------------------------------------------------------------------------
_ERROR_CLASS_ALREADY_EXISTS = 1410
_WS_EX_TOOLWINDOW = 0x00000080

_NIM_ADD = 0
_NIM_MODIFY = 1
_NIM_DELETE = 2
_NIF_MESSAGE = 0x00000001
_NIF_ICON = 0x00000002
_NIF_TIP = 0x00000004
_NIF_INFO = 0x00000010
_NIIF_WARNING = 0x00000002

_WM_NULL = 0x0000
_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_WM_CONTEXTMENU = 0x007B
_WM_TIMER = 0x0113
_WM_LBUTTONUP = 0x0202
_WM_RBUTTONUP = 0x0205
_WM_APP = 0x8000
_WM_TRAY_CALLBACK = _WM_APP + 1
_WM_WAKE = _WM_APP + 2
_NIN_BALLOONUSERCLICK = 0x0400 + 5

_MF_STRING = 0x00000000
_MF_CHECKED = 0x00000008
_MF_SEPARATOR = 0x00000800
_TPM_RIGHTBUTTON = 0x0002
_TPM_NONOTIFY = 0x0080
_TPM_RETURNCMD = 0x0100

_IDI_APPLICATION = 32512

# Owner follow-up to quick 260926-ieo — the tray's own icon image. See the module
# docstring's "THE TRAY ICON'S OWN IMAGE" section for why the loading path branches on
# frozen vs. from-source.
_SM_CXSMICON = 49
_SM_CYSMICON = 50
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x00000010

ASSET_ICON_PATH = pathlib.Path(__file__).resolve().parent / "assets" / "treedger.ico"
"""The on-disk icon this program uses when running from source — never read at all in a
frozen (Nuitka `--standalone`) build, which embeds this same file's contents into
`Treedger.exe`'s own resources at build time via `--windows-icon-from-ico` instead (see
module docstring). Public (no leading underscore) so `agent/tests/test_tray.py` and the
packaging tests can locate it without duplicating this path calculation."""

_ADD_RETRY_INTERVAL_MS = 2000
_ADD_RETRY_MAX_ATTEMPTS = 60  # ~2 minutes at 2 s each
_TIMER_ID = 1

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


# ---------------------------------------------------------------------------
# Pure helpers — no Windows dependency, run anywhere.
# ---------------------------------------------------------------------------


class MenuEntry(NamedTuple):
    command: "Optional[str]"
    label: str
    checked: bool = False


def clamp_text(text: str, limit: int) -> str:
    """`text` unchanged when it already fits `limit`; otherwise truncated to exactly
    `limit` characters, ending in a single «…»."""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + "…"


def menu_entries(sounds_muted: bool) -> "list[MenuEntry]":
    """
    The tray menu, in the exact fixed order OR-1/OR-12 specify. Only the sound item is
    ever `checked`; `checked=True` means "sounds are ON" (i.e. NOT muted) — the tick
    reflects the state a click would move away from, matching a normal checkable menu
    item's convention, while the LABEL always describes the CURRENT state in words.
    """
    sound_label = _SOUND_LABEL_MUTED if sounds_muted else _SOUND_LABEL_UNMUTED
    return [
        MenuEntry(CMD_SYNC_NOW, _LABEL_SYNC_NOW),
        MenuEntry(CMD_OPEN_WINDOW, _LABEL_OPEN_WINDOW),
        MenuEntry(CMD_OPEN_SITE, _LABEL_OPEN_SITE),
        MenuEntry(CMD_TOGGLE_SOUNDS, sound_label, checked=not sounds_muted),
        MenuEntry(None, ""),  # separator
        MenuEntry(CMD_EXIT, _LABEL_EXIT),
    ]


def notify_action(event: int) -> "Optional[str]":
    """
    What a `Shell_NotifyIconW` callback's low-word event means: a left click or a
    balloon click both mean "bring the window up"; a right click or the keyboard
    context-menu key both mean "show the menu"; anything else (mouse move, etc.) means
    nothing.
    """
    if event in (_WM_LBUTTONUP, _NIN_BALLOONUSERCLICK):
        return CMD_OPEN_WINDOW
    if event in (_WM_RBUTTONUP, _WM_CONTEXTMENU):
        return "show_menu"
    return None


# ---------------------------------------------------------------------------
# Win32 handles — this module's OWN WinDLL instances (never shared with
# agent/single_instance.py or agent/terminal_process.py, per this quick task's own
# interface note on argtypes collisions).
# ---------------------------------------------------------------------------
try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
except AttributeError:
    # Non-Windows platform — see agent/single_instance.py's identical pattern.
    _user32 = None  # type: ignore[assignment]
    _shell32 = None  # type: ignore[assignment]
    _kernel32 = None  # type: ignore[assignment]


class _GUID(ctypes.Structure):
    """This module's own tiny GUID structure — `NOTIFYICONDATAW.guidItem` is never
    populated (this program never uses per-icon GUID identity), but the struct must
    still be the right size for `NOTIFYICONDATAW`'s own layout to be correct."""

    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


if _user32 is not None:
    _user32.RegisterClassExW.restype = wintypes.ATOM
    _user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    _user32.UnregisterClassW.restype = wintypes.BOOL
    _user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.DefWindowProcW.restype = LRESULT
    _user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    _user32.TranslateMessage.restype = wintypes.BOOL
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    _user32.DispatchMessageW.restype = LRESULT
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    _user32.PostMessageW.restype = wintypes.BOOL
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.PostQuitMessage.restype = None
    _user32.PostQuitMessage.argtypes = [ctypes.c_int]
    _user32.RegisterWindowMessageW.restype = wintypes.UINT
    _user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    _user32.SetTimer.restype = ctypes.c_size_t
    _user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
    _user32.KillTimer.restype = wintypes.BOOL
    _user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    _user32.CreatePopupMenu.restype = wintypes.HMENU
    _user32.CreatePopupMenu.argtypes = []
    _user32.AppendMenuW.restype = wintypes.BOOL
    _user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    _user32.TrackPopupMenu.restype = wintypes.BOOL
    _user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p,
    ]
    _user32.DestroyMenu.restype = wintypes.BOOL
    _user32.DestroyMenu.argtypes = [wintypes.HMENU]
    _user32.GetCursorPos.restype = wintypes.BOOL
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.LoadIconW.restype = wintypes.HICON
    _user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    _user32.DestroyIcon.restype = wintypes.BOOL
    _user32.DestroyIcon.argtypes = [wintypes.HICON]
    _user32.GetSystemMetrics.restype = ctypes.c_int
    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.LoadImageW.restype = wintypes.HANDLE
    _user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]

if _shell32 is not None:
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    _shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    _shell32.ExtractIconExW.restype = wintypes.UINT
    _shell32.ExtractIconExW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON), wintypes.UINT,
    ]

if _kernel32 is not None:
    _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


def _is_frozen_build() -> bool:
    """True inside the Nuitka `--standalone` build, False when run from source.

    Checks BOTH markers: `sys.frozen` (the convention `agent/autostart.py` relies on)
    and `__compiled__`, the name Nuitka itself documents as its runtime marker and
    defines in every compiled module's globals. Whether Nuitka also sets `sys.frozen`
    for `--standalone` has never been observed in this project's build evidence, and
    guessing wrong here would send the installed build to the `.ico` file it does not
    ship, silently showing the generic icon instead of the Treedger "T".
    """
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


class TrayIcon:
    """
    One tray icon, owning one daemon thread — see the module docstring for the full
    threading contract. `command_sink` is called ON THE TRAY THREAD; it must only
    enqueue, never touch Tk directly (see `agent/main.py`'s `tray_command_sink`).
    """

    def __init__(
        self, command_sink: "Callable[[str], None]", *, tooltip: str = "Treedger", sounds_muted: bool = True
    ) -> None:
        self._command_sink = command_sink
        self._sounds_muted = sounds_muted
        self._lock = threading.Lock()
        self._updates: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._hwnd: "Optional[int]" = None
        self._icon_handle: "Optional[int]" = None
        self._icon_extracted = False
        self._icon_added = False
        self._failed_attempts = 0
        self._ready = threading.Event()
        self._start_ok = False
        self._thread: "Optional[threading.Thread]" = None
        # Kept alive for the object's whole life — ctypes does not keep a reference to
        # a callback wrapped in a WINFUNCTYPE on its own, and a garbage-collected
        # callback means Windows calls freed memory the next time it fires.
        self._wndproc = None
        self._taskbar_created_msg = 0
        self._show_window_msg = 0
        self._current_tooltip = tooltip

    # -----------------------------------------------------------------
    # Public API — called from the Tk main thread.
    # -----------------------------------------------------------------
    def start(self, timeout_seconds: float = 5.0) -> bool:
        if _user32 is None or _shell32 is None or _kernel32 is None:
            return False
        self._thread = threading.Thread(target=self._run, name="treedger-tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout_seconds)
        return self._start_ok

    def set_tooltip(self, text: str) -> None:
        if self._hwnd is None:
            return
        self._updates.put(("tooltip", text))
        self._post_wake()

    def show_balloon(self, title: str, text: str) -> None:
        if self._hwnd is None:
            return
        self._updates.put(("balloon", (title, text)))
        self._post_wake()

    def set_sounds_muted(self, muted: bool) -> None:
        with self._lock:
            self._sounds_muted = muted

    def stop(self, timeout_seconds: float = 3.0) -> None:
        if self._hwnd is not None:
            try:
                _user32.PostMessageW(self._hwnd, _WM_CLOSE, 0, 0)
            except Exception:  # noqa: BLE001 — stop() must never raise
                logger.debug("tray: stop() post failed", exc_info=True)
        if self._thread is not None:
            self._thread.join(timeout_seconds)

    def _post_wake(self) -> None:
        try:
            _user32.PostMessageW(self._hwnd, _WM_WAKE, 0, 0)
        except Exception:  # noqa: BLE001 — a missed wake just waits for the next update
            logger.debug("tray: wake post failed", exc_info=True)

    # -----------------------------------------------------------------
    # Tray thread — everything below this line runs ONLY on `self._thread`.
    # -----------------------------------------------------------------
    def _run(self) -> None:
        try:
            h_instance = _kernel32.GetModuleHandleW(None)
            self._wndproc = WNDPROC(self._wndproc_callback)

            wc = WNDCLASSEXW()
            ctypes.memset(ctypes.byref(wc), 0, ctypes.sizeof(wc))
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = h_instance
            wc.lpszClassName = TRAY_WINDOW_CLASS
            atom = _user32.RegisterClassExW(ctypes.byref(wc))
            if not atom and ctypes.get_last_error() != _ERROR_CLASS_ALREADY_EXISTS:
                logger.warning("tray: RegisterClassExW failed: %s", ctypes.get_last_error())
                self._start_ok = False
                self._ready.set()
                return

            hwnd = _user32.CreateWindowExW(
                _WS_EX_TOOLWINDOW, TRAY_WINDOW_CLASS, "Treedger tray", 0,
                0, 0, 0, 0, None, None, h_instance, None,
            )
            if not hwnd:
                logger.warning("tray: CreateWindowExW failed: %s", ctypes.get_last_error())
                self._start_ok = False
                self._ready.set()
                return
            self._hwnd = hwnd

            self._taskbar_created_msg = _user32.RegisterWindowMessageW("TaskbarCreated")
            self._show_window_msg = _user32.RegisterWindowMessageW(SHOW_WINDOW_MESSAGE_NAME)
            self._icon_handle = self._load_icon(h_instance)

            self._start_ok = True
            self._ready.set()

            if not self._try_add_icon():
                _user32.SetTimer(hwnd, _TIMER_ID, _ADD_RETRY_INTERVAL_MS, None)

            self._message_loop()
        except Exception:  # noqa: BLE001 — the tray thread must never crash the process
            logger.exception("tray: thread body failed")
            self._start_ok = False
            self._ready.set()
        finally:
            self._cleanup()

    def _message_loop(self) -> None:
        msg = MSG()
        while True:
            result = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result <= 0:
                break
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    def _cleanup(self) -> None:
        try:
            if self._icon_added and self._hwnd is not None:
                data = self._build_notify_icon_data()
                _shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(data))
        except Exception:  # noqa: BLE001
            logger.debug("tray: icon cleanup failed", exc_info=True)
        try:
            _user32.UnregisterClassW(TRAY_WINDOW_CLASS, _kernel32.GetModuleHandleW(None))
        except Exception:  # noqa: BLE001
            logger.debug("tray: class cleanup failed", exc_info=True)
        if self._icon_extracted and self._icon_handle:
            try:
                _user32.DestroyIcon(self._icon_handle)
            except Exception:  # noqa: BLE001
                pass

    def _load_icon(self, h_instance: int) -> "Optional[int]":
        """
        Branches on the SAME frozen-detection `agent/autostart.py`'s own
        `own_executable_path()` already uses, per the module docstring's "THE TRAY
        ICON'S OWN IMAGE" section: a frozen build loads the icon Nuitka embedded into
        `Treedger.exe`'s own resources; running from source loads
        `ASSET_ICON_PATH` from disk. Neither path is ever attempted in the other mode —
        a frozen build has no `agent/assets/treedger.ico` on disk to open at all
        (`--windows-icon-from-ico` embeds, it does not ship the file), and a from-source
        run's own interpreter executable (`python.exe`/`pythonw.exe`) carries no
        Treedger-branded resource to extract. Either branch falls back to the generic
        `IDI_APPLICATION` icon on any failure.
        """
        self._icon_extracted = False
        if _is_frozen_build():
            icon = self._load_icon_from_exe_resource()
        else:
            icon = self._load_icon_from_asset_file()
        if icon is not None:
            return icon
        return self._load_fallback_application_icon()

    def _load_icon_from_exe_resource(self) -> "Optional[int]":
        """The first icon embedded in `sys.executable` (never `sys.argv` — see F4/F12)
        — in a frozen build this IS the icon `--windows-icon-from-ico` embedded, the
        same resource Explorer/the Start menu/the taskbar already show for this exe."""
        try:
            large = wintypes.HICON()
            small = wintypes.HICON()
            count = _shell32.ExtractIconExW(sys.executable, 0, ctypes.byref(large), ctypes.byref(small), 1)
            if count and small.value:
                if large.value:
                    _user32.DestroyIcon(large.value)
                self._icon_extracted = True
                return small.value
            if large.value:
                _user32.DestroyIcon(large.value)
        except Exception:  # noqa: BLE001
            logger.debug("tray: exe-resource icon extraction failed", exc_info=True)
        return None

    def _load_icon_from_asset_file(self) -> "Optional[int]":
        """`ASSET_ICON_PATH` loaded from disk at the small-icon size — the only source
        available when running from source (no embedded exe resource exists)."""
        try:
            cx = _user32.GetSystemMetrics(_SM_CXSMICON)
            cy = _user32.GetSystemMetrics(_SM_CYSMICON)
            handle = _user32.LoadImageW(
                None, str(ASSET_ICON_PATH), _IMAGE_ICON, cx, cy, _LR_LOADFROMFILE
            )
            if handle:
                self._icon_extracted = True
                return handle
        except Exception:  # noqa: BLE001
            logger.debug("tray: asset-file icon load failed", exc_info=True)
        return None

    def _load_fallback_application_icon(self) -> "Optional[int]":
        self._icon_extracted = False
        try:
            return _user32.LoadIconW(None, ctypes.c_void_p(_IDI_APPLICATION))
        except Exception:  # noqa: BLE001
            return None

    def _try_add_icon(self) -> bool:
        try:
            data = self._build_notify_icon_data(with_tip=True)
            ok = bool(_shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(data)))
        except Exception:  # noqa: BLE001
            ok = False
        self._icon_added = ok
        return ok

    def _build_notify_icon_data(
        self, *, with_tip: bool = False, with_balloon: "Optional[tuple[str, str]]" = None
    ) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        ctypes.memset(ctypes.byref(data), 0, ctypes.sizeof(data))
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uCallbackMessage = _WM_TRAY_CALLBACK
        flags = _NIF_MESSAGE
        if self._icon_handle:
            data.hIcon = self._icon_handle
            flags |= _NIF_ICON
        if with_tip:
            flags |= _NIF_TIP
            data.szTip = clamp_text(self._current_tooltip, TOOLTIP_MAX_CHARS)
        if with_balloon is not None:
            title, text = with_balloon
            flags |= _NIF_INFO
            data.szInfoTitle = clamp_text(title, BALLOON_TITLE_MAX_CHARS)
            data.szInfo = clamp_text(text, BALLOON_TEXT_MAX_CHARS)
            data.dwInfoFlags = _NIIF_WARNING
        data.uFlags = flags
        return data

    def _wndproc_callback(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        try:
            if self._taskbar_created_msg and msg == self._taskbar_created_msg:
                self._icon_added = False
                self._try_add_icon()
                return 0
            if self._show_window_msg and msg == self._show_window_msg:
                self._safe_sink(CMD_OPEN_WINDOW)
                return 0
            if msg == _WM_TRAY_CALLBACK:
                action = notify_action(lparam & 0xFFFF)
                if action == CMD_OPEN_WINDOW:
                    self._safe_sink(CMD_OPEN_WINDOW)
                elif action == "show_menu":
                    self._show_menu(hwnd)
                return 0
            if msg == _WM_WAKE:
                self._drain_updates()
                return 0
            if msg == _WM_TIMER:
                self._on_timer(hwnd)
                return 0
            if msg == _WM_CLOSE:
                self._on_close(hwnd)
                return 0
            if msg == _WM_DESTROY:
                _user32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001 — the window procedure must never raise
            logger.exception("tray: wndproc failed for msg=%s", msg)
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_timer(self, hwnd: int) -> None:
        if self._icon_added:
            return
        self._failed_attempts += 1
        if self._try_add_icon():
            _user32.KillTimer(hwnd, _TIMER_ID)
        elif self._failed_attempts >= _ADD_RETRY_MAX_ATTEMPTS:
            _user32.KillTimer(hwnd, _TIMER_ID)
            self._safe_sink(CMD_TRAY_FAILED)

    def _on_close(self, hwnd: int) -> None:
        if self._icon_added:
            try:
                data = self._build_notify_icon_data()
                _shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(data))
            except Exception:  # noqa: BLE001
                logger.debug("tray: icon delete on close failed", exc_info=True)
            self._icon_added = False
        _user32.DestroyWindow(hwnd)

    def _drain_updates(self) -> None:
        tip: "Optional[str]" = None
        balloon: "Optional[tuple[str, str]]" = None
        try:
            while True:
                kind, payload = self._updates.get_nowait()
                if kind == "tooltip":
                    tip = payload  # type: ignore[assignment]
                elif kind == "balloon":
                    balloon = payload  # type: ignore[assignment]
        except queue.Empty:
            pass
        if not self._icon_added:
            return
        if tip is not None:
            self._current_tooltip = tip
            try:
                data = self._build_notify_icon_data(with_tip=True)
                _shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(data))
            except Exception:  # noqa: BLE001
                logger.debug("tray: tooltip update failed", exc_info=True)
        if balloon is not None:
            try:
                data = self._build_notify_icon_data(with_balloon=balloon)
                _shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(data))
            except Exception:  # noqa: BLE001
                logger.debug("tray: balloon update failed", exc_info=True)

    def _show_menu(self, hwnd: int) -> None:
        with self._lock:
            sounds_muted = self._sounds_muted
        entries = menu_entries(sounds_muted)
        menu = _user32.CreatePopupMenu()
        if not menu:
            return
        try:
            id_map: "dict[int, str]" = {}
            next_id = 1
            for entry in entries:
                if entry.command is None:
                    _user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)
                    continue
                flags = _MF_STRING | (_MF_CHECKED if entry.checked else 0)
                _user32.AppendMenuW(menu, flags, next_id, entry.label)
                id_map[next_id] = entry.command
                next_id += 1

            _user32.SetForegroundWindow(hwnd)
            point = POINT()
            _user32.GetCursorPos(ctypes.byref(point))
            chosen = _user32.TrackPopupMenu(
                menu, _TPM_RIGHTBUTTON | _TPM_RETURNCMD | _TPM_NONOTIFY, point.x, point.y, 0, hwnd, None,
            )
            # Required by the documented TrackPopupMenu pattern: without this, the
            # menu can fail to close correctly the next time it is invoked.
            _user32.PostMessageW(hwnd, _WM_NULL, 0, 0)
            command = id_map.get(chosen)
            if command:
                self._safe_sink(command)
        finally:
            _user32.DestroyMenu(menu)

    def _safe_sink(self, command: str) -> None:
        try:
            self._command_sink(command)
        except Exception:  # noqa: BLE001 — the tray thread must never crash on this
            logger.exception("tray: command sink raised for %s", command)
