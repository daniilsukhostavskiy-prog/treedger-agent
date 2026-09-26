"""
agent/tests/test_tray.py — the test coverage for `agent/tray.py`'s pure helpers plus
structural (AST) audits (quick 260926-ieo). No test here ever calls the real
`TrayIcon.start()` — see `agent/tests/conftest.py`'s `_never_create_a_real_tray_icon`
autouse guard, which fails loudly if it ever tried to.
"""
from __future__ import annotations

import ast
import pathlib
import sys

import pytest

from agent import tray

_MODULE_PATH = pathlib.Path(__file__).resolve().parent.parent / "tray.py"

# Windows message constants a test needs to call `notify_action` with — plain,
# well-known Win32 literals, not re-exported by tray.py itself (see the plan's own
# "Windows ABI facts" section; these are general facts, not new public symbols).
_WM_LBUTTONUP = 0x0202
_WM_RBUTTONUP = 0x0205
_WM_CONTEXTMENU = 0x007B
_WM_MOUSEMOVE = 0x0200
_NIN_BALLOONUSERCLICK = 0x0400 + 5

# Captured at collection time, BEFORE the autouse conftest guard
# (`_never_create_a_real_tray_icon`) monkeypatches `TrayIcon.start` for every test — the
# one test below that proves the real off-Windows/no-handles degradation path needs the
# real method back, deliberately bypassing that guard for itself only.
_REAL_START = tray.TrayIcon.start


# ---------------------------------------------------------------------------
# menu_entries()
# ---------------------------------------------------------------------------


def test_menu_entries_when_muted_has_the_fixed_order_and_labels() -> None:
    entries = tray.menu_entries(True)

    assert [e.command for e in entries] == [
        tray.CMD_SYNC_NOW,
        tray.CMD_OPEN_WINDOW,
        tray.CMD_OPEN_SITE,
        tray.CMD_TOGGLE_SOUNDS,
        None,
        tray.CMD_EXIT,
    ]
    sound_entry = entries[3]
    assert sound_entry.label == "Звуки MT5: выкл"
    assert sound_entry.checked is False
    assert entries[0].label == "Синхронизировать сейчас"
    assert entries[1].label == "Открыть окно"
    assert entries[2].label == "Открыть Treedger"
    assert entries[5].label == "Выход"
    # Only the sound item is ever checked.
    assert [e.checked for i, e in enumerate(entries) if i != 3] == [False, False, False, False, False]


def test_menu_entries_when_unmuted_flips_only_the_sound_item() -> None:
    muted = tray.menu_entries(True)
    unmuted = tray.menu_entries(False)

    assert [e.command for e in unmuted] == [e.command for e in muted]
    assert [e.label for i, e in enumerate(unmuted) if i != 3] == [
        e.label for i, e in enumerate(muted) if i != 3
    ]
    sound_entry = unmuted[3]
    assert sound_entry.label == "Звуки MT5: вкл"
    assert sound_entry.checked is True


# ---------------------------------------------------------------------------
# clamp_text()
# ---------------------------------------------------------------------------


def test_clamp_text_leaves_short_text_unchanged() -> None:
    assert tray.clamp_text("short", 127) == "short"


def test_clamp_text_truncates_to_exactly_limit_ending_in_ellipsis() -> None:
    result = tray.clamp_text("a" * 200, tray.TOOLTIP_MAX_CHARS)
    assert len(result) == tray.TOOLTIP_MAX_CHARS
    assert result.endswith("…")


def test_tooltip_max_chars_is_127() -> None:
    assert tray.TOOLTIP_MAX_CHARS == 127


# ---------------------------------------------------------------------------
# notify_action()
# ---------------------------------------------------------------------------


def test_notify_action_left_click_and_balloon_click_open_the_window() -> None:
    assert tray.notify_action(_WM_LBUTTONUP) == tray.CMD_OPEN_WINDOW
    assert tray.notify_action(_NIN_BALLOONUSERCLICK) == tray.CMD_OPEN_WINDOW


def test_notify_action_right_click_and_context_menu_key_show_the_menu() -> None:
    assert tray.notify_action(_WM_RBUTTONUP) == "show_menu"
    assert tray.notify_action(_WM_CONTEXTMENU) == "show_menu"


