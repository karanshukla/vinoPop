# vinopop-plugin-version: 1
"""Show word, character and line counts of the selected text via notification."""

from __future__ import annotations

import subprocess

from classifier import ContentType
from plugin_base import Plugin


def _count(text: str) -> None:
    words = len(text.split())
    chars = len(text)
    chars_no_ws = len("".join(text.split()))
    lines = len(text.splitlines())
    body = f"{words} words, {chars} chars ({chars_no_ws} without whitespace), {lines} lines"
    try:
        subprocess.run(
            [
                "notify-send",
                "--hint=byte:transient:1",
                "-t",
                "3000",
                "-i",
                "vinopop-wordcount",
                "vinoPop wordcount",
                body,
            ],
            check=False,
        )
    except FileNotFoundError:
        print(f"[wordcount] {body}")


def register(register_plugin) -> None:
    register_plugin(
        Plugin(
            name="wordcount",
            icon="vinopop-wordcount-symbolic",
            tooltip="Word & char count",
            handler=_count,
            content_types=(ContentType.PLAIN_TEXT,),
            priority=50,
        )
    )
