"""Root entry point for the packaged Treedger agent executable.

DESTINATION: this file is copied to the NEW repository's ROOT as `main.py`.
It does not belong to the `agent` package and is never imported by it.

Why this file exists
--------------------
`agent/` is a real Python package (`agent/__init__.py`), and four of its
modules import their siblings absolutely -- `from agent import ...`,
`from agent.error_codes import ...`. Nine test modules do the same. The
package therefore has to stay a package: flattening its contents into the
repository root deletes the `agent` name those imports resolve against, and
the program dies on its first import line with
`ModuleNotFoundError: No module named 'agent'` -- before tkinter is ever
reached, so no window renders and the failure looks like a D-27 answer when
it is nothing of the kind. Probe run #2 (treedger-agent@dd1ef43) failed
exactly that way.

Keeping the package intact also honours the standing Phase 39 guarantee that
`agent/` moves to the new repository WHOLE AND UNEDITED. Rewriting those
imports to suit a flat layout would edit 13 files and break that guarantee,
and would contradict `agent/CHECKLIST-RU.md`, which already documents the
source-run entry point as `python -m agent.main`.

This shim stays the BUILD entry point: Nuitka is pointed at `main.py`, so its
standalone output folder is `build_output/main.dist/` — a build-internal name
nobody downloads. Since quick task 260925-k6y the binary inside that folder is
named `Treedger.exe` by Nuitka's `--output-filename=Treedger.exe` (both
workflows), and the release step copies `main.dist` into a staging folder named
`Treedger` and zips THAT folder, so the published archive contains exactly one
top-level `Treedger/` folder with `Treedger/Treedger.exe` inside. The shim's code
below is unchanged.
"""

from agent.main import main

if __name__ == "__main__":
    main()
