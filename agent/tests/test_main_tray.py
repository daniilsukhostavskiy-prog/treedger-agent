"""
agent/tests/test_main_tray.py — the test coverage for `agent/main.py`'s tray + MT5-sound
wiring (quick 260926-ieo): withdraw-first startup, close-to-tray, «Выход» restoring
mute, tray command dispatch, the mute tick, and the two balloon triggers.

Two testing techniques, exactly as `agent/tests/test_main_timer.py`'s own header
explains: `_FakeRoot` drives `AgentWindow.__init__`'s real startup sequence for the one
test that needs to prove something about it (the first mute-tick delay);
`AgentWindow.__new__` (bypassing `__init__`) is used for every test that exercises
exactly one method in isolation.
"""
from __future__ import annotations

import ast
import pathlib
import queue

import pytest

from agent import audio_mute, config_store, errors, main, sync, tray, ui_state


class _FakeRoot:
    """A stand-in for `tk.Tk()` — no `iconify` method at all, so a leftover
    `root.iconify()` call anywhere raises `AttributeError` and fails the test loudly."""

    def __init__(self) -> None:
        self.after_calls: "list[tuple[int, object]]" = []
        self.withdraw_calls = 0
        self.deiconify_calls = 0
        self.lift_calls = 0
        self.focus_force_calls = 0
        self.destroy_calls = 0

    def title(self, *_a: object, **_k: object) -> None:
        pass

    def geometry(self, *_a: object, **_k: object) -> None:
        pass

    def minsize(self, *_a: object, **_k: object) -> None:
        pass

    def protocol(self, *_a: object, **_k: object) -> None:
        pass

    def after(self, delay_ms: int, callback: object) -> None:
        self.after_calls.append((delay_ms, callback))

    def withdraw(self) -> None:
        self.withdraw_calls += 1

    def deiconify(self) -> None:
        self.deiconify_calls += 1

    def lift(self) -> None:
        self.lift_calls += 1

    def focus_force(self) -> None:
        self.focus_force_calls += 1

    def destroy(self) -> None:
        self.destroy_calls += 1


class _FakeTray:
    def __init__(self, *, start_result: bool = True) -> None:
        self.start_result = start_result
        self.tooltip_calls: "list[str]" = []
        self.balloon_calls: "list[tuple[str, str]]" = []
        self.sounds_muted_calls: "list[bool]" = []
        self.stop_calls = 0

    def start(self, timeout_seconds: float = 5.0) -> bool:
        return self.start_result

    def set_tooltip(self, text: str) -> None:
        self.tooltip_calls.append(text)

    def show_balloon(self, title: str, text: str) -> None:
        self.balloon_calls.append((title, text))

    def set_sounds_muted(self, muted: bool) -> None:
        self.sounds_muted_calls.append(muted)

    def stop(self, timeout_seconds: float = 3.0) -> None:
        self.stop_calls += 1


class _FakeMute:
    def __init__(
        self,
        *,
        apply_result: "audio_mute.ApplyResult | None" = None,
        restore_result: "audio_mute.RestoreResult | None" = None,
        has_recorded: bool = False,
    ) -> None:
        self.apply_calls: "list[bool]" = []
        self.restore_calls: "list[bool]" = []
        self._apply_result = apply_result or audio_mute.ApplyResult()
        self._restore_result = restore_result or audio_mute.RestoreResult()
        self._has_recorded = has_recorded

    def apply(self, *, adopt_already_muted: bool) -> "audio_mute.ApplyResult":
        self.apply_calls.append(adopt_already_muted)
        return self._apply_result

    def restore(self, *, adopt_already_muted: bool = False) -> "audio_mute.RestoreResult":
        self.restore_calls.append(adopt_already_muted)
        return self._restore_result

    def has_recorded(self) -> bool:
        return self._has_recorded


