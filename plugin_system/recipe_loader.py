"""Load JSON "recipes" from ~/.config/vinopop/recipes/ as Plugin objects.

A recipe is a no-code plugin definition: name, icon, content types, and an
action declaration. Four action types cover ~90% of real plugin ideas:

  - open_url:        xdg-open a URL built from a template
  - run_command:     bash -c a command built from a template
  - notify:          notify-send with a templated body
  - copy_transformed: render template, put result on clipboard

Available substitution variables in templates:
  {text}        - raw selection
  {text_url}    - percent-encoded (safe for URLs)
  {text_shell}  - shlex.quoted; in a run_command, the same as {text}
  {text_upper}  - uppercased
  {text_lower}  - lowercased
  {text_strip}  - whitespace-trimmed

In a run_command every placeholder, snippet tokens like {selection} too, is
passed to bash as an argument rather than pasted into the command line, so
quoting is automatic: `notify-send {text}` is safe for any selection.
"""

from __future__ import annotations

import json
import random as _random
import re
import shlex
import shutil
import string
import subprocess
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from core.classifier import ContentType
from core.json_file import write_json
from core.xdg_paths import CONFIG_DIR
from plugin_system.plugin_base import Plugin


def notify(title: str, body: str, icon: str = "vinopop", timeout_ms: int = 3000) -> None:
    from core import actions

    actions.notify(title, body, icon, timeout_ms)


RECIPES_DIR = CONFIG_DIR / "recipes"

_CTYPE_BY_NAME = {
    "plain_text": ContentType.PLAIN_TEXT,
    "url": ContentType.URL,
    "email": ContentType.EMAIL,
    "path": ContentType.PATH,
    "command": ContentType.COMMAND,
}

VALID_ACTION_TYPES = (
    "open_url",
    "run_command",
    "notify",
    "copy_transformed",
    "chain",
)


def _mock_case(text: str) -> str:
    """rAnDoM cAsE per character, seeded with the text so a preview and the
    click after it give the same jumble."""
    rng = _random.Random(text)  # nosec B311 - text styling
    return "".join(c.upper() if rng.random() < 0.5 else c.lower() for c in text)


def _apply_snippet_placeholders(text: str) -> str:
    """Bridge to the snippet placeholder engine in clipboard_history.

    Lets recipes use {date}, {time}, {datetime}, {weekday}, {date:FORMAT},
    {date:+7d}, {name}, {clipboard}, {selection}, {shell:CMD} - the same
    dynamic tokens snippets support. {ask:} resolves to an empty string
    here because recipes fire from popup clicks where blocking on a
    dialog is jarring; users who need fill-ins should make a snippet.

    Lookup is best-effort: if clipboard_history isn't loaded (it's
    user-installed and could be removed), recipes fall back to the
    text passing through unchanged.
    """
    if "{" not in text:
        return text
    try:
        import sys

        ch = sys.modules.get("vinopop_user_clipboard_history") or sys.modules.get(
            "clipboard_history"
        )
        if ch is None or not hasattr(ch, "render_placeholders"):
            return text
        rendered, _, _ = ch.render_placeholders(
            text,
            lambda fields: {label: "" for label, _ in fields},
        )
        return rendered
    except Exception as exc:
        print(f"[recipe] snippet-placeholder bridge failed: {exc}")
        return text


def _safe_b64_decode(text: str) -> str:
    """Best-effort base64 decode. Returns the original text unless it is
    strict base64 of printable UTF-8 text, so a chain step that hands it
    an ordinary word ("test" is valid base64 of junk bytes) passes it
    through instead of opening or pasting garbage."""
    import base64 as _b64

    try:
        cleaned = "".join(text.split())
        padded = cleaned + "=" * (-len(cleaned) % 4)
        decoded = _b64.b64decode(padded, validate=True).decode("utf-8")
    except Exception:
        return text
    if not decoded or any(not c.isprintable() and c not in "\t\r\n" for c in decoded):
        return text
    return decoded


