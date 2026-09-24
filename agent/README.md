# Treedger local MT5 sync agent

## What this program is

It logs into **your own** MT5 accounts, using the investor password you already have for
each one, from **your own computer**. It then reads your closed-trade history and sends
it to Treedger so your journal and statistics fill in.

The login comes from your own internet address, not a shared server — that is the entire
point of this program existing. It replaces a shared-server sync that broker/prop-firm
rules increasingly treat as a red flag ("third party access"), even when the access is
provably read-only.

## What it cannot do (verifiable in the source, not just claimed here)

- **It cannot place, modify or close a trade.** There is no order-sending code anywhere
  in this folder — grep it yourself: `grep -rn "order_send\|order_check" agent/` returns
  nothing.
- **It opens no port and listens on no socket.** It only ever makes outbound HTTPS
  requests to Treedger; nothing binds to localhost or any other address.
- **It never updates itself.** There is no self-download, self-replace or auto-update
  code path in this folder.
- **It never stores your investor password on disk.** The password is held in memory
  only, for the duration of one sync run, and is discarded when the run ends.
- **It holds no database credential of any kind.** This folder does not depend on the
  `supabase` package or any other Postgres client library, and contains no service-role
  key, connection string or equivalent — it only ever talks to Treedger's own
  token-authenticated HTTP endpoints.

An investor password is physically read-only at the broker level: even a fully
compromised copy of this program could not withdraw money, place a trade or otherwise
touch your account's trading state.

These claims are enforced by the folder's own structural audit test, not just written
down here: `agent/tests/test_sync.py::TestAgentFolderStructuralAudit` re-checks every one
of them (no listening/serving construct, no `eval`/`exec`, no unsafe deserialization, no
order-sending MT5 API surface, no database client import) against every `.py` file in
this folder on every test run. One command shows whether the promises above still hold:

```
python -m pytest agent/tests/test_sync.py -k StructuralAudit -q
```

## Requirements

- Windows, with MetaTrader 5 already installed and at least one account added to it.
- Python installed from [python.org](https://www.python.org/downloads/) (this includes
  `tkinter`, the GUI toolkit this program's window is built with — some other Python
  distributions omit it).
- The computer switched on and awake while the program runs — it does nothing while your
  machine is asleep or off.

## How to run it (this phase)

```
pip install -r agent/requirements.txt
python agent/main.py
```

There is deliberately **no packaged executable, installer, code signing, autostart or
tray icon** in this phase (see `PHASE-LOCAL-SYNC-SPEC.md` §9 and 39-CONTEXT.md D-24).
Acceptance this phase is walked by running the script directly, exactly as above.

### First run, step by step

1. Install the two things above: MetaTrader 5 (with at least one account already added to
   it) and Python from python.org.
2. `pip install -r agent/requirements.txt`
3. In Treedger, open **Настройки → Аккаунт → Программа синхронизации** and press
   **«Привязать программу»**. This generates a short-lived, single-use pairing code — no
   port is opened on your computer and no browser redirect is ever accepted, which is
   precisely why pairing works this way instead (39-CONTEXT.md D-25).
4. Run `python agent/main.py`. A window opens asking for the server address (pre-filled)
   and the pairing code.
5. Paste the code and press **«Привязать»**. The program exchanges it for a real,
   long-lived token, which it stores locally, and the window moves to the account list.
6. Press **«Обновить»**. The program walks every one of your accounts one at a time,
   showing each one's row update live.

If MetaTrader 5 cannot be found on your computer at all, the window shows a plain,
non-crashing notice instead of the account list — see **Troubleshooting** below.

### What each account row means

Each row shows one account, in the shape `логин → статус`:

| Status shown | Meaning |
|---|---|
| `вход` | Logging into this account via its investor password. |
| `чтение` | Reading the account's full closed-trade history from the terminal. |
| `отправка` | Sending the read trades to Treedger. |
| `отправлено N сделок` | This account finished successfully; `N` trades were sent (`N` can be `0` for a brand-new account with no closed trades yet). |
| `ошибка: <reason>` | This account failed this run. The reason is plain text — e.g. a login failure, a malformed account row, or an unexpected error — and never includes the investor password. |

