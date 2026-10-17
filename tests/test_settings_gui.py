"""Settings window structure checks that don't need GTK."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "settings_gui.py"


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
