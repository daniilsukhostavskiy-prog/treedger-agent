"""
agent/tests/test_packaging_icon.py — parity checks for the tray icon asset and the
`NUITKA_FLAGS` that embed it into the built `Treedger.exe` (owner follow-up to quick
260926-ieo).

Follows `agent/tests/test_installer_script.py`'s own convention: candidate paths cover
BOTH this repository's layout (`agent/packaging/github-workflows/*.yml`) and the public
`treedger-agent` repository's layout (`.github/workflows/*.yml` at the repo root, since
`packaging/` moves to the root and `agent/` stays a subfolder) — the test FAILS, never
skips, when neither exists.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from agent import tray

_AGENT_DIR = pathlib.Path(tray.__file__).resolve().parent
_ICON_FLAG = "--windows-icon-from-ico=agent/assets/treedger.ico"


def _workflow_path(name: str) -> pathlib.Path:
    candidates = (
        _AGENT_DIR / "packaging" / "github-workflows" / name,
        _AGENT_DIR.parent / ".github" / "workflows" / name,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    pytest.fail(f"{name} not found at any of: " + ", ".join(str(c) for c in candidates))
    raise AssertionError("unreachable")


def _nuitka_flags(workflow_name: str) -> str:
    text = _workflow_path(workflow_name).read_text(encoding="utf-8")
    match = re.search(r'NUITKA_FLAGS:\s*"([^"]*)"', text)
    assert match, f"NUITKA_FLAGS not found in {workflow_name}"
    return match.group(1)


# ---------------------------------------------------------------------------
# The icon asset itself
# ---------------------------------------------------------------------------


def test_icon_asset_exists_inside_agent_package() -> None:
    assert tray.ASSET_ICON_PATH.is_file()
    # Must live INSIDE agent/, never under agent/packaging/ (which moves to root
    # packaging/ in treedger-agent) — the path in NUITKA_FLAGS must resolve
    # identically in both repositories.
    assert tray.ASSET_ICON_PATH.parent.name == "assets"
    assert tray.ASSET_ICON_PATH.parent.parent == _AGENT_DIR


def test_icon_header_lists_the_six_expected_sizes() -> None:
    data = tray.ASSET_ICON_PATH.read_bytes()
    reserved, image_type, count = struct_unpack_header(data)
    assert reserved == 0
    assert image_type == 1  # ICO, not CUR
    assert count == 6

    sizes = []
    for i in range(count):
        offset = 6 + i * 16
        width_byte, height_byte = data[offset], data[offset + 1]
        sizes.append(width_byte or 256)
    assert sorted(sizes) == [16, 32, 48, 64, 128, 256]


def struct_unpack_header(data: bytes) -> "tuple[int, int, int]":
    import struct

    return struct.unpack("<HHH", data[:6])


def test_icon_is_byte_identical_to_the_site_favicon_when_present() -> None:
    """The single source of truth (per the owner) is `src/app/favicon.ico`. This repo
    has it; a standalone `treedger-agent` checkout would not (the site lives in a
    different repository there) — the comparison is skipped in that case, never
    failed, matching this test file's own header docstring."""
    favicon_path = _AGENT_DIR.parent / "src" / "app" / "favicon.ico"
    if not favicon_path.is_file():
        pytest.skip("src/app/favicon.ico not present in this checkout (expected in treedger-agent)")
    assert tray.ASSET_ICON_PATH.read_bytes() == favicon_path.read_bytes()


# ---------------------------------------------------------------------------
# NUITKA_FLAGS parity
# ---------------------------------------------------------------------------


def test_build_release_and_probe_window_nuitka_flags_are_identical() -> None:
    assert _nuitka_flags("build-release.yml") == _nuitka_flags("probe-window.yml")


@pytest.mark.parametrize("workflow_name", ["build-release.yml", "probe-window.yml"])
def test_nuitka_flags_contains_the_icon_flag(workflow_name: str) -> None:
    assert _ICON_FLAG in _nuitka_flags(workflow_name)


def test_icon_flag_path_exists_relative_to_the_repo_root_the_workflow_runs_from() -> None:
    """The workflow checks out the repo at its root and runs Nuitka from there
    (`actions/checkout@v5`, no `working-directory` override for the Nuitka step) — so
    the flag's own relative path, `agent/assets/treedger.ico`, must resolve from the
    REPO ROOT, not from `agent/`. In both this repo and treedger-agent, `agent/` is a
    direct subfolder of the repo root, so `_AGENT_DIR.parent / "agent" / "assets" /
    "treedger.ico"` is exactly what the flag names."""
    relative_path = _ICON_FLAG.split("=", 1)[1]
    assert relative_path == "agent/assets/treedger.ico"
    repo_root = _AGENT_DIR.parent
    assert (repo_root / relative_path).is_file()


def test_installer_smoke_workflow_carries_no_nuitka_flags_to_keep_in_sync() -> None:
    """`installer-smoke.yml` never runs Nuitka at all (it compiles the installer around
    a stub payload — see that workflow's own header comment), so it has no
    `NUITKA_FLAGS` of its own and nothing here needs to stay in sync with it."""
    text = _workflow_path("installer-smoke.yml").read_text(encoding="utf-8")
    assert "NUITKA_FLAGS" not in text
