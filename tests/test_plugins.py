"""Every GTK-free bundled plugin imports and registers at least one action."""

import importlib
from pathlib import Path

import pytest

from plugin_system.plugin_base import Plugin

PLUGINS = Path(__file__).resolve().parent.parent / "plugins_repo"
GTK_FREE = sorted(
    p.stem
    for p in PLUGINS.glob("*.py")
    if "gi.repository" not in p.read_text() and "import gi" not in p.read_text()
)


@pytest.mark.parametrize("name", GTK_FREE)
def test_plugin_registers(name):
    registered: list[Plugin] = []
    importlib.import_module(name).register(registered.append)
    assert registered
    assert all(isinstance(p, Plugin) and p.name and callable(p.handler) for p in registered)


def test_calculator_predicate():
    import calculator

    assert calculator._looks_like_math("2 * (3 + 4)")
    assert not calculator._looks_like_math("hello world")


def test_url_decode_predicate():
    import url_encode

    assert url_encode._looks_percent_encoded("a%20b%2Fc")
    assert not url_encode._looks_percent_encoded("plain text")


def test_base64_predicate():
    import base64_codec

    assert base64_codec._looks_like_base64("aGVsbG8gdmlub1BvcCBmcm9tIGEgdGVzdA==")
    assert not base64_codec._looks_like_base64("not base64 at all!")


# ---- loading user plugins (#33) ----------------------------------------------


@pytest.fixture
def user_plugins(tmp_path, monkeypatch):
    """An empty user plugin dir that load_all() reads instead of the real one."""
    from plugin_system import plugin_loader

    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    monkeypatch.setattr(plugin_loader, "USER_PLUGIN_DIR", plugin_dir)
    monkeypatch.setattr(plugin_loader, "_PLUGIN_SEED_MARKER", tmp_path / "seeded")
    (tmp_path / "seeded").touch()
    monkeypatch.setattr(plugin_loader, "_load_recipes", lambda: None)
    monkeypatch.setattr(plugin_loader, "_install_all_icons", lambda: None)
    yield plugin_dir
    plugin_loader._PLUGINS.clear()
    plugin_loader._SOURCE_BY_NAME.clear()


_GOOD = """
from plugin_system.plugin_base import Plugin

def register(r):
    r(Plugin(name="good", icon="x", tooltip="Good", handler=print))
"""


@pytest.mark.parametrize(
    "bad",
    [
        "import sys\nsys.exit(0)\n",
        "import sys\ndef register(r):\n    sys.exit(3)\n",
        "raise SystemExit\n",
    ],
)
def test_a_plugin_calling_sys_exit_does_not_stop_the_daemon(user_plugins, bad):
    import sys

    from plugin_system import plugin_loader

    (user_plugins / "a_bad.py").write_text(bad)
    (user_plugins / "b_good.py").write_text(_GOOD)
    plugin_loader.load_all()  # used to raise SystemExit out of App.__init__
    names = {p.name for p in plugin_loader.all_plugins()}
    assert "good" in names
    assert "vinopop_user_a_bad" not in sys.modules


def test_a_register_that_raises_leaves_no_buttons_behind(user_plugins):
    from plugin_system import plugin_loader

    (user_plugins / "half.py").write_text(
        "from plugin_base import Plugin\n"
        "def register(r):\n"
        "    r(Plugin(name='half', icon='x', tooltip='Half', handler=print))\n"
        "    raise RuntimeError('boom')\n"
    )
    (user_plugins / "whole.py").write_text(_GOOD)
    plugin_loader.load_all()
    names = {p.name for p in plugin_loader.all_plugins()}
    assert "half" not in names
    assert "good" in names
    assert plugin_loader.source_for("half") is None
    assert plugin_loader.source_for("good") == "whole.py"


# ---- Plugin Manager custom tab (#33) -------------------------------------------