Above the rows, a single overall progress line reads `Прогресс: done/total (pct%)`, where
`done` counts both successful AND failed accounts — a run with one failed account among
several still reaches 100%, because that account has been walked, just not synced
successfully. This is what makes it visible at a glance that one account's failure never
stops the rest (readiness criterion 8).

## How to run the tests

```
python -m pytest agent/tests -q
```

This test root is independent of `mt5-service/tests/` (currently 82/82) — running the
command above never runs, and never disturbs, that suite.

## Troubleshooting

This section is written directly from `agent/error_codes.py` — the one table of every MT5
error code this program knows anything about. That table honestly labels each of its own
rows as **confirmed in practice** (observed live), **from the MQL5 docs**, or an
**assumption** (inferred, not verified). No live probe against a real MetaTrader 5
terminal has run yet at the time of this writing, so anything below drawn from an
`assumption`-labelled row is stated as unconfirmed, on purpose — inflating it to sound
more certain than it is would only mean nobody double-checks it later.

**«MetaTrader 5 не найден на этом компьютере»** — the program looks for an installed
MetaTrader 5 terminal by scanning the Windows registry's own Uninstall entries for
anything whose display name mentions "MetaTrader 5" (case-insensitively), which is how it
finds broker-branded installs too (e.g. "FTMO MetaTrader 5", "RoboForex MetaTrader 5") —
those install under their own name in their own folder, and are the case that most often
defeats a naive "look for the default folder" check. If this notice still appears, confirm
MetaTrader 5 is actually installed on this machine (not just a shortcut to a portable copy
elsewhere), then restart the program — there is no configuration option to point it at a
path manually, by design.

**«Алготрейдинг» (algo-trading disabled)** — the program recognises MT5 error code
`-10006` and shows a dedicated message telling you to enable algo-trading in your
terminal. What is **confirmed**: the program handles this exact code with its own branch
rather than lumping it in with a generic connection error. What is **not established**:
whether the algo-trading setting actually gates a *read-only history pull* at all — this
program never places, modifies or closes trades, and no live probe has yet run to confirm
or rule out that MT5 requires algo-trading to be on even for read-only account/history
access. Do not treat either direction (that it definitely gates history reads, or that it
definitely doesn't) as established fact from this program's behaviour alone; the honest
answer today is "unconfirmed, pending a real-terminal probe."

**"The program switched my terminal account"** — this is expected, not a bug. On startup,
the program checks whether an account was already logged into your terminal and, if so,
shows a persistent notice for the rest of the run: it switched to your other accounts to
read them, and if you were working in that account yourself, you need to log back into it
afterward. The program deliberately never restores your previous session and never opens
a second, separate terminal instance to avoid this — both were considered and are out of
scope for this phase (39-CONTEXT.md D-23).

**A revoked or expired pairing code** — the pairing screen shows a plain error line and
stays on the pairing screen; request a fresh code from Treedger and try again.

**A revoked token** — if your token is revoked from Treedger (or the server otherwise
refuses it), the window clears its locally stored token and returns to the pairing screen
automatically the next time it tries to sync. Pair again with a fresh code to continue.

## What this phase deliberately does not include

This is a scope decision (39-CONTEXT.md D-24, `PHASE-LOCAL-SYNC-SPEC.md` §9), not an
oversight or something forgotten:

- No packaged `.exe` — the program is run with `python agent/main.py` directly.
- No installer.
- No code signing.
- No autostart registration (nothing adds itself to Windows startup).
- No tray icon (the program only ever shows its one window).
- No support for an account's live open positions — only closed-trade history is read
  and sent.

## Moving this folder

`agent/` is self-contained on purpose: its own dependency list
(`agent/requirements.txt`), its own pytest configuration (`agent/pytest.ini`) and its
own README (this file). It is expected to be lifted whole, unedited, into a future
repository — see `PHASE-LOCAL-SYNC-SPEC.md` §3 and §10 for why this phase is being built
inside the old repository at all.
