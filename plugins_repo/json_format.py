# vinopop-plugin-version: 2
"""Pretty-print JSON: copies formatted result to clipboard."""

from __future__ import annotations

import json

import actions
from classifier import ContentType
from plugin_base import Plugin


def _looks_like_json(text: str) -> bool:
    """Cheap shape check - does the text *start* with a JSON container or
    string? We deliberately don't json.loads() here (too slow to run on
    every popup show)."""
    s = text.lstrip()
    if not s:
        return False
    first = s[0]
    if first not in '{["':
        return False
    # A lone '{' or '[' is too eager - require at least a paired closer
    # somewhere in the selection.
    last = s.rstrip()[-1:]
    return (first, last) in (("{", "}"), ("[", "]"), ('"', '"'))


class _Number(str):
    """A number exactly as the selection wrote it. json.loads would turn
    1.00000000000000000001 into 1.0 and 1e400 into inf, and a 5000-digit int
    trips Python's int-to-str limit; keeping the literal avoids all three."""


def _no_duplicate_keys(pairs: list) -> dict:
    out = {}
    for key, value in pairs:
        if key in out:
            # json.loads keeps the last one silently: pasting that back would
            # drop data from the selection
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _no_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")


def _parse(text: str):
    return json.loads(
        text,
        object_pairs_hook=_no_duplicate_keys,
        parse_float=_Number,
        parse_int=_Number,
        parse_constant=_no_constant,
    )


def _dump(obj, indent: str = "") -> str:
    """json.dumps(obj, indent=2, ensure_ascii=False), but numbers go out as
    they came in."""
    inner = indent + "  "
    if isinstance(obj, _Number):
        return str(obj)
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        items = (
            f"{inner}{json.dumps(k, ensure_ascii=False)}: {_dump(v, inner)}" for k, v in obj.items()
        )
        return "{\n" + ",\n".join(items) + f"\n{indent}}}"
    if isinstance(obj, list):
        if not obj:
            return "[]"
        return "[\n" + ",\n".join(inner + _dump(v, inner) for v in obj) + f"\n{indent}]"
    return json.dumps(obj, ensure_ascii=False)


def _pretty(text: str) -> str:
    """The selection pretty-printed. Raises ValueError for anything that isn't
    JSON or that formatting would change (duplicate keys, NaN/Infinity)."""
    try:
        return _dump(_parse(text))
    except RecursionError as exc:
        raise ValueError("nested too deeply") from exc


def _format(text: str) -> None:
    try:
        pretty = _pretty(text)
    except ValueError as exc:
        actions.notify("JSON error", str(exc)[:300], "dialog-error")
        return
    actions.replace_selection(pretty)
    actions.notify(
        "JSON formatted",
        f"Replaced selection ({len(pretty)} chars)",
        "accessories-text-editor",
        2500,
    )


def register(register_plugin) -> None:
    register_plugin(
        Plugin(
            name="json-format",
            icon="vinopop-json-symbolic",
            tooltip="JSON pretty-print",
            handler=_format,
            content_types=(ContentType.PLAIN_TEXT,),
            priority=55,
            predicate=_looks_like_json,
        )
    )
