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


# ---- shell safety --------------------------------------------------------------


def _run(template):
    return {"name": "x", "action": {"type": "run_command", "template": template}}


@pytest.mark.parametrize(
    "template",
    [
        "xdg-open {text_shell}",
        "echo {text_url}",
        "echo {text_b64} | base64 -d",
        "notify-send hi",
    ],
)
def test_quoted_placeholders_are_accepted_in_commands(template):
    assert recipe_loader.safety_errors(_run(template)) == []


@pytest.mark.parametrize(
    "template",
    [
        "echo {text}",
        "echo {text_upper}",
        "echo {text_json}",  # not quoted either, though the old denylist missed it
        "echo {text_url_decode}",
        "echo {text_b64_decode}",
        "echo {text!s}",  # a conversion still substitutes the raw selection
        "echo {text:>10}",
    ],
)
def test_unquoted_placeholders_are_refused_in_commands(template):
    assert recipe_loader.safety_errors(_run(template))


def test_chain_run_command_steps_are_checked_too():
    def chain(step):
        return {"name": "x", "action": {"type": "chain", "steps": [step]}}

    assert recipe_loader.safety_errors(chain({"type": "run_command", "template": "echo {text}"}))
    # an empty template would execute the selection itself
    assert recipe_loader.safety_errors(chain({"type": "run_command"}))
    assert not recipe_loader.safety_errors(
        chain({"type": "run_command", "template": "echo {text_shell}"})
    )
    assert not recipe_loader.safety_errors(chain({"type": "notify", "template": "{text}"}))


def test_an_unsafe_recipe_builds_a_handler_that_never_runs_the_command(monkeypatch):
    import subprocess

    ran = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: ran.append(a))
    notes = []
    monkeypatch.setattr(recipe_loader, "notify", lambda *a, **kw: notes.append(a))
    handler = recipe_loader._build_handler(_run("echo {text}"))
    handler("; rm -rf ~")
    assert ran == []
    assert notes and "disabled" in notes[0][0]


def test_non_shell_actions_may_use_the_raw_selection():
    recipe = {"name": "x", "action": {"type": "notify", "template": "{text}"}}
    assert recipe_loader.safety_errors(recipe) == []


def test_new_recipes_get_a_unique_name_instead_of_overwriting(tmp_path, monkeypatch):
    monkeypatch.setattr(recipe_loader, "RECIPES_DIR", tmp_path)
    base = {"name": "search", "action": {"type": "open_url", "template": "https://x/{text_url}"}}
    first = recipe_loader.save_recipe(dict(base))
    second = recipe_loader.save_recipe(dict(base))
    third = recipe_loader.save_recipe(dict(base))
    assert [p.name for p in (first, second, third)] == [
        "search.json",
        "search-2.json",
        "search-3.json",
    ]
    import json

    assert json.loads(second.read_text())["name"] == "search-2"  # the plugin's identity too


def test_editing_a_recipe_still_overwrites_its_own_file(tmp_path, monkeypatch):
    monkeypatch.setattr(recipe_loader, "RECIPES_DIR", tmp_path)
    recipe = {"name": "search", "action": {"type": "open_url", "template": "a"}}
    path = recipe_loader.save_recipe(dict(recipe))
    recipe["action"]["template"] = "b"
    assert recipe_loader.save_recipe(recipe, target_path=path) == path
    assert sorted(p.name for p in tmp_path.glob("*.json")) == ["search.json"]


def test_bundled_recipes_are_shell_safe():
    import json

    for path in recipe_loader.Path(recipe_loader.__file__).parent.glob(
        "plugins_repo/recipes/*.json"
    ):
        assert recipe_loader.safety_errors(json.loads(path.read_text())) == [], path.name


@pytest.mark.parametrize("template", ["https://x/?q={text_url", "oops}", "{", "a {b {c}} d {"])
def test_validate_flags_unbalanced_braces(template):
    recipe = {"name": "x", "action": {"type": "open_url", "template": template}}
    assert any("template" in e for e in recipe_loader.validate(recipe))
    chain = {"name": "x", "action": {"type": "chain", "steps": [{"template": template}]}}
    assert any("template" in e for e in recipe_loader.validate(chain))


def test_validate_accepts_normal_templates():
    for template in ("https://x/?q={text_url}", "{{literal}} {text}", "no placeholders"):
        recipe = {"name": "x", "action": {"type": "open_url", "template": template}}
        assert recipe_loader.validate(recipe) == [], template
