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
   `packaging/github-workflows/probe-window.yml` and
   `packaging/github-workflows/build-release.yml` into a new `.github/workflows/`
   directory at the root. `packaging/observe_runtime.ps1` and
   `packaging/assert_dpapi_executed.py` stay under `packaging/` at the new root — the
   workflows reference them at that path.
4. Copy `packaging/root_main.py` to the new repository's root, renaming it to `main.py`.
   This three-line shim (`from agent.main import main`) is the build entry point. It is the
   reason the produced executable is still called `main.exe` inside `main.dist/`, which is
   what `packaging/observe_runtime.ps1`, both workflows, the D-13 install instructions and
   the autostart task all already expect. It is not part of the `agent` package and the
   package never imports it.
5. Move `agent/.gitignore` to the new repository's root.
6. Commit once, as the initial commit of the new repository.

### The resulting layout

```
treedger-agent/
├── .github/workflows/{probe-window.yml, build-release.yml}
├── packaging/{observe_runtime.ps1, assert_dpapi_executed.py, README.md, root_main.py}
├── agent/          ← moved whole and unedited: __init__.py, *.py, tests/,
│                     pytest.ini, requirements.txt, README.md, CHECKLIST-RU.md
├── main.py         ← packaging/root_main.py, renamed
└── .gitignore
```

### Why the package must not be flattened

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

`.github/` must NOT be created in this repository under any circumstances. The two CI
workflow files above are staged as a plain `packaging/github-workflows/` folder
precisely so GitHub Actions in `tradeproof` never picks them up and never runs them
here — they only start running once they land under `.github/workflows/` in the public
`treedger-agent` repository.
