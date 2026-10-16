"""Glue between an LLM task and a result bubble (anything with the
bubble's push_status / push_token / push_done methods)."""

from __future__ import annotations

import logging
from typing import Protocol

from . import available

log = logging.getLogger("vinopop")


class Sink(Protocol):
    def push_status(self, msg: str) -> None: ...
    def push_token(self, chunk: str) -> None: ...
    def push_done(self, text: str | None = None, error: str | None = None) -> None: ...


def run_into(sink: Sink, task: str, text: str, *, lang: str = "English") -> None:
    """Run `task` on `text`, streaming into `sink`. Blocking; never raises:
    every failure ends as a readable message in the bubble."""
    ok, reason = available()
    if not ok:
        sink.push_done(error=reason)
        return
    from . import llm

    try:
        answer = llm.run(
            task, text, lang=lang, on_token=sink.push_token, on_status=sink.push_status
        )
    except Exception as exc:  # noqa: BLE001 - shown to the user, logged in full
        log.exception("[vino_ai] %s failed", task)
        sink.push_done(error=f"Couldn't run the local model: {exc}")
        return
    sink.push_done(text=answer)
