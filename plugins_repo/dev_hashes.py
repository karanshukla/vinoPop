# vinopop-plugin-version: 2
"""Developer hashing and decoding utilities: SHA-256, MD5, JWT payload decode.
SHA and MD5 copy the digest to the clipboard. JWT decode pretty-prints the
payload and copies it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re

import actions
from classifier import ContentType
from plugin_base import Plugin

# JWT: three base64url segments separated by dots. Header decodes to JSON
# starting with '{' - we don't validate that here, just shape-match.
_JWT_SHAPE = re.compile(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*$")


def _looks_like_jwt(text: str) -> bool:
    stripped = text.strip()
    if not _JWT_SHAPE.match(stripped):
        return False
    # Reject short shape-matches (e.g. "a.b.c"). Real JWTs are 100+ chars.
    return len(stripped) >= 30


def _copy_and_notify(text: str, title: str) -> None:
    actions.replace_selection(text)
    actions.notify(title, text[:280], "security-high-symbolic", 2500)


def _sha256(text: str) -> None:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    _copy_and_notify(digest, "SHA-256")


def _md5(text: str) -> None:
    digest = hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()
    _copy_and_notify(digest, "MD5")


def _b64url_decode(part: str) -> bytes:
    # JWT uses url-safe base64 without padding
    padded = part + "=" * (-len(part) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _jwt_decode(text: str) -> None:
    parts = text.strip().split(".")
    if len(parts) < 2:
        actions.notify(
            "JWT decode", "Not a JWT (expected header.payload.signature)", "dialog-error"
        )
        return
    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
    except Exception as exc:
        actions.notify("JWT decode", str(exc)[:200], "dialog-error")
        return
    if not isinstance(header, dict):
        header = {}
    pretty = json.dumps(payload, indent=2, ensure_ascii=False)
    actions.replace_selection(pretty)
    alg = header.get("alg", "?")
    typ = header.get("typ", "?")
    actions.notify(
        f"JWT decoded (alg={alg}, typ={typ})", pretty[:400], "security-high-symbolic", 2500
    )


def register(register_plugin) -> None:
    types = (ContentType.PLAIN_TEXT,)

    # Predicate: don't offer to hash 'Hello world' or a two-letter
    # selection - typical use is hashing a key, password, or file
    # contents, all of which are at least 8 chars and rarely natural
    # sentences. Same shape as the base64-encode gate.
    def _worth_hashing(text: str) -> bool:
        s = text.strip()
        if len(s) < 8:
            return False
        if "\n" in s:
            return True
        return s.count(" ") <= max(1, len(s) // 12)

    register_plugin(
        Plugin(
            name="sha256",
            icon="security-high-symbolic",
            tooltip="SHA-256 hash",
            handler=_sha256,
            content_types=types,
            priority=180,
            predicate=_worth_hashing,
        )
    )
    register_plugin(
        Plugin(
            name="md5",
            icon="security-medium-symbolic",
            tooltip="MD5 hash",
            handler=_md5,
            content_types=types,
            priority=181,
            predicate=_worth_hashing,
        )
    )
    register_plugin(
        Plugin(
            name="jwt-decode",
            icon="dialog-password-symbolic",
            tooltip="Decode JWT payload",
            handler=_jwt_decode,
            content_types=types,
            priority=182,
            predicate=_looks_like_jwt,
        )
    )
