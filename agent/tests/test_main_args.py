"""
agent/tests/test_main_args.py — the test coverage for `agent/main.py`'s
`parse_argv`/`WindowLaunchOptions` and `main()`'s single-instance-first ordering.

The ordering tests are behavioural, not a reading of the source: they monkeypatch
`config_store.load_token`, `terminal_discovery.find_terminal_path` and the Tk root
constructor with recorders and assert those recorders saw zero calls when the lock
was not acquired — proving "before any work happens", not merely asserting it.
"""
from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from agent import main


# ---------------------------------------------------------------------------
# "parse_argv([]) reports the window should start normal."
# "parse_argv(["--minimized"]) reports the window should start minimized."
# ---------------------------------------------------------------------------


def test_parse_argv_empty_starts_normal() -> None:
    options = main.parse_argv([])
    assert options.minimized is False


def test_parse_argv_minimized_flag_starts_minimized() -> None:
    options = main.parse_argv(["--minimized"])
    assert options.minimized is True


# ---------------------------------------------------------------------------
# "parse_argv ignores every unrecognised argument without raising and without
#  changing any other option — driven by a table covering an unknown flag, a
#  bare word, a key=value pair, an empty string, a flag that merely starts with
#  the same prefix, and the minimized flag mixed in among junk."
# ---------------------------------------------------------------------------

_UNRECOGNISED_ARGV_TABLE: "list[tuple[str, list[str], bool]]" = [
    ("unknown flag", ["--unknown"], False),
    ("bare word", ["hello"], False),
    ("key=value pair", ["base_url=https://evil.example"], False),
    ("empty string", [""], False),
    ("flag sharing only the prefix", ["--minimize"], False),
    ("minimized flag mixed in among junk", ["--unknown", "--minimized", "hello", ""], True),
]


@pytest.mark.parametrize(
    "argv,expected_minimized",
    [pytest.param(argv, expected, id=label) for label, argv, expected in _UNRECOGNISED_ARGV_TABLE],
)
def test_parse_argv_ignores_unrecognised_arguments(
    argv: "list[str]", expected_minimized: bool
) -> None:
    # Must not raise for any of these shapes — a raising parse_argv would crash
    # the whole program on a stray argument from Task Scheduler or a typo.
    options = main.parse_argv(argv)
    assert options.minimized is expected_minimized


# ---------------------------------------------------------------------------
# A one-shot CI probe measured whether a tkinter window renders at all on a
# GitHub-hosted runner (a real risk, since a headless CI runner might never
# render a window) — it does, launched with no flags at all and a
# deliberately invalid token, before any `--selftest` code ever existed.
# That measurement settled the question empirically: no second command-line
# mode was ever built, and `--selftest` is
# therefore just another unrecognised token — this test exists so a future
# contributor who wants to add the flag has to delete this test first, and
# read why it says not to: the exact command-line flag this program recognises
# is a hard constraint (see `WindowLaunchOptions`'s own docstring in
# `agent/main.py`), and a CI-only `--selftest` mode would mean CI never tests
# the same code path a real user runs — a divergence this program deliberately
# avoided because it was never needed.
# ---------------------------------------------------------------------------


def test_parse_argv_ignores_selftest_flag_exactly_like_any_unknown_token() -> None:
    options = main.parse_argv(["--selftest"])
    assert options.minimized is False


# ---------------------------------------------------------------------------
# "WindowLaunchOptions carries exactly one field, and it is the minimized
#  boolean."
# ---------------------------------------------------------------------------


def test_window_launch_options_has_exactly_one_field() -> None:
    fields = dataclasses.fields(main.WindowLaunchOptions)
    assert len(fields) == 1
    assert fields[0].name == "minimized"


# ---------------------------------------------------------------------------
# "main() acquires the single-instance lock before any config, token or MT5
#  call is reached."
# "When the lock is not acquired, main() calls the window raise and returns
#  without constructing the window."
# "When the lock is not acquired, main()'s process exit code is 0."
# ---------------------------------------------------------------------------


def test_main_touches_nothing_when_lock_not_acquired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main.single_instance, "acquire_single_instance_lock", lambda: False)

    raise_calls: "list[None]" = []
    monkeypatch.setattr(
        main.single_instance, "raise_existing_window", lambda: raise_calls.append(None) or True
    )

    load_token_calls: "list[None]" = []
    monkeypatch.setattr(
        main.config_store, "load_token", lambda: load_token_calls.append(None)
    )

    find_terminal_calls: "list[None]" = []
    monkeypatch.setattr(
        main.terminal_discovery,
        "find_terminal_path",
        lambda: find_terminal_calls.append(None),
    )

    tk_constructor_calls: "list[None]" = []

    class _RecordingTk:
        def __init__(self, *args: object, **kwargs: object) -> None:
            tk_constructor_calls.append(None)

    monkeypatch.setattr(main.tk, "Tk", _RecordingTk)

    # main() must return normally (exit code 0), never call sys.exit/raise.
    result = main.main()

    assert result is None
    assert raise_calls == [None]
    assert load_token_calls == []
    assert find_terminal_calls == []
    assert tk_constructor_calls == []


# ---------------------------------------------------------------------------
# Structural: agent/main.py never imports argparse (see parse_argv's own
# docstring for why argparse is the wrong tool here).
# ---------------------------------------------------------------------------


def test_main_module_never_imports_argparse() -> None:
    """
    An `ast` assertion (not a text search) that no `import argparse` or
    `from argparse import ...` statement exists anywhere in agent/main.py.
    """
    source_path = pathlib.Path(__file__).resolve().parent.parent / "main.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

    offenders: "list[str]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "argparse":
                    offenders.append(f"{source_path}:{node.lineno} imports argparse")
        elif isinstance(node, ast.ImportFrom) and node.module == "argparse":
            offenders.append(f"{source_path}:{node.lineno} imports from argparse")

    assert not offenders, f"argparse import found: {offenders}"


# ---------------------------------------------------------------------------
# 260925-k6y — button labels, decided by `ast` (the text= constants of every
# tk.Button call in main.py), never by a text search.
# ---------------------------------------------------------------------------


def _button_texts() -> "list[str]":
    source_path = pathlib.Path(main.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    texts: "list[str]" = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "Button"):
            continue
        for kw in node.keywords:
            if kw.arg == "text" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                texts.append(kw.value.value)
    return texts


def test_window_buttons_are_synchronize_cancel_and_open_log_folder() -> None:
    texts = _button_texts()
    assert "Синхронизировать" in texts
    assert "Отменить" in texts
    assert "Открыть папку журнала" in texts
    assert "Обновить" not in texts
