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

There are three ways to run this program. Most people want the first one.

### The installer (recommended)

Download `treedger-agent-<version>-windows-setup.exe` from this repository's Releases page
(or via treedger.com/download, which always serves the latest one) and run it:

1. Windows SmartScreen may warn that the publisher is unknown (the program is not
   code-signed) — «Подробнее» → «Выполнить в любом случае».
2. Windows asks for permission to make changes to the device (UAC) — «Да». This is the
   only administrator prompt, once per install or update: the program goes into
   `C:\Program Files\Treedger`.
3. Choose the language, keep the folder, and decide on the two options — a desktop
   shortcut and «Запускать вместе с Windows». **Both are off by default.**
4. At the end the program starts by itself — as your normal user, not as administrator.

Updating is the same: run the new installer. If the program is open, the installer asks
you to close it first; your pairing and settings are kept.

### The portable archive (alternative)

Download `treedger-agent-<version>-windows.zip`, unzip it, open the `Treedger` folder
inside and run `Treedger.exe` — keep every other file in that folder beside it (the
program needs them; do not move `Treedger.exe` out on its own). This copy lives wherever
you unzip it, so it carries the residual risk described below.

For either path, see **Where it installs** and **Verifying what you downloaded** below
before you run it for the first time. No Python installation is needed; no console window
opens.

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

The installer puts the program into `C:\Program Files\Treedger` (per machine) — a folder
only an administrator can write to, so no program running under your account can quietly
replace `Treedger.exe`. That is why installing and updating ask for one UAC confirmation.

**Changed 2026-09-25 (quick 260925-qhs, owner decision):** Phase 40's decision D-13 — a
per-user `%LOCALAPPDATA%\Programs\TreedgerAgent` folder that never needs admin — is
**revoked**. The owner's reason: in a user-writable folder any process under the same
account can silently overwrite the exe, and with autostart that is a ready re-execution
point; Program Files needs admin to write. One UAC prompt at install/update is an
acceptable price.

What did not change: the program itself never needs administrator rights to **run**, and
your settings and token stay in your own profile, `%APPDATA%\TreedgerAgent` (the token
DPAPI-encrypted for your Windows account). The portable zip still exists as an
alternative; it lives wherever you unzip it.

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
- **Checking the install folder's permissions.** Also useless: for the installed copy
  Program Files already does the job (writing there needs admin), and a portable copy's
  folder is always writable by its owner by design, so the warning would be permanent —
  and a permanent warning stops being read within a week.

The general rule behind both rejections: **a mechanism that creates a feeling of
protection without providing it is worse than no mechanism — it obstructs sober
assessment of the residual risk and spends the user's trust for nothing.**

## The residual risk (read this)

**Installed copy (Program Files).** Replacing `Treedger.exe` needs administrator rights,
so a process running under your own account can no longer swap the file that autostart
launches. What remains: the pairing token is protected with Windows DPAPI, which is
bound to your Windows account — any process running as you can still ask Windows to
decrypt it, exactly as before. The autostart entry itself is a per-user registry value
that any process running as you could rewrite, just as it could add its own; the program
only ever writes its own path there and never re-points an entry at another existing
copy.

**Portable copy (zip).** Everything above about the token applies, and additionally any
process running under your account can overwrite the unzipped `.exe`; turning on
**Запускать вместе с Windows** makes such a substitution persistent — it would run again
at every login, undetected. The program does not check for this and does not try to.
Use the installer if that matters to you.

Code-signing the executable is still deliberately deferred, not forgotten.

## Tray icon and MT5 sounds (quick 260926-ieo)

Since this quick task, autostart (`--minimized`) no longer opens a minimised window at
all — it opens **no window and no taskbar button**, only a small icon in the Windows
notification area (the tray, next to the clock). Right-click it for:

| Item | What it does |
|---|---|
| **Синхронизировать сейчас** | Starts a sync immediately, exactly like the in-window button. |
| **Открыть окно** | Brings the program's window up. |
| **Открыть Treedger** | Opens the site in your browser (your saved server address, or treedger.com). |
| **Звуки MT5: выкл / вкл** | A checkbox — toggles whether this program mutes MetaTrader 5's own sounds (see below). Off by default (muted). |
| **Выход** | The ONLY thing that quits the program. |

Left-clicking the icon (or clicking a warning balloon) also brings the window up.

**Closing the window with the X does not quit the program** — it just hides it back to
the tray; syncing keeps happening on schedule. Only **«Выход»** in the tray menu ends the
program. The one exception: if the tray icon itself could not be created (rare — see
below), X quits exactly as it always did, so the program can never be left running with
neither a window nor a tray icon.

