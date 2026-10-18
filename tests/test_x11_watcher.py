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
