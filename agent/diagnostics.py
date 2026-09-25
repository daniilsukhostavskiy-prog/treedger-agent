"""
agent/diagnostics.py — the program's diagnostic log and its uncaught-exception hooks.

WHY THIS FILE EXISTS
--------------------------------------------------------------------------
The released program is a windowed executable with no console. Before this module
existed, every `logging.getLogger(__name__)` call in this package went nowhere at all:
no handler was configured anywhere, so a run that hung or failed on someone else's
computer left no trace of why. This module gives every one of those calls a
destination — a rotating UTF-8 text file — and routes every uncaught exception (main
thread, worker thread, Tk callback) into that same file.

WHERE THE LOG LIVES
--------------------------------------------------------------------------
`%APPDATA%\\TreedgerAgent\\logs\\agent.log` — the `logs/` subfolder of the exact
per-user folder `agent/config_store.py` already owns (reached through its public
`app_data_dir()`, never recomputed here). 1 MB per file, three rotated backups, so the
folder can never grow past about 4 MB. Never under %LOCALAPPDATA% or %TEMP%: the
release gate's runtime observation (`agent/packaging/observe_runtime.ps1`) only counts
a write as allowed under `%APPDATA%\\TreedgerAgent\\`.

WHAT THE LOG NEVER CONTAINS
--------------------------------------------------------------------------
The bearer token, the investor password and the pairing code are never logged by any
module in this package — every log call logs counts, `mt_login`, `broker_server`,
exception class names and MT5's own `(code, text)` error tuples only. The
`urllib3`/`requests` loggers are pinned to WARNING so a DEBUG-level log can never pick
up a request line or header from the HTTP layer either. `agent/tests/test_diagnostics.py`
proves this by grepping the log of a full mocked sync run at DEBUG level for the
secrets it was handed.

This module never raises from any public function: a log folder that cannot be
created (read-only profile, full disk) simply means no file log — the program keeps
running exactly as before.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from agent import api_client, config_store, terminal_process

LOG_SUBDIR = "logs"
LOG_FILENAME = "agent.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3

# Private tag on every handler this module attaches, so a second
# configure_logging() call adds nothing and remove_configured_handlers() removes
# exactly what this module added (and nothing a test harness attached itself).
_HANDLER_TAG = "_treedger_agent_handler"

_QUIET_LOGGERS = ("urllib3", "requests")

logger = logging.getLogger(__name__)

_configured_path: Optional[Path] = None
_hooks_installed = False


class _LocalTimeFormatter(logging.Formatter):
    """Local wall-clock time WITH its UTC offset (e.g. 2026-09-25T14:05:03.120+03:00),
    so a log a user emails in can be lined up against server-side UTC timestamps
    without asking which timezone their computer is in. `time.strftime("%z")` is not
    used because on Windows it yields a timezone NAME, not an offset."""

    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:  # noqa: N802
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")


_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"


def log_dir() -> Path:
    """`config_store.app_data_dir() / "logs"` — the one folder this module writes to."""
    return config_store.app_data_dir() / LOG_SUBDIR


def _our_handlers(root: logging.Logger) -> "list[logging.Handler]":
    return [h for h in root.handlers if getattr(h, _HANDLER_TAG, False)]


def configure_logging(level: int = logging.INFO, directory: Optional[Path] = None) -> Optional[Path]:
    """
    Attach ONE rotating file handler (and, only when a console exists, one stream
    handler) to the root logger. Idempotent: a second call adds nothing and returns
    the already-configured path. Never raises — returns `None` when the folder or the
    file cannot be created, and the program keeps running without a file log.
    """
    global _configured_path

    root = logging.getLogger()
    root.setLevel(level)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    if _our_handlers(root):
        return _configured_path

    formatter = _LocalTimeFormatter(_FORMAT)
    target_dir = directory if directory is not None else log_dir()
    log_path: Optional[Path] = None
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        log_path = target_dir / LOG_FILENAME
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        setattr(file_handler, _HANDLER_TAG, True)
        root.addHandler(file_handler)
    except OSError:
        log_path = None

    # A console-less (windowed) build has no stderr at all — sys.stderr is None there.
    if sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        setattr(stream_handler, _HANDLER_TAG, True)
        root.addHandler(stream_handler)

    _configured_path = log_path
    return log_path


def remove_configured_handlers() -> None:
    """Detach and close every handler `configure_logging()` added (test teardown)."""
    global _configured_path
    root = logging.getLogger()
    for handler in _our_handlers(root):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001 — teardown must never raise
            pass
    _configured_path = None


def log_startup_banner() -> None:
    """
    One block of facts about THIS process, logged once per start: build and protocol
    version, Windows version, the executable path, whether this is a frozen/compiled
    build, the Python version, whether this process is elevated, and where the log
    file is. Deliberately never reads the command line — the structural audit
    (`agent/tests/test_sync.py`) permits the argument vector in exactly two places,
    and neither is here; `sys.executable` is the path of the running binary itself.
    """
    try:
        windows_version: Any = None
        getwindowsversion = getattr(sys, "getwindowsversion", None)
        if getwindowsversion is not None:
            wv = getwindowsversion()
            windows_version = f"{wv.major}.{wv.minor}.{wv.build}"
        logger.info(
            "=== Treedger agent start: build=%s protocol=%s ===",
            api_client.AGENT_BUILD_VERSION,
            api_client.AGENT_PROTOCOL_VERSION,
        )
        logger.info("platform=%s windows_version=%s", platform.platform(), windows_version)
        logger.info(
            "executable=%s frozen=%s compiled=%s python=%s",
            sys.executable,
            bool(getattr(sys, "frozen", False)),
            "__compiled__" in globals(),
            sys.version.split()[0],
        )
        logger.info("process_elevated=%s", terminal_process.current_process_elevated())
        logger.info("log_file=%s", _configured_path)
    except Exception:  # noqa: BLE001 — a banner failure must never stop the program
        logger.exception("startup banner failed")


def install_exception_hooks() -> None:
    """
    Route every uncaught exception into the log: `sys.excepthook` (main thread) and
    `threading.excepthook` (any worker thread). Each wrapper logs CRITICAL with the
    full traceback, then hands off to whichever hook was installed before it, inside a
    try/except that swallows — a failing previous hook must never mask the original
    error. Tk callbacks are covered separately by `log_tk_callback_exception`, which
    `agent/main.py` assigns to `root.report_callback_exception`. Idempotent.
    """
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True

    previous_sys_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def _sys_hook(exc_type, exc_value, exc_tb) -> None:  # type: ignore[no-untyped-def]
        logger.critical("uncaught exception (main thread)", exc_info=(exc_type, exc_value, exc_tb))
        try:
            previous_sys_hook(exc_type, exc_value, exc_tb)
        except Exception:  # noqa: BLE001
            pass

    def _thread_hook(args) -> None:  # type: ignore[no-untyped-def]
        thread_name = getattr(args.thread, "name", "?")
        logger.critical(
            "uncaught exception in thread %s",
            thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        try:
            previous_thread_hook(args)
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook


def log_tk_callback_exception(exc_type, exc_value, exc_tb) -> None:  # type: ignore[no-untyped-def]
    """Assigned to `root.report_callback_exception`: Tk otherwise prints a callback's
    traceback to stderr, which a console-less build does not have."""
    logger.critical("uncaught exception in a Tk callback", exc_info=(exc_type, exc_value, exc_tb))


def open_log_folder() -> bool:
    """
    Open the log folder in Explorer (the «Открыть папку журнала» button). Returns
    False — never raises — on a non-Windows system or when the folder cannot be opened.
    """
    folder = log_dir()
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        logger.warning("open_log_folder: not supported on this platform (%s)", folder)
        return False
    try:
        folder.mkdir(parents=True, exist_ok=True)
        startfile(str(folder))
        return True
    except OSError as exc:
        logger.warning("open_log_folder failed for %s: %s", folder, exc)
        return False
