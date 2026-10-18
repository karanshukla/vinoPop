"""Which PRIMARY changes the X11 watcher treats as real selections."""

import types

import pytest

pytest.importorskip("Xlib")

from platform_backend.base import expect_selection  # noqa: E402
from watcher import SelectionWatcher  # noqa: E402

CLIPBOARD = 42


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
    assert w._note_event(_event(1))  # PRIMARY


def test_copy_button_is_ignored():
    w = _watcher("curl … | bash")
    w._clip_changed_at = 100.1
    assert not w._should_show("curl … | bash", burst_t0=100.0)


def test_reselecting_what_was_copied_is_shown():
    # select "foo", Ctrl+C, later select "foo" again
    w = _watcher("foo")
    w._clip_changed_at = 50.0
    assert w._should_show("foo", burst_t0=100.0)


def test_ocr_text_is_shown_though_clipboard_matches():
    w = _watcher("recognised text")
    w._clip_changed_at = 100.1
    expect_selection("recognised text")
    assert w._should_show("recognised text", burst_t0=100.0)


# ---- selecting gestures over XRecord -----------------------------------------


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


def test_x_events_reach_the_gesture_tracker():
    from Xlib import X

    from platform_backend import evdev_input as ev
    from platform_backend.gestures import SelectionGestures
    from platform_backend.x11_input import XRecordInput

    src = XRecordInput()
    src._recording = True  # pretend XRecord is up; no thread started
    seen = []
    src._subs[1] = seen.append
    g = SelectionGestures(src)
    src._subs[2] = g._on_event
    import time

    t = time.monotonic()
    src.on_x_event(X.KeyPress, ev.KEY_LEFTSHIFT + 8, 1000)
    src.on_x_event(X.KeyPress, 114, 1100)  # Right arrow: evdev 106
    assert [(e.code, e.value) for e in seen] == [(ev.KEY_LEFTSHIFT, 1), (106, 1)]
    assert g.selected_since(t)

    g2, t = SelectionGestures(src), time.monotonic()
    src._subs[2] = g2._on_event
    src.on_x_event(X.ButtonPress, 1, 5000)
    src.on_x_event(X.ButtonRelease, 1, 5080)  # click to focus
    assert not g2.selected_since(t)
    src.on_x_event(X.ButtonPress, 1, 6000)
    src.on_x_event(X.ButtonRelease, 1, 6500)  # drag
    assert g2.selected_since(t)