def _make_bare_window(**overrides: object) -> "tuple[main.AgentWindow, _FakeRoot]":
    window = main.AgentWindow.__new__(main.AgentWindow)
    fake_root = _FakeRoot()
    window.root = fake_root
    window._queue = queue.Queue()
    window._sync_in_flight = bool(overrides.pop("sync_in_flight", False))
    window._tray = overrides.pop("tray", None)
    window._window_visible = bool(overrides.pop("window_visible", False))
    window._sounds_muted = bool(overrides.pop("sounds_muted", True))
    window._mute_pending = bool(overrides.pop("mute_pending", False))
    window._mute = overrides.pop("mute", _FakeMute())
    window._run_login_failures = overrides.pop("run_login_failures", [])
    window._last_attention = overrides.pop("last_attention", frozenset())
    window._last_tooltip = overrides.pop("last_tooltip", None)
    window._quitting = bool(overrides.pop("quitting", False))
    window.state = overrides.pop("state", ui_state.UiState(screen=ui_state.SCREEN_READY))
    for key, value in overrides.items():
        setattr(window, key, value)
    return window, fake_root


def _drain(window: "main.AgentWindow") -> "list[object]":
    events: "list[object]" = []
    while True:
        try:
            events.append(window._queue.get_nowait())
        except queue.Empty:
            return events


# ---------------------------------------------------------------------------
# main() — withdraw-first startup, tray wiring, no root.iconify()
# ---------------------------------------------------------------------------


def _install_main_recorders(monkeypatch: pytest.MonkeyPatch, *, start_result: bool):
    timeline: "list[str]" = []
    created_windows: "list[object]" = []
    created_icons: "list[object]" = []

    class _RecorderRoot:
        def __init__(self) -> None:
            timeline.append("Tk")

        def withdraw(self) -> None:
            timeline.append("withdraw")

        def mainloop(self) -> None:
            timeline.append("mainloop")

    class _RecorderWindow:
        def __init__(self, root: object) -> None:
            timeline.append("AgentWindow")
            self.root = root
            self.state = ui_state.UiState(screen=ui_state.SCREEN_READY)
            self.attach_calls: "list[object]" = []
            self.show_initial_calls: "list[bool]" = []
            created_windows.append(self)

        def tray_command_sink(self, _command: str) -> None:
            pass

        @property
        def sounds_muted(self) -> bool:
            return True

        def attach_tray(self, icon: object) -> None:
            self.attach_calls.append(icon)

        def show_initial(self, *, minimized: bool) -> None:
            self.show_initial_calls.append(minimized)

    class _RecorderTrayIcon:
        def __init__(self, sink: object, *, tooltip: str, sounds_muted: bool) -> None:
            self.sink = sink
            self.tooltip = tooltip
            self.sounds_muted = sounds_muted
            created_icons.append(self)

        def start(self, timeout_seconds: float = 5.0) -> bool:
            return start_result

    monkeypatch.setattr(main.single_instance, "acquire_single_instance_lock", lambda: True)
    monkeypatch.setattr(main.diagnostics, "configure_logging", lambda: None)
    monkeypatch.setattr(main.diagnostics, "install_exception_hooks", lambda: None)
    monkeypatch.setattr(main.diagnostics, "log_startup_banner", lambda: None)
    monkeypatch.setattr(main.tk, "Tk", _RecorderRoot)
    monkeypatch.setattr(main, "AgentWindow", _RecorderWindow)
    monkeypatch.setattr(main.tray, "TrayIcon", _RecorderTrayIcon)
    monkeypatch.setattr(main.sys, "argv", ["prog", "--minimized"])

    return timeline, created_windows, created_icons


def test_main_constructs_tk_withdraws_then_agentwindow_then_wires_tray_and_shows_initial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline, created_windows, created_icons = _install_main_recorders(monkeypatch, start_result=True)

    main.main()

    assert timeline == ["Tk", "withdraw", "AgentWindow", "mainloop"]
    assert len(created_windows) == 1
    assert len(created_icons) == 1
    window = created_windows[0]
    assert window.attach_calls == [created_icons[0]]
    assert window.show_initial_calls == [True]


def test_main_attach_tray_receives_none_when_start_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _timeline, created_windows, _created_icons = _install_main_recorders(monkeypatch, start_result=False)

    main.main()

    window = created_windows[0]
    assert window.attach_calls == [None]


def test_main_py_never_calls_root_iconify() -> None:
    """AST proof (not a grep) that `main()` never calls `root.iconify()` — the plan's
    own replacement for the old `if options.minimized: root.iconify()` line."""
    source_path = pathlib.Path(main.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "iconify"
    ]
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# show_initial()
# ---------------------------------------------------------------------------


