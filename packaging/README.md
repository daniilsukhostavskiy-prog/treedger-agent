# Moving `agent/` to `treedger-agent`

This file is the move instruction sheet. It stays with `agent/` only until the move
happens — delete it from the new repository afterward, or leave it, the choice is the
owner's.

## What this plan staged, and where each file goes

| File in this repository | Destination in `treedger-agent` |
|---|---|
| `agent/packaging/README.md` (this file) | `packaging/README.md`, or delete after the move — it is the move instruction sheet, not part of the running program |
| `agent/packaging/github-workflows/probe-window.yml` | `.github/workflows/probe-window.yml` |
| `agent/packaging/github-workflows/build-release.yml` | `.github/workflows/build-release.yml` |
| `agent/packaging/github-workflows/installer-smoke.yml` | `.github/workflows/installer-smoke.yml` (quick 260925-qhs) |
| `agent/packaging/installer/treedger.iss` | `packaging/installer/treedger.iss` (quick 260925-qhs) |
| `agent/packaging/observe_runtime.ps1` | `packaging/observe_runtime.ps1` |
| `agent/packaging/assert_dpapi_executed.py` | `packaging/assert_dpapi_executed.py` |
| `agent/.gitignore` | `.gitignore` (repo root) |

Everything else already inside `agent/` (`main.py`, `config_store.py`, `requirements.txt`,
`pytest.ini`, `README.md`, `tests/`, and so on) moves **unedited, and stays inside a folder
still named `agent/`** at the new repository's root. It is a real Python package —
`agent/__init__.py` exists, four of its modules import their siblings absolutely
(`from agent import ...`, `from agent.error_codes import ...`) and nine test modules do the
same. Flattening its contents into the repository root deletes the very name those thirteen
files resolve against.

## Procedure

1. Create the new repository, `treedger-agent`, as **public**. The whole point of this
   move is that a reader can verify the published binary matches the published source
   (40-CONTEXT.md D-01) — a private repo defeats that from the start.
2. Copy this repository's `agent/` folder to the new repository's ROOT **as a folder still
   named `agent/`**. `agent/main.py` stays `agent/main.py`; `agent/tests/` stays
   `agent/tests/`; `agent/pytest.ini`, `agent/requirements.txt` and `agent/README.md` stay
   where they are. Do NOT flatten its contents into the root — see the warning below.
3. Move `agent/packaging/` OUT of the package, to `packaging/` at the new repository's root,
   so that `agent/` is left as a pure importable package. Then move
   `packaging/github-workflows/probe-window.yml`,
   `packaging/github-workflows/build-release.yml` and
   `packaging/github-workflows/installer-smoke.yml` into a new `.github/workflows/`
   directory at the root. `packaging/observe_runtime.ps1`,
   `packaging/assert_dpapi_executed.py` and `packaging/installer/treedger.iss` stay under
   `packaging/` at the new root — the workflows reference them at that path (and
   `agent/tests/test_installer_script.py` looks for the `.iss` at
   `../packaging/installer/treedger.iss` relative to the package, failing — never
   skipping — when it is missing).
