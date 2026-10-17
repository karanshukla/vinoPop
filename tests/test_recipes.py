import sys
import types

import pytest

import recipe_loader


@pytest.fixture
def snippet_engine(monkeypatch):
    """A stand-in clipboard_history whose engine expands {date} and {shell:..}."""
    calls = []

    def render_placeholders(text, ask):
        calls.append(text)
        return text.replace("{date}", "2026-01-02").replace("{shell:id}", "uid=0"), 0, False

    module = types.SimpleNamespace(render_placeholders=render_placeholders)
    monkeypatch.setitem(sys.modules, "clipboard_history", module)
    return calls


def test_render_substitutes_selection_variants():
    assert recipe_loader._render("q={text_url}", "a b&c") == "q=a%20b%26c"
    assert recipe_loader._render("{text_upper}-{text_lower}", "aB") == "AB-ab"
    assert recipe_loader._render("{unknown} {text}", "x") == "{unknown} x"


def test_selection_is_never_run_through_the_snippet_engine(snippet_engine):
    # The template's own tokens expand...
    assert recipe_loader._render("{date}: {text}", "hi") == "2026-01-02: hi"
    # ...but a selection that merely looks like one is left alone.
    assert recipe_loader._render("echo {text_shell}", "{shell:id}") == "echo '{shell:id}'"
    assert recipe_loader._render("{text}", "{date}") == "{date}"
    assert all("{shell:id}" not in seen for seen in snippet_engine)


def test_selection_that_looks_like_a_marker_is_not_expanded_twice():
    assert recipe_loader._render("{text}{text_upper}", "\x00text_upper\x00") == (
        "\x00text_upper\x00\x00TEXT_UPPER\x00"
    )


@pytest.mark.parametrize("bad", [[], "just a string", 3, None])
def test_validate_rejects_non_object_recipes(bad):
    assert recipe_loader.validate(bad)


def test_validate_rejects_non_object_action():
    assert recipe_loader.validate({"name": "x", "action": "open_url"})


def test_validate_flags_unknown_content_types():
    recipe = {
        "name": "x",
        "content_types": ["urls"],
        "action": {"type": "open_url", "template": "https://x/{text_url}"},
    }
    assert any("content_types" in e for e in recipe_loader.validate(recipe))
    recipe["content_types"] = ["url"]
    assert recipe_loader.validate(recipe) == []


def test_save_recipe_refuses_names_that_escape_the_recipes_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(recipe_loader, "RECIPES_DIR", tmp_path / "recipes")
    with pytest.raises(ValueError):
        recipe_loader.save_recipe({"name": "../evil", "action": {}})
    path = recipe_loader.save_recipe({"name": "fine-1", "action": {}})
    assert path.parent == tmp_path / "recipes"


def test_bundled_recipes_validate():
    import json

    for path in recipe_loader.Path(recipe_loader.__file__).parent.glob(
        "plugins_repo/recipes/*.json"
    ):
        assert recipe_loader.validate(json.loads(path.read_text())) == [], path.name