def test_show_initial_minimized_ready_screen_with_tray_stays_hidden() -> None:
    window, fake_root = _make_bare_window(
        tray=_FakeTray(), state=ui_state.UiState(screen=ui_state.SCREEN_READY)
    )
    window.show_initial(minimized=True)
    assert fake_root.deiconify_calls == 0
    assert window._window_visible is False


def test_show_initial_minimized_pairing_screen_shows_window() -> None:
    window, fake_root = _make_bare_window(
        tray=_FakeTray(), state=ui_state.UiState(screen=ui_state.SCREEN_PAIRING)
    )
    window.show_initial(minimized=True)
    assert fake_root.deiconify_calls == 1


def test_show_initial_minimized_with_no_tray_shows_window() -> None:
    window, fake_root = _make_bare_window(
        tray=None, state=ui_state.UiState(screen=ui_state.SCREEN_READY)
    )
    window.show_initial(minimized=True)
    assert fake_root.deiconify_calls == 1


def test_show_initial_not_minimized_shows_window() -> None:
    window, fake_root = _make_bare_window(
        tray=_FakeTray(), state=ui_state.UiState(screen=ui_state.SCREEN_READY)
    )
    window.show_initial(minimized=False)
    assert fake_root.deiconify_calls == 1


# ---------------------------------------------------------------------------
# attach_tray()
# ---------------------------------------------------------------------------


def test_attach_tray_pushes_sounds_muted_and_initial_tooltip() -> None:
    fake_tray = _FakeTray()
    window, _fake_root = _make_bare_window(
        sounds_muted=True, state=ui_state.UiState(screen=ui_state.SCREEN_READY)
    )
    window.attach_tray(fake_tray)
    assert window._tray is fake_tray
    assert fake_tray.sounds_muted_calls == [True]
    assert len(fake_tray.tooltip_calls) == 1


def test_attach_tray_with_none_leaves_tray_unset() -> None:
    window, _fake_root = _make_bare_window()
    window.attach_tray(None)
    assert window._tray is None


# ---------------------------------------------------------------------------
# _on_close()
# ---------------------------------------------------------------------------


def test_on_close_with_tray_only_hides_no_destroy_or_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    shutdown_calls: "list[None]" = []
    monkeypatch.setattr(main.mt5_bridge, "shutdown_terminal", lambda: shutdown_calls.append(None))
    fake_mute = _FakeMute()
    window, fake_root = _make_bare_window(tray=_FakeTray(), mute=fake_mute, sounds_muted=True)

    window._on_close()

    assert fake_root.withdraw_calls == 1
    assert fake_root.destroy_calls == 0
    assert shutdown_calls == []
    assert fake_mute.restore_calls == []


def test_on_close_without_tray_runs_full_quit(monkeypatch: pytest.MonkeyPatch) -> None:
    shutdown_calls: "list[None]" = []
    monkeypatch.setattr(main.mt5_bridge, "shutdown_terminal", lambda: shutdown_calls.append(None))
    window, fake_root = _make_bare_window(tray=None, sounds_muted=False, mute_pending=False)

    window._on_close()

    assert fake_root.destroy_calls == 1
    assert shutdown_calls == [None]


# ---------------------------------------------------------------------------
# _quit()
# ---------------------------------------------------------------------------


def test_quit_order_restore_then_tray_stop_then_shutdown_then_destroy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline: "list[str]" = []

    fake_mute = _FakeMute(
        restore_result=audio_mute.RestoreResult(terminal_sessions_seen=1, unmuted=1, errors=0)
    )
    fake_mute.restore = lambda *, adopt_already_muted: (  # type: ignore[method-assign]
        timeline.append("restore") or fake_mute._restore_result
    )
    fake_tray = _FakeTray()
    fake_tray.stop = lambda timeout_seconds=3.0: timeline.append("tray.stop")  # type: ignore[method-assign]
    monkeypatch.setattr(
        main.mt5_bridge, "shutdown_terminal", lambda: timeline.append("shutdown_terminal")
    )

    window, fake_root = _make_bare_window(tray=fake_tray, mute=fake_mute, sounds_muted=True, mute_pending=False)
    fake_root.destroy = lambda: timeline.append("destroy")  # type: ignore[method-assign]

    window._quit()

    assert timeline == ["restore", "tray.stop", "shutdown_terminal", "destroy"]


