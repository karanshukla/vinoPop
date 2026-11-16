"""Rules of the custom-button wizard's details step and the recipe it saves.
They build the real page, so they need GTK 4 and a display; with none they skip."""

from __future__ import annotations

import pytest

pytest.importorskip("gi")


@pytest.fixture(scope="module")
def Gdk():
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gdk

        from ui import gtk4_app
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 / libadwaita unavailable: {exc}")
    try:
        gtk4_app.init()
    except Exception as exc:  # Gtk.init() raises without a display
        pytest.skip(f"no display: {exc}")
    if Gdk.Display.get_default() is None:
        pytest.skip("no display")
    return Gdk


def _wizard(recipe=None):
    from ui.recipe_wizard import RecipeWizard

    wiz = RecipeWizard(recipe=recipe)
    wiz._add_detail_page()
    return wiz


def _text(wiz) -> str:
    buf = wiz._template_buffer
    return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)


def test_the_sample_template_does_not_complete_the_details_step(Gdk):
    wiz = _wizard()
    assert _text(wiz)  # the dimmed sample is on screen
    assert not wiz._detail_page_complete()


def test_a_typed_template_completes_the_details_step(Gdk):
    wiz = _wizard()
    wiz._on_template_focus_in()
    wiz._template_buffer.set_text("https://example.org/?q={text_url}")
    assert wiz._detail_page_complete()


def test_a_typing_key_clears_the_sample(Gdk):
    wiz = _wizard()
    wiz._on_template_key(None, Gdk.KEY_a, 0, 0)
    assert _text(wiz) == ""


def test_tab_or_a_modifier_keeps_the_sample(Gdk):
    wiz = _wizard()
    sample = _text(wiz)
    for key in (Gdk.KEY_Tab, Gdk.KEY_Shift_L, Gdk.KEY_Control_L):
        wiz._on_template_key(None, key, 0, 0)
    assert _text(wiz) == sample
    assert wiz._sample_showing


def test_inserting_over_the_sample_replaces_it(Gdk):
    wiz = _wizard()
    wiz._on_insert(None, "{text}")
    assert _text(wiz) == "{text}"


def test_inserting_into_typed_text_keeps_it(Gdk):
    wiz = _wizard({"name": "x", "action": {"type": "notify", "template": "Hi "}})
    wiz._template_buffer.place_cursor(wiz._template_buffer.get_end_iter())
    wiz._on_insert(None, "{text}")
    assert _text(wiz) == "Hi {text}"


def test_a_new_recipe_is_named_after_its_label():
    from ui.recipe_wizard import RecipeWizard

    wiz = RecipeWizard()
    wiz._state["tooltip"] = "Search Wiki!"
    assert wiz._build_recipe_dict()["name"] == "search-wiki"


def test_an_edited_recipe_keeps_its_name():
    from ui.recipe_wizard import RecipeWizard

    wiz = RecipeWizard(recipe={"name": "old-name", "tooltip": "Old"})
    wiz._state["tooltip"] = "Renamed"
    assert wiz._build_recipe_dict()["name"] == "old-name"