**The tooltip** (hover over the icon) always shows the current status — the last
successful sync time, "syncing…", or the current problem. A small notification balloon
appears only for two specific problems, and only while the window is hidden: MetaTrader 5
refused a login (wrong investor password), or MetaTrader 5 could not be found at all.
Nothing else ever pops up a balloon — a routine, successful sync is silent, exactly as
before.

**«Звуки MT5»** — muted by default. While muted, the program periodically (about every
15 seconds, every 2 seconds during a sync) mutes MetaTrader 5's own sound through
Windows' own per-application volume mixer — the same mixer you would open yourself
(right-click the speaker icon → "Open Volume Mixer"). This only ever touches
`terminal64.exe`'s own entry there: never the master/speaker volume, never any other
program, and never a MetaTrader 5 setting or file. Turning the tray checkbox off, or
choosing «Выход», restores MT5's sound — but only the sessions THIS program muted; if
you had muted MT5 yourself in the volume mixer before turning this preference on, this
program leaves your own choice alone and never unmutes it.

Two things worth knowing:
- If you unmute MT5 by hand in the Windows volume mixer while «Звуки MT5: выкл» is still
  selected, the program mutes it again within about 15 seconds — use the tray checkbox
  if you want it to stay unmuted.
- If MetaTrader 5 is not running at the moment you choose «Выход» (or toggle the
  checkbox off), Windows may still remember the mute for that program's identity; it is
  restored the next time this program runs with sounds on, or if you unmute it yourself
  in the volume mixer.

If the tray icon cannot be created at all (Explorer was still starting up at logon, and
never became ready even after two minutes of retries — rare), the program falls back to
showing its window instead, exactly as it did before this quick task.

## Autostart and periodic sync

**Запускать вместе с Windows** (off by default) means exactly one thing: whether the
program starts automatically when you log into Windows. It carries no second meaning —
it does not change how often the program syncs.

About 15 seconds after the window opens (or the tray icon appears), the program starts
one sync by itself, and then syncs once an hour after that — however it was launched,
manually or via autostart, in the tray or with its window open. `--minimized` (what
autostart passes) changes only the program's initial visibility — see "Tray icon and MT5
sounds" above for exactly what it now means. Together that makes the unattended chain:
log into Windows → the program appears only as a tray icon → it syncs ~15 s later,
starting MetaTrader 5 minimised if it is not running (with its own sounds muted, unless
you turned that off) → every hour after that.

