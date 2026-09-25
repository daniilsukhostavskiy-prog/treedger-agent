"""
agent/error_codes.py — the ONE MT5 error-code table.

This module is DATA, not logic: no branching, no classification function, no
MetaTrader5 import, nothing a reader has to execute in their head to audit. Every
consumer (`agent/errors.py`, `agent/mt5_bridge.py`) imports FROM here and never
redeclares an MT5 error-code integer literal of its own — this file holds the ONLY
MT5 error-code literals anywhere in `agent/`.

Why this exists now, without a live probe
------------------------------------------
The owner CANCELLED the live MT5 probe outright, not deferred it: they trade from a
phone, have no MetaTrader 5 installed and an empty registry, and the second
developer — who does have a terminal — receives this code separately, with
`agent/probe_mt5.py` shipped unrun for them to run themselves. So this table
is written now, from the official MQL5 docs and from the donor's own observed
production behaviour (`mt5-service/services/mt5_bridge.py`), and is structured so a
later real probe can REFINE ONE TABLE and change nothing else: refining it means
flipping a row's `source` from ASSUMPTION to CONFIRMED_IN_PRACTICE and, if the
probe's findings disagree with the guess recorded here, correcting that row's
`category`. No consumer of this module may hardcode an MT5 error code of its own.

First real-terminal evidence (2026-09-25, quick 260925-qhs): the owner ran the
packaged v0.2.0 program against a real MetaTrader 5 terminal and a real FTMO account
on their own machine. Three rows changed on that evidence, and only those three: -10005
gained the observation that «Алготрейдинг» OFF blocks the IPC connection itself (and
the `suggests_algotrading_off_at_connect` flag), -6 moved from ASSUMPTION to
CONFIRMED_IN_PRACTICE (it arrived from a broken terminal install, a non-credential
cause), and -10006 records that it was NOT observed. R-01 — whether any code separates
a broker-closed account from a wrong password — is still open: -6 now has positive
evidence that one code covers several unrelated causes.

THE LOAD-BEARING INVARIANT
---------------------------
`ErrorCategory` has NO `broker_closed` member. Not "we avoid mapping to it" — it does
not exist as a category, so no row in `MT5_ERROR_CODES` can produce it and no future
edit to this table can accidentally introduce it. `broker_closed` is a LIFECYCLE
VERDICT reached only by `agent.errors.classify_outcome()`'s sustained-failure
heuristic on a previously-successful account, never a meaning any single error code
can carry.

The reason is an asymmetry, not a style preference: `broker_closed` stops polling an
account FOREVER, while `auth_failed` merely causes a retry on
the next run. A code is therefore never evidence that an account is closed by
itself — only a sustained pattern over time is, and that verdict lives in
`errors.py`, never here. Consequently an unknown code, an ambiguous code, and any
code whose `source` is `ASSUMPTION` all resolve to `CONNECTION_ERROR` or
`AUTH_FAILED` here, never to anything that can retire an account.

`DEFAULT_CATEGORY = ErrorCategory.CONNECTION_ERROR` because it is the ONLY category
that can never feed the `broker_closed` heuristic at any count — strictly safer than
`AUTH_FAILED` for a code this table has never seen.

Evidence labels (per-row `source`), and why honesty here matters more than looking
confident
--------------------------------------------------------------------------------
`ErrorSource.CONFIRMED_IN_PRACTICE` — observed live in this project's own history or
the donor's production history.
`ErrorSource.MQL5_DOCS` — taken from the official MQL5 documentation.
`ErrorSource.ASSUMPTION` — inferred, not verified. This is not a lesser row; it is
the one a future probe is EXPECTED to change, and inflating a row's confidence to
make the table look more finished would only mean nobody re-checks it later.

Research found this repo's own working classifier and the official
MQL5 docs assigning OPPOSITE meanings to the SAME numeric codes (`-6` is `IPC_FAILED`
— connection-class — in the donor's production comments, but `RES_E_AUTH_FAILED` per
the official docs page). Several rows below carry that conflict explicitly in their
`note` rather than picking a side and hiding the disagreement.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ErrorSource(Enum):
    """Where a row's `category` claim comes from. Every row must declare exactly one."""

    CONFIRMED_IN_PRACTICE = "confirmed_in_practice"
    MQL5_DOCS = "mql5_docs"
    ASSUMPTION = "assumption"


