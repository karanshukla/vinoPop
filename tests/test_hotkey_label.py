"""How Settings shows a saved hotkey: as GTK keycaps, and in GNOME's
'Shift+Super+Y' wording for screen readers and tooltips. Needs GTK 4."""

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk  # noqa: F401
except (ImportError, ValueError):
    pytest.skip("needs GTK 4 + libadwaita", allow_module_level=True)

from ui import settings_gui  # noqa: E402


@pytest.mark.parametrize(
    ("combo", "accel"),
    [
        ("super+shift+y", "<Super><Shift>y"),
        ("ctrl+alt+space", "<Control><Alt>space"),
        ("shift+super+o", "<Shift><Super>o"),
        ("f9", "F9"),
    ],
)
def test_combo_becomes_a_gtk_accelerator(combo, accel):
    assert settings_gui._combo_to_accel(combo) == accel


def test_unparseable_combo_has_no_accelerator():
    assert settings_gui._combo_to_accel("") == ""
    assert settings_gui._combo_to_accel("super+notakey") == ""


def test_pretty_combo_reads_like_gnome():
    assert settings_gui._pretty_combo("super+shift+y") == "Shift+Super+Y"
    # Falls back to capitalising what it can't parse
    assert settings_gui._pretty_combo("super+notakey") == "Super+Notakey"
