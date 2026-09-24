"""
agent/tests/test_main_timer.py — the test coverage for `agent/main.py`'s hourly
periodic-sync timer, the autostart checkbox, and the protocol-too-old
except clause.

Two testing techniques, chosen per what each test actually needs to prove:

  - `_FakeRoot` (a plain stand-in for `tk.Tk()`, never a real Tcl interpreter) drives
    `AgentWindow.__init__`'s real startup sequence for the tests that need to prove
    something about THAT sequence (the first-tick delay, the startup autostart
    self-check). `_build_widgets`/`_render` are monkeypatched to no-ops for these —
    real widget construction needs a genuine Tcl interpreter this fake root does not
    provide, and neither test is about widgets.
  - `AgentWindow.__new__(AgentWindow)` (bypassing `__init__` entirely) is used for the
    tests that exercise exactly one method in isolation (`_on_periodic_tick`,
    `_on_autostart_toggled`, `_run_sync_worker`) — no window construction, no config
    I/O, no terminal discovery, just the one behaviour under test.
"""
from __future__ import annotations

import queue

import pytest

from agent import api_client, autostart, config_store, constants, main, sync, terminal_discovery, ui_state


class _FakeRoot:
    """
    A stand-in for `tk.Tk()`, used only to drive `AgentWindow.__init__`'s startup
    sequence deterministically without a real Tcl interpreter. `after` records every
    `(delay_ms, callback)` pair it is given, in call order, so a test can assert on the
    exact schedule without a real event loop ever running.
    """

    def __init__(self) -> None:
        self.after_calls: "list[tuple[int, object]]" = []

    def title(self, *_args: object, **_kwargs: object) -> None:
        pass

    def geometry(self, *_args: object, **_kwargs: object) -> None:
        pass

    def minsize(self, *_args: object, **_kwargs: object) -> None:
        pass

    def protocol(self, *_args: object, **_kwargs: object) -> None:
        pass

    def after(self, delay_ms: int, callback: object) -> None:
        self.after_calls.append((delay_ms, callback))


def _make_bare_window(*, sync_in_flight: bool = False) -> "tuple[main.AgentWindow, _FakeRoot]":
    """
    Builds an `AgentWindow` WITHOUT running `__init__` at all — no config I/O, no
    terminal discovery, no widgets. Only the attributes the method under test actually
    reads are set. Used for tests that exercise exactly one method in isolation.
    """
    window = main.AgentWindow.__new__(main.AgentWindow)
    fake_root = _FakeRoot()
    window.root = fake_root
    window._sync_in_flight = sync_in_flight
    window._queue = queue.Queue()
    return window, fake_root


# ---------------------------------------------------------------------------
# "The periodic timer is scheduled with a delay equal to the sync-interval
#  constant expressed in milliseconds — the first tick is an hour after
#  launch, not at launch."
# "Startup calls the scheduler path-repair function exactly once, and never
#  the create function."
# ---------------------------------------------------------------------------


def test_first_tick_scheduled_with_interval_delay_and_startup_repairs_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main.AgentWindow, "_build_widgets", lambda self: None)
    monkeypatch.setattr(main.AgentWindow, "_render", lambda self: None)
    monkeypatch.setattr(config_store, "load_token", lambda: None)
    monkeypatch.setattr(terminal_discovery, "find_terminal_path", lambda: None)

    repair_calls: "list[None]" = []
    monkeypatch.setattr(
        autostart, "repair_task_path", lambda: repair_calls.append(None) or "repaired"
    )
    create_calls: "list[None]" = []
    monkeypatch.setattr(autostart, "create_task", lambda: create_calls.append(None))

    fake_root = _FakeRoot()
    window = main.AgentWindow(fake_root)  # type: ignore[arg-type]

    # Computed from the import, never a literal repeated here — per the plan's own
    # acceptance criterion.
    expected_delay_ms = constants.SYNC_INTERVAL_SECONDS * 1000
    periodic_calls = [
        (delay, callback)
        for delay, callback in fake_root.after_calls
        if callback == window._on_periodic_tick
    ]
    assert periodic_calls == [(expected_delay_ms, window._on_periodic_tick)]

    assert repair_calls == [None]
    assert create_calls == []


# ---------------------------------------------------------------------------
# "A tick arriving while a sync is already in flight performs no sync and
#  starts no thread, and reschedules itself."
# "The timer reschedules itself after every tick, whether or not that tick
#  ran a sync."
# ---------------------------------------------------------------------------


