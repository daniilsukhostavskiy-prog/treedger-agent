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
`pytest.ini`, `README.md`, `tests/`, and so on) moves unedited — it becomes the new
repository's own root, not a subfolder inside it.

## Procedure

1. Create the new repository, `treedger-agent`, as **public**. The whole point of this
   move is that a reader can verify the published binary matches the published source
   (40-CONTEXT.md D-01) — a private repo defeats that from the start.
2. Copy the CONTENTS of this repository's `agent/` folder to the new repository's ROOT.
   That means `agent/main.py` becomes `main.py` at the new repo's root, `agent/tests/`
   becomes `tests/` at the root, and so on — the new repository's root holds
   `pytest.ini`, `requirements.txt`, `README.md`, `.gitignore`, and (after step 3)
   `.github/`. Do not nest everything under a subfolder called `agent/` inside the new
   repository.
3. Move `packaging/github-workflows/probe-window.yml` and
   `packaging/github-workflows/build-release.yml` into a new `.github/workflows/`
   directory at the new repository's root. `packaging/observe_runtime.ps1` and
   `packaging/assert_dpapi_executed.py` stay under `packaging/` at the new root — the
   workflows reference them at that path.
4. Commit once, as the initial commit of the new repository.

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