def _try_json_format(text: str) -> str:
    """Pretty-print text if it parses as JSON; return original otherwise.
    Used by the {text_json} transform - never raises so a chain step
    that hands it non-JSON still produces a usable value."""
    try:
        parsed = json.loads(text)
    except Exception:
        return text
    try:
        return json.dumps(parsed, indent=2, ensure_ascii=False)
    except Exception:
        return text


def _try_json_minify(text: str) -> str:
    try:
        parsed = json.loads(text)
        return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return text


def _text_values(text: str) -> dict[str, str]:
    """Every {text_*} variable for one selection."""
    import base64 as _b64

    return {
        "text": text,
        "text_url": urllib.parse.quote(text, safe=""),
        # For a notification or the clipboard; a command gets an argument (_command_parts)
        "text_shell": shlex.quote(text),
        "text_upper": text.upper(),
        "text_lower": text.lower(),
        "text_strip": text.strip(),
        "text_mock": _mock_case(text),
        "text_json": _try_json_format(text),
        "text_json_min": _try_json_minify(text),
        "text_b64": _b64.b64encode(text.encode("utf-8")).decode("ascii"),
        "text_url_decode": urllib.parse.unquote(text),
        "text_b64_decode": _safe_b64_decode(text),
    }


_FORMATTER = string.Formatter()

# Fields the snippet engine expands. Formatter.parse() reads {date:+7d} as
# the field "date" with the spec "+7d", so the name alone identifies them.
_SNIPPET_FIELDS = frozenset(
    {
        "date",
        "time",
        "datetime",
        "weekday",
        "clipboard",
        "selection",
        "cursor",
        "name",
        "ask",
        "shell",
        "var",
    }
)
_FIELD_BASE_RE = re.compile(r"[^.\[]*")


class _Field:
    """One {placeholder} in a template, as string.Formatter parses it.
    `token` is the field as written; `base` is "text" for {text_upper} or
    {text[0]}, else the name; `kind` is "text", "snippet" or "unknown"."""

    __slots__ = ("base", "conv", "kind", "name", "spec", "token")

    def __init__(self, name: str, spec: str, conv: str | None) -> None:
        self.name = name
        self.spec = spec
        self.conv = conv
        self.token = "{" + name + (f"!{conv}" if conv else "") + (f":{spec}" if spec else "") + "}"
        self.base = _FIELD_BASE_RE.match(name).group(0)  # type: ignore[union-attr]
        if self.base in _text_variables():
            self.kind = "text"
        elif name in _SNIPPET_FIELDS:
            self.kind = "snippet"
        else:
            self.kind = "unknown"


def _template_parts(template: str) -> list[tuple[str, _Field | None]]:
    """[(literal text, the field after it or None), ...]. Escaped braces come
    back as literal text, so {{text}} is never a field. Raises ValueError on
    an unbalanced brace."""
    return [
        (literal, None if name is None else _Field(name, spec or "", conv))
        for literal, name, spec, conv in _FORMATTER.parse(template)
    ]


def _render(template: str, text: str) -> str:
    """Substitute template variables, leaving unknown placeholders as written.
    Snippet fields are expanded from the template only, never from the
    selection (pinned by
    `tests/test_recipes.py::test_selection_is_never_run_through_the_snippet_engine`,
    `tests/test_recipes.py::test_selection_that_looks_like_a_marker_is_not_expanded_twice`).

    For URLs, notifications and the clipboard. A shell command goes through
    _command_parts instead, which keeps the values out of the command line."""
    values = _text_values(text)
    expanded: dict[str, str] = {}
    out = []
    try:
        for literal, field in _template_parts(template):
            out.append(literal)
            if field is None:
                continue
            if field.kind == "text":
                value = _FORMATTER.get_field(field.name, (), values)[0]
                value = _FORMATTER.convert_field(value, field.conv)
                out.append(_FORMATTER.format_field(value, field.spec))
            elif field.kind == "snippet":
                if field.token not in expanded:
                    expanded[field.token] = _apply_snippet_placeholders(field.token)
                out.append(expanded[field.token])
            else:
                out.append(field.token)
    except Exception as exc:  # noqa: BLE001
        print(f"[recipe] template render failed: {exc}")
        return template
    return "".join(out)


