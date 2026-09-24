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
- The computer switched on and awake while the program runs — it does nothing while your
  machine is asleep or off.
- Only if you run it from source rather than the released executable: Python installed
  from [python.org](https://www.python.org/downloads/) (this includes `tkinter`, the GUI
  toolkit this program's window is built with — some other Python distributions omit it).

## Running it

There are two ways to run this program. Most people want the first one.

### The released executable

Download the release archive from this repository's Releases page, unzip it, and run the
`.exe` inside — see **Where it installs** and **Verifying what you downloaded** below
before you run it for the first time. No Python installation is needed for this path; the
executable is self-contained.

### From source (for development)

```
pip install -r agent/requirements.txt
python -m agent.main
```

To run the test suite as well, install the test-only dependencies too — `pytest` is
deliberately not in `requirements.txt`, which lists what the shipped program needs at
runtime:

```
pip install -r agent/requirements.txt -r agent/requirements-dev.txt
python -m pytest agent/tests -q
```

`agent/` is a real Python package — launching `main.py` directly by its file path (rather
than as a module) fails with `ModuleNotFoundError: No module named 'agent'`, because
Python does not add the project root to its module search path when a file is launched
that way. Always use the module-execution form above (`python -m agent.main`), run from
the repository root.

## Where it installs

The released executable installs to `%LOCALAPPDATA%\Programs\TreedgerAgent` — a folder
your own Windows account already owns. The program never needs, and never asks for,
administrator rights, which means you can drop a new version in yourself, at any time,
with no UAC elevation prompt.

## Verifying what you downloaded

A hash is published beside each release. Compare it against the file you downloaded
before running it for the first time — that comparison, done once at first run, is the
entire verification step this program relies on.

## What this program does not do

There is **no self-integrity check anywhere in the code**, and the program nowhere
pretends to guard itself. Two ways of adding one were considered and rejected:

- **Remembering the file's hash at the moment autostart is enabled.** Useless: a process
  able to replace the `.exe` can rewrite whatever file is holding the remembered hash,
  too.
- **Checking the install folder's permissions.** Also useless: the folder above is always
  writable by its own owner by design (that is what "no admin rights" means), so the
  warning would be permanent, and a permanent warning stops being read within a week.

The general rule behind both rejections: **a mechanism that creates a feeling of
protection without providing it is worse than no mechanism — it obstructs sober
assessment of the residual risk and spends the user's trust for nothing.**

## The residual risk (read this)

Any process running under your own Windows account can overwrite this program's `.exe`
file, and turning on **Запускать вместе с Windows** (see below) makes such a substitution
persistent — it would run again at every login, undetected. The program does not check
for this and does not try to.

Two things would close this gap, and both are deliberately deferred, not forgotten: an
installer that places the program in a system-owned directory (which needs administrator
rights to write to, unlike the per-user folder above), and code-signing the executable.

## Autostart and periodic sync

**Запускать вместе с Windows** (off by default) means exactly one thing: whether the
program's window opens automatically when you log into Windows. It carries no second
meaning — it does not change how often the program syncs, and it does not put the program
into any special "background" mode; a minimised window is the exact same program, just
iconified.

The program syncs once an hour while its window is open, however it was launched —
manually or via autostart.

## Single instance

If the program is already running and you start it again, the second copy brings the
first one's window to the front and exits immediately. It never opens a second window and
never starts a second connection to your MT5 terminal.

### First run, step by step

1. Install MetaTrader 5 (with at least one account already added to it), and get the
   program by one of the two methods above.
2. In Treedger, open **Настройки → Аккаунт → Программа синхронизации** and press
   **«Привязать программу»**. This generates a short-lived, single-use pairing code — no
   port is ever opened on your computer to receive it, and no browser redirect is
   accepted either; pasting the code into the program's own window is the only path in,
   by design.
3. Run the program. A window opens asking for the server address (pre-filled) and the
   pairing code.
4. Paste the code and press **«Привязать»**. The program exchanges it for a real,
   long-lived token, which it stores locally, encrypted, and the window moves to the
   account list.
5. Press **«Обновить»**. The program walks every one of your accounts one at a time,
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
stops the rest.

## How to run the tests

```
python -m pytest agent/tests -q
```

## Troubleshooting

This section is written directly from `agent/error_codes.py` — the one table of every MT5
error code this program knows anything about. That table honestly labels each of its own
rows as **confirmed in practice** (observed live), **from the MQL5 docs**, or an
**assumption** (inferred, not verified). Anything below drawn from an
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
a second, separate terminal instance to avoid this — both were considered and rejected as
out of scope: restoring a previous session silently is its own source of surprising
behaviour, and a second terminal instance would double MetaTrader 5's own resource use for
a case this notice already handles honestly.

**A revoked or expired pairing code** — the pairing screen shows a plain error line and
stays on the pairing screen; request a fresh code from Treedger and try again.

**A revoked token** — if your token is revoked from Treedger (or the server otherwise
refuses it), the window clears its locally stored token and returns to the pairing screen
automatically the next time it tries to sync. Pair again with a fresh code to continue.

## What this program deliberately does not include

- No installer.
- No code signing.
- No tray icon — the program only ever shows its one window (minimised or not).
- No support for an account's live open positions — only closed-trade history is read
  and sent.
