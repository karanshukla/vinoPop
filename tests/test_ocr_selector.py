"""Keyboard selection in the OCR region selector (#125): the rectangle the
keys build, and the crop handed to the callback. Builds the real overlay
window without showing it, so it needs GTK 4 and a display (CI's test-gtk
job); it skips without."""

from __future__ import annotations

import pytest

pytest.importorskip("gi")


@pytest.fixture(scope="module")
def sel_mod():
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk

        from capture import ocr_selector
        from ui import gtk4_app
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 unavailable: {exc}")
    try:
        gtk4_app.init()
    except Exception as exc:  # Gtk.init() raises without a display
        pytest.skip(f"no display: {exc}")
    if Gdk.Display.get_default() is None:
        pytest.skip("no display")
    return ocr_selector


# The window is 400x300 logical; the screenshot is 800x600, as on a 2x screen
W, H = 400, 300


@pytest.fixture
def selector(sel_mod, tmp_path, monkeypatch):
    from gi.repository import GdkPixbuf

    monkeypatch.setattr(sel_mod, "_layer_shell", lambda: None)
    shot = tmp_path / "shot.png"
    pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 2 * W, 2 * H)
    pb.fill(0xFFFFFFFF)
    pb.savev(str(shot), "png", [], [])
    got: list = []
    sel = sel_mod._Selector(str(shot), got.append)
    sel._area.get_width = lambda: W
    sel._area.get_height = lambda: H
    sel.announced = []
    sel._announce = sel.announced.append
    yield sel, got
    sel.destroy()


def _press(sel, key, *mods):
    from gi.repository import Gdk

    state = Gdk.ModifierType(0)
    for m in mods:
        state |= m
    return sel._on_key(None, key, 0, state)


def _crop_size(path):
    from gi.repository import GdkPixbuf

    pb = GdkPixbuf.Pixbuf.new_from_file(path)
    return pb.get_width(), pb.get_height()


def test_window_is_named(selector, sel_mod):
    sel, _ = selector
    assert sel.get_title() == "Select a region to read"


def test_enter_with_nothing_selected_reads_the_whole_screen(selector):
    from gi.repository import Gdk

    sel, got = selector
    _press(sel, Gdk.KEY_Return)
    assert len(got) == 1 and got[0]
    assert _crop_size(got[0]) == (2 * W, 2 * H)


def test_escape_cancels(selector):
    from gi.repository import Gdk

    sel, got = selector
    _press(sel, Gdk.KEY_Right)
    _press(sel, Gdk.KEY_Escape)
    assert got == [None]


def test_arrows_place_move_and_resize(selector):
    from gi.repository import Gdk

    sel, got = selector
    shift, ctrl = Gdk.ModifierType.SHIFT_MASK, Gdk.ModifierType.CONTROL_MASK
    _press(sel, Gdk.KEY_Down)  # places the box: W/3 x H/4, centred
    assert sel._selection() == (133, 112, 133, 75)
    assert sel.announced[-1] == "133 by 75, at 133, 112"
    _press(sel, Gdk.KEY_Right)
    _press(sel, Gdk.KEY_Up, ctrl)
    assert sel._selection() == (143, 62, 133, 75)
    _press(sel, Gdk.KEY_Right, shift)
    _press(sel, Gdk.KEY_Down, shift, ctrl)
    _press(sel, Gdk.KEY_Left, shift)
    assert sel._selection() == (143, 62, 133, 125)
    assert sel.announced[-1] == "133 by 125, at 143, 62"
    _press(sel, Gdk.KEY_KP_Enter)
    # Crop is in screenshot pixels: twice the logical box
    assert _crop_size(got[0]) == (266, 250)


def test_box_stays_on_screen(sel_mod):
    nudge = sel_mod.nudge
    assert nudge((0, 0, 50, 50), -1, -1, False, 10, W, H) == (0, 0, 50, 50)
    assert nudge((340, 240, 50, 50), 1, 1, False, 50, W, H) == (350, 250, 50, 50)
    # Growing stops at the edge, shrinking at the minimum size
    assert nudge((340, 240, 50, 50), 1, 1, True, 50, W, H) == (340, 240, 60, 60)
    assert nudge((0, 0, 12, 12), -1, -1, True, 10, W, H) == (0, 0, 8, 8)