def _text_variables() -> list[str]:
    return [
        "text",
        "text_url",
        "text_shell",
        "text_upper",
        "text_lower",
        "text_strip",
        "text_mock",
        "text_json",
        "text_json_min",
        "text_b64",
        "text_url_decode",
        "text_b64_decode",
    ]


# Commands: the user's text is never rendered into shell source. It goes in
# as an argument and each placeholder becomes a reference to it (pinned by
# `tests/test_recipes.py::test_quoting_in_the_template_never_lets_the_selection_run`,
# `tests/test_recipes.py::test_command_argv_keeps_the_values_out_of_the_script`).
# docs/how-it-works.md, "Recipe commands".

# Where a value reference can't go: bash runs code found in an arithmetic operand
_COMMAND_CONTEXT_PROBLEMS = {
    "arith": "can't be used inside $(( )), (( )) or $[ ]: arithmetic runs code found in the value",
    "dbl": "can't be used inside [[ ]]: -eq and friends run code found in the value; use [ ]",
    "let": "can't be an argument to let or declare/typeset/local -i: they evaluate it as arithmetic",
    "param": "can't be used inside ${...}: an array subscript runs code found in the value",
    "subscript": "can't be used as an array subscript (name[...]): it runs code found in the value",
    "heredoc_quoted": "can't go inside a quoted heredoc (<<'EOF'): nothing expands there",
}
# Stack states that make a field refused however it's quoted
_ARITH_STATES = ("arith", "arith_b", "dbl", "let", "param", "subscript")
# A command word can start here: after a separator or a keyword
_COMMAND_START_RE = re.compile(
    r"(?:^|[;&|(\n{!]|\b(?:then|do|else|elif|if|while|until|time))[ \t]*$"
)
_ARITH_COMMAND_RE = re.compile(r"(let|declare|typeset|local)(?=[ \t])((?:[ \t]+-\w+)*)")