def test_quit_clears_mute_pending_on_clean_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_mute = _FakeMute(
        restore_result=audio_mute.RestoreResult(terminal_sessions_seen=1, unmuted=1, errors=0),
        has_recorded=False,
    )
    monkeypatch.setattr(main.mt5_bridge, "shutdown_terminal", lambda: None)
    saved: "list[bool]" = []
    monkeypatch.setattr(main.config_store, "save_mt5_mute_pending", lambda v: saved.append(v))

    window, _fake_root = _make_bare_window(tray=None, mute=fake_mute, sounds_muted=True, mute_pending=True)
    window._quit()

    assert saved == [False]
    assert window._mute_pending is False


def test_quit_keeps_pending_when_mute_still_has_recorded_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_mute = _FakeMute(
        restore_result=audio_mute.RestoreResult(terminal_sessions_seen=1, unmuted=0, errors=0),
        has_recorded=True,
    )
    monkeypatch.setattr(main.mt5_bridge, "shutdown_terminal", lambda: None)
    saved: "list[bool]" = []
    monkeypatch.setattr(main.config_store, "save_mt5_mute_pending", lambda v: saved.append(v))

    window, _fake_root = _make_bare_window(tray=None, mute=fake_mute, sounds_muted=True, mute_pending=True)
    window._quit()

    assert saved == []


def test_quit_twice_only_acts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: "list[str]" = []
    monkeypatch.setattr(main.mt5_bridge, "shutdown_terminal", lambda: calls.append("shutdown"))
    window, fake_root = _make_bare_window(
        tray=None, mute=_FakeMute(), sounds_muted=False, mute_pending=False
    )

    window._quit()
    window._quit()

    assert calls == ["shutdown"]
    assert fake_root.destroy_calls == 1


def test_quit_destroy_runs_even_if_restore_and_tray_stop_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    class _RaisingMute:
        def restore(self, *, adopt_already_muted: bool) -> "audio_mute.RestoreResult":
            raise RuntimeError("boom-restore")

        def has_recorded(self) -> bool:
            return False

    class _RaisingTray:
        def stop(self, timeout_seconds: float = 3.0) -> None:
            raise RuntimeError("boom-stop")

    monkeypatch.setattr(main.mt5_bridge, "shutdown_terminal", lambda: None)
    window, fake_root = _make_bare_window(
        tray=_RaisingTray(), mute=_RaisingMute(), sounds_muted=True, mute_pending=False
    )

    window._quit()

    assert fake_root.destroy_calls == 1


# ---------------------------------------------------------------------------
# _handle_tray_command()
# ---------------------------------------------------------------------------


def test_handle_tray_command_sync_now_calls_on_refresh_clicked_once() -> None:
    window, _fake_root = _make_bare_window()
    calls: "list[None]" = []
    window._on_refresh_clicked = lambda: calls.append(None)  # type: ignore[method-assign]
    window._handle_tray_command(tray.CMD_SYNC_NOW)
    assert calls == [None]


def test_handle_tray_command_open_window_shows_the_window() -> None:
    window, fake_root = _make_bare_window()
    window._handle_tray_command(tray.CMD_OPEN_WINDOW)
    assert fake_root.deiconify_calls == 1
    assert fake_root.lift_calls == 1
    assert fake_root.focus_force_calls == 1


def test_handle_tray_command_open_site_opens_stored_https_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main.config_store, "load_base_url", lambda: "https://example.com")
    opened: "list[str]" = []
    monkeypatch.setattr(main.webbrowser, "open", lambda url: opened.append(url))
    window, _fake_root = _make_bare_window()
    window._handle_tray_command(tray.CMD_OPEN_SITE)
    assert opened == ["https://example.com"]


def test_handle_tray_command_open_site_falls_back_on_file_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main.config_store, "load_base_url", lambda: "file:///x")
    opened: "list[str]" = []
    monkeypatch.setattr(main.webbrowser, "open", lambda url: opened.append(url))
    window, _fake_root = _make_bare_window()
    window._handle_tray_command(tray.CMD_OPEN_SITE)
    assert opened == ["https://treedger.com"]


