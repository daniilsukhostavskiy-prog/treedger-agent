"""
agent/api_client.py — the ONLY module in this package that speaks HTTP.

This file is the Python TRANSCRIPTION of `src/lib/api/agent/contract.ts` — that
TypeScript file is the canonical source of truth for the wire shape (field names,
types, nullability, the seven outcome strings). This module copies its field names
VERBATIM, never paraphrased. If `contract.ts` changes, this file changes in the same
commit — its own file header says so, and this docstring repeats it because a Python
reader of this file will never see that TypeScript header.

TLS — NOT CONFIGURABLE, READ BEFORE "FIXING" A CONNECTION ERROR
------------------------------------------------------------------
Every request in this module goes out over `requests`' own default HTTPS behaviour,
which verifies the server's TLS certificate. No function in this file accepts a
"verify" argument, no call below ever passes one, and no TLS warning is ever
suppressed. This is deliberate and permanent (PHASE-LOCAL-SYNC-SPEC.md §8) — a
certificate-verification bypass here would let a network-position attacker read (and
rewrite) every investor password this program ever receives. Do not add a `verify=`
parameter to this file "to work around a corporate proxy" or any other reason; that is
an architectural decision, not a bug to patch around.

JSON ONLY — NO OBJECT DESERIALIZATION OF ANY KIND
------------------------------------------------------------------
Every response is parsed with `Response.json()` (the standard library `json` module
under the hood) and NOTHING ELSE. There is no `pickle`, no `yaml.load`, no `eval`, no
`exec` anywhere in this file — a compromised server can, at worst, hand this program a
JSON value; it can never hand it something that executes.

No socket, no `http.server`, no `socketserver` import anywhere in this file. Every
request this module makes is OUTBOUND only.
"""
from __future__ import annotations

from typing import Any, NamedTuple, Optional

import requests

# ---------------------------------------------------------------------------
# Transcribed from src/lib/constants/agent.ts / src/lib/api/agent/contract.ts.
# Integer/string literals only — never re-derived, never guessed.
# ---------------------------------------------------------------------------
AGENT_PROTOCOL_VERSION: int = 1
MAX_TRADES_PER_BATCH: int = 500

_CONNECT_TIMEOUT_SECONDS: float = 10.0
_READ_TIMEOUT_SECONDS: float = 60.0


# ---------------------------------------------------------------------------
# Exceptions — one distinct type per refusal reason the GUI needs to branch on.
# ---------------------------------------------------------------------------

class AgentApiError(Exception):
    """Base class for every error this client raises. Carries the HTTP status code, if any."""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AgentUnauthorizedError(AgentApiError):
    """
    The server rejected the bearer token (HTTP 401 — `missing`, `malformed` or
    `rejected` on the wire, all folded into this one exception since none of those
    codes changes what the caller must do: clear the stored token and return to the
    pairing screen).
    """


class AgentRateLimitedError(AgentApiError):
    """HTTP 429. Carries the server's `Retry-After` hint, in seconds, when present."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: Optional[int] = None,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.retry_after_seconds = retry_after_seconds


class AgentProtocolError(AgentApiError):
    """The server refused this program's `X-Agent-Protocol` version — a newer build is required."""


class AccountsFetchResult(NamedTuple):
    """
    `fetch_accounts()`'s return shape. `token` is the ROTATED token for the NEXT run
    (D-26) — the CALLER (agent/sync.py) must persist it via
    `config_store.save_token()` IMMEDIATELY on receiving this result, because the
    server has already rotated by the time this function returns; a crash between the
    two is exactly what the D-26 grace window exists to survive.
    """

    token: str
    accounts: list[dict]


