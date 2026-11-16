"""What the Settings window's Updates row offers after a check."""

import pytest

pytest.importorskip("gi")


@pytest.fixture
def gui():
    try:
        from ui import settings_gui
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 unavailable: {exc}")
    return settings_gui


def test_a_development_build_is_not_offered_the_release(gui):
    offer = gui._update_offer("1.3.0-5-gabc123", "v1.4.0", None, can_upgrade=True)
    assert offer.next == gui.NEXT_CHECK
    assert "development build" in offer.subtitle


def test_an_older_release_is_offered_the_update(gui):
    offer = gui._update_offer("1.3.0", "v1.4.0", None, can_upgrade=True)
    assert offer.next == gui.NEXT_INSTALL
    assert offer.label == "Update to v1.4.0"


def test_without_an_upgrade_command_the_release_page_is_offered(gui):
    offer = gui._update_offer("1.3.0", "v1.4.0", None, can_upgrade=False)
    assert offer.next == gui.NEXT_PAGE


def test_the_newest_release_is_not_offered_anything(gui):
    offer = gui._update_offer("1.4.0", "v1.4.0", None, can_upgrade=True)
    assert (offer.next, offer.label) == (gui.NEXT_CHECK, "Check again")


def test_a_failed_check_offers_to_try_again(gui):
    offer = gui._update_offer("1.3.0", None, "offline", can_upgrade=True)
    assert (offer.subtitle, offer.label, offer.next) == ("offline", "Try again", gui.NEXT_CHECK)
