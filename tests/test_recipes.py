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


@pytest.fixture
def fake_desktop(tmp_path, monkeypatch):
    """Runs a recipe's command for real, synchronously, with notify-send and
    xdg-open replaced by scripts that log their argv. Returns run(handler,
    text) -> (logged argv lines, stdout)."""
    import os
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "argv.log"
    for name in ("notify-send", "xdg-open"):
        script = bin_dir / name
        script.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> '{log}'\n")
        script.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    real_popen = subprocess.Popen
    out = []

    def popen(argv, **_kw):
        with real_popen(argv, env=env, stdout=subprocess.PIPE, text=True) as proc:
            out.append(proc.communicate(timeout=10)[0])

    monkeypatch.setattr(subprocess, "Popen", popen)

    def run(handler, text):
        handler(text)
        logged = log.read_text().splitlines() if log.exists() else []
        return logged, "".join(out)

    return run


@pytest.fixture
def snippet_values(monkeypatch):
    """A snippet engine whose {selection}, {clipboard} and {ask:} come from a
    dict, like attacker-controlled PRIMARY, clipboard or a typed answer."""
    values = {}

    def render_placeholders(text, ask):
        for token, value in values.items():
            text = text.replace(token, value)
        return text, 0, False

    module = types.SimpleNamespace(render_placeholders=render_placeholders)
    monkeypatch.setitem(sys.modules, "clipboard_history", module)
    return values


@pytest.mark.parametrize(
    ("template", "token"),
    [
        ("echo {selection}", "{selection}"),
        ("echo {clipboard}", "{clipboard}"),
        ("echo {ask:Name}", "{ask:Name}"),
        ('notify-send "{selection}"', "{selection}"),
    ],
)
def test_selection_clipboard_and_ask_values_never_run(
    template, token, tmp_path, fake_desktop, snippet_values
):
    canary = tmp_path / "PWNED"
    payload = f"x; touch {canary}; y$(touch {canary})`touch {canary}`"
    snippet_values[token] = payload
    recipe = _run(template)
    assert recipe_loader.validate(recipe) == []
    assert recipe_loader.safety_errors(recipe) == []
    logged, stdout = fake_desktop(recipe_loader._build_handler(recipe), "hi")
    assert not canary.exists()
    assert payload in (logged + [stdout.rstrip("\n")])


@pytest.mark.parametrize(
    "template",
    [
        'notify-send "{text_shell}"',
        "notify-send '{text_shell}'",
        "notify-send {text_shell}",
        "notify-send {text}",
        "notify-send {text_upper}",
        'notify-send "$(echo {text})"',
        "notify-send `echo {text_shell}`",
        "cat <<EOF\n{text_shell}\nEOF",
        "cat <<-EOF\n\t{text}\n\tEOF",
    ],
)
def test_quoting_in_the_template_never_lets_the_selection_run(template, tmp_path, fake_desktop):
    canary = tmp_path / "PWNED"
    for payload in (
        f"$(touch {canary})",
        f"`touch {canary}`",
        f"x; touch {canary}",
        f"x' ; touch {canary} ; '",
        f'x" ; touch {canary} ; "',
        f"EOF\ntouch {canary}\nEOF",
    ):
        recipe = _run(template)
        assert recipe_loader.safety_errors(recipe) == [], template
        fake_desktop(recipe_loader._build_handler(recipe), payload)
        assert not canary.exists(), (template, payload)


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ('notify-send "{text_shell}"', ["a b's $HOME"]),
        ("notify-send {text_shell}", ["a b's $HOME"]),
        ("notify-send {text}", ["a b's $HOME"]),
        ("notify-send 'You selected:' {text}", ["You selected:", "a b's $HOME"]),
        ("notify-send 'say {text}!'", ["say a b's $HOME!"]),
        ('notify-send "[{text_upper}]"', ["[A B'S $HOME]"]),
        ("xdg-open https://x/?q={text_url}", ["https://x/?q=a%20b%27s%20%24HOME"]),
        ("xdg-open {text} {text}", ["a b's $HOME", "a b's $HOME"]),
    ],
)
def test_commands_get_the_value_as_one_argument(template, expected, fake_desktop):
    logged, _ = fake_desktop(recipe_loader._build_handler(_run(template)), "a b's $HOME")
    assert logged == expected


def test_echo_text_prints_the_selection(fake_desktop):
    _, stdout = fake_desktop(recipe_loader._build_handler(_run("echo {text}")), "a  *  b")
    assert stdout == "a  *  b\n"
    _, stdout = fake_desktop(
        recipe_loader._build_handler(_run("cat <<EOF\n{text_shell}\nEOF")), "$(id)"
    )
    assert stdout.endswith("$(id)\n")


