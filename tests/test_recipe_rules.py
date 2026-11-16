"""Recipe rules that used to be stated only in comments: how a chain
passes its value along and where it stops, when default recipes are
seeded, and what `enabled` and the mocking-case transform do."""

from __future__ import annotations

import json

import pytest

from plugin_system import recipe_loader


@pytest.fixture
def desktop(monkeypatch):
    """Record what a recipe handler does instead of doing it."""
    done: list[tuple] = []
    monkeypatch.setattr(recipe_loader, "notify", lambda *a, **kw: done.append(("notify", a[1])))

    class Backend:
        def set_clipboard(self, text):
            done.append(("clipboard", text))

    import platform_backend
    from core import actions

    monkeypatch.setattr(platform_backend, "get_backend", lambda: Backend())
    monkeypatch.setattr(actions, "replace_selection", lambda t: done.append(("paste", t)))
    monkeypatch.setattr(actions, "open_web_url", lambda u: done.append(("open", u)))
    return done


def _chain(*steps):
    recipe = {"name": "c", "action": {"type": "chain", "steps": list(steps)}}
    return recipe_loader._build_handler(recipe)


def test_each_transform_gets_the_one_befores_output_as_text(desktop):
    _chain({"template": "{text_upper}"}, {"template": "<{text}>"}, {"type": "copy"})("hi")
    assert desktop == [("clipboard", "<HI>"), ("notify", "<HI>")]


def test_a_step_without_a_type_is_a_transform(desktop):
    _chain({"template": "{text}!"}, {"type": "paste"})("hi")
    assert desktop == [("paste", "hi!")]


def test_the_first_terminal_step_ends_the_chain(desktop):
    _chain({"type": "paste"}, {"template": "{text_upper}"}, {"type": "copy"})("hi")
    assert desktop == [("paste", "hi")]


def test_a_chain_of_only_transforms_copies_its_result(desktop):
    _chain({"template": "{text_upper}"})("hi")
    assert desktop == [("clipboard", "HI"), ("notify", "HI")]


def test_an_open_url_step_renders_its_template_against_the_value(desktop):
    _chain({"type": "open_url", "template": "https://example.com/?q={text_url}"})("a b")
    assert desktop == [("open", "https://example.com/?q=a%20b")]


def test_an_unknown_step_type_ends_the_chain_without_doing_anything(desktop):
    _chain({"type": "teleport"}, {"type": "copy"})("hi")
    assert desktop == []


def test_a_chain_without_steps_says_so(desktop):
    _chain()("hi")
    assert desktop == [("notify", "c: no steps defined")]


def test_mocking_case_is_the_same_every_time_for_the_same_text():
    once = recipe_loader._mock_case("the same input")
    assert once == recipe_loader._mock_case("the same input")
    assert once.lower() == "the same input"
    assert once != "the same input"


@pytest.fixture
def recipes_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(recipe_loader, "RECIPES_DIR", tmp_path / "recipes")
    monkeypatch.setattr(recipe_loader, "_SEED_MARKER", tmp_path / "seeded")
    return tmp_path / "recipes"


def test_a_fresh_install_is_seeded_with_the_default_recipes_once(recipes_dir):
    recipe_loader._seed_default_recipes()
    seeded = sorted(p.name for p in recipes_dir.glob("*.json"))
    assert seeded == sorted(recipe_loader._DEFAULT_RECIPE_SEEDS)
    (recipes_dir / seeded[0]).unlink()
    recipe_loader._seed_default_recipes()
    assert not (recipes_dir / seeded[0]).exists()


def test_a_folder_that_already_has_recipes_is_not_seeded(recipes_dir):
    recipes_dir.mkdir()
    (recipes_dir / "mine.json").write_text("{}")
    recipe_loader._seed_default_recipes()
    assert [p.name for p in recipes_dir.glob("*.json")] == ["mine.json"]
    assert recipe_loader._SEED_MARKER.is_file()


def _recipe(name, **extra):
    action = {"type": "notify", "template": "{text}"}
    return {"name": name, "icon": "x", "tooltip": name, "action": action, **extra}


def test_a_recipe_is_on_unless_it_says_enabled_false(recipes_dir):
    recipes_dir.mkdir()
    recipe_loader._SEED_MARKER.touch()
    (recipes_dir / "a.json").write_text(json.dumps(_recipe("a")))
    (recipes_dir / "b.json").write_text(json.dumps(_recipe("b", enabled=False)))
    (recipes_dir / "c.json").write_text(json.dumps(_recipe("c", enabled=True)))
    registered = []
    assert recipe_loader.load_recipes(registered.append) == 2
    assert [p.name for p in registered] == ["a", "c"]


def test_a_misspelt_content_type_is_an_error_not_every_content_type():
    errors = recipe_loader.validate(_recipe("a", content_types=["plain_txt"]))
    assert any("plain_txt" in e for e in errors)
