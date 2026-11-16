"""plugin_loader rules: disabled_plugins, plugin_order, first-run seeding and
the Pin as snippet button."""

import sys
import types

import pytest

from plugin_system import plugin_loader
from plugin_system.plugin_base import Plugin


def _plugin(name: str, priority: int = 100) -> Plugin:
    return Plugin(name=name, icon="i", tooltip=name, handler=print, priority=priority)


@pytest.fixture
def settings(monkeypatch):
    from core import settings as settings_mod

    stored: dict = {}
    fake = types.SimpleNamespace(get=lambda key, default=None: stored.get(key, default))
    monkeypatch.setattr(settings_mod, "get_settings", lambda: fake)
    monkeypatch.setattr(plugin_loader, "_PLUGINS", [])
    return stored


def test_a_disabled_plugin_is_not_registered(settings):
    settings["disabled_plugins"] = ["select-all"]
    plugin_loader.register(_plugin("select-all"))
    assert plugin_loader.all_plugins() == []


def test_a_plugin_not_in_disabled_plugins_is_registered(settings):
    settings["disabled_plugins"] = ["select-all"]
    plugin_loader.register(_plugin("cut"))
    assert [p.name for p in plugin_loader.all_plugins()] == ["cut"]


def test_plugin_order_puts_listed_plugins_first_in_its_order(settings):
    settings["plugin_order"] = ["b", "a"]
    plugins = [_plugin("a", 1), _plugin("c", 0), _plugin("b", 2)]
    assert [p.name for p in plugin_loader.in_user_order(plugins)] == ["b", "a", "c"]


def test_unlisted_plugins_follow_by_priority(settings):
    plugins = [_plugin("late", 30), _plugin("early", 10)]
    assert [p.name for p in plugin_loader.in_user_order(plugins)] == ["early", "late"]


@pytest.fixture
def seeding(tmp_path, monkeypatch):
    """A catalogue holding two of the default seeds, and an empty user dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("wordcount.py", "translate.py", "base64_codec.py"):
        (repo / name).write_text(f"# {name}\n")
    user = tmp_path / "config" / "plugins"
    monkeypatch.setattr(plugin_loader, "REPO_PLUGIN_DIR", repo)
    monkeypatch.setattr(plugin_loader, "USER_PLUGIN_DIR", user)
    monkeypatch.setattr(plugin_loader, "_PLUGIN_SEED_MARKER", tmp_path / "config" / ".seeded")
    return user


def _installed(user_dir) -> set[str]:
    return {p.name for p in user_dir.glob("*.py")}


def test_a_new_install_gets_the_default_plugins(seeding):
    plugin_loader._seed_default_plugins()
    assert _installed(seeding) == {"wordcount.py", "translate.py"}


def test_a_plugin_dir_with_plugins_is_not_seeded(seeding):
    seeding.mkdir(parents=True)
    (seeding / "mine.py").write_text("")
    plugin_loader._seed_default_plugins()
    assert _installed(seeding) == {"mine.py"}


def test_seeding_happens_only_once(seeding):
    plugin_loader._seed_default_plugins()
    (seeding / "wordcount.py").unlink()
    (seeding / "translate.py").unlink()
    plugin_loader._seed_default_plugins()
    assert _installed(seeding) == set()


def test_pin_as_snippet_shows_with_the_clipboard_plugin_loaded(monkeypatch):
    loaded = types.ModuleType("vinopop_user_clipboard_history")
    loaded._create_snippet = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "vinopop_user_clipboard_history", loaded)
    assert plugin_loader._snippets_loaded()


def test_pin_as_snippet_hides_without_the_clipboard_plugin(monkeypatch):
    monkeypatch.delitem(sys.modules, "vinopop_user_clipboard_history", raising=False)
    monkeypatch.delitem(sys.modules, "clipboard_history", raising=False)
    assert not plugin_loader._snippets_loaded()