def test_command_argv_keeps_the_values_out_of_the_script():
    argv = recipe_loader._command_argv("echo {text} {text_shell} {text_url}", "a;b")
    assert argv == ["bash", "-c", 'echo "${1}" "${2}" "${3}"', "vinopop", "a;b", "a;b", "a%3Bb"]
    # the same placeholder twice is one argument
    assert recipe_loader._command_argv("echo {text}{text}", "a")[2:] == [
        'echo "${1}""${1}"',
        "vinopop",
        "a",
    ]


def test_command_preview_shows_the_effective_command():
    preview = recipe_loader.command_preview
    assert preview("notify-send {text}", "it's") == "notify-send 'it'\"'\"'s'"
    assert preview('notify-send "{text}"', 'say "$x"') == 'notify-send "say \\"\\$x\\""'
    assert preview("xdg-open https://x/?q={text_url}", "a b") == "xdg-open https://x/?q=a%20b"


@pytest.mark.parametrize(
    "template",
    [
        "xdg-open {text_shell}",
        "echo {text}",
        "echo {text_url}",
        "echo {text_b64} | base64 -d",
        "echo {text_json} {selection} {clipboard} {ask:Label}",
        "notify-send hi",
        "jq '{{text: .a}}'",  # escaped braces aren't a field
        "echo {unknown}",
    ],
)
def test_placeholders_are_accepted_in_commands(template):
    assert recipe_loader.safety_errors(_run(template)) == []


@pytest.mark.parametrize(
    "template",
    [
        "echo {text!s}",  # a conversion or spec would change the value
        "echo {text:>10}",
        "echo {text_shell:.3}",
        "echo {text[0]}",
        "echo $(( {text} + 1 ))",  # arithmetic evaluates a[$(cmd)]
        "(( {text} > 1 )) && echo big",
        'echo "$(({text}))"',
        "cat <<'EOF'\n{text}\nEOF",  # nothing expands in a quoted heredoc
    ],
)
def test_templates_that_would_change_or_evaluate_the_value_are_refused(template):
    assert recipe_loader.safety_errors(_run(template))


def test_escaped_braces_render_as_literals_in_a_command():
    assert recipe_loader._command_argv("jq '{{text: .a}}'", "x")[2] == "jq '{text: .a}'"


def test_a_nul_in_a_template_is_refused():
    # The old renderer used \x00name\x00 as an internal marker: a template
    # containing one passed every check and then received the raw selection.
    recipe = _run("echo \x00text\x00")
    assert any("NUL" in e for e in recipe_loader.validate(recipe))
    assert recipe_loader.safety_errors(recipe)
    chain = {"name": "x", "action": {"type": "chain", "steps": [{"template": "\x00text\x00"}]}}
    assert any("NUL" in e for e in recipe_loader.validate(chain))


def test_a_nul_in_the_selection_reports_an_error_instead_of_raising(monkeypatch):
    import subprocess

    notes = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: notes.append(argv))
    recipe_loader._build_handler(_run("echo {text}"))("a\x00b")  # Popen raises ValueError
    assert notes and "Recipe error" in notes[0]


def test_chain_run_command_steps_are_checked_too():
    def chain(step):
        return {"name": "x", "action": {"type": "chain", "steps": [step]}}

    assert recipe_loader.safety_errors(chain({"type": "run_command", "template": "echo {text!r}"}))
    # an empty template would execute the selection itself
    assert recipe_loader.safety_errors(chain({"type": "run_command"}))
    assert not recipe_loader.safety_errors(
        chain({"type": "run_command", "template": "echo {text}"})
    )
    assert not recipe_loader.safety_errors(chain({"type": "notify", "template": "{text}"}))


def test_chain_run_command_step_gets_the_value_as_an_argument(tmp_path, fake_desktop):
    canary = tmp_path / "PWNED"
    recipe = {
        "name": "x",
        "action": {
            "type": "chain",
            "steps": [
                {"type": "transform", "template": "{text_strip}"},
                {"type": "run_command", "template": 'notify-send "{text_shell}"'},
            ],
        },
    }
    assert recipe_loader.safety_errors(recipe) == []
    logged, _ = fake_desktop(recipe_loader._build_handler(recipe), f"  $(touch {canary})  ")
    assert not canary.exists()
    assert logged == [f"$(touch {canary})"]


def test_an_unsafe_recipe_builds_a_handler_that_never_runs_the_command(monkeypatch):
    import subprocess

    ran = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: ran.append(a))
    notes = []
    monkeypatch.setattr(recipe_loader, "notify", lambda *a, **kw: notes.append(a))
    handler = recipe_loader._build_handler(_run("echo {text:.3}"))
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
