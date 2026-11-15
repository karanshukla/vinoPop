"""The clipboard picker's keyboard: what each key does on the Recent and
Snippets views. The window is real (needs a display); nothing is pasted."""

from types import SimpleNamespace

import pytest

pytest.importorskip("gi")  # clipboard_history is a GTK plugin


@pytest.fixture
def ch():
    try:
        import clipboard_history

        from ui import gtk4_app

        gtk4_app.init()
    except Exception as exc:  # no GTK 4, or no display
        pytest.skip(f"GTK 4 unavailable: {exc}")
    return clipboard_history


@pytest.fixture
def picker(ch, monkeypatch):
    import platform_backend

    backend = SimpleNamespace(
        name="x11", prepare_popup_window=lambda _w: None, move_popup_window=lambda *_a: None
    )
    monkeypatch.setattr(platform_backend, "get_backend", lambda: backend)
    monkeypatch.setattr(ch, "_position_at_pointer", lambda _w: None)
    monkeypatch.setattr(
        ch,
        "_history",
        [
            ch.Entry(id="h1", timestamp=2.0, kind="text", text="newest"),
            ch.Entry(id="h2", timestamp=1.0, kind="text", text="older"),
        ],
    )
    monkeypatch.setattr(
        ch, "_snippets", [ch.Entry(id="s1", timestamp=1.0, kind="text", text="Regards", name="Sig")]
    )
    p = ch._PickerDialog()
    p.show()
    p.calls = []
    for handler in ("_on_pin_clicked", "_on_rename_clicked", "_on_delete_history_clicked"):
        monkeypatch.setattr(
            p, handler, lambda _btn, entry, name=handler: p.calls.append((name, entry.id))
        )
    monkeypatch.setattr(
        p, "_paste_and_close", lambda entry, is_snippet=False: p.calls.append(("paste", entry.id))
    )
    yield p
    if p.dialog is not None:
        p.dialog.destroy()


def _key(ch, name):
    return getattr(ch.Gdk, f"KEY_{name}")


def _ctrl(ch):
    return ch.Gdk.ModifierType.CONTROL_MASK


def _select_first(listbox):
    listbox.select_row(listbox.get_row_at_index(0))


def test_escape_closes_the_picker(ch, picker):
    assert picker._handle_key(_key(ch, "Escape"), 0) is True
    assert picker.dialog is None


def test_tab_flips_between_recent_and_snippets(ch, picker):
    assert picker._handle_key(_key(ch, "Tab"), 0) is True
    assert picker.tabs.get_current_page() == 1
    assert picker._handle_key(_key(ch, "ISO_Left_Tab"), 0) is True
    assert picker.tabs.get_current_page() == 0


def test_down_from_the_search_box_selects_the_first_row_and_up_the_last(ch, picker):
    assert picker._handle_key(_key(ch, "Down"), 0) is True
    assert picker.recent_listbox.get_selected_row().entry.id == "h1"
    picker.search_entry.grab_focus()
    assert picker._handle_key(_key(ch, "Up"), 0) is True
    assert picker.recent_listbox.get_selected_row().entry.id == "h2"


def test_down_skips_rows_the_search_filtered_out(ch, picker):
    picker.search_entry.set_text("older")
    picker._on_search_changed(picker.search_entry)
    picker._handle_key(_key(ch, "Down"), 0)
    assert picker.recent_listbox.get_selected_row().entry.id == "h2"


def test_enter_pastes_the_selected_row_and_does_nothing_without_one(ch, picker):
    assert picker._handle_key(_key(ch, "Return"), 0) is False
    _select_first(picker.recent_listbox)
    assert picker._handle_key(_key(ch, "KP_Enter"), 0) is True
    assert picker.calls == [("paste", "h1")]


def test_ctrl_p_pins_on_recent_only(ch, picker):
    _select_first(picker.recent_listbox)
    _select_first(picker.snippets_listbox)
    assert picker._handle_key(_key(ch, "p"), _ctrl(ch)) is True
    picker.tabs.set_current_page(1)
    assert picker._handle_key(_key(ch, "p"), _ctrl(ch)) is True
    assert picker.calls == [("_on_pin_clicked", "h1")]


def test_ctrl_r_renames_on_snippets_only(ch, picker):
    _select_first(picker.recent_listbox)
    _select_first(picker.snippets_listbox)
    assert picker._handle_key(_key(ch, "r"), _ctrl(ch)) is True
    picker.tabs.set_current_page(1)
    assert picker._handle_key(_key(ch, "r"), _ctrl(ch)) is True
    assert picker.calls == [("_on_rename_clicked", "s1")]


def test_plain_delete_in_the_search_box_edits_the_text(ch, picker):
    _select_first(picker.recent_listbox)
    picker.search_entry.grab_focus()
    assert picker._handle_key(_key(ch, "Delete"), 0) is False
    assert picker.calls == []


def test_ctrl_delete_removes_the_selected_history_row_from_anywhere(ch, picker):
    _select_first(picker.recent_listbox)
    picker.search_entry.grab_focus()
    assert picker._handle_key(_key(ch, "Delete"), _ctrl(ch)) is True
    assert picker.calls == [("_on_delete_history_clicked", "h1")]


def test_delete_never_removes_a_snippet(ch, picker):
    _select_first(picker.snippets_listbox)
    picker.tabs.set_current_page(1)
    assert picker._handle_key(_key(ch, "Delete"), _ctrl(ch)) is False
    assert picker.calls == []


def test_typing_on_a_row_goes_to_the_search_box(ch, picker):
    picker._handle_key(_key(ch, "Down"), 0)  # focus is on a row now
    assert picker._handle_key(_key(ch, "o"), 0) is True
    assert picker.search_entry.get_text() == "o"


def test_a_ctrl_chord_is_not_typed_into_the_search_box(ch, picker):
    picker._handle_key(_key(ch, "Down"), 0)
    assert picker._handle_key(_key(ch, "o"), _ctrl(ch)) is False
    assert picker.search_entry.get_text() == ""


def test_a_prompt_inside_the_picker_gets_its_own_keys(ch, picker):
    picker._modal_child_open = True
    assert picker._on_key_pressed(None, _key(ch, "Escape"), 0, 0) is False
    assert picker.dialog is not None