def _shell_contexts(skeleton: str) -> list[str]:
    """The quoting context at each \\x00 in `skeleton` (a command template with
    its fields replaced by \\x00): "plain" (an ordinary word, also inside $(...)
    or backticks), "dq" (inside "..." or an unquoted heredoc), "sq" (inside
    '...'), "heredoc_quoted", or a _COMMAND_CONTEXT_PROBLEMS key for a spot
    where bash would evaluate the value as arithmetic.

    The quoting part only picks how a reference is written so the value stays
    one word in that spot. It is not what stops injection: however the
    reference is written, the value is expanded and never parsed. A wrong
    guess there costs word splitting, not a command. The arithmetic spots err
    towards refusing.
    """
    contexts: list[str] = []
    stack = ["plain"]  # plain, cmd, bt, dq, sq + the _ARITH_STATES
    heredocs: list[tuple[str, bool, bool]] = []  # (delimiter, quoted, strip tabs)
    i, n = 0, len(skeleton)
    while i < n:
        top, c = stack[-1], skeleton[i]
        m: re.Match[str] | None = None
        if c == "\x00":
            danger = next((s for s in reversed(stack) if s in _ARITH_STATES), None)
            if danger:
                contexts.append("arith" if danger == "arith_b" else danger)
            else:
                contexts.append("plain" if top in ("cmd", "bt") else top)
        elif top == "sq":
            if c == "'":
                stack.pop()
        elif c == "\\":
            i += 1
        elif skeleton.startswith("$((", i):
            stack += ["arith", "arith"]  # one per paren, so each ")" pops one
            i += 2
        elif skeleton.startswith("$(", i):
            stack.append("cmd")
            i += 1
        elif skeleton.startswith("$[", i):
            stack.append("arith_b")
            i += 1
        elif skeleton.startswith("${", i):
            stack.append("param")
            i += 1
        elif c == "`":
            if top == "bt":
                stack.pop()
            else:
                stack.append("bt")
        elif (c == "}" and top == "param") or (c == "]" and top in ("arith_b", "subscript")):
            stack.pop()
        elif top == "dq":
            if c == '"':
                stack.pop()
        elif (
            c == "#"
            and top in ("plain", "cmd", "bt")
            and (i == 0 or skeleton[i - 1] in " \t\n;&|(")
        ):
            end = skeleton.find("\n", i)
            end = n if end == -1 else end
            contexts += ["comment"] * skeleton[i:end].count("\x00")
            i = end
            continue
        elif c == "'":
            stack.append("sq")
        elif c == '"':
            stack.append("dq")
        elif top == "let" and c in ";&|\n)":
            stack.pop()
            continue  # the terminator still ends whatever else it ends
        elif c == "(":
            if top != "arith" and skeleton.startswith("((", i):
                stack += ["arith", "arith"]
                i += 1
            else:
                stack.append("arith" if top == "arith" else "cmd")
        elif c == ")":
            if len(stack) > 1 and top in ("cmd", "arith"):
                stack.pop()
        elif skeleton.startswith("[[", i) and _COMMAND_START_RE.search(skeleton[:i]):
            stack.append("dbl")
            i += 1
        elif skeleton.startswith("]]", i) and top == "dbl":
            stack.pop()
            i += 1
        elif c == "[" and i and (skeleton[i - 1].isalnum() or skeleton[i - 1] in "_\x00"):
            # name[...]: an assignment or unset subscript on an indexed array
            stack.append("subscript")
        elif (m := _ARITH_COMMAND_RE.match(skeleton, i)) and _COMMAND_START_RE.search(skeleton[:i]):
            if m.group(1) == "let" or "i" in m.group(2).replace("-", ""):
                stack.append("let")
            i = m.end()
            continue
        elif skeleton.startswith("<<", i) and not skeleton.startswith("<<<", i):
            m = re.match(r"<<(-?)[ \t]*([^\s;&|<>()]+)", skeleton[i:])
            if m:
                word = m.group(2)
                delim = re.sub(r"[\"'\\]", "", word)
                heredocs.append((delim, delim != word, m.group(1) == "-"))
                i += m.end() - 1
        elif c == "\n" and heredocs:
            # The bodies start on the next line, one after the other
            i += 1
            for delim, quoted, strip_tabs in heredocs:
                while i < n:
                    end = skeleton.find("\n", i)
                    end = n if end == -1 else end
                    line = skeleton[i:end]
                    i = end + 1
                    if (line.lstrip("\t") if strip_tabs else line) == delim:
                        break
                    contexts += ["heredoc_quoted" if quoted else "dq"] * line.count("\x00")
            heredocs = []
            continue
        i += 1
    return contexts


def _command_fields(template: str) -> tuple[list[str], list[tuple[_Field, str]]]:
    """Split a command template into literal shell text and the fields between
    it, each with its quoting context: literals[k] comes before fields[k].
    Unknown {names} stay in the literal text as written. Raises ValueError on
    an unbalanced brace or a NUL (used here as the field marker)."""
    if "\x00" in template:
        raise ValueError("the template contains a NUL character")
    literals = [""]
    fields: list[_Field] = []
    for literal, field in _template_parts(template):
        literals[-1] += literal
        if field is None:
            continue
        if field.kind == "unknown":
            literals[-1] += field.token
        else:
            fields.append(field)
            literals.append("")
    contexts = _shell_contexts("\x00".join(literals))
    return literals, list(zip(fields, contexts, strict=True))


def _field_problem(field: _Field, context: str) -> str | None:
    if field.kind == "text" and (field.conv or field.spec or field.name != field.base):
        return (
            f"{field.token} can't take a conversion or format spec in a command; "
            f"use {{{field.base}}}"
        )
    if context in _COMMAND_CONTEXT_PROBLEMS:
        return f"{field.token} {_COMMAND_CONTEXT_PROBLEMS[context]}"
    return None


def command_template_problems(template: str) -> list[str]:
    """Why `template` can't be run as a command, or []. Placeholders need no
    quoting, so this is about syntax that would change the value or put it
    where bash evaluates it."""
    try:
        _, fields = _command_fields(template or "")
    except ValueError as exc:
        return [str(exc)]
    return [p for f, ctx in fields if (p := _field_problem(f, ctx))]


