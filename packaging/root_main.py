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

This shim keeps the built executable named `main.exe` in `main.dist/`, which
is what `packaging/observe_runtime.ps1`, both workflows, the D-13 install
instructions and the autostart task all already expect.
"""

from agent.main import main

if __name__ == "__main__":
    main()
