"""
agent/errors.py — LOGIC only. Reads `agent/error_codes.py`'s table; contains no MT5
error-code integer literal of its own (grep-asserted by this plan's own verify
command). Every classification decision in this file traces back to that one table
— never to a re-typed code.

`classify_login_error(code)` is a thin lookup. `classify_outcome(...)` maps one
login/sync attempt onto one of the SIX outcomes an agent is ever allowed to report
over the wire — never onto the system-wide seventh outcome, which is reserved for
the owner's own browser session (an explicit "this account is closed" action taken
in their own browser, never inferred by this program) and can never be produced from
this module. See the comment on `classify_outcome()`'s
AUTH_FAILED branch for what used to live here and why it was deleted rather than
merely left unused.

Wire-contract mirror
---------------------
The six string literals this module returns from `classify_outcome()` MUST exactly
match `AGENT_REPORTABLE_OUTCOMES` in `src/lib/api/agent/contract.ts` — the
agent-facing SUBSET of that file's own `AGENT_ACCOUNT_OUTCOMES` (which additionally
carries the owner-only seventh outcome this module can never produce). That
TypeScript file is the canonical source of truth for the wire shape, and its own
header already states the transcription-drift risk between it and the Python side
of this boundary. If a value is ever added, removed or renamed on either side,
mirror the change here in the same change.
"""
from __future__ import annotations

from typing import Optional

from agent.error_codes import DEFAULT_CATEGORY, MT5_ERROR_CODES, ErrorCategory

# ---------------------------------------------------------------------------
# The six outcomes `classify_outcome()` may return — mirrors
# src/lib/api/agent/contract.ts's AGENT_REPORTABLE_OUTCOMES verbatim. NOT the
# same list as that file's AGENT_ACCOUNT_OUTCOMES, which additionally carries
# a seventh, owner-only outcome no agent may ever report. Do not
# add, remove or rename a value here without touching that file too.
# ---------------------------------------------------------------------------
OUTCOME_OK = "ok"
OUTCOME_AUTH_FAILED = "auth_failed"
OUTCOME_SERVER_UNAVAILABLE = "server_unavailable"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_ALGOTRADING_DISABLED = "algotrading_disabled"
OUTCOME_INTERNAL = "internal"


def classify_login_error(code: Optional[int]) -> ErrorCategory:
    """
    Look up `code` in `error_codes.MT5_ERROR_CODES`, falling back to
    `DEFAULT_CATEGORY` for `None` or any code the table has never seen. No
    `if code == -6` (or any other literal) anywhere in this function or this file —
    the table in `error_codes.py` is the only place that comparison is made.
    """
    if code is None:
        return DEFAULT_CATEGORY
    info = MT5_ERROR_CODES.get(code)
    return info.category if info is not None else DEFAULT_CATEGORY


def classify_outcome(
    *,
    login_succeeded: bool,
    error_code: Optional[int] = None,
    consecutive_auth_failures: int = 0,
    has_synced_successfully_before: bool = False,
) -> str:
    """
    Classify the result of one account's login/sync attempt into one of the six
    outcomes an agent is ever allowed to report (`AGENT_REPORTABLE_OUTCOMES`,
    src/lib/api/agent/contract.ts).

    Args:
        login_succeeded: True if `mt5_bridge.login_account()` returned True this run.
        error_code: the `mt5.last_error()` code from this run's failed login attempt,
            if any. Ignored when `login_succeeded` is True.
        consecutive_auth_failures, has_synced_successfully_before: accepted but
            IGNORED — see the comment on the AUTH_FAILED branch below for why they
            still exist as parameters here at all. Neither carries any signal into
            this function's return value anymore.
    """
    if login_succeeded:
        return OUTCOME_OK

    category = classify_login_error(error_code)

    if category is ErrorCategory.AUTH_FAILED:
        # --------------------------------------------------------------------
        # REMOVED — read before "fixing"
        # this by feeding a real count into the two unused parameters above.
        #
        # This branch used to escalate to the `broker_closed` outcome once
        # `consecutive_auth_failures` crossed a threshold (`LOGIN_FAILURE_
        # THRESHOLD`, since deleted) AND `has_synced_successfully_before` was
        # True — a sustained-failure heuristic guessing that the broker, not
        # the owner, had changed something. The owner REJECTED that heuristic
        # outright: at the MT5 level a broker-closed account and an account
        # whose owner simply changed their broker password are
        # INDISTINGUISHABLE — both surface as a run of AUTH_FAILED
        # classifications on a previously-working account. A changed
        # password is far more common than a real closure, and guessing
        # wrong is one-directional: `broker_closed` stops polling an account
        # FOREVER, while the plain `auth_failed` outcome below just retries
        # on the very next run.
        #
        # The heuristic is DELETED here, not merely left unfed by its one
        # caller (`agent/sync.py`, which always passes
        # `consecutive_auth_failures=0, has_synced_successfully_before=False`
        # into it regardless) — leaving dead-but-callable retirement logic
        # around is exactly the guarantee-by-convention the owner already
        # rejected one level down, at the error-code table itself
        # (`error_codes.py`'s `ErrorCategory` has no matching member either,
        # by the same reasoning).
        #
        # `consecutive_auth_failures`/`has_synced_successfully_before` remain
        # as accepted-and-ignored parameters ONLY because `sync.py`'s single
        # call site still passes them by keyword — a future change that also
        # touches `sync.py` should delete
        # both the parameters and that call site's arguments together, in the
        # same change, for a clean signature.
        #
        # Account lifecycle now belongs entirely to the owner, exercised in
        # their own browser session (a dedicated "mark this account closed"
        # route, reachable only from the owner's own logged-in session) —
        # never guessed here, and never appliable via an agent token either
        # (the server-side ingest path refuses a `broker_closed` outcome
        # reported by an agent token even if
        # somehow attempted). The only path back is a live
        # MT5 probe finding a code that reliably tells the two cases apart —
        # that would be a NEW decision for the owner to make, not a reason to
        # quietly restore this. Git history holds the deleted implementation
        # and its tests verbatim if that day comes.
        # --------------------------------------------------------------------
        return OUTCOME_AUTH_FAILED

    if category is ErrorCategory.TIMEOUT:
        return OUTCOME_TIMEOUT

    if category is ErrorCategory.ALGOTRADING_DISABLED:
        return OUTCOME_ALGOTRADING_DISABLED

    if category is ErrorCategory.CONNECTION_ERROR:
        return OUTCOME_SERVER_UNAVAILABLE

    # Unreachable given ErrorCategory's exhaustive membership today — kept as a
    # defensive fallback rather than an assertion, since classify_outcome() must
    # never raise from inside a per-account sync loop (mirrors the donor
    # classifier's own "never raises" contract, error_classification.py).
    return OUTCOME_INTERNAL