@pytest.mark.parametrize(
    "recipe",
    [
        {"name": "a", "action": "x"},
        {"name": "a", "action": {"type": "notify", "template": 5}},
        {"name": 5, "tooltip": None, "action": {"type": ["x"], "template": ["y"]}},
        {},
    ],
)
def test_custom_tab_row_survives_a_malformed_recipe(recipe):
    try:
        from ui import plugin_manager
    except (ImportError, ValueError):
        pytest.skip("needs GTK 4 + libadwaita")
    title, subtitle = plugin_manager._recipe_row_text(Path("a.json"), recipe, {})
    assert isinstance(title, str) and title
    assert isinstance(subtitle, str)


def test_custom_tab_flags_a_recipe_vinopop_stopped_bundling():
    try:
        from ui import plugin_manager
    except (ImportError, ValueError):
        pytest.skip("needs GTK 4 + libadwaita")
    recipe = {"name": "define", "action": {"type": "open_url", "template": "x"}}
    _, subtitle = plugin_manager._recipe_row_text(
        Path("define.json"), recipe, {}, {"define.json": "Gone."}
    )
    assert "No longer bundled" in subtitle


# ---- bundled versions (#33) -------------------------------------------------------


def _manifest():
    import json

    return json.loads((PLUGINS / "manifest.json").read_text())


def test_every_catalogue_entry_and_its_file_carry_the_same_version():
    from plugin_system import plugin_loader

    for entry in _manifest():
        sub = "recipes/" if entry.get("kind") == "recipe" else ""
        version, _ = plugin_loader._read_bundled(PLUGINS / f"{sub}{entry['file']}")
        assert isinstance(entry.get("version"), int) and entry["version"] >= 1, entry["file"]
        assert version == entry["version"], entry["file"]


def test_retired_files_are_not_in_the_catalogue():
    from plugin_system import plugin_loader

    retired = plugin_loader.retired_bundled()
    assert {"speak.py", "define.json"} <= set(retired)
    assert not set(retired) & {e["file"] for e in _manifest()}
    assert not [name for name in retired if (PLUGINS / name).exists()]


def test_an_older_install_is_offered_the_update(tmp_path):
    from plugin_system import plugin_loader

    src = tmp_path / "repo.py"
    src.write_text("# vinopop-plugin-version: 2\nfixed = True\n")
    dst = tmp_path / "installed.py"
    assert plugin_loader.bundled_status(src, dst, 2) == "missing"
    # A copy from before files had versions
    dst.write_text("fixed = False\n")
    assert plugin_loader.bundled_status(src, dst, 2) == "outdated"
    dst.write_text("# vinopop-plugin-version: 1\nfixed = False\n")
    assert plugin_loader.bundled_status(src, dst, 2) == "outdated"
    plugin_loader.update_bundled(src, dst)
    assert plugin_loader.bundled_status(src, dst, 2) == "current"
    # The same code without the header line is already up to date
    dst.write_text("fixed = True\n")
    assert plugin_loader.bundled_status(src, dst, 2) == "current"
    # Edited by hand at the current version: left alone
    dst.write_text("# vinopop-plugin-version: 2\nfixed = 'mine'\n")
    assert plugin_loader.bundled_status(src, dst, 2) == "changed"


def test_updating_a_recipe_keeps_its_on_off_switch(tmp_path, monkeypatch):
    import json

    from plugin_system import plugin_loader, recipe_loader

    monkeypatch.setattr(recipe_loader, "RECIPES_DIR", tmp_path)
    src = tmp_path / "repo.json"
    src.write_text(json.dumps({"name": "s", "version": 2, "action": {"template": "new"}}))
    dst = tmp_path / "s.json"
    dst.write_text(json.dumps({"name": "s", "enabled": False, "action": {"template": "old"}}))
    assert plugin_loader.bundled_status(src, dst, 2) == "outdated"
    plugin_loader.update_bundled(src, dst)
    data = json.loads(dst.read_text())
    assert data["enabled"] is False
    assert data["action"]["template"] == "new"
    # Turning a current recipe off doesn't make it look out of date
    assert plugin_loader.bundled_status(src, dst, 2) == "current"