class ApiClient:
    """
    Thin `requests`-based wrapper around the four `/api/agent/*` endpoints
    (`src/app/api/agent/*/route.ts`, plan 39-11). Holds a base URL and a bearer token;
    every method call is a single logical operation, with `post_trades` internally
    issuing one HTTP request per chunk.
    """

    def __init__(self, base_url: str, token: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    # -----------------------------------------------------------------
    # Internal request plumbing
    # -----------------------------------------------------------------

    def _headers(self, *, authorized: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Agent-Protocol": str(AGENT_PROTOCOL_VERSION),
        }
        if authorized:
            if not self.token:
                raise AgentApiError("ApiClient has no token set for an authorized call")
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        authorized: bool = True,
    ) -> Any:
        """
        Issue one HTTP request and return its parsed JSON body.

        Deliberately does NOT accept a `verify` keyword and never passes one to
        `requests` — see this module's own header. Always sets an explicit connect and
        read timeout, so a hung server can never hang this program's sync loop
        forever.
        """
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(
                method,
                url,
                headers=self._headers(authorized=authorized),
                json=json_body,
                timeout=(_CONNECT_TIMEOUT_SECONDS, _READ_TIMEOUT_SECONDS),
            )
        except requests.RequestException as exc:
            raise AgentApiError(f"network error calling {method} {path}: {exc}") from exc

        return self._handle_response(method, path, response)

    def _handle_response(self, method: str, path: str, response: "requests.Response") -> Any:
        if response.status_code == 401:
            raise AgentUnauthorizedError(
                f"{method} {path} refused the bearer token", status_code=401
            )
        if response.status_code == 429:
            retry_after_header = response.headers.get("Retry-After")
            retry_after_seconds: Optional[int] = None
            if retry_after_header is not None and retry_after_header.strip().isdigit():
                retry_after_seconds = int(retry_after_header.strip())
            raise AgentRateLimitedError(
                f"{method} {path} was rate limited",
                retry_after_seconds=retry_after_seconds,
                status_code=429,
            )

        body: Any = None
        body_error: Optional[Exception] = None
        try:
            body = response.json()
        except ValueError as exc:
            # `json.JSONDecodeError` is itself a `ValueError` subclass, and
            # `requests`' own `JSONDecodeError` is a subclass of both — one `except`
            # clause covers every JSON-parse failure `Response.json()` can raise.
            body_error = exc

        if response.status_code == 400 and isinstance(body, dict) and body.get("error") == "protocol":
            raise AgentProtocolError(
                "the server refused this program's protocol version — an update is required",
                status_code=400,
            )

        if not response.ok:
            detail = body if body is not None else "<non-JSON response body>"
            raise AgentApiError(
                f"{method} {path} failed with status {response.status_code}: {detail}",
                status_code=response.status_code,
            )

        if body_error is not None:
            # A 2xx response that is not valid JSON is refused outright — this program
            # parses ONLY JSON, on purpose (see this module's own header). There is no
            # fallback parser and no attempt to evaluate the body as anything else.
            raise AgentApiError(
                f"{method} {path} returned a non-JSON response body", status_code=response.status_code
            ) from body_error

        return body

    # -----------------------------------------------------------------
    # Pairing: POST /api/agent/pair/redeem
    # -----------------------------------------------------------------

    def redeem_pairing_code(self, code: str) -> str:
        """Exchange a short-lived pairing code for a real rotating bearer token."""
        body = self._request(
            "POST", "/api/agent/pair/redeem", json_body={"code": code}, authorized=False
        )
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise AgentApiError("pairing redeem response did not include a token")
        return token

    # -----------------------------------------------------------------
    # Accounts: GET /api/agent/accounts
    # -----------------------------------------------------------------

    def fetch_accounts(self) -> AccountsFetchResult:
        """
        Fetch this run's account list, together with the ROTATED token for the next
        run. See `AccountsFetchResult`'s own docstring for the persist-immediately
        requirement — this function does NOT persist the token itself; the caller
        must.
        """
        body = self._request("GET", "/api/agent/accounts")
        if not isinstance(body, dict):
            raise AgentApiError("accounts response was not a JSON object")
        token = body.get("token")
        accounts = body.get("accounts")
        if not isinstance(token, str) or not token:
            raise AgentApiError("accounts response did not include a token")
        if not isinstance(accounts, list):
            raise AgentApiError("accounts response did not include an accounts list")
        return AccountsFetchResult(token=token, accounts=accounts)

    # -----------------------------------------------------------------
    # Trades: POST /api/agent/trades
    # -----------------------------------------------------------------

    def post_trades(
        self,
        *,
        account_id: str,
        deposit_base: float,
        currency: Optional[str],
        is_demo: bool,
        trades: list[dict],
    ) -> dict:
        """
        POST `trades` to the server, split into chunks of at most
        `MAX_TRADES_PER_BATCH` rows.

        THIS CHUNKING IS A CORRECTNESS REQUIREMENT, NOT AN OPTIMISATION: Vercel
        Functions enforce a hard 4.5MB request-body cap and return 413 above it, and
        the server cannot ask this client for a smaller body once one is already
        oversized — a naive single POST 413s on any multi-year account. `batchIndex`
        increments from 0 and `isFinalBatch` is `True` on exactly the last request,
        including when `trades` is empty (a single, empty, final batch is still sent
        so the server sees the run end for this account).
        """
        chunks: list[list[dict]] = [
            trades[start : start + MAX_TRADES_PER_BATCH]
            for start in range(0, len(trades), MAX_TRADES_PER_BATCH)
        ]
        if not chunks:
            chunks = [[]]

        aggregate = {"inserted": 0, "updated": 0, "skipped": 0}
        last_index = len(chunks) - 1
        for index, chunk in enumerate(chunks):
            body = {
                "accountId": account_id,
                "batchIndex": index,
                "isFinalBatch": index == last_index,
                "depositBase": deposit_base,
                "currency": currency,
                "isDemo": is_demo,
                "trades": chunk,
            }
            result = self._request("POST", "/api/agent/trades", json_body=body)
            if isinstance(result, dict):
                for key in aggregate:
                    value = result.get(key)
                    if isinstance(value, int):
                        aggregate[key] += value
        return aggregate

    # -----------------------------------------------------------------
    # Account status: POST /api/agent/account-status
    # -----------------------------------------------------------------

    def post_account_status(
        self,
        *,
        account_id: str,
        outcome: str,
        is_demo: Optional[bool],
        stats: Optional[dict] = None,
        equity_points: Optional[list[dict]] = None,
    ) -> None:
        """
        Report this account's outcome for the run, once per account per run.

        `stats`/`equity_points` MUST be `None` on every outcome except `ok` — the
        server must never overwrite an account's last-known aggregates with zeros
        just because one run failed. This function does not enforce that itself (the
        caller, `agent/sync.py`, is the one place that decides success/failure and
        must never pass real aggregates alongside a non-`ok` outcome); it only
        transcribes whatever it is given onto the wire, matching the contract's own
        `stats.nullable()` / `equityPoints.nullable()` shape.
        """
        body = {
            "accountId": account_id,
            "outcome": outcome,
            "isDemo": is_demo,
            "stats": stats,
            "equityPoints": equity_points,
        }
        self._request("POST", "/api/agent/account-status", json_body=body)