def _command_parts(template: str, text: str, preview: bool = False) -> tuple[str, list[str]]:
    """The `bash -c` script for a command template and the arguments its
    prelude copies into the variables it references. With preview=True the values are written
    into the script, shell-quoted, for showing to the user (never for
    running). Raises ValueError for a template command_template_problems()
    refuses."""
    literals, fields = _command_fields(template)
    values = {**_text_values(text), "text_shell": text}
    args: list[str] = []
    index: dict[str, int] = {}
    out = [literals[0]]
    for (field, context), literal in zip(fields, literals[1:], strict=True):
        problem = _field_problem(field, context)
        if problem:
            raise ValueError(problem)
        if field.token not in index:
            if field.kind == "text":
                args.append(values[field.name])
            else:
                args.append(_apply_snippet_placeholders(field.token))
            index[field.token] = len(args)
        value = args[index[field.token] - 1]
        if preview and context == "dq":
            out.append(re.sub(r'([\\"$`])', r"\\\1", value))
        elif preview and context == "sq":
            out.append(value.replace("'", "'\\''"))
        elif preview:
            out.append(shlex.quote(value))
        else:
            ref = f"${{vinopop_{index[field.token]}}}"
            out.append({"dq": ref, "sq": f"'\"{ref}\"'"}.get(context, f'"{ref}"'))
        out.append(literal)
    if args and not preview:
        out.insert(0, " ".join(f'vinopop_{k}="${{{k}}}"' for k in range(1, len(args) + 1)) + "; ")
    return "".join(out), args


def command_preview(template: str, text: str) -> str:
    """What a command template runs for `text`, as one readable command line."""
    try:
        return _command_parts(template, text, preview=True)[0]
    except ValueError:
        return template


def _command_argv(template: str, text: str) -> list[str]:
    script, args = _command_parts(template, text)
    return ["bash", "-c", script, "vinopop", *args]


def safety_errors(recipe: dict) -> list[str]:
    """Reasons a recipe must not be allowed to run shell commands. Kept apart
    from validate(): an unsafe recipe still loads, as a button that explains
    why it was disabled, rather than silently vanishing (pinned by
    `tests/test_recipes.py::test_an_unsafe_recipe_builds_a_handler_that_never_runs_the_command`,
    `tests/test_recipes.py::test_a_chain_command_step_without_a_template_is_refused`)."""
    action = recipe.get("action") if isinstance(recipe, dict) else None
    if not isinstance(action, dict):
        return []
    errors = []
    if action.get("type") == "run_command":
        for problem in command_template_problems(action.get("template", "")):
            errors.append(f"the command: {problem}")
    elif action.get("type") == "chain":
        for i, step in enumerate(action.get("steps") or []):
            if not isinstance(step, dict) or step.get("type") != "run_command":
                continue
            if not step.get("template"):
                errors.append(f"chain.steps[{i}] (run_command) needs a command template")
                continue
            for problem in command_template_problems(step["template"]):
                errors.append(f"chain.steps[{i}]: {problem}")
    return errors


def _disabled_handler(name: str) -> Callable[[str], None]:
    def handler(_text: str) -> None:
        notify(
            "vinoPop recipe disabled",
            f"{name}: unsafe command template - see ~/.cache/vinopop/vinopop.log",
            "dialog-warning",
            5000,
        )

    return handler