How it works (quick 260925-qhs): autostart is ONE per-user registry value,
`TreedgerAgent`, under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, with the data
`"<path to Treedger.exe>" --minimized`. It needs no administrator rights. (Until then it
was a Windows Task Scheduler task, which failed for standard users with "Access is
denied" — that mechanism is gone; the installer removes a leftover task.) You can see and
switch the entry in Task Manager → «Автозагрузка приложений».

The checkbox shows the real state: it reads as ticked only when the value exists, points
at this very executable, AND has not been switched off in Task Manager. After every click
the program re-reads the registry and shows what is actually there. On every start a
silent self-check repairs the value only when the executable it names no longer exists
(for example after the folder was moved); it never creates a value and never re-points a
value at another copy that still exists. The installer's own «Запускать вместе с Windows»
option (unchecked by default) writes the very same value.

## Uninstalling

Use «Приложения» / «Программы и компоненты» → Treedger → Удалить (the same prompt to close
the program appears if it is running). This removes the program folder, the shortcuts and
the autostart entry — also one created by the in-app checkbox.

It deliberately **keeps** `%APPDATA%\TreedgerAgent` — your pairing token, settings and logs
(per-user data; the uninstaller runs as administrator, possibly under another profile). To
remove everything: delete that folder yourself, and revoke the pairing in Treedger under
**Настройки → Аккаунт → Программа синхронизации**.

## What the first live run established (2026-09-25)

The owner ran the packaged v0.2.0 against a real MetaTrader 5 terminal and a real account
on their own machine; one account synced and 5 trades arrived in the journal.

- The infinite hang in `mt5.initialize` is gone: attach-first plus timeouts plus the
  abandoned-call refusal turned it into clear errors within fractions of a second.
- «Алготрейдинг» OFF means **no connection at all** — `(-10005, 'IPC timeout')` after 20 s
  (attempt A) and 88 s (attempt B); `-10006` was not observed. The window now says
  «Включите „Алготрейдинг“ в терминале…» in that case instead of a generic failure.
- `(-6, 'Terminal: Authorization failed')` came from a **broken terminal install**, not a
  wrong password (a reinstall fixed it) — so an error code still cannot tell a closed
  account, a wrong password and a broken install apart (R-01 stays open).
- Terminal discovery found `C:\Program Files\MetaTrader 5\terminal64.exe` on that machine.

## Single instance

If the program is already running and you start it again, the second copy brings the
first one's window to the front — even when it currently lives only as a tray icon with
no window open — and exits immediately. It never opens a second window and never starts a
second connection to your MT5 terminal.

### First run, step by step

1. Install MetaTrader 5 (with at least one account already added to it), and get the
   program by one of the methods above (the installer is simplest).
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
5. Press **«Синхронизировать»**. The program walks every one of your accounts one at a
   time, showing each one's row update live.

If MetaTrader 5 cannot be found on your computer at all, the window shows a plain,
non-crashing notice instead of the account list — see **Troubleshooting** below.

### What the window shows during a run

- A stage list, each line marked `…` (in progress), `✓` (done), `✗` (failed) or `–`
  (cancelled):
  - **Поиск терминала** — with the path of the `terminal64.exe` that was found;
  - **Запуск терминала** — only when the program had to start MetaTrader 5 itself;
  - **Подключение к терминалу** — with a live seconds counter. It can never run forever:
    after about 90 seconds (150 seconds when the program has just started MetaTrader 5
    itself — a cold start is slower) the run stops with «Терминал MetaTrader 5 не
    отвечает…» and a hint to look for a Windows dialog hidden behind other windows;
  - **Получение списка счетов** — with the number of accounts found;
  - then one row per account (see below).
- **«Отменить»** stops the run *after the current step* — a call already waiting on the
  terminal cannot be interrupted, only bounded by that 90-second limit.
- **Последняя успешная синхронизация** — the time of the last run in which at least one
  account synced (kept only while the window is open).
- A red line under the header when a whole run failed (terminal not responding, could not
  be started, server unreachable, unexpected error).

### MetaTrader 5 is started for you

If no MetaTrader 5 terminal is running when a sync starts, the program starts the one it
found, minimized and without taking focus (Windows and MetaTrader may still restore the
terminal's own saved window position — that request is best-effort). If a terminal is
already running, nothing is started. The program never closes, restarts or ends the
terminal — not even one it started itself; closing the program's window only disconnects
from it.

### Diagnostic log

Every start writes a log to `%APPDATA%\TreedgerAgent\logs\agent.log` (UTF-8, rotated at
1 MB, three old files kept). It contains the program version, Windows version, the
executable path, which terminal was found and whether it was already running (and whether
it or this program runs as administrator), every connection attempt with its result, error
code and elapsed time, per-account outcomes, and any unexpected error with its traceback.
It never contains your investor password, the program's token, or the pairing code. The
**«Открыть папку журнала»** button, at the bottom of every screen, opens that folder — if
something goes wrong, send `agent.log` to support.

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

**«Включите „Алготрейдинг“ в терминале…»** — **confirmed live 2026-09-25**: with the
terminal's «Алготрейдинг» (Algo Trading) button off, the program cannot connect to the
terminal at all — `mt5.initialize()` fails with `-10005` ('IPC timeout'), not just a
history read. When a connect fails with `-10005` (or `-10006`/`-8`, which the tables name
for algo-trading), the window shows this instruction instead of the generic «Не удалось
подключиться…»; when the connection works but the terminal reports `trade_allowed=False`,
the same instruction is pinned as a notice and the run continues. Turn the button on
(green) and sync again. The same code can occasionally come from a very slow terminal
start — if «Алготрейдинг» is already on, send `agent.log`. The program still never
places, modifies or closes a trade; the investor password is read-only either way.

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
automatically the next time it tries to sync. Pair again with a fresh code to continue —
the «Токен был отозван…» notice disappears as soon as the pairing succeeds.

**Pasting the pairing code** — Ctrl+V, Ctrl+C, Ctrl+X and Ctrl+A work in both fields under
any keyboard layout (including Russian), and a right-click opens «Вырезать / Копировать /
Вставить / Выделить всё».

## What this program deliberately does not include

- No code signing (the installer exists since quick 260925-qhs; it is not signed either).
- No support for an account's live open positions — only closed-trade history is read
  and sent.
- Never hides, minimises or reconfigures the MT5 terminal window; never edits any MT5
  file or setting — the tray icon and the «Звуки MT5» preference (quick 260926-ieo) only
  ever touch this program's own window/tray and MetaTrader 5's own Windows audio session,
  nothing about the terminal itself.
