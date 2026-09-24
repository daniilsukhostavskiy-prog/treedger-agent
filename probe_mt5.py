"""
agent/probe_mt5.py — standalone MT5-runtime diagnostic probe.

Run by hand, on the Windows machine where the real MetaTrader 5 terminal is
installed:

    pip install -r agent/requirements.txt
    python agent/probe_mt5.py

WHY THIS SCRIPT EXISTS: this repo's own working error classifier
(mt5-service/services/error_classification.py) and the official MQL5 docs assign
OPPOSITE meanings to some of the same numeric error codes, and NEITHER source names a
code meaning "this account no longer exists on the broker's side" — a closed prop
challenge today files under the exact same category as a wrong password
(39-CONTEXT.md R-01, 39-RESEARCH.md Pitfall 1 / Open Question 1). Writing
`agent/errors.py`'s `broker_closed` detector, or trusting the donor's registry-path
shortcut, on top of that would be guesswork. This script settles those questions
empirically instead. Nothing in `agent/mt5_bridge.py` / `agent/errors.py` (a later,
deliberately blocked plan task) is written until this script's real output is recorded
in `agent/PROBE-RESULTS.md`.

This file is imported by NOTHING in this package — it is a script, not a library.
`agent/main.py` never imports it.

It must run and print something useful even when parts of it fail: a failure IS a
result here, not a bug in the probe. It prints five sections in order:
    1. Registry dump         — every Uninstall-registry hit + find_terminal_path()
    2. AutoTrading gate      — terminal_info().trade_allowed + a real history read
    3. Error codes           — wrong password / wrong server / closed account
    4. Opening-order read    — one real position's full order history (sl / tp)
    5. Pre-existing session  — whatever account was already logged in, captured
                               immediately after initialize() and BEFORE section 3
                               logs in and out of other accounts

SECURITY: this script never prints a password, and never writes anything to disk at
all — there is no file here a password could ever leak into. Every credential is read
via `getpass.getpass()`, which never echoes to the terminal, and is never sourced from
a file or a command-line argument.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from getpass import getpass
from typing import Optional

try:
    from agent.terminal_discovery import enumerate_registry_candidates, find_terminal_path
except ImportError:
    # `python agent/probe_mt5.py` (the invocation README.md/this file's own docstring
    # documents) puts agent/ itself, not the repo root, on sys.path[0] — fall back to
    # the bare module name for that invocation style.
    from terminal_discovery import enumerate_registry_candidates, find_terminal_path  # type: ignore[no-redef]

try:
    import MetaTrader5 as mt5
    _MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore[assignment]
    _MT5_AVAILABLE = False


def _harden_console_encoding() -> None:
    """
    Reconfigure stdout/stderr to UTF-8 with a lossy fallback, so this script never
    crashes mid-run on a Windows console whose default codepage cannot encode the
    Cyrillic UI-term prompts this script prints (e.g. «Алготрейдинг») or the box-
    drawing characters in _print_header(). Without this, an operator whose console
    isn't already UTF-8 could hit a bare UnicodeEncodeError traceback partway
    through — the exact "must run and produce useful output even when parts of it
    fail" requirement this script exists to satisfy, applied to itself.

    `reconfigure()` is available on Python 3.7+'s text-mode streams; guarded for the
    rare case of a redirected/non-reconfigurable stream (e.g. some CI runners).
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _print_header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------------------
# Section 1 — Registry dump
# ---------------------------------------------------------------------------

def _section_1_registry_dump() -> Optional[str]:
    _print_header("SECTION 1 — Registry dump")
    candidates = enumerate_registry_candidates()
    if not candidates:
        print("No 'MetaTrader 5' entry found anywhere in the Windows Uninstall registry.")
    for c in candidates:
        print(f"- [{c['hive']} / {c['view']}] DisplayName={c['display_name']!r}")
        print(f"    InstallLocation={c['install_location']!r}")
        print(f"    terminal64.exe exists on disk: {c['exists']}")

    terminal_path = find_terminal_path()
    print()
    print(f"find_terminal_path() returned: {terminal_path!r}")
    return terminal_path


# ---------------------------------------------------------------------------
# Section 2 — AutoTrading gate (also where the terminal is initialized, and where
# the pre-existing session for Section 5 is captured — see module docstring)
# ---------------------------------------------------------------------------

