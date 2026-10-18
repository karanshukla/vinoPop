# vinopop-plugin-version: 1
"""Base64 encode/decode: writes result to clipboard and shows a notification."""

from __future__ import annotations

import base64
import re
import subprocess

from classifier import ContentType
from plugin_base import Plugin

_BASE64_CHARS = re.compile(r"^[A-Za-z0-9+/=\s]+$")
_BASE64URL_CHARS = re.compile(r"^[A-Za-z0-9_\-=\s]+$")


def _looks_like_base64(text: str) -> bool:
    """True if the text plausibly is base64 (or base64url): right alphabet,
    a length that padding can complete (unpadded base64 is common, and the
    decoder pads it), at least 8 chars (shorter strings collide too easily
    with plain words like 'cafe')."""
    stripped = "".join(text.split())
    # A remainder of 1 can't be completed: no byte count encodes to it
    if len(stripped) < 8 or len(stripped.rstrip("=")) % 4 == 1:
        return False
    if not (_BASE64_CHARS.match(stripped) or _BASE64URL_CHARS.match(stripped)):
        return False
    # Don't trigger on words that just happen to fit the alphabet ('information' = 11 chars, not %4).
    # A real base64 string usually mixes upper + lower OR contains digits/+//=.
    has_mixed_case = any(c.isupper() for c in stripped) and any(c.islower() for c in stripped)
    has_digit_or_sym = any(c.isdigit() or c in "+/=_-" for c in stripped)
    if not (has_mixed_case or has_digit_or_sym):
        return False
    # Without the length-divisible-by-4 rule a capitalised word like
    # "Information" fits too. Decoding is cheap: offer it only when the
    # result is text, which is all _decode will paste anyway.
    try:
        _decode_base64(text)
    except ValueError:
        return False
    return True


def _notify(title: str, body: str) -> None:
    try:
        subprocess.run(
            [
                "notify-send",
                "--hint=byte:transient:1",
                "-t",
                "3000",
                "-i",
                "accessories-character-map",
                title,
                body[:300],
            ],
            check=False,
        )
    except FileNotFoundError:
        print(f"[{title}] {body}")


def _to_clipboard(text: str) -> None:
    """Replace the user's selection with the encoded/decoded text. The
    original 'just copy' behaviour left the user having to manually paste;
    transformation plugins should swap text in-place - clipboard fallback
    still works when the focus is read-only."""
    import actions

    actions.replace_selection(text)


def _encode(text: str) -> None:
    enc = base64.b64encode(text.encode("utf-8")).decode("ascii")
    _to_clipboard(enc)
    _notify("Base64 encoded", f"Copied ({len(enc)} chars): {enc[:80]}")


def _decode_base64(text: str) -> str:
    """Decode standard or URL-safe base64, tolerating wrapped lines and
    missing padding. Raises ValueError for non-base64 or non-text payloads."""
    compact = "".join(text.split()).replace("-", "+").replace("_", "/")
    compact += "=" * (-len(compact) % 4)
    raw = base64.b64decode(compact, validate=True)
    return raw.decode("utf-8")


def _decode(text: str) -> None:
    try:
        dec = _decode_base64(text)
    except UnicodeDecodeError:
        # Pasting replacement characters over the selection would destroy it
        _notify("Base64 decoded", "The data isn't text (binary), so nothing was replaced.")
        return
    except Exception as exc:  # noqa: BLE001
        _notify("Base64 error", f"Could not decode: {exc}")
        return
    _to_clipboard(dec)
    _notify("Base64 decoded", f"Copied: {dec[:120]}")


def _worth_encoding(text: str) -> bool:
    """Base64-encoding casual prose is rarely useful - bias the button
    toward selections that look like data or keys: 16+ chars, single-line,
    no natural-language whitespace. Filters out 'Hello world' shaped
    selections while keeping the action available for things like
    `MySuperSecretToken123` or pasted binary."""
    s = text.strip()
    if len(s) < 16:
        return False
    if "\n" in s:
        return True  # multi-line selections often ARE data dumps
    # Natural prose has lots of internal whitespace; data tends not to
    return s.count(" ") <= max(1, len(s) // 12)


def register(register_plugin) -> None:
    register_plugin(
        Plugin(
            name="base64-encode",
            icon="vinopop-base64-encode-symbolic",
            tooltip="Base64 encode",
            handler=_encode,
            content_types=(ContentType.PLAIN_TEXT,),
            priority=60,
            predicate=_worth_encoding,
        )
    )
    register_plugin(
        Plugin(
            name="base64-decode",
            icon="vinopop-base64-decode-symbolic",
            tooltip="Base64 decode",
            handler=_decode,
            content_types=(ContentType.PLAIN_TEXT,),
            priority=61,
            predicate=_looks_like_base64,
        )
    )
