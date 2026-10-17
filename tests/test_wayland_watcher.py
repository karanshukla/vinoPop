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