def _build_handler(recipe: dict) -> Callable[[str], None]:
    action = recipe.get("action") or {}
    atype = action.get("type", "")
    template = action.get("template", "")
    title = action.get("title") or recipe.get("tooltip") or recipe.get("name", "vinoPop")
    icon = recipe.get("icon") or "emblem-system-symbolic"

    problems = safety_errors(recipe)
    if problems:
        name = recipe.get("name") or recipe.get("tooltip") or "unnamed"
        print(f"[recipe] REFUSED to load recipe {name!r}: {'; '.join(problems)}")
        return _disabled_handler(name)

    if atype == "open_url":

        def handler(text: str) -> None:
            url = _render(template, text).strip()
            from core import actions

            # Not open_url: a template starting with {text} could make any scheme
            actions.open_web_url(url)

        return handler

    if atype == "run_command":

        def handler(text: str) -> None:
            """A NUL in the selection is reported, not raised (pinned by
            `tests/test_recipes.py::test_a_nul_in_the_selection_reports_an_error_instead_of_raising`)."""
            try:
                subprocess.Popen(_command_argv(template, text), start_new_session=True)
            except (OSError, ValueError) as exc:
                notify("Recipe error", f"Could not run: {exc}", "dialog-error")

        return handler

    if atype == "notify":

        def handler(text: str) -> None:
            body = _render(template, text)
            notify(title, body[:600], icon)

        return handler

    if atype == "copy_transformed":

        def handler(text: str) -> None:
            out = _render(template, text)
            from platform_backend import get_backend

            get_backend().set_clipboard(out)
            notify(title, out[:200], icon)

        return handler

    if atype == "chain":
        steps = action.get("steps") or []
        if not steps:

            def handler(_text: str) -> None:
                notify(
                    "Empty chain",
                    f"{recipe.get('name', 'chain')}: no steps defined",
                    "dialog-warning",
                )

            return handler
        return lambda text: _run_chain(recipe, steps, title, icon, text)

    def handler(text: str) -> None:
        print(f"[recipe] unknown action type for {recipe.get('name')!r}: {atype!r}")

    return handler


def _run_chain(recipe: dict, steps: list, title: str, icon: str, text: str) -> None:
    """Transforms pass their output on as {text}; the first other step ends
    the chain, and a chain of transforms only copies its result (pinned by
    `tests/test_recipe_rules.py::test_each_transform_gets_the_one_befores_output_as_text`,
    `tests/test_recipe_rules.py::test_the_first_terminal_step_ends_the_chain`,
    `tests/test_recipe_rules.py::test_a_chain_of_only_transforms_copies_its_result`,
    `tests/test_recipe_rules.py::test_an_unknown_step_type_ends_the_chain_without_doing_anything`)."""
    import logging as _logging

    log = _logging.getLogger("vinopop")
    current = text
    for i, step in enumerate(steps):
        stype = (step.get("type") or "transform").strip()
        stemplate = step.get("template", "{text}")
        if stype == "transform":
            current = _render(stemplate, current)
            log.info(
                "[chain] %s step %d transform -> %d chars",
                recipe.get("name"),
                i,
                len(current),
            )
            continue
        if stype == "run_command":
            # safety_errors() already refused a step without a template
            try:
                argv = _command_argv(stemplate, current)
                subprocess.Popen(argv, start_new_session=True)
            except (OSError, ValueError) as exc:
                log.warning("[chain] run_command failed: %s", exc)
            return
        rendered = _render(stemplate, current) if stemplate else current
        if stype == "copy":
            from platform_backend import get_backend

            get_backend().set_clipboard(current)
            notify(title or "Copied", current[:200], icon, 2500)
        elif stype == "paste":
            from core import actions

            actions.replace_selection(current)
        elif stype == "notify":
            notify(title or "Chain", (rendered or current)[:600], icon, 4000)
        elif stype == "open_url":
            url = (rendered or current).strip()
            from core import actions

            actions.open_web_url(url)
        else:
            log.warning("[chain] unknown step type %r in %s", stype, recipe.get("name"))
        return
    from platform_backend import get_backend

    get_backend().set_clipboard(current)
    notify(title or "Chain", current[:200], icon, 2500)


def _content_types(recipe: dict) -> tuple:
    names = recipe.get("content_types") or []
    if not names:
        return ()
    return tuple(_CTYPE_BY_NAME[n] for n in names if n in _CTYPE_BY_NAME)


def _template_problem(template: str) -> str | None:
    """Why `template` can't be rendered (an unbalanced brace), or None."""
    if "\x00" in template:
        # Invisible in an editor, so a shared recipe could hide text in it
        # (pinned by `tests/test_recipes.py::test_a_nul_in_a_template_is_refused`)
        return "contains a NUL character"
    try:
        list(string.Formatter().parse(template))
    except ValueError as exc:
        return str(exc)
    return None


