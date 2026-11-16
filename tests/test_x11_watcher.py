"""Which PRIMARY changes the X11 watcher treats as real selections."""

import time
import types

import pytest

pytest.importorskip("Xlib")

from platform_backend.base import expect_selection  # noqa: E402
from triggers.watcher import SelectionWatcher  # noqa: E402

CLIPBOARD = 42
PRIMARY = 1


def _watcher(clipboard):
    w = SelectionWatcher(lambda *_: None)
    w._clipboard_atom = CLIPBOARD
    w._read_clipboard = lambda: clipboard
    return w


def _event(selection):
    return types.SimpleNamespace(selection=selection)


def test_clipboard_event_is_only_timestamped():
    w = _watcher("")
    assert not w._note_event(_event(CLIPBOARD))
    assert w._clip_changed_at is not None
    assert w._note_event(_event(PRIMARY))


def test_copy_button_is_ignored():
    w = _watcher("curl … | bash")
    w._clip_changed_at = 100.1
    assert not w._should_show("curl … | bash", burst_t0=100.0)


def test_reselecting_what_was_copied_long_ago_is_shown():
    w = _watcher("foo")
    w._clip_changed_at = 50.0
    assert w._should_show("foo", burst_t0=100.0)


def test_ocr_text_is_shown_though_clipboard_matches():
    w = _watcher("recognised text")
    w._clip_changed_at = 100.1
    expect_selection("recognised text")
    assert w._should_show("recognised text", burst_t0=100.0)


class _Gestures:
    def __init__(self, selected, settle=0.05):
        self.selected, self.settle = selected, settle

    def selected_since(self, t):
        return self.selected

    def settle_s(self, burst_t0, default):
        return self.settle


def test_primary_change_without_gesture_is_ignored():
    w = _watcher("")
    w._gestures = _Gestures(selected=False)
    assert not w._should_show("old terminal text", burst_t0=100.0)


def test_settle_comes_from_the_gesture():
    w = _watcher("")
    w._gestures = _Gestures(selected=True, settle=0.05)
    assert w._settle_s(100.0) == 0.05


KEY_RIGHT = 106  # evdev
X_KEYCODE_OFFSET = 8
X_BUTTON_LEFT = 1


@pytest.fixture
def xrecord():
    from platform_backend.x11_input import XRecordInput

    src = XRecordInput()
    src._recording = True  # no thread started
    return src


def _tracker(src):
    from platform_backend.gestures import SelectionGestures

    g = SelectionGestures(src)
    src._subs[2] = g._on_event
    return g


def test_x_keys_reach_subscribers_as_evdev_codes(xrecord):
    from Xlib import X

    from platform_backend import evdev_input as ev

    seen = []
    xrecord._subs[1] = seen.append
    xrecord.on_x_event(X.KeyPress, ev.KEY_LEFTSHIFT + X_KEYCODE_OFFSET, 1000)
    xrecord.on_x_event(X.KeyPress, KEY_RIGHT + X_KEYCODE_OFFSET, 1100)
    assert [(e.code, e.value) for e in seen] == [(ev.KEY_LEFTSHIFT, 1), (KEY_RIGHT, 1)]


def test_shift_arrow_over_xrecord_is_a_selecting_gesture(xrecord):
    from Xlib import X

    from platform_backend import evdev_input as ev

    g, t = _tracker(xrecord), time.monotonic()
    xrecord.on_x_event(X.KeyPress, ev.KEY_LEFTSHIFT + X_KEYCODE_OFFSET, 1000)
    xrecord.on_x_event(X.KeyPress, KEY_RIGHT + X_KEYCODE_OFFSET, 1100)
    assert g.selected_since(t)


def test_a_click_over_xrecord_is_not_a_selecting_gesture(xrecord):
    from Xlib import X

    g, t = _tracker(xrecord), time.monotonic()
    xrecord.on_x_event(X.ButtonPress, X_BUTTON_LEFT, 5000)
    xrecord.on_x_event(X.ButtonRelease, X_BUTTON_LEFT, 5080)
    assert not g.selected_since(t)


def test_a_drag_over_xrecord_is_a_selecting_gesture(xrecord):
    from Xlib import X

    g, t = _tracker(xrecord), time.monotonic()
    xrecord.on_x_event(X.ButtonPress, X_BUTTON_LEFT, 6000)
    xrecord.on_x_event(X.ButtonRelease, X_BUTTON_LEFT, 6500)
    assert g.selected_since(t)


def _shown_after_reads(monkeypatch, primary):
    from triggers import watcher

    monkeypatch.setattr(watcher.time, "sleep", lambda _s: None)
    shown = []
    w = SelectionWatcher(lambda text, x, y: shown.append(text))
    w._gestures = None
    w._read_primary = lambda: primary
    w._pointer_position = lambda _dpy: (0, 0)
    return w, shown


def test_reselecting_the_same_text_shows_it_again(monkeypatch):
    w, shown = _shown_after_reads(monkeypatch, "foo")
    w._handle_selection_change(None, burst_t0=0.0)
    w._handle_selection_change(None, burst_t0=0.0)
    assert shown == ["foo", "foo"]


def test_a_blank_selection_is_not_shown(monkeypatch):
    w, shown = _shown_after_reads(monkeypatch, "  ")
    w._handle_selection_change(None, burst_t0=0.0)
    assert shown == []
