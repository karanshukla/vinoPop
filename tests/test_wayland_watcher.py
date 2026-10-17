"""Which PRIMARY changes the Wayland watcher treats as real selections."""

import pytest

pytest.importorskip("gi")

from platform_backend.wayland_kde import WaylandSelectionWatcher  # noqa: E402


class _Backend:
    def __init__(self, clipboard):
        self.clipboard = clipboard

    def read_selection(self, source):
        assert source == "clipboard"
        return self.clipboard


def _watcher(clipboard):
    backend = _Backend(clipboard)
    w = WaylandSelectionWatcher(backend, lambda *_: None)
    w._last_clipboard = clipboard
    return w, backend


def test_copy_button_is_ignored():
    w, backend = _watcher("old")
    backend.clipboard = "curl … | bash"  # the button set both at once
    assert w._is_copy_button("curl … | bash")


def test_plain_selection_is_shown():
    w, _ = _watcher("old")
    assert not w._is_copy_button("some highlighted text")


def test_reselecting_what_was_copied_is_shown():
    w, _ = _watcher("copied earlier")
    assert not w._is_copy_button("copied earlier")


# ---- modifier+double-click over the shared evdev hub (#27) --------------------


class _Hub:
    def __init__(self):
        self.subs = {}

    def subscribe(self, cb):
        self.subs[1] = cb
        return 1

    def unsubscribe(self, handle):
        self.subs.pop(handle)


class _NoXkb:
    def update(self, code, down):
        pass

    def is_active(self, mod):
        return None  # raw-keycode fallback


def test_ctrl_double_click_from_a_virtual_keyboard_fires(monkeypatch):
    from platform_backend import evdev_input as ev
    from platform_backend import wayland_kde

    fired = []
    monkeypatch.setattr(wayland_kde, "_XkbModifierState", _NoXkb)
    monkeypatch.setattr(wayland_kde.GLib, "timeout_add", lambda ms, fn, *a: fired.append(a))

    class Backend:
        def pointer_position(self):
            return (500, 300)

    hub = _Hub()
    w = wayland_kde.WaylandDoubleClickWatcher(Backend(), lambda x, y: None, hub=hub)
    w.start()
    send = hub.subs[1]
    kbd, ptr = frozenset({ev.KEYBOARD}), frozenset({ev.POINTER})
    # keyd's virtual keyboard carries the Ctrl press; the mouse the clicks
    send(ev.InputEvent(ev.KEY_LEFTCTRL, 1, kbd, 10.0))
    send(ev.InputEvent(ev.BTN_LEFT, 1, ptr, 10.10))
    send(ev.InputEvent(ev.BTN_LEFT, 0, ptr, 10.15))
    send(ev.InputEvent(ev.BTN_LEFT, 1, ptr, 10.30))
    assert fired == [(500, 300)]
    # without the modifier, a double-click is just a double-click
    send(ev.InputEvent(ev.KEY_LEFTCTRL, 0, kbd, 11.0))
    send(ev.InputEvent(ev.BTN_LEFT, 1, ptr, 11.1))
    send(ev.InputEvent(ev.BTN_LEFT, 1, ptr, 11.2))
    assert len(fired) == 1
    w.stop()
    assert hub.subs == {}