def test_handle_tray_command_open_site_falls_back_when_none_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main.config_store, "load_base_url", lambda: None)
    opened: "list[str]" = []
    monkeypatch.setattr(main.webbrowser, "open", lambda url: opened.append(url))
    window, _fake_root = _make_bare_window()
    window._handle_tray_command(tray.CMD_OPEN_SITE)
    assert opened == ["https://treedger.com"]


def test_handle_tray_command_toggle_sounds_flips_saves_and_reconciles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: "list[bool]" = []
    monkeypatch.setattr(main.config_store, "save_mt5_sounds_muted", lambda v: saved.append(v))
    fake_tray = _FakeTray()
    window, _fake_root = _make_bare_window(tray=fake_tray, sounds_muted=True)
    reconciled: "list[None]" = []
    window._reconcile_mt5_sound = lambda: reconciled.append(None)  # type: ignore[method-assign]

    window._handle_tray_command(tray.CMD_TOGGLE_SOUNDS)

    assert window._sounds_muted is False
    assert saved == [False]
    assert fake_tray.sounds_muted_calls == [False]
    assert reconciled == [None]


def test_handle_tray_command_exit_calls_quit() -> None:
    window, _fake_root = _make_bare_window()
    calls: "list[None]" = []
    window._quit = lambda: calls.append(None)  # type: ignore[method-assign]
    window._handle_tray_command(tray.CMD_EXIT)
    assert calls == [None]


def test_handle_tray_command_tray_failed_clears_tray_and_shows_window() -> None:
    window, fake_root = _make_bare_window(tray=_FakeTray())
    window._handle_tray_command(tray.CMD_TRAY_FAILED)
    assert window._tray is None
    assert fake_root.deiconify_calls == 1


def test_handle_tray_command_unknown_does_nothing() -> None:
    window, fake_root = _make_bare_window()
    window._handle_tray_command("bogus-command")
    assert fake_root.deiconify_calls == 0
    assert fake_root.destroy_calls == 0


# ---------------------------------------------------------------------------
# _reconcile_mt5_sound() / _set_mute_pending()
# ---------------------------------------------------------------------------


def test_reconcile_muted_pending_false_applies_and_sets_pending_when_newly_muted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_mute = _FakeMute(apply_result=audio_mute.ApplyResult(newly_muted=1))
    saved: "list[bool]" = []
    monkeypatch.setattr(main.config_store, "save_mt5_mute_pending", lambda v: saved.append(v))
    window, _fake_root = _make_bare_window(mute=fake_mute, sounds_muted=True, mute_pending=False)

    window._reconcile_mt5_sound()

    assert fake_mute.apply_calls == [False]
    assert window._mute_pending is True
    assert saved == [True]


def test_reconcile_muted_pending_true_applies_with_adopt() -> None:
    fake_mute = _FakeMute(apply_result=audio_mute.ApplyResult(newly_muted=0))
    window, _fake_root = _make_bare_window(mute=fake_mute, sounds_muted=True, mute_pending=True)

    window._reconcile_mt5_sound()

    assert fake_mute.apply_calls == [True]


def test_reconcile_sounds_on_pending_true_restores_and_clears_on_clean_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_mute = _FakeMute(
        restore_result=audio_mute.RestoreResult(terminal_sessions_seen=1, unmuted=1, errors=0)
    )
    saved: "list[bool]" = []
    monkeypatch.setattr(main.config_store, "save_mt5_mute_pending", lambda v: saved.append(v))
    window, _fake_root = _make_bare_window(mute=fake_mute, sounds_muted=False, mute_pending=True)

    window._reconcile_mt5_sound()

    assert fake_mute.restore_calls == [True]
    assert saved == [False]


def test_reconcile_sounds_on_pending_true_keeps_pending_when_nothing_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_mute = _FakeMute(restore_result=audio_mute.RestoreResult(terminal_sessions_seen=0))
    saved: "list[bool]" = []
    monkeypatch.setattr(main.config_store, "save_mt5_mute_pending", lambda v: saved.append(v))
    window, _fake_root = _make_bare_window(mute=fake_mute, sounds_muted=False, mute_pending=True)

    window._reconcile_mt5_sound()

    assert saved == []


