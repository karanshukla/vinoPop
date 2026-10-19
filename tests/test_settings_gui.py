"""Settings window structure checks that don't need GTK."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "ui" / "settings_gui.py"


def _dialog_methods() -> dict[str, ast.FunctionDef]:
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SettingsDialog")
    return {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}


def test_every_group_builder_is_on_a_page():
    # A group builder no page lists is dead UI: the Snippets & Clipboard
    # group vanished that way once (#23).
    methods = _dialog_methods()
    builders = {name for name in methods if name.startswith("_build_") and name.endswith("_group")}
    on_pages = {
        node.attr
        for node in ast.walk(methods["_pages"])
        if isinstance(node, ast.Attribute) and node.attr.startswith("_build_")
    }
    assert builders, "no _build_*_group methods found - renamed?"
    assert builders - on_pages == set()


PLUGIN_MANAGER = SRC.parent / "plugin_manager.py"


def _module_constant(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path.name}")


def test_plugins_page_links_every_plugin_manager_tab():
    # #69: Settings has a row per Plugin Manager page, and each row's tab id
    # is one the Plugin Manager knows (an unknown id silently opens the
    # default page).
    links = [tab for _title, _subtitle, tab in _module_constant(SRC, "_PLUGIN_LINKS")]
    tabs = [name for name, _title, _icon in _module_constant(PLUGIN_MANAGER, "TABS")]
    assert links == tabs


def test_both_windows_share_the_sidebar_layout():
    # The Plugin Manager was an Adw.PreferencesWindow with its tabs in the
    # header bar; it now has the same sidebar layout as Settings.
    for path in (SRC, PLUGIN_MANAGER):
        src = path.read_text(encoding="utf-8")
        assert "sidebar_window.build(" in src, path.name
        assert "PreferencesWindow(" not in src, path.name
