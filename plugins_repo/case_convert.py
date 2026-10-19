# vinopop-plugin-version: 2
"""UPPERCASE / lowercase / Title Case / slugify text transformations."""

from __future__ import annotations

import re
import unicodedata

import actions
from classifier import ContentType
from plugin_base import Plugin


def _copy(text: str, label: str) -> None:
    # Replace the user's selection with the result AND keep
    # it on the clipboard. Fallback (read-only context): the
    # clipboard still has it so the user can paste manually.
    actions.replace_selection(text)
    actions.notify(label, text[:200], "accessories-text-editor", 2500)


# Letters NFKD can't decompose into base + accent, so "ignore" would drop them
_TRANSLITERATE = str.maketrans(
    {"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss", "đ": "d",
     "Đ": "D", "ł": "l", "Ł": "L", "þ": "th", "Þ": "Th", "ð": "d", "Ð": "D", "ı": "i"}
)  # fmt: skip


def _slug(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.translate(_TRANSLITERATE))
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()


def _slugify(text: str) -> None:
    slug = _slug(text)
    if not slug:
        # e.g. Cyrillic/CJK: nothing survives, and pasting "" would delete the selection
        actions.notify("Slugify", "Nothing in the selection can be written as a URL slug.")
        return
    _copy(slug, "Slugified")


def _upper(text: str) -> None:
    _copy(text.upper(), "UPPERCASE")


def _lower(text: str) -> None:
    _copy(text.lower(), "lowercase")


_WORD_RE = re.compile(r"\w+(?:['’]\w+)*")


def _title_case(text: str) -> str:
    """str.title() turns "it's" into "It'S"; capitalise whole words instead."""
    return _WORD_RE.sub(lambda m: m.group(0).capitalize(), text)


def _title(text: str) -> None:
    _copy(_title_case(text), "Title Case")


# Predicates: only show a case button when clicking it would actually
# change the selection. Cheap string operations; safe to call on every
# popup show.


def _has_letters(text: str) -> bool:
    return any(c.isalpha() for c in text)


def _not_already_upper(text: str) -> bool:
    return _has_letters(text) and text != text.upper()


def _not_already_lower(text: str) -> bool:
    return _has_letters(text) and text != text.lower()


def _not_already_title(text: str) -> bool:
    return _has_letters(text) and text != _title_case(text)


def register(register_plugin) -> None:
    register_plugin(
        Plugin(
            name="upper-case",
            icon="vinopop-case-upper-symbolic",
            tooltip="UPPERCASE",
            handler=_upper,
            content_types=(ContentType.PLAIN_TEXT,),
            priority=80,
            predicate=_not_already_upper,
        )
    )
    register_plugin(
        Plugin(
            name="lower-case",
            icon="vinopop-case-lower-symbolic",
            tooltip="lowercase",
            handler=_lower,
            content_types=(ContentType.PLAIN_TEXT,),
            priority=81,
            predicate=_not_already_lower,
        )
    )
    register_plugin(
        Plugin(
            name="title-case",
            icon="vinopop-case-title-symbolic",
            tooltip="Title Case",
            handler=_title,
            content_types=(ContentType.PLAIN_TEXT,),
            priority=82,
            predicate=_not_already_title,
        )
    )
    register_plugin(
        Plugin(
            name="slugify",
            icon="vinopop-slugify-symbolic",
            tooltip="Slugify (URL-friendly)",
            handler=_slugify,
            content_types=(ContentType.PLAIN_TEXT,),
            priority=83,
            predicate=_has_letters,  # nothing to slug if it's all digits/symbols
        )
    )
