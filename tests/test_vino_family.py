"""core/vino_family.py mirrors vino-ui's family and tray modules."""

from core import vino_family


def test_every_app_has_its_own_page_on_the_site():
    names = [app.name for app in vino_family.APPS]
    assert len(set(names)) == len(names) == 8
    for app in vino_family.APPS:
        assert app.page == f"{vino_family.SITE}apps/#{app.name.lower()}"
        assert app.blurb and not app.blurb.endswith(".")


def test_others_leaves_out_only_vinopop():
    others = [app.name for app in vino_family.others("vinoPop")]
    assert "vinoPop" not in others
    assert len(others) == len(vino_family.APPS) - 1


def test_menu_icons_are_symbolic_and_distinct():
    names = list(vino_family.MENU_ICONS.values())
    assert all(name.endswith("-symbolic") for name in names)
    assert len(set(names)) == len(names)
