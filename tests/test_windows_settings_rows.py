"""Settings rows built for real: the textarea placeholder and combo rows.
They need GTK 4 and a display; with none they skip."""

from __future__ import annotations

import pytest

pytest.importorskip("gi")


@pytest.fixture(scope="module")
def gtk():
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gdk, Gtk

        from ui import gtk4_app, settings_gui
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 / libadwaita unavailable: {exc}")
    try:
        gtk4_app.init()
    except Exception as exc:  # Gtk.init() raises without a display
        pytest.skip(f"no display: {exc}")
    if Gdk.Display.get_default() is None:
        pytest.skip("no display")
    return Gtk, Gdk, settings_gui


def _view_with_placeholder(gtk):
    Gtk, _Gdk, gui = gtk
    view = Gtk.TextView()
    gui._attach_textarea_placeholder(view, "one per line")
    return view


def _controller(view, cls):
    return next(c for c in view.observe_controllers() if isinstance(c, cls))


def _press(gtk, view, keyval):
    Gtk, Gdk, _gui = gtk
    _controller(view, Gtk.EventControllerKey).emit("key-pressed", keyval, 0, Gdk.ModifierType(0))


def _text(view) -> str:
    buf = view.get_buffer()
    return buf.get_text(*buf.get_bounds(), True)


def test_a_typed_character_clears_the_placeholder(gtk):
    view = _view_with_placeholder(gtk)
    _press(gtk, view, gtk[1].KEY_a)
    assert not view._placeholder_active
    assert _text(view) == ""


def test_a_modifier_or_arrow_key_keeps_the_placeholder(gtk):
    view = _view_with_placeholder(gtk)
    for key in (gtk[1].KEY_Shift_L, gtk[1].KEY_Control_L, gtk[1].KEY_Left):
        _press(gtk, view, key)
    assert view._placeholder_active
    assert _text(view) == "one per line"


def test_leaving_the_field_blank_restores_the_placeholder(gtk):
    Gtk = gtk[0]
    view = _view_with_placeholder(gtk)
    _press(gtk, view, gtk[1].KEY_a)
    _controller(view, Gtk.EventControllerFocus).emit("leave")
    assert view._placeholder_active


def test_an_unknown_combo_id_shows_the_first_option_unsaved(gtk):
    gui = gtk[2]
    saved = []
    row = gui.SettingsDialog._combo_row(
        None, "T", "S", [("a", "A"), ("b", "B")], "gone", saved.append
    )
    assert row.get_selected() == 0
    assert saved == []


def test_a_known_combo_id_is_selected(gtk):
    gui = gtk[2]
    row = gui.SettingsDialog._combo_row(None, "T", "S", [("a", "A"), ("b", "B")], "b", print)
    assert row.get_selected() == 1
    assert gui.SettingsDialog._combo_id(row) == "b"