class ErrorCategory(Enum):
    """
    Every category an MT5 error code may map to.

    Deliberately has NO `broker_closed` member — see this module's docstring and
    `agent/errors.py`'s. No row in `MT5_ERROR_CODES` below may ever be assigned a
    `broker_closed` meaning, known code, unknown code, or ambiguous code alike; the
    Python type system itself makes that impossible since the member does not exist.
    """

    AUTH_FAILED = "auth_failed"
    CONNECTION_ERROR = "connection_error"
    TIMEOUT = "timeout"
    ALGOTRADING_DISABLED = "algotrading_disabled"


@dataclass(frozen=True)
class ErrorCodeInfo:
    """One row of `MT5_ERROR_CODES`. `note` must never be empty — it is the reasoning
    a future probe (or a future reader) checks this row against."""

    category: ErrorCategory
    source: ErrorSource
    note: str
    # Marks a code that, when `mt5.initialize()` ITSELF fails with it, most often means
    # «Алготрейдинг» (Algo Trading) is switched off in the terminal. A HINT for the
    # user-facing message only — never a category change and never a lifecycle
    # verdict: the same code can also come from, e.g., a slow cold start.
    suggests_algotrading_off_at_connect: bool = False


# ---------------------------------------------------------------------------
# THE table. Every MT5 integer error code this project knows anything about, plus
# its category, its evidence, and the reasoning behind it. Add rows here — and
# ONLY here — as more codes become known.
# ---------------------------------------------------------------------------
MT5_ERROR_CODES: dict[int, ErrorCodeInfo] = {
    -10004: ErrorCodeInfo(
        category=ErrorCategory.AUTH_FAILED,
        source=ErrorSource.CONFIRMED_IN_PRACTICE,
        note=(
            "AUTHORIZATION_ERROR. The donor's production classifier "
            "(mt5-service/services/mt5_bridge.py AUTH_ERROR_CODES) has carried this "
            "as a hard credential rejection in live use, with no reported "
            "counter-example."
        ),
    ),
    -2: ErrorCodeInfo(
        category=ErrorCategory.AUTH_FAILED,
        source=ErrorSource.CONFIRMED_IN_PRACTICE,
        note=(
            "INVALID_PARAMS. The donor's production classifier groups this with "
            "-10004 as commonly a malformed or wrong login value passed to "
            "mt5.login()."
        ),
    ),
    -10007: ErrorCodeInfo(
        category=ErrorCategory.TIMEOUT,
        source=ErrorSource.CONFIRMED_IN_PRACTICE,
        note=(
            "CONNECTION_TIMEOUT — the donor's own comment calls this "
            "'community-confirmed'; carried over unchanged, no conflicting source "
            "found for this specific code."
        ),
    ),
    -10008: ErrorCodeInfo(
        category=ErrorCategory.TIMEOUT,
        source=ErrorSource.ASSUMPTION,
        note=(
            "SEND_FAILED. The donor's production classifier groups this alongside "
            "-10007 as timeout-class, but no live confirmation exists in this "
            "project specifically for -10008 — carried over as an honest "
            "assumption, not a confirmed fact, pending the second developer's probe."
        ),
    ),
    -6: ErrorCodeInfo(
        category=ErrorCategory.CONNECTION_ERROR,
        source=ErrorSource.CONFIRMED_IN_PRACTICE,
        note=(
            "Observed live 2026-09-25 (v0.2.0, the owner's machine) as "
            "(-6, 'Terminal: Authorization failed') in 0.0 s, right after "
            "«Алготрейдинг» was enabled. The cause was a BROKEN TERMINAL INSTALL: a "
            "plain MetaQuotes build that could not reach FTMO-Server4 — zero "
            "traffic, not one line in the terminal's own journal; after reinstalling "
            "MT5 the same credentials worked first time. So -6 arrives for a "
            "non-credential cause, and the recoverable CONNECTION_ERROR side chosen "
            "here (the donor calls it IPC_FAILED) is confirmed right. The official "
            "MQL5 docs still name this code RES_E_AUTH_FAILED, so a broker-closed "
            "account, a wrong password and a broken install remain indistinguishable "
            "by code — R-01 is unresolved, now with positive evidence, and the "
            "never-automate-broker_closed rule (D-29..D-36) stands."
        ),
    ),
    -10001: ErrorCodeInfo(
        category=ErrorCategory.CONNECTION_ERROR,
        source=ErrorSource.CONFIRMED_IN_PRACTICE,
        note=(
            "NO_CONNECT. The donor's production classifier groups this as an "
            "unreachable broker/terminal IPC condition, connection-class."
        ),
    ),
    -10005: ErrorCodeInfo(
        category=ErrorCategory.CONNECTION_ERROR,
        source=ErrorSource.CONFIRMED_IN_PRACTICE,
        suggests_algotrading_off_at_connect=True,
        note=(
            "Observed live in the donor's own incident history (DEPLOY.md's "
            "'Known Issue: IPC timeout' section, 2026-08-01) as an IPC timeout — "
            "connection-class and recoverable on retry, not a credential problem. "
            "Observed live again 2026-09-25 on the owner's machine with v0.2.0: with "
            "«Алготрейдинг» OFF, mt5.initialize() returned (-10005, 'IPC timeout') "
            "after 20 s (attempt A) and 88 s (attempt B). Algo-trading OFF blocks the "
            "IPC connection itself, not only history reads (Phase 39 §12.1 settled); "
            "-10006 was NOT observed. Hence the connect hint flag on this row."
        ),
    ),
    -10006: ErrorCodeInfo(
        category=ErrorCategory.ALGOTRADING_DISABLED,
        source=ErrorSource.ASSUMPTION,
        note=(
            "The donor's own production comment names this AUTO_TRADING_DISABLED "
            "(mt5-service/services/mt5_bridge.py, filed under a 'wrong server' "
            "inline comment there), while the official MQL5 docs instead name -8 "
            "as RES_E_AUTO_TRADING_DISABLED and are silent on -10006 specifically "
            "— the two sources disagree with each other "
            "about which literal code carries this meaning. Kept here as "
            "ALGOTRADING_DISABLED per this program's own decision to give "
            "AUTO_TRADING_DISABLED its own branch; this row is an "
            "honest assumption, not a confirmed fact, pending the second "
            "developer's probe run once with algo-trading off and once with it on. "
            "Not observed in the 2026-09-25 live run — algo-trading OFF produced "
            "-10005 instead."
        ),
    ),
    -8: ErrorCodeInfo(
        category=ErrorCategory.ALGOTRADING_DISABLED,
        source=ErrorSource.MQL5_DOCS,
        note=(
            "RES_E_AUTO_TRADING_DISABLED per the official MQL5 last_error() docs "
            "page. Not observed live in this project, and the donor's own comment "
            "attaches this exact meaning to -10006 instead — see that row's note "
            "for the same conflict from the other direction."
        ),
    ),
}

# The ONLY category an unknown code may resolve to. Chosen because it is the ONE
# category that can never feed agent.errors.classify_outcome()'s broker_closed
# heuristic at any count — strictly safer than ALGOTRADING_DISABLED or AUTH_FAILED
# for a code this table has never seen.
DEFAULT_CATEGORY: ErrorCategory = ErrorCategory.CONNECTION_ERROR