def validate(recipe: dict) -> list[str]:
    """Human-readable validation errors, [] for a valid recipe. Every value
    comes from a hand-editable file, so types are checked before use (pinned
    by `tests/test_recipes.py::test_a_malformed_recipe_is_an_error_not_an_exception`,
    `tests/test_recipe_rules.py::test_a_misspelt_content_type_is_an_error_not_every_content_type`)."""
    if not isinstance(recipe, dict):
        return ["recipe must be a JSON object"]
    errors = []
    name = recipe.get("name") or ""
    if not isinstance(name, str):
        errors.append("'name' must be a string")
    elif not name.strip():
        errors.append("'name' is required")
    elif not all(c.isalnum() or c in "-_" for c in name.strip()):
        errors.append("'name' must be alphanumeric / '-' / '_'")
    for key in ("icon", "tooltip"):
        if recipe.get(key) is not None and not isinstance(recipe[key], str):
            errors.append(f"'{key}' must be a string")
    action = recipe.get("action") or {}
    if not isinstance(action, dict):
        return [*errors, "'action' must be an object"]
    if action.get("title") is not None and not isinstance(action["title"], str):
        errors.append("action.title must be a string")
    atype = action.get("type")
    if not isinstance(atype, str) or atype not in VALID_ACTION_TYPES:
        errors.append(f"action.type must be one of {VALID_ACTION_TYPES}")
    if atype == "chain":
        steps = action.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append("chain.steps must be a non-empty list")
        else:
            valid_step_types = {
                "transform",
                "copy",
                "paste",
                "notify",
                "open_url",
                "run_command",
            }
            for i, step in enumerate(steps):
                if not isinstance(step, dict):
                    errors.append(f"chain.steps[{i}] must be an object")
                    continue
                stype = step.get("type", "transform")
                if not isinstance(stype, str) or stype not in valid_step_types:
                    errors.append(
                        f"chain.steps[{i}].type {stype!r} not in {sorted(valid_step_types)}"
                    )
                stemplate = step.get("template") or ""
                if not isinstance(stemplate, str):
                    errors.append(f"chain.steps[{i}].template must be a string")
                    continue
                if stype == "transform" and not stemplate:
                    errors.append(f"chain.steps[{i}] (transform) needs a template")
                problem = _template_problem(stemplate)
                if problem:
                    errors.append(f"chain.steps[{i}].template: {problem}")
    else:
        if not action.get("template", ""):
            errors.append("action.template is required")
        elif not isinstance(action["template"], str):
            errors.append("action.template must be a string")
        else:
            problem = _template_problem(action["template"])
            if problem:
                errors.append(f"action.template: {problem}")
    names = recipe.get("content_types") or []
    if not isinstance(names, list):
        errors.append("'content_types' must be a list")
    else:
        unknown = [n for n in names if not isinstance(n, str) or n not in _CTYPE_BY_NAME]
        if unknown:
            errors.append(f"unknown content_types {unknown}; use {sorted(_CTYPE_BY_NAME)}")
    return errors


_DEFAULT_RECIPE_SEEDS = (
    "google-search.json",
    "wikipedia.json",
    "youtube-search.json",
)
_SEED_MARKER = RECIPES_DIR.parent / ".default-recipes-seeded"


def _seed_default_recipes() -> None:
    """Copy the default recipes into an empty recipes folder, once: a
    default the user deletes later stays deleted (pinned by
    `tests/test_recipe_rules.py::test_a_fresh_install_is_seeded_with_the_default_recipes_once`,
    `tests/test_recipe_rules.py::test_a_folder_that_already_has_recipes_is_not_seeded`)."""
    if _SEED_MARKER.is_file():
        return
    RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    has_existing = any(RECIPES_DIR.glob("*.json"))
    if has_existing:
        _SEED_MARKER.touch()
        return
    repo_dir = Path(__file__).resolve().parent.parent / "plugins_repo" / "recipes"
    if not repo_dir.is_dir():
        _SEED_MARKER.touch()
        return

    for filename in _DEFAULT_RECIPE_SEEDS:
        src = repo_dir / filename
        if not src.is_file():
            continue
        dst = RECIPES_DIR / filename
        if dst.is_file():
            continue
        try:
            shutil.copy2(src, dst)
            print(f"[recipe_loader] seeded default recipe: {filename}")
        except OSError as exc:
            print(f"[recipe_loader] could not seed {filename}: {exc}")
    _SEED_MARKER.touch()