def test_reconcile_sounds_on_pending_false_makes_no_controller_call() -> None:
    fake_mute = _FakeMute()
    window, _fake_root = _make_bare_window(mute=fake_mute, sounds_muted=False, mute_pending=False)

    window._reconcile_mt5_sound()

    assert fake_mute.apply_calls == []
    assert fake_mute.restore_calls == []


def test_set_mute_pending_save_oserror_is_logged_and_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_value: bool) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(main.config_store, "save_mt5_mute_pending", _raise)
    window, _fake_root = _make_bare_window()

    window._set_mute_pending(True)  # must not raise

    assert window._mute_pending is True


# ---------------------------------------------------------------------------
# _on_mute_tick()
# ---------------------------------------------------------------------------


def test_mute_tick_reschedules_idle_interval() -> None:
    window, fake_root = _make_bare_window(sync_in_flight=False)
    window._reconcile_mt5_sound = lambda: None  # type: ignore[method-assign]

    window._on_mute_tick()

    assert fake_root.after_calls == [(audio_mute.REAPPLY_INTERVAL_SECONDS * 1000, window._on_mute_tick)]


def test_mute_tick_reschedules_fast_interval_during_sync() -> None:
    window, fake_root = _make_bare_window(sync_in_flight=True)
    window._reconcile_mt5_sound = lambda: None  # type: ignore[method-assign]

    window._on_mute_tick()

    assert fake_root.after_calls == [
        (audio_mute.REAPPLY_INTERVAL_DURING_SYNC_SECONDS * 1000, window._on_mute_tick)
    ]


def test_mute_tick_reschedules_even_when_reconcile_raises() -> None:
    window, fake_root = _make_bare_window(sync_in_flight=False)

    def _raise() -> None:
        raise RuntimeError("boom")

    window._reconcile_mt5_sound = _raise  # type: ignore[method-assign]
    window._on_mute_tick()

    assert fake_root.after_calls == [(audio_mute.REAPPLY_INTERVAL_SECONDS * 1000, window._on_mute_tick)]


def test_init_schedules_first_mute_tick_and_keeps_existing_schedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main.AgentWindow, "_build_widgets", lambda self: None)
    monkeypatch.setattr(main.AgentWindow, "_render", lambda self: None)
    monkeypatch.setattr(main.config_store, "load_token", lambda: None)
    monkeypatch.setattr(main.terminal_discovery, "find_terminal_path", lambda: None)
    monkeypatch.setattr(main.autostart, "repair_autostart_path", lambda: "no autostart value")

    fake_root = _FakeRoot()
    window = main.AgentWindow(fake_root)  # type: ignore[arg-type]

    mute_calls = [c for c in fake_root.after_calls if c[1] == window._on_mute_tick]
    assert mute_calls == [(main._MUTE_FIRST_DELAY_MS, window._on_mute_tick)]

    startup = [c for c in fake_root.after_calls if c[1] == window._on_startup_sync]
    periodic = [c for c in fake_root.after_calls if c[1] == window._on_periodic_tick]
    assert startup == [(main.constants.STARTUP_SYNC_DELAY_SECONDS * 1000, window._on_startup_sync)]
    assert periodic == [(main.constants.SYNC_INTERVAL_SECONDS * 1000, window._on_periodic_tick)]


# ---------------------------------------------------------------------------
# Worker report() — F13: login-failed signal
# ---------------------------------------------------------------------------


def test_worker_report_pushes_login_failed_signal_on_auth_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run_sync(_client, report, *, report_stage=None, cancel_event=None):  # type: ignore[no-untyped-def]
        report(
            sync.AccountProgress(
                account_id="a1", mt_login="123", phase=sync.PHASE_FAILED, outcome=errors.OUTCOME_AUTH_FAILED
            )
        )
        return sync.RunSummary()

    monkeypatch.setattr(sync, "run_sync", _fake_run_sync)
    window, _fake_root = _make_bare_window()
    window._run_sync_worker(object(), None)  # type: ignore[arg-type]
    events = _drain(window)

    signals = [e for e in events if isinstance(e, main._LoginFailedSignal)]
    assert len(signals) == 1
    assert signals[0].mt_login == "123"


