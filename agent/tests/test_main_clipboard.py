"""
agent/tests/test_main_clipboard.py — layout-independent clipboard keys and the entry
right-click menu in `agent/main.py` (quick 260925-qhs, brief E1).

Tk binds its <<Paste>>/<<Copy>>/<<Cut>>/<<SelectAll>> virtual events to LATIN keysyms
(`<Control-Key-v>` …), so under the Russian layout Ctrl+V produces keysym
`Cyrillic_em` and nothing pastes. The fix matches the Windows virtual-key code
(`event.keycode`, identical under every layout) instead, generates the Tk virtual
event, and returns "break".

No real Tk interpreter anywhere here: every widget, event and menu is a plain
recorder object, and `AgentWindow.__new__` bypasses `__init__` entirely.
"""
from __future__ import annotations

import ast
import logging
import pathlib
from typing import Optional

import pytest

from agent import main


class _FakeWidget:
    def __init__(self) -> None:
        self.generated: "list[str]" = []
        self.bindings: "dict[str, object]" = {}
        self.focused = 0

    def event_generate(self, sequence: str) -> None:
        self.generated.append(sequence)

    def bind(self, sequence: str, func: object) -> None:
        self.bindings[sequence] = func

    def focus_set(self) -> None:
        self.focused += 1


class _FakeEvent:
    def __init__(self, keycode: int, keysym: str, widget: Optional[_FakeWidget] = None) -> None:
        self.keycode = keycode
        self.keysym = keysym
        self.widget = widget if widget is not None else _FakeWidget()
        self.x_root = 10
        self.y_root = 20


class _FakeMenu:
    def __init__(self) -> None:
        self.popups: "list[tuple[int, int]]" = []
        self.released = 0

    def tk_popup(self, x: int, y: int) -> None:
        self.popups.append((x, y))

    def grab_release(self) -> None:
        self.released += 1


def _bare_window() -> "main.AgentWindow":
    return main.AgentWindow.__new__(main.AgentWindow)


@pytest.mark.parametrize(
    ("keycode", "expected"),
    [(86, "<<Paste>>"), (67, "<<Copy>>"), (88, "<<Cut>>"), (65, "<<SelectAll>>"), (77, None), (17, None)],
)
def test_clipboard_virtual_event_maps_windows_virtual_key_codes(keycode: int, expected: "Optional[str]") -> None:
    assert main.clipboard_virtual_event(keycode) == expected


@pytest.mark.parametrize("keysym", ["v", "Cyrillic_em"])
def test_ctrl_v_pastes_exactly_once_under_any_layout(keysym: str) -> None:
    window = _bare_window()
    event = _FakeEvent(86, keysym)

    result = window._on_entry_control_key(event)

    assert result == "break"
    assert event.widget.generated == ["<<Paste>>"]


@pytest.mark.parametrize(
    ("keycode", "keysym", "virtual"),
    [(67, "Cyrillic_es", "<<Copy>>"), (88, "Cyrillic_che", "<<Cut>>"), (65, "Cyrillic_ef", "<<SelectAll>>")],
)
def test_ctrl_c_x_a_under_the_russian_layout(keycode: int, keysym: str, virtual: str) -> None:
    window = _bare_window()
    event = _FakeEvent(keycode, keysym)

    assert window._on_entry_control_key(event) == "break"
    assert event.widget.generated == [virtual]


def test_other_control_keys_are_left_alone() -> None:
    window = _bare_window()
    event = _FakeEvent(77, "m")

    assert window._on_entry_control_key(event) is None
    assert event.widget.generated == []


def test_the_handler_emits_no_log_record(caplog: pytest.LogCaptureFixture) -> None:
    window = _bare_window()
    with caplog.at_level(logging.DEBUG):
        window._on_entry_control_key(_FakeEvent(86, "Cyrillic_em"))
        window._on_entry_control_key(_FakeEvent(77, "m"))
    assert caplog.records == []


def test_installing_support_binds_control_keys_and_right_click() -> None:
    window = _bare_window()
    entry = _FakeWidget()

    window._install_entry_clipboard_support(entry)  # type: ignore[arg-type]

    assert set(entry.bindings) == {"<Control-KeyPress>", "<Button-3>"}
    assert entry.bindings["<Control-KeyPress>"] == window._on_entry_control_key
    assert entry.bindings["<Button-3>"] == window._show_entry_menu


def test_right_click_menu_targets_the_clicked_entry_and_releases_the_grab() -> None:
    window = _bare_window()
    menu = _FakeMenu()
    window._entry_menu = menu  # type: ignore[attr-defined]
    window._entry_menu_target = None  # type: ignore[attr-defined]
    entry = _FakeWidget()

    window._show_entry_menu(_FakeEvent(0, "", widget=entry))

    assert window._entry_menu_target is entry
    assert entry.focused == 1
    assert menu.popups == [(10, 20)]
    assert menu.released == 1

    window._entry_menu_generate("<<Paste>>")
    assert entry.generated == ["<<Paste>>"]


def test_menu_command_without_a_target_does_nothing() -> None:
    window = _bare_window()
    window._entry_menu_target = None  # type: ignore[attr-defined]
    window._entry_menu_generate("<<Paste>>")  # must not raise


def _main_source_tree() -> ast.Module:
    source = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    return ast.parse(source)


def test_every_entry_in_main_gets_clipboard_support() -> None:
    tree = _main_source_tree()
    entry_constructions = 0
    install_calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "Entry":
            entry_constructions += 1
        if isinstance(func, ast.Attribute) and func.attr == "_install_entry_clipboard_support":
            install_calls += 1
    assert entry_constructions >= 2
    assert entry_constructions == install_calls


def test_main_never_reads_the_clipboard_into_python() -> None:
    tree = _main_source_tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"clipboard_get", "selection_get"}, node.attr