def _capture_pre_existing_session() -> Optional[dict]:
    """Must only be called AFTER a successful mt5.initialize(), and BEFORE any
    login attempt (Section 3) — this is the input D-23's collision warning depends
    on."""
    account = mt5.account_info()
    if account is None:
        return None
    return {"login": account.login, "server": account.server, "name": account.name}


def _section_2_algo_trading_gate(terminal_path: Optional[str]) -> Optional[dict]:
    _print_header("SECTION 2 — AutoTrading gate")
    print(
        "Run this script TWICE for this section: once with «Алготрейдинг» turned OFF\n"
        "in the terminal's own toolbar, once with it turned ON. Paste both outputs."
    )
    if not _MT5_AVAILABLE:
        print(
            "MetaTrader5 package not installed — skipping sections 2-5.\n"
            "Run: pip install -r agent/requirements.txt"
        )
        return None

    print(f"\nInitializing with path={terminal_path!r} ..." if terminal_path else "\nInitializing with no explicit path (letting MetaTrader5 find its own default) ...")
    ok = mt5.initialize(path=terminal_path) if terminal_path else mt5.initialize()
    if not ok:
        print(f"mt5.initialize() FAILED. last_error()={mt5.last_error()}")
        return None

    # Captured HERE, immediately after a successful initialize() and before any
    # login attempt below or in Section 3 — printed later, in Section 5.
    pre_existing = _capture_pre_existing_session()

    info = mt5.terminal_info()
    if info is None:
        print(f"mt5.terminal_info() returned None. last_error()={mt5.last_error()}")
    else:
        print(f"terminal_info().trade_allowed = {info.trade_allowed}")
        print(f"terminal_info() full = {info._asdict()}")

    now = datetime.now(timezone.utc)
    ninety_days_ago = now - timedelta(days=90)
    try:
        deals = mt5.history_deals_get(ninety_days_ago, now)
    except Exception as exc:  # noqa: BLE001 - a failure here IS a result to record
        print(f"mt5.history_deals_get() RAISED: {exc!r}. last_error()={mt5.last_error()}")
    else:
        if deals is None:
            print(f"mt5.history_deals_get() returned None. last_error()={mt5.last_error()}")
        else:
            print(
                f"mt5.history_deals_get() returned {len(deals)} row(s). "
                f"last_error()={mt5.last_error()}"
            )

    return pre_existing


# ---------------------------------------------------------------------------
# Section 3 — Error codes
# ---------------------------------------------------------------------------

def _attempt_login(login: int, password: str, server: str) -> dict:
    """
    Records the exact evidence the probe cares about — never the password.
    mt5.login() is a boolean-returning API that reports failure via last_error(),
    not exceptions, so this never raises on a failed login.
    """
    login_returned = mt5.login(login, password=password, server=server)
    last_error = mt5.last_error()
    account = mt5.account_info()
    return {
        "login_returned": login_returned,
        "last_error": last_error,
        "account_info_is_none": account is None,
    }


def _prompt_login_attempt(prompt_label: str, password_prompt: str) -> Optional[dict]:
    print(f"\n{prompt_label}")
    try:
        login_raw = input("  MT5 login (account number), or blank to skip: ").strip()
        if not login_raw:
            print("  Skipped — this is itself a recorded result.")
            return None
        server = input("  Server: ").strip()
        password = getpass(f"  {password_prompt} (never echoed): ")
    except (KeyboardInterrupt, EOFError) as exc:
        print(f"  Skipped: {exc!r}")
        return None
    try:
        login = int(login_raw)
    except ValueError:
        print(f"  '{login_raw}' is not a valid integer login — skipped.")
        return None
    return _attempt_login(login, password, server)


