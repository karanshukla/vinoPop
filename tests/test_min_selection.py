"""min_selection_length filters the auto-popup only: opt-in, and never the
hotkey, which shows the popup for a selection of any length."""

import types

import pytest

pytest.importorskip("gi")


@pytest.fixture(scope="module")
def main_mod():
    # as in test_watcher_toggle: main may set GDK_BACKEND at import
    mp = pytest.MonkeyPatch()
    mp.setenv("GDK_BACKEND", "x11")
    try:
        import main
    finally:
        mp.undo()
    return main


def _app(enabled, min_len=3):
    return types.SimpleNamespace(
        settings={"min_selection_length_enabled": enabled}, min_len=min_len, ignore_ws=True
    )


def test_the_watcher_skips_a_selection_one_short_of_the_minimum(main_mod):
    assert main_mod.App._below_min_selection(_app(True), "ab") is True


def test_the_watcher_shows_a_selection_at_the_minimum(main_mod):
    assert main_mod.App._below_min_selection(_app(True), "abc") is False


def test_the_filter_is_off_until_enabled(main_mod):
    assert main_mod.App._below_min_selection(_app(False), "a") is False


def test_the_hotkey_shows_a_selection_shorter_than_the_minimum(main_mod, monkeypatch):
    shown = []
    app = _app(True)
    app._note_use = lambda: None
    app.popup = types.SimpleNamespace(show_for=lambda text, *a, **kw: shown.append(text))
    backend = types.SimpleNamespace(active_window_haystacks=lambda: [])
    monkeypatch.setattr(main_mod, "get_backend", lambda: backend)
    monkeypatch.setattr(main_mod, "is_focus_editable", lambda **kw: False)
    monkeypatch.setattr(main_mod.GLib, "idle_add", lambda fn: fn())
    main_mod.App._prepare_popup(app, "a", 0, 0, via_hotkey=True)
    assert shown == ["a"]
