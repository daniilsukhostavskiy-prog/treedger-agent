"""
agent/constants.py — the periodic-sync and staleness tuning constants, and ONLY those
tuning constants.

This module holds exactly two module-level values: the fixed periodic-sync interval
and the silence threshold the SITE uses to flag a stale agent. It has no imports at
all — it is a leaf module, checked by an `ast` audit, so nothing that reads these two
numbers can ever drag in anything else transitively.

WHY THESE LIVE HERE AND NOT AS A THIRD `config.json` KEY
----------------------------------------------------------------------------------
`agent/config_store.py`'s own docstring forbids a third key in `config.json` — the
file holds exactly `token` and `base_url`, on purpose, so no account/login/server/
password detail ever accumulates there. A user-editable sync-interval dropdown is
not worth breaking that rule for: it is a value the program should simply always use
correctly, not a setting somebody sets once, forgets about, and later can't explain.

`SYNC_INTERVAL_SECONDS` is also deliberately NOT tied to the autostart checkbox
(Task Scheduler registration). Autostart controls whether the program launches on
login; the sync timer controls whether an already-open window keeps syncing. Wiring
the two together would produce a state that is impossible to explain to a user: the
program open, visible, working — and syncing nothing, because a checkbox whose
meaning is "start me on login" happens to be unticked. The timer runs whenever the
window is open, full stop, regardless of how the program was launched.

`AGENT_SILENCE_THRESHOLD_HOURS` — READ THIS BEFORE CHANGING EITHER SIDE
----------------------------------------------------------------------------------
The SITE, not this program, is the component that actually acts on this threshold:
it compares `agent_pairings.last_seen_at` (bumped on every authenticated agent
request) against "now" and shows a staleness notice in the cabinet once that gap
exceeds the threshold. The authoritative value the site reads is
`AGENT_STALE_AFTER_HOURS` in `src/lib/constants/agent.ts` — THIS module's value is
only the agent-side transcription of that same number, kept here so it sits directly
next to the sync interval it's related to, per this phase's own decision to keep the
two side by side. This is the same transcription-with-a-named-risk convention
`agent/api_client.py` already uses for the wire contract it copies from
`src/lib/api/agent/contract.ts`: a change to one side without the other silently
desyncs client-visible behaviour from what the site actually enforces. If either
number changes, change BOTH in the same commit.

WHAT IS DELIBERATELY NOT HERE
----------------------------------------------------------------------------------
`AGENT_PROTOCOL_VERSION`, `MAX_TRADES_PER_BATCH`, `_CONNECT_TIMEOUT_SECONDS` and
`_READ_TIMEOUT_SECONDS` all currently live inline in `agent/api_client.py`. This
module's own scope is the two NEW constants only — moving those existing,
working constants here for tidiness alone, so close to this folder moving to
its own repository, is out of scope. Do not "finish the job" by migrating them.
"""

# A fixed one-hour periodic sync, always, while the program's window is open —
# never tied to the autostart checkbox. See the module docstring above for why.
SYNC_INTERVAL_SECONDS: int = 3600

# The agent-side transcription of `AGENT_STALE_AFTER_HOURS` in
# src/lib/constants/agent.ts, the value the SITE actually reads and acts on. Change
# both together.
AGENT_SILENCE_THRESHOLD_HOURS: int = 36