def load_recipes(register) -> int:
    """Register each valid, enabled recipe in RECIPES_DIR; returns how many.
    One bad recipe never stops the rest (pinned by
    `tests/test_recipes.py::test_a_malformed_recipe_does_not_stop_the_ones_after_it`,
    `tests/test_recipe_rules.py::test_a_recipe_is_on_unless_it_says_enabled_false`)."""
    _seed_default_recipes()
    count = 0
    if not RECIPES_DIR.is_dir():
        return 0
    for path in sorted(RECIPES_DIR.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as f:
                recipe = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[recipe] could not read {path.name}: {exc}")
            continue
        try:
            errors = validate(recipe)
        except Exception as exc:  # noqa: BLE001
            errors = [f"could not validate: {exc!r}"]
        if errors:
            print(f"[recipe] {path.name} has errors: {', '.join(errors)}")
            continue
        if not recipe.get("enabled", True):
            continue
        try:
            register(
                Plugin(
                    name=recipe["name"],
                    icon=recipe.get("icon") or "emblem-system-symbolic",
                    tooltip=recipe.get("tooltip") or recipe["name"],
                    handler=_build_handler(recipe),
                    content_types=_content_types(recipe),
                    priority=int(recipe.get("priority", 200)),
                )
            )
            count += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[recipe] failed to register {path.name}: {exc}")
    if count:
        print(f"[recipe_loader] loaded {count} recipe(s)")
    return count


def list_recipes() -> list[tuple[Path, dict]]:
    """Return [(path, recipe_dict), ...] for the UI. Files that don't hold a
    JSON object are skipped: the UI reads fields off each one."""
    out = []
    if not RECIPES_DIR.is_dir():
        return out
    for path in sorted(RECIPES_DIR.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as f:
                recipe = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(recipe, dict):
            out.append((path, recipe))
    return out


def save_recipe(recipe: dict, target_path: Path | None = None) -> Path:
    """Save a recipe to `target_path`, or as a new <name>.json. A new recipe
    whose name is taken gets a numbered one, since the name is also its
    plugin identity (pinned by
    `tests/test_recipes.py::test_new_recipes_get_a_unique_name_instead_of_overwriting`,
    `tests/test_recipes.py::test_editing_a_recipe_still_overwrites_its_own_file`)."""
    RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    if target_path is None:
        name = str(recipe.get("name") or "")
        if not name or not all(c.isalnum() or c in "-_" for c in name):
            raise ValueError(f"invalid recipe name {name!r}")
        unique, n = name, 2
        while (RECIPES_DIR / f"{unique}.json").exists():
            unique, n = f"{name}-{n}", n + 1
        recipe = {**recipe, "name": unique}
        target_path = RECIPES_DIR / f"{unique}.json"
    write_json(target_path, recipe)
    return target_path


def delete_recipe(path: Path) -> None:
    try:
        path.unlink()
    except OSError as exc:
        print(f"[recipe] could not delete {path}: {exc}")


def set_recipe_enabled(path: Path, enabled: bool) -> bool:
    """Set the 'enabled' field of the recipe at `path`; False if it failed."""
    try:
        with path.open("r", encoding="utf-8") as f:
            recipe = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[recipe] could not read {path} for toggle: {exc}")
        return False
    recipe["enabled"] = bool(enabled)
    try:
        save_recipe(recipe, target_path=path)
        return True
    except OSError as exc:
        print(f"[recipe] could not save toggle for {path}: {exc}")
        return False