def test_tick_while_sync_in_flight_skips_sync_and_reschedules() -> None:
    window, fake_root = _make_bare_window(sync_in_flight=True)

    refresh_calls: "list[None]" = []
    window._on_refresh_clicked = lambda: refresh_calls.append(None)  # type: ignore[method-assign]

    window._on_periodic_tick()

    assert refresh_calls == []  # no sync started
    expected_delay_ms = constants.SYNC_INTERVAL_SECONDS * 1000
    assert fake_root.after_calls == [(expected_delay_ms, window._on_periodic_tick)]


# ---------------------------------------------------------------------------
# "A tick arriving when idle triggers exactly one sync run through the same
#  path the refresh control uses."
# ---------------------------------------------------------------------------


def test_tick_when_idle_triggers_refresh_path_exactly_once() -> None:
    window, fake_root = _make_bare_window(sync_in_flight=False)

    refresh_calls: "list[None]" = []
    window._on_refresh_clicked = lambda: refresh_calls.append(None)  # type: ignore[method-assign]

    window._on_periodic_tick()

    assert refresh_calls == [None]
    expected_delay_ms = constants.SYNC_INTERVAL_SECONDS * 1000
    assert fake_root.after_calls == [(expected_delay_ms, window._on_periodic_tick)]


# ---------------------------------------------------------------------------
# "The timer runs regardless of whether autostart is enabled."
# ---------------------------------------------------------------------------


def test_periodic_tick_never_reads_autostart_state() -> None:
    """
    `_on_periodic_tick`/`_schedule_periodic_sync` must never branch on
    `_autostart_var` or call any `agent.autostart` function — the timer's only
    dependency is `_sync_in_flight`. Proven by simply never setting
    `window._autostart_var` at all and calling the tick twice (idle, then again
    while `_sync_in_flight` is manually flipped True): neither call raises
    `AttributeError`, which it would if either method read that attribute.
    """
    window, _fake_root = _make_bare_window(sync_in_flight=False)
    window._on_refresh_clicked = lambda: None  # type: ignore[method-assign]

    window._on_periodic_tick()  # idle tick
    window._sync_in_flight = True
    window._on_periodic_tick()  # in-flight tick

    assert not hasattr(window, "_autostart_var")


# ---------------------------------------------------------------------------
# "Toggling the autostart control on calls the create function exactly once;
#  toggling it off calls the remove function exactly once."
# ---------------------------------------------------------------------------


class _FakeBooleanVar:
    def __init__(self, value: bool) -> None:
        self._value = value

    def get(self) -> bool:
        return self._value


def test_autostart_toggle_on_calls_create_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    window, _fake_root = _make_bare_window()
    window._autostart_var = _FakeBooleanVar(True)  # type: ignore[attr-defined]

    create_calls: "list[None]" = []
    remove_calls: "list[None]" = []
    monkeypatch.setattr(autostart, "create_task", lambda: create_calls.append(None))
    monkeypatch.setattr(autostart, "remove_task", lambda: remove_calls.append(None))

    window._on_autostart_toggled()

    assert create_calls == [None]
    assert remove_calls == []


def test_autostart_toggle_off_calls_remove_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    window, _fake_root = _make_bare_window()
    window._autostart_var = _FakeBooleanVar(False)  # type: ignore[attr-defined]

    create_calls: "list[None]" = []
    remove_calls: "list[None]" = []
    monkeypatch.setattr(autostart, "create_task", lambda: create_calls.append(None))
    monkeypatch.setattr(autostart, "remove_task", lambda: remove_calls.append(None))

    window._on_autostart_toggled()

    assert remove_calls == [None]
    assert create_calls == []


# ---------------------------------------------------------------------------
# "A protocol-too-old error raised during a sync run dispatches the
#  protocol-too-old event, and does not fall into the generic error clause."
# ---------------------------------------------------------------------------


def test_run_sync_worker_dispatches_protocol_too_old_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_protocol_error(_client: object, _report: object) -> None:
        raise api_client.AgentProtocolError("the server refused this build's protocol version")

    monkeypatch.setattr(sync, "run_sync", _raise_protocol_error)

    window, _fake_root = _make_bare_window()
    window._run_sync_worker(client=object())  # type: ignore[arg-type]

    events: "list[object]" = []
    while True:
        try:
            events.append(window._queue.get_nowait())
        except queue.Empty:
            break

    assert len(events) == 2
    assert isinstance(events[0], ui_state.ProtocolTooOldEvent)
    assert isinstance(events[1], main._SyncFinishedSentinel)
    # Proves the branch returned early rather than falling through to the
    # generic (sync.SyncAbortedError, AgentApiError) clause, which would have
    # pushed a RunFinishedEvent instead.
    assert not any(isinstance(event, ui_state.RunFinishedEvent) for event in events)