def test_notify_action_other_events_return_none() -> None:
    assert tray.notify_action(_WM_MOUSEMOVE) is None
    assert tray.notify_action(999999) is None


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def test_window_class_and_message_name_are_non_empty_and_message_has_no_whitespace() -> None:
    assert tray.TRAY_WINDOW_CLASS
    assert tray.SHOW_WINDOW_MESSAGE_NAME
    assert " " not in tray.SHOW_WINDOW_MESSAGE_NAME
    assert "\t" not in tray.SHOW_WINDOW_MESSAGE_NAME


# ---------------------------------------------------------------------------
# start()/set_tooltip()/show_balloon()/set_sounds_muted()/stop() degrade off-Windows
# or when the WinDLL handles are unavailable — without a real tray icon.
# ---------------------------------------------------------------------------


def test_start_returns_false_when_handles_unavailable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(tray.TrayIcon, "start", _REAL_START)
    monkeypatch.setattr(tray, "_user32", None)
    icon = tray.TrayIcon(lambda _cmd: None)
    assert icon.start() is False


def test_public_methods_are_no_ops_before_start_ever_ran() -> None:
    icon = tray.TrayIcon(lambda _cmd: None)
    # _hwnd is None until a (never-called-in-tests) real start() sets it.
    icon.set_tooltip("hello")
    icon.show_balloon("title", "text")
    icon.set_sounds_muted(False)
    icon.stop()  # must not raise even though nothing was ever started


# ---------------------------------------------------------------------------
# _load_icon() — frozen vs. from-source (owner follow-up to quick 260926-ieo).
# `--windows-icon-from-ico` EMBEDS the icon into the built exe's own resources; it does
# NOT ship agent/assets/treedger.ico as a file, so the icon reaches a frozen tray ONLY
# through that build flag — never by opening the .ico file, which does not exist on
# disk in a frozen build. These tests mock both Win32 surfaces so neither branch ever
# touches a real file or a real icon handle.
# ---------------------------------------------------------------------------


class _FakeShell32Icons:
    def __init__(self, *, extract_count: int = 0, small_value: int = 0, large_value: int = 0) -> None:
        self.extract_calls: "list[str]" = []
        self._extract_count = extract_count
        self._small_value = small_value
        self._large_value = large_value

    def ExtractIconExW(self, path: str, _index: int, large_ref: object, small_ref: object, _n: int) -> int:
        self.extract_calls.append(path)
        large_ref._obj.value = self._large_value  # type: ignore[attr-defined]
        small_ref._obj.value = self._small_value  # type: ignore[attr-defined]
        return self._extract_count


class _FakeUser32Icons:
    def __init__(self, *, load_image_result: int = 0, load_icon_result: int = 999) -> None:
        self.load_image_calls: "list[tuple[object, ...]]" = []
        self.load_icon_calls: "list[tuple[object, object]]" = []
        self.destroy_icon_calls: "list[object]" = []
        self._load_image_result = load_image_result
        self._load_icon_result = load_icon_result

    def GetSystemMetrics(self, _index: int) -> int:
        return 16

    def LoadImageW(self, hinst: object, name: object, itype: int, cx: int, cy: int, flags: int) -> int:
        self.load_image_calls.append((hinst, name, itype, cx, cy, flags))
        return self._load_image_result

    def LoadIconW(self, hinst: object, name: object) -> int:
        self.load_icon_calls.append((hinst, name))
        return self._load_icon_result

    def DestroyIcon(self, handle: object) -> bool:
        self.destroy_icon_calls.append(handle)
        return True


def test_load_icon_frozen_uses_exe_resource_and_never_touches_the_asset_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `sys.frozen` is True, only `ExtractIconExW(sys.executable, ...)` is ever
    called — the on-disk `.ico` path is never attempted at all."""
    monkeypatch.setattr(tray.sys, "frozen", True, raising=False)
    fake_shell32 = _FakeShell32Icons(extract_count=1, small_value=42)
    fake_user32 = _FakeUser32Icons()
    monkeypatch.setattr(tray, "_shell32", fake_shell32)
    monkeypatch.setattr(tray, "_user32", fake_user32)

    icon = tray.TrayIcon(lambda _cmd: None)
    result = icon._load_icon(0)

    assert result == 42
    assert fake_shell32.extract_calls == [sys.executable]
    assert fake_user32.load_image_calls == []


def test_load_icon_nuitka_compiled_marker_alone_selects_exe_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nuitka's documented runtime marker is `__compiled__` in module globals; the
    build evidence never confirmed that `--standalone` also sets `sys.frozen`. With
    ONLY `__compiled__` present, the installed build must still take the embedded
    exe icon and never look for the `.ico` file it does not ship."""
    monkeypatch.delattr(tray.sys, "frozen", raising=False)
    monkeypatch.setitem(tray.__dict__, "__compiled__", object())
    fake_shell32 = _FakeShell32Icons(extract_count=1, small_value=43)
    fake_user32 = _FakeUser32Icons()
    monkeypatch.setattr(tray, "_shell32", fake_shell32)
    monkeypatch.setattr(tray, "_user32", fake_user32)

    icon = tray.TrayIcon(lambda _cmd: None)
    result = icon._load_icon(0)

    assert result == 43
    assert fake_shell32.extract_calls == [sys.executable]
    assert fake_user32.load_image_calls == []