4. Copy `packaging/root_main.py` to the new repository's root, renaming it to `main.py`.
   This three-line shim (`from agent.main import main`) is the build entry point, so
   Nuitka's standalone output folder is `build_output/main.dist/` — a build-internal name
   nobody downloads. The binary inside it is named `Treedger.exe` by
   `--output-filename=Treedger.exe` (both workflows' `NUITKA_FLAGS`), and that is the path
   `packaging/observe_runtime.ps1` is pointed at (`build_output/main.dist/Treedger.exe`).
   The release zip ships the folder as a single top-level `Treedger/` folder (see
   **Release archive layout** below). The shim is not part of the `agent` package and the
   package never imports it.
   **`agent/assets/treedger.ico`** (added by the owner follow-up to quick 260926-ieo)
   moves with `agent/` unedited, like everything else in step 2 — it is deliberately
   NOT under `agent/packaging/`, precisely so its path is the same repo-root-relative
   string, `agent/assets/treedger.ico`, in both repositories. Both workflows'
   `NUITKA_FLAGS` reference exactly that path via
   `--windows-icon-from-ico=agent/assets/treedger.ico`, which EMBEDS the icon into
   `Treedger.exe`'s own resources at build time — it does not copy the `.ico` file
   into the shipped `Treedger/` folder, and the frozen program never opens it from
   disk (`agent/tray.py`'s `TrayIcon` reads the embedded resource instead when
   `sys.frozen` is True; running from source it loads `agent/assets/treedger.ico`
   directly). Changing `NUITKA_FLAGS` changes the Nuitka cache key, so the first
   build after this change is a full cold build (~38 minutes), not the usual
   incremental one.
5. Move `agent/.gitignore` to the new repository's root.
6. Commit once, as the initial commit of the new repository.

### The resulting layout

```
treedger-agent/
├── .github/workflows/{probe-window.yml, build-release.yml, installer-smoke.yml}
├── packaging/{observe_runtime.ps1, assert_dpapi_executed.py, README.md, root_main.py,
│              installer/treedger.iss}
├── agent/          ← moved whole and unedited: __init__.py, *.py, tests/,
│                     pytest.ini, requirements.txt, README.md, CHECKLIST-RU.md,
│                     assets/treedger.ico
├── main.py         ← packaging/root_main.py, renamed
└── .gitignore
```

### Release archive layout

`build-release.yml` copies `build_output/main.dist` to `release_staging/Treedger` and
zips that FOLDER (not its contents), so `treedger-agent-<tag>-windows.zip` contains exactly
one top-level folder:

```
Treedger/
├── Treedger.exe      ← the program (no console window: --windows-console-mode=disable)
└── …                 ← the runtime files it needs; they must stay beside Treedger.exe
```

Before hashing, the workflow opens the zip and fails the release unless every entry starts
with `Treedger/` and `Treedger/Treedger.exe` exists. The asset name pattern is unchanged
(`^treedger-agent-.+-windows\.zip$`, which the site's `/download/agent` route matches).
Releases up to and including v0.1.0 used the old layout (`main.exe` plus ~980 files at the
archive root).

### Installer (quick 260925-qhs)

Phase 40 D-13 (a per-user `%LOCALAPPDATA%` folder, never admin) is **revoked** by the
owner: the primary install path is now an Inno Setup installer into Program Files, one
UAC prompt at install/update, so no process running as the user can silently overwrite
the exe. `packaging/installer/treedger.iss`:

- installs the same staged `Treedger/` folder the zip ships into `{autopf}\Treedger`
  (per-machine only — no per-user override);
- refuses to install or uninstall while the program runs (`AppMutex` = the program's own
  single-instance mutex); closes nothing, ever — the MetaTrader 5 terminal lives outside
  the install folder;
- offers two tasks, both **unchecked** by default: a desktop shortcut and «Запускать
  вместе с Windows» (the same HKCU `Run` value `TreedgerAgent` the in-app checkbox
  writes, with the same warning text);
- starts the program at the end **non-elevated** (`runasoriginaluser`);
- on uninstall removes files, shortcuts and the autostart value (also one the in-app
  checkbox created), deletes a leftover `TreedgerAgent` scheduled task from the old
  autostart mechanism, and **keeps** `%APPDATA%\TreedgerAgent` (per-user token, settings,
  logs).

Assets per release: `treedger-agent-<tag>-windows.zip` + its `.sha256.txt` (portable,
unchanged), `treedger-agent-<tag>-windows-setup.exe` + its `.sha256.txt`, and
`runtime-observation.json` — five in total. ISCC is preinstalled on `windows-latest`;
both workflows fall back to `choco install innosetup --version 6.7.1` only when it is
absent.

The web change needs **no hold**: `/download/agent` and the `/download` wizard already
choose the setup.exe when the latest release has one and fall back to the zip (with the
zip instructions, size and hash) when it does not — so the site can deploy before the
first installer tag.

## Release checklist

1. Push the copied files to `treedger-agent`.
2. Run `installer-smoke.yml` by hand (Actions → Installer smoke test → Run workflow).
   It must be green before anything is tagged.
3. Tag `vX.Y.Z` and push the tag.
4. Wait for `build-release.yml` (~38 min).
5. Verify the release has all five assets and that each asset's GitHub digest
   (`gh release view vX.Y.Z --json assets`) equals the hash in its `.sha256.txt`.
6. Open `https://treedger.com/download/agent` and confirm it downloads the
   `-windows-setup.exe`.
7. The owner walks `agent/CHECKLIST-RU.md` → «Шаг 4».

## Why the package must not be flattened

The first version of this sheet said to copy the CONTENTS of `agent/` to the root. That is
wrong, and probe run #2 (`treedger-agent@dd1ef43`) is the proof: the executable built
cleanly, launched, and died instantly with

```
ModuleNotFoundError: No module named 'agent'
```

at `main.py` line 54 — the first import line — because the flattened layout had deleted the
`agent` package name. The program never reached tkinter, so no window rendered, and the probe
faithfully recorded `D27_WINDOW_RENDERED=no`. That value was a report of a dead process, not
an answer to D-27, and it must not be read as one.

Keeping the package intact is also what makes the standing Phase 39 guarantee true: `agent/`
moves to this repository whole and unedited. The alternative — rewriting those thirteen files
to suit a flat layout — would break that guarantee and would contradict
`agent/CHECKLIST-RU.md`, which already documents the source-run entry point as
`python -m agent.main`.

Both `python -m pytest agent/tests` and `python -m nuitka ... main.py` are run from the
repository root, and `python -m` places the current directory on `sys.path`, which is what
makes `agent` importable in CI without any `PYTHONPATH` set.

## Three things stated once, plainly

- The repository is public so a reader can verify the binary matches the source
  (D-01).
- The history starts clean — the private development history from this repository is
  not carried over (D-02).
- A license choice is the owner's, made in GitHub's own repo-creation UI. This phase
  deliberately does not pick one — no `LICENSE` file is created by this plan.

## What must never happen in `tradeproof` (this repository)

`.github/` must NOT be created in this repository under any circumstances. The three CI
workflow files above are staged as a plain `packaging/github-workflows/` folder
precisely so GitHub Actions in `tradeproof` never picks them up and never runs them
here — they only start running once they land under `.github/workflows/` in the public
`treedger-agent` repository.
