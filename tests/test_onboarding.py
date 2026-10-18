"""Onboarding keys and copy (#34), without opening the window."""

import pytest

pytest.importorskip("gi")


@pytest.fixture
def onboarding():
    try:
        import onboarding
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 unavailable: {exc}")
    return onboarding


def test_enter_and_space_belong_to_a_focused_control(onboarding):
    from gi.repository import Gdk

    for key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
        # Space on "Don't show this again" used to turn the page; Enter on
        # Skip / Back went forward
        assert onboarding._page_key(key, on_control=True) is None
        assert onboarding._page_key(key, on_control=False) == 1


def test_arrows_and_escape_work_from_anywhere(onboarding):
    from gi.repository import Gdk

    for on_control in (True, False):
        assert onboarding._page_key(Gdk.KEY_Right, on_control) == 1
        assert onboarding._page_key(Gdk.KEY_Left, on_control) == -1
        assert onboarding._page_key(Gdk.KEY_Escape, on_control) == "close"
    assert onboarding._page_key(Gdk.KEY_a, False) is None


def test_summon_copy_lists_both_hotkeys(onboarding):
    body, segs = onboarding._summon_copy("super+shift+y", "super+shift+v", True)
    assert "Super+Shift+Y" in body and "Super+Shift+V" in body
    assert segs == (0, 1, 2)


def test_a_cleared_clipboard_hotkey_is_not_advertised(onboarding):
    body, segs = onboarding._summon_copy("super+shift+y", "", True)
    assert "Super+Shift+V" not in body and "clipboard history" not in body
    assert segs == (0, 2)


def test_history_off_hides_the_clipboard_hotkey_and_snippets(onboarding):
    body, segs = onboarding._summon_copy("super+shift+y", "super+shift+v", False)
    assert "Super+Shift+V" not in body and "nippets" not in body
    assert segs == (0,)


def test_build_pages_keeps_an_empty_hotkey_empty(onboarding, monkeypatch):
    monkeypatch.setattr(onboarding, "_model_to_offer", lambda: None)
    settings = {"hotkey": "", "clipboard_hotkey": "super+shift+v"}
    pages = onboarding._build_pages(settings)
    summon = next(p for p in pages if p["title"] == "Summon it your way")
    # an empty hotkey is switched off; it used to fall back to the default
    assert "Super+Shift+Y" not in summon["body"]
    assert onboarding._draw_summon.segs == (1, 2)


def test_plugins_page_offers_to_open_plugins(onboarding, monkeypatch):
    monkeypatch.setattr(onboarding, "_model_to_offer", lambda: None)
    pages = onboarding._build_pages({})
    assert any(p.get("open_plugins") for p in pages)