def test_load_icon_from_source_loads_the_asset_file_and_never_touches_exe_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(tray.sys, "frozen", raising=False)
    fake_shell32 = _FakeShell32Icons()
    fake_user32 = _FakeUser32Icons(load_image_result=77)
    monkeypatch.setattr(tray, "_shell32", fake_shell32)
    monkeypatch.setattr(tray, "_user32", fake_user32)

    icon = tray.TrayIcon(lambda _cmd: None)
    result = icon._load_icon(0)

    assert result == 77
    assert fake_shell32.extract_calls == []
    assert len(fake_user32.load_image_calls) == 1
    assert fake_user32.load_image_calls[0][1] == str(tray.ASSET_ICON_PATH)
    assert fake_user32.load_image_calls[0][5] == tray._LR_LOADFROMFILE


def test_load_icon_frozen_falls_back_to_idi_application_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tray.sys, "frozen", True, raising=False)
    monkeypatch.setattr(tray, "_shell32", _FakeShell32Icons(extract_count=0))
    fake_user32 = _FakeUser32Icons(load_icon_result=999)
    monkeypatch.setattr(tray, "_user32", fake_user32)

    icon = tray.TrayIcon(lambda _cmd: None)
    result = icon._load_icon(0)

    assert result == 999
    assert len(fake_user32.load_icon_calls) == 1


def test_load_icon_from_source_falls_back_to_idi_application_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(tray.sys, "frozen", raising=False)
    monkeypatch.setattr(tray, "_shell32", _FakeShell32Icons())
    fake_user32 = _FakeUser32Icons(load_image_result=0, load_icon_result=999)
    monkeypatch.setattr(tray, "_user32", fake_user32)

    icon = tray.TrayIcon(lambda _cmd: None)
    result = icon._load_icon(0)

    assert result == 999
    assert len(fake_user32.load_icon_calls) == 1


def test_asset_icon_path_points_inside_agent_assets() -> None:
    assert tray.ASSET_ICON_PATH.parts[-2:] == ("assets", "treedger.ico")


# ---------------------------------------------------------------------------
# Structural (AST) audits — never text greps.
# ---------------------------------------------------------------------------


def _tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"), filename=str(_MODULE_PATH))


def test_module_imports_no_subprocess_socket_or_winreg() -> None:
    tree = _tree()
    forbidden = {"subprocess", "socket", "winreg"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden, f"forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden, f"forbidden import: {node.module}"


def test_module_never_references_sys_argv() -> None:
    tree = _tree()
    offenders: "list[str]" = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "argv"
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys"
        ):
            offenders.append(f"{node.lineno}: sys.argv")
    assert not offenders, offenders


def test_no_forbidden_process_or_window_calls() -> None:
    tree = _tree()
    forbidden_names = {
        "kill",
        "terminate",
        "TerminateProcess",
        "ExitProcess",
        "ShowWindow",
        "SetWindowPos",
        "MoveWindow",
        "CloseWindow",
        "EnumWindows",
    }
    offenders: "list[str]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else (func.id if isinstance(func, ast.Name) else None)
            )
            if name in forbidden_names:
                offenders.append(f"{node.lineno}: calls {name}(...)")
    assert not offenders, offenders


def test_create_window_ex_w_call_exists_and_never_uses_hwnd_message() -> None:
    tree = _tree()
    found = False
    offenders: "list[str]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None
            )
            if name == "CreateWindowExW":
                found = True
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Name) and "HWND_MESSAGE" in sub.id:
                            offenders.append(f"{node.lineno}: argument references {sub.id!r}")
    assert found, "no CreateWindowExW call found"
    assert not offenders, offenders
