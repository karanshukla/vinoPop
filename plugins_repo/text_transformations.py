# vinopop-plugin-version: 3
"""A bundle of small text-transform actions: reverse, sort lines, dedupe,
trim per line, strip HTML, remove diacritics, ASCII-only, swap case.
Each transforms the selection and copies the result to the clipboard.
"""

from __future__ import annotations

import html
import re
import unicodedata

import actions
from classifier import ContentType
from plugin_base import Plugin


def _copy(text: str, label: str) -> None:
    """Replace the user's selection with `text` AND copy to clipboard.
    Falls back to clipboard-only if the focused widget is read-only -
    user still gets the transformed text, they just have to paste it
    themselves. Imported from actions so all transformers share the
    same paste-over-selection behaviour."""
    actions.replace_selection(text)
    actions.notify(label, text[:200], "accessories-text-editor", 2500)


# Code points that attach to the character before them. A stand-in for the
# `regex` module's \X, which isn't a dependency: combining marks (category M),
# zero-width joiner sequences, variation selectors, skin-tone modifiers and
# the tag characters of subdivision flags.
_ZWJ = "\u200d"


def _extends(c: str) -> bool:
    cp = ord(c)
    return (
        unicodedata.category(c).startswith("M")
        or 0xFE00 <= cp <= 0xFE0F
        or 0xE0100 <= cp <= 0xE01EF
        or 0x1F3FB <= cp <= 0x1F3FF
        or 0xE0020 <= cp <= 0xE007F
    )


def _is_regional_indicator(c: str) -> bool:
    return 0x1F1E6 <= ord(c) <= 0x1F1FF


def _graphemes(text: str) -> list[str]:
    """Split text into user-perceived characters, so reversing keeps an
    accent on its letter and a flag or family emoji in one piece."""
    clusters: list[str] = []
    for c in text:
        if clusters:
            last = clusters[-1]
            if (
                _extends(c)
                or c == _ZWJ
                or last.endswith(_ZWJ)
                or (last == "\r" and c == "\n")
                or (_is_regional_indicator(c) and len(last) == 1 and _is_regional_indicator(last))
            ):
                clusters[-1] += c
                continue
        clusters.append(c)
    return clusters


def _reversed_text(text: str) -> str:
    return "".join(reversed(_graphemes(text)))


def _reverse(text: str) -> None:
    _copy(_reversed_text(text), "Reversed")


def _relines(text: str, lines: list[str]) -> str:
    """Join transformed lines back the way `text` had them: its line ending
    (CRLF stays CRLF) and its trailing newline, so the line after the
    selection doesn't end up joined onto the last one."""
    eol = "\r\n" if "\r\n" in text else "\n"
    joined = eol.join(lines)
    if lines and text.endswith(("\n", "\r")):
        joined += eol
    return joined


def _sort_lines(text: str) -> None:
    _copy(_relines(text, sorted(text.splitlines())), "Sorted lines")


def _dedupe_lines(text: str) -> None:
    seen, out = set(), []
    for line in text.splitlines():
        if line not in seen:
            seen.add(line)
            out.append(line)
    _copy(_relines(text, out), f"Deduplicated ({len(out)} unique lines)")


def _trim_lines(text: str) -> None:
    _copy(_relines(text, [line.strip() for line in text.splitlines()]), "Trimmed lines")


# Recognises real tag-shaped HTML (open or close), not stray '<' chars
# in prose. Requires at least one letter after the opening '<'.
_TAG_RE = re.compile(r"<\/?[a-zA-Z][^<>]*>")


def _strip_html(text: str) -> None:
    # One pass: chained .replace() calls turn "&amp;lt;" into "<" (double decode)
    cleaned = html.unescape(_TAG_RE.sub("", text)).replace("\xa0", " ")
    _copy(cleaned, "HTML stripped")


# Japanese voiced/semi-voiced marks are part of the kana, not a diacritic to drop
_KEEP_MARKS = "\u3099\u309a"


def _strip_marks(text: str) -> str:
    """Drop combining accents. NFD (not NFKD) so compatibility characters such
    as "²" or "™" survive, and recompose afterwards so Hangul stays intact."""
    decomposed = unicodedata.normalize("NFD", text)
    kept = "".join(c for c in decomposed if c in _KEEP_MARKS or not unicodedata.combining(c))
    return unicodedata.normalize("NFC", kept)


def _remove_diacritics(text: str) -> None:
    _copy(_strip_marks(text), "Diacritics removed")


