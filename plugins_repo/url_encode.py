# vinopop-plugin-version: 2
"""URL-encode and decode selected text."""

from __future__ import annotations

import re
import urllib.parse

import actions
from classifier import ContentType  # noqa: E402
from plugin_base import Plugin  # noqa: E402

_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_PLUS_AS_SPACE = re.compile(r"\S\+\S")


def _looks_percent_encoded(text: str) -> bool:
    """Return True if the selection contains percent-escape sequences
    (\\x%XX), which is the only thing url-decode can actually change."""
    return bool(_PERCENT_ESCAPE.search(text))


_URL_UNRESERVED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

# Characters that strongly suggest the selection is URL-shaped (or
# is meant to be slotted into a URL): query separators, anchors,
# percent signs, plus already-escaped sequences.
_URL_SIGNAL_CHARS = set("?&=#%")


def _has_chars_needing_url_encode(text: str) -> bool:
    """Show URL-encode only when the selection is URL-shaped or actually
    contains characters that produce different output after encoding.

    The classifier already routes recognised URLs through ContentType.URL,
    which the plugin matches via content_types. For PLAIN_TEXT we apply
    a stricter heuristic: at least one URL-signal char (?&=#%) OR
    non-ASCII (where encoding matters for cross-system safety). 'Hello
    world' has only a space - encoding it gives 'Hello%20world', which
    is technically correct but rarely the user's intent for prose.
    """
    if any(c in _URL_SIGNAL_CHARS for c in text):
        return True
    return bool(any(ord(c) > 127 for c in text))


def _copy_and_notify(label: str, text: str) -> None:
    actions.replace_selection(text)
    actions.notify(label, text[:200], "applications-internet", 2500)


_HAS_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
# Delimiters that mean something inside a URL's path, query and fragment
_URL_RESERVED = "/?#[]@!$&'()*+,;=:"
_STRAY_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _quote_component(part: str) -> str:
    """Escape what can't appear in a URL component (spaces, quotes, non-ASCII)
    and keep its delimiters and any %XX escapes it already has."""
    return urllib.parse.quote(_STRAY_PERCENT.sub("%25", part), safe=_URL_RESERVED + "%")


def _url_encoded(text: str) -> str:
    """A whole URL keeps its scheme, host and delimiters and only gets its
    components escaped, so it still opens. Anything else is encoded in full,
    '/' included, ready to be slotted into a query string."""
    if not _HAS_SCHEME.match(text):
        return urllib.parse.quote(text, safe="")
    parts = urllib.parse.urlsplit(text)
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            _quote_component(parts.path),
            _quote_component(parts.query),
            _quote_component(parts.fragment),
        )
    )


def _url_decoded(text: str) -> str:
    # Form-encoded text (hello+world) writes spaces as '+'. Real spaces in
    # the selection mean the '+' signs are literal.
    if _PLUS_AS_SPACE.search(text) and not any(c.isspace() for c in text):
        return urllib.parse.unquote_plus(text)
    return urllib.parse.unquote(text)


def _encode(text: str) -> None:
    _copy_and_notify("URL-encoded", _url_encoded(text))


def _decode(text: str) -> None:
    _copy_and_notify("URL-decoded", _url_decoded(text))


def register(register_plugin) -> None:
    register_plugin(
        Plugin(
            name="url-encode",
            icon="vinopop-url-encode-symbolic",
            tooltip="URL-encode",
            handler=_encode,
            content_types=(ContentType.PLAIN_TEXT, ContentType.URL),
            priority=70,
            predicate=_has_chars_needing_url_encode,
        )
    )
    register_plugin(
        Plugin(
            name="url-decode",
            icon="vinopop-url-decode-symbolic",
            tooltip="URL-decode",
            handler=_decode,
            content_types=(ContentType.PLAIN_TEXT, ContentType.URL),
            priority=71,
            predicate=_looks_percent_encoded,
        )
    )