def test_worker_report_no_login_failed_signal_on_timeout_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run_sync(_client, report, *, report_stage=None, cancel_event=None):  # type: ignore[no-untyped-def]
        report(
            sync.AccountProgress(account_id="a1", mt_login="123", phase=sync.PHASE_FAILED, outcome="timeout")
        )
        return sync.RunSummary()

    monkeypatch.setattr(sync, "run_sync", _fake_run_sync)
    window, _fake_root = _make_bare_window()
    window._run_sync_worker(object(), None)  # type: ignore[arg-type]
    events = _drain(window)

    assert not any(isinstance(e, main._LoginFailedSignal) for e in events)


def test_poll_queue_handles_login_failed_signal_by_appending_to_run_login_failures() -> None:
    window, fake_root = _make_bare_window()
    window._queue.put(main._LoginFailedSignal("999"))
    window._poll_queue()
    assert window._run_login_failures == ["999"]


def test_poll_queue_handles_tray_command_via_handle_tray_command() -> None:
    window, fake_root = _make_bare_window()
    calls: "list[str]" = []
    window._handle_tray_command = lambda command: calls.append(command)  # type: ignore[method-assign]
    window._queue.put(main._TrayCommand(tray.CMD_SYNC_NOW))
    window._poll_queue()
    assert calls == [tray.CMD_SYNC_NOW]


# ---------------------------------------------------------------------------
# _refresh_tray_status()
# ---------------------------------------------------------------------------


def test_refresh_tray_status_sets_tooltip_only_when_text_changed() -> None:
    fake_tray = _FakeTray()
    window, _fake_root = _make_bare_window(
        tray=fake_tray, state=ui_state.UiState(screen=ui_state.SCREEN_READY), last_tooltip=None
    )
    window._refresh_tray_status()
    assert len(fake_tray.tooltip_calls) == 1

    window._refresh_tray_status()  # same state, same text — no second call
    assert len(fake_tray.tooltip_calls) == 1


def test_refresh_tray_status_is_a_no_op_without_a_tray() -> None:
    window, _fake_root = _make_bare_window(tray=None)
    window._refresh_tray_status()  # must not raise


# ---------------------------------------------------------------------------
# _evaluate_attention()
# ---------------------------------------------------------------------------


def test_evaluate_attention_balloons_once_per_run_for_login_failure_while_hidden() -> None:
    fake_tray = _FakeTray()
    window, _fake_root = _make_bare_window(
        tray=fake_tray,
        window_visible=False,
        run_login_failures=["123"],
        state=ui_state.UiState(screen=ui_state.SCREEN_READY),
    )
    window._evaluate_attention()
    assert len(fake_tray.balloon_calls) == 1

    window._evaluate_attention()  # same run, same failure set — no second balloon
    assert len(fake_tray.balloon_calls) == 1


def test_evaluate_attention_balloons_again_after_an_intervening_clean_run() -> None:
    fake_tray = _FakeTray()
    window, _fake_root = _make_bare_window(
        tray=fake_tray,
        window_visible=False,
        run_login_failures=["123"],
        state=ui_state.UiState(screen=ui_state.SCREEN_READY),
    )
    window._evaluate_attention()
    assert len(fake_tray.balloon_calls) == 1

    window._run_login_failures = []  # a clean run in between
    window._evaluate_attention()
    assert len(fake_tray.balloon_calls) == 1

    window._run_login_failures = ["123"]  # failing again
    window._evaluate_attention()
    assert len(fake_tray.balloon_calls) == 2


def test_evaluate_attention_no_balloon_while_window_visible_but_dedupe_still_updates() -> None:
    fake_tray = _FakeTray()
    window, _fake_root = _make_bare_window(
        tray=fake_tray,
        window_visible=True,
        run_login_failures=["123"],
        state=ui_state.UiState(screen=ui_state.SCREEN_READY),
    )
    window._evaluate_attention()
    assert fake_tray.balloon_calls == []
    assert ui_state.ATTENTION_LOGIN_FAILED in window._last_attention