def _ascii_only(text: str) -> None:
    out = text.encode("ascii", "ignore").decode("ascii")
    _copy(out, "ASCII only")


def _swap_case(text: str) -> None:
    _copy(text.swapcase(), "Case swapped")


# ---- predicates (cheap, run on every popup show) -----------------------


def _has_multiple_lines(text: str) -> bool:
    return "\n" in text.strip("\n")


def _lines_can_dedupe(text: str) -> bool:
    """At least 2 non-empty lines AND at least one duplicate."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return len(lines) >= 2 and len(set(lines)) < len(lines)


def _lines_have_edge_whitespace(text: str) -> bool:
    """At least one line has leading or trailing whitespace worth trimming."""
    return any(ln != ln.strip() for ln in text.splitlines())


def _has_html_tags(text: str) -> bool:
    return bool(_TAG_RE.search(text))


def _has_diacritics(text: str) -> bool:
    """True only when removing accents would actually change the text."""
    return _strip_marks(text) != text


def _has_non_ascii(text: str) -> bool:
    return any(ord(c) > 127 for c in text)


def _has_letters(text: str) -> bool:
    """Skip case-changing buttons on selections without alphabetic chars
    (numbers, symbols, emoji-only selections)."""
    return any(c.isalpha() for c in text)


def _has_mixed_case(text: str) -> bool:
    """Swap-case is only meaningful when there's actually case to swap.
    Otherwise it does the same thing as upper / lower."""
    return any(c.isupper() for c in text) and any(c.islower() for c in text)


def _worth_reversing(text: str) -> bool:
    """Reversing 1-2 chars is rarely useful; the action is mostly a
    novelty on full words/sentences."""
    return len(text.strip()) >= 3


def register(register_plugin) -> None:
    types = (ContentType.PLAIN_TEXT,)
    # Reverse: any non-empty selection - no predicate needed.
    register_plugin(
        Plugin(
            name="reverse-text",
            icon="object-flip-horizontal-symbolic",
            tooltip="Reverse text",
            handler=_reverse,
            content_types=types,
            priority=150,
            predicate=_worth_reversing,
        )
    )
    # Sort: only show when there's more than one line to sort.
    register_plugin(
        Plugin(
            name="sort-lines",
            icon="view-sort-ascending-symbolic",
            tooltip="Sort lines",
            handler=_sort_lines,
            content_types=types,
            priority=151,
            predicate=_has_multiple_lines,
        )
    )
    # Dedupe: only show when there's actually a duplicate to remove.
    register_plugin(
        Plugin(
            name="dedupe-lines",
            icon="edit-clear-all-symbolic",
            tooltip="Deduplicate lines",
            handler=_dedupe_lines,
            content_types=types,
            priority=152,
            predicate=_lines_can_dedupe,
        )
    )
    # Trim: only show if at least one line has leading/trailing whitespace.
    register_plugin(
        Plugin(
            name="trim-lines",
            icon="format-justify-left-symbolic",
            tooltip="Trim each line",
            handler=_trim_lines,
            content_types=types,
            priority=153,
            predicate=_lines_have_edge_whitespace,
        )
    )
    # Strip-HTML: only show if there's an actual <tag> in the selection.
    register_plugin(
        Plugin(
            name="strip-html",
            icon="text-html-symbolic",
            tooltip="Strip HTML tags",
            handler=_strip_html,
            content_types=types,
            priority=154,
            predicate=_has_html_tags,
        )
    )
    # Remove diacritics: only show when there are accented characters.
    register_plugin(
        Plugin(
            name="remove-diacritics",
            icon="format-text-strikethrough-symbolic",
            tooltip="Remove diacritics",
            handler=_remove_diacritics,
            content_types=types,
            priority=155,
            predicate=_has_diacritics,
        )
    )
    # ASCII-only: only show when there are non-ASCII characters to drop.
    register_plugin(
        Plugin(
            name="ascii-only",
            icon="font-x-generic-symbolic",
            tooltip="ASCII only",
            handler=_ascii_only,
            content_types=types,
            priority=156,
            predicate=_has_non_ascii,
        )
    )
    # Swap case: only meaningful if the selection has letters.
    register_plugin(
        Plugin(
            name="swap-case",
            icon="object-flip-vertical-symbolic",
            tooltip="Swap case",
            handler=_swap_case,
            content_types=types,
            priority=157,
            predicate=_has_mixed_case,
        )
    )