def _section_3_error_codes() -> None:
    _print_header("SECTION 3 — Error codes (wrong password / wrong server / closed account)")
    if not _MT5_AVAILABLE:
        print("MetaTrader5 package not installed — skipping.")
        return

    results: dict[str, Optional[dict]] = {
        "wrong_password": _prompt_login_attempt(
            "(a) A REAL account, with a DELIBERATELY WRONG password.",
            "Password (type a WRONG one on purpose)",
        ),
        "wrong_server": _prompt_login_attempt(
            "(b) The SAME (or another) real account, with a WRONG server string.",
            "Password (the REAL one this time)",
        ),
        "closed_account": _prompt_login_attempt(
            "(c) A CLOSED or EXPIRED account, if you have one available.",
            "Password",
        ),
    }

    _print_header("SECTION 3 — Side-by-side comparison")
    labels = {
        "wrong_password": "(a) wrong password  ",
        "wrong_server": "(b) wrong server    ",
        "closed_account": "(c) closed account  ",
    }
    for key, label in labels.items():
        r = results[key]
        if r is None:
            print(f"{label}: NOT OBSERVED (skipped)")
        else:
            print(
                f"{label}: login()={r['login_returned']!r}  "
                f"last_error()={r['last_error']!r}  "
                f"account_info() is None={r['account_info_is_none']!r}"
            )
    print(
        "\nCompare the three last_error() tuples above by eye: does ANY field differ\n"
        "between the closed-account row and the wrong-password row? If not,\n"
        "broker_closed must be implemented as a heuristic, never a code lookup (R-01)."
    )


# ---------------------------------------------------------------------------
# Section 4 — Opening-order read
# ---------------------------------------------------------------------------

def _section_4_opening_order_read() -> None:
    _print_header("SECTION 4 — Opening-order read (initial SL / TP)")
    if not _MT5_AVAILABLE:
        print("MetaTrader5 package not installed — skipping.")
        return

    print(
        "Ideally, pick a position that was opened by a PENDING order (a limit or\n"
        "stop order), if you can identify one — that is the case the earliest-by-\n"
        "time_setup-any-type rule exists to cover correctly."
    )
    try:
        raw = input("Enter a position_id from one real CLOSED trade (blank to skip): ").strip()
    except (KeyboardInterrupt, EOFError):
        raw = ""
    if not raw:
        print("Skipped — this is itself a recorded result.")
        return
    try:
        position_id = int(raw)
    except ValueError:
        print(f"'{raw}' is not a valid integer position_id — skipped.")
        return

    orders = mt5.history_orders_get(position=position_id)
    if orders is None:
        print(
            f"mt5.history_orders_get(position={position_id}) returned None. "
            f"last_error()={mt5.last_error()}"
        )
        return
    if not orders:
        print(f"mt5.history_orders_get(position={position_id}) returned zero orders.")
        return

    ordered = sorted(orders, key=lambda o: o.time_setup)
    print(f"{len(ordered)} order(s) for position {position_id}, sorted by time_setup:")
    for o in ordered:
        print(f"  ticket={o.ticket} type={o.type} time_setup={o.time_setup} sl={o.sl} tp={o.tp}")

    earliest = ordered[0]
    print(
        f"\nEarliest order (ANY type, never filtered to market types 0/1): "
        f"ticket={earliest.ticket} type={earliest.type} sl={earliest.sl} tp={earliest.tp}"
    )


# ---------------------------------------------------------------------------
# Section 5 — Pre-existing session
# ---------------------------------------------------------------------------

def _section_5_pre_existing_session(pre_existing: Optional[dict]) -> None:
    _print_header("SECTION 5 — Pre-existing session (captured before any login attempt)")
    if not _MT5_AVAILABLE:
        print("MetaTrader5 package not installed — skipping.")
        return
    if pre_existing is None:
        print("No account was already logged in when this script attached to the terminal.")
        return
    print(
        f"An account WAS already logged in: login={pre_existing['login']!r} "
        f"server={pre_existing['server']!r} name={pre_existing['name']!r}"
    )
    print(
        "If the login attempts in Section 3 switched you out of it, log back into\n"
        "your own account in the terminal when you are done running this script."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _harden_console_encoding()
    print("Treedger local MT5 sync agent — diagnostic probe")
    print("This script never prints or stores a password. Ctrl+C aborts any prompt safely.")

    terminal_path = _section_1_registry_dump()
    pre_existing = _section_2_algo_trading_gate(terminal_path)
    _section_3_error_codes()
    _section_4_opening_order_read()
    _section_5_pre_existing_session(pre_existing)

    if _MT5_AVAILABLE:
        mt5.shutdown()

    print("\nDone. Paste the sections above (never a password) into the checkpoint response.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted by user (Ctrl+C).")
        sys.exit(1)