def test_evaluate_attention_no_terminal_at_startup_balloons_when_hidden() -> None:
    fake_tray = _FakeTray()
    window, _fake_root = _make_bare_window(
        tray=fake_tray,
        window_visible=False,
        run_login_failures=[],
        state=ui_state.UiState(screen=ui_state.SCREEN_NO_TERMINAL),
    )
    window._evaluate_attention()
    assert len(fake_tray.balloon_calls) == 1


# ---------------------------------------------------------------------------
# Owner follow-up to quick 260926-ieo — two more balloon states (token revoked,
# «Алготрейдинг» off), exercised through the SAME generic `_evaluate_attention()`
# dedupe logic the two original states already use — no new call site was added
# because `attention_kinds()` recomputes fresh from `state.notices` every time.
# ---------------------------------------------------------------------------


def test_evaluate_attention_token_revoked_balloons_once_then_not_again() -> None:
    fake_tray = _FakeTray()
    window, _fake_root = _make_bare_window(
        tray=fake_tray,
        window_visible=False,
        state=ui_state.UiState(screen=ui_state.SCREEN_PAIRING, notices=frozenset({ui_state.NOTICE_TOKEN_REVOKED})),
    )
    window._evaluate_attention()
    assert len(fake_tray.balloon_calls) == 1

    window._evaluate_attention()  # same state, repeated poll — no second balloon
    assert len(fake_tray.balloon_calls) == 1


def test_evaluate_attention_token_revoked_recovers_then_relapses() -> None:
    fake_tray = _FakeTray()
    window, _fake_root = _make_bare_window(
        tray=fake_tray,
        window_visible=False,
        state=ui_state.UiState(screen=ui_state.SCREEN_PAIRING, notices=frozenset({ui_state.NOTICE_TOKEN_REVOKED})),
    )
    window._evaluate_attention()
    assert len(fake_tray.balloon_calls) == 1

    window.state = ui_state.UiState(screen=ui_state.SCREEN_READY)  # re-paired — notice cleared
    window._evaluate_attention()
    assert len(fake_tray.balloon_calls) == 1  # no new balloon on recovery itself

    window.state = ui_state.UiState(
        screen=ui_state.SCREEN_PAIRING, notices=frozenset({ui_state.NOTICE_TOKEN_REVOKED})
    )  # revoked again
    window._evaluate_attention()
    assert len(fake_tray.balloon_calls) == 2


def test_evaluate_attention_algo_trading_off_balloons_once_then_not_again() -> None:
    fake_tray = _FakeTray()
    window, _fake_root = _make_bare_window(
        tray=fake_tray,
        window_visible=False,
        state=ui_state.UiState(screen=ui_state.SCREEN_READY, notices=frozenset({ui_state.NOTICE_ALGO_TRADING_OFF})),
    )
    window._evaluate_attention()
    assert len(fake_tray.balloon_calls) == 1

    window._evaluate_attention()  # repeated poll — still off, no second balloon
    assert len(fake_tray.balloon_calls) == 1


def test_evaluate_attention_algo_trading_off_recovers_then_relapses() -> None:
    fake_tray = _FakeTray()
    window, _fake_root = _make_bare_window(
        tray=fake_tray,
        window_visible=False,
        state=ui_state.UiState(screen=ui_state.SCREEN_READY, notices=frozenset({ui_state.NOTICE_ALGO_TRADING_OFF})),
    )
    window._evaluate_attention()
    assert len(fake_tray.balloon_calls) == 1

    window.state = ui_state.UiState(screen=ui_state.SCREEN_READY)  # turned back on
    window._evaluate_attention()
    assert len(fake_tray.balloon_calls) == 1

    window.state = ui_state.UiState(
        screen=ui_state.SCREEN_READY, notices=frozenset({ui_state.NOTICE_ALGO_TRADING_OFF})
    )  # switched off again
    window._evaluate_attention()
    assert len(fake_tray.balloon_calls) == 2


def test_evaluate_attention_routine_successful_sync_fires_no_balloon_for_any_state() -> None:
    fake_tray = _FakeTray()
    window, _fake_root = _make_bare_window(
        tray=fake_tray,
        window_visible=False,
        run_login_failures=[],
        state=ui_state.UiState(screen=ui_state.SCREEN_READY, last_success_label="24.09.2026 10:00"),
    )
    window._evaluate_attention()
    assert fake_tray.balloon_calls == []
