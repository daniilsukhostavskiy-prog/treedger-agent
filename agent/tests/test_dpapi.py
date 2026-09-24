"""
agent/tests/test_dpapi.py — the test coverage for `agent/dpapi.py`.

The trap this file is written around: the `dpapi`-marked tests below are Windows-only by design and
therefore SKIP silently on any non-Windows CI runner — a green pytest exit code alone
cannot distinguish "ran and passed" from "silently skipped everywhere." This is
exactly why `build-release.yml`'s CI gate asserts on the junit-xml report's own
skipped-count for this marker, rather than trusting pytest's process exit code — a
green build with an unexecuted test is the worst possible outcome, because it looks
like proof. The off-Windows behaviour tests below are deliberately left
UNMARKED so they always run, including in this repository's own non-Windows sandbox.
"""
from __future__ import annotations

import sys

import pytest

from agent import dpapi


# ---------------------------------------------------------------------------
# Real-syscall round trip — Windows only, marked `dpapi`.
# ---------------------------------------------------------------------------

@pytest.mark.dpapi
@pytest.mark.skipif(not sys.platform.startswith("win"), reason="DPAPI is Windows-only")
def test_unprotect_of_protect_round_trips_on_windows() -> None:
    ciphertext = dpapi.protect(b"secret")
    assert dpapi.unprotect(ciphertext) == b"secret"


@pytest.mark.dpapi
@pytest.mark.skipif(not sys.platform.startswith("win"), reason="DPAPI is Windows-only")
def test_protect_output_is_not_the_input_on_windows() -> None:
    ciphertext = dpapi.protect(b"secret")
    assert ciphertext != b"secret"


@pytest.mark.dpapi
@pytest.mark.skipif(not sys.platform.startswith("win"), reason="DPAPI is Windows-only")
def test_unprotect_of_garbage_raises_never_guesses_on_windows() -> None:
    with pytest.raises(OSError):
        dpapi.unprotect(b"not-really-ciphertext")


# ---------------------------------------------------------------------------
# Off-Windows behaviour — deliberately UNMARKED so this always runs, including
# in this repository's own non-Windows sandbox (config_store.py's documented
# assumption).
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform.startswith("win"), reason="this asserts the OFF-Windows path")
def test_protect_raises_dpapi_unavailable_off_windows() -> None:
    with pytest.raises(dpapi.DpapiUnavailableError):
        dpapi.protect(b"secret")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="this asserts the OFF-Windows path")
def test_unprotect_raises_dpapi_unavailable_off_windows() -> None:
    with pytest.raises(dpapi.DpapiUnavailableError):
        dpapi.unprotect(b"anything")


def test_module_imports_successfully_regardless_of_platform() -> None:
    # Import already happened at module load above; re-import must also succeed —
    # this proves agent.dpapi is always importable so the rest of the suite collects.
    import importlib

    importlib.reload(dpapi)
