"""Glue between an LLM task and a result bubble (anything with the
bubble's push_status / push_token / push_done methods)."""

from __future__ import annotations

import logging
from typing import Protocol

from . import available

log = logging.getLogger("vinopop")

TRUNCATED_INPUT = "Only the first part of the selection was used: it's too long for the model."
HIT_MAX_TOKENS = "The answer stopped at its length limit and may be cut short."


class Sink(Protocol):
    def push_status(self, msg: str) -> None: ...
    def push_token(self, chunk: str) -> None: ...
    def push_done(
        self, text: str | None = None, error: str | None = None, warning: str | None = None
    ) -> None: ...


def _warning(answer) -> str | None:
    if answer.truncated_input:
        return TRUNCATED_INPUT
    if answer.hit_max_new_tokens:
        return HIT_MAX_TOKENS
    return None


def run_into(sink: Sink, task: str, text: str, *, lang: str = "English") -> None:
    """Run `task` on `text`, streaming into `sink`. Blocking; never raises:
    every failure ends as a readable message in the bubble. An incomplete
    answer comes with a warning, which also turns off Replace. If the sink
    has `cancelled()`, the run stops once it returns True."""
    _stream_into(sink, task, lambda llm, **kw: llm.run(task, text, lang=lang, **kw))


def describe_into(sink: Sink, image_path: str, ocr_text: str = "") -> None:
    """Stream alt text for the image at `image_path` into `sink`, like
    run_into. `ocr_text` is what OCR read in the image."""
    _stream_into(sink, "describe", lambda llm, **kw: llm.describe(image_path, ocr_text, **kw))


def _stream_into(sink: Sink, label: str, call) -> None:
    ok, reason = available()
    if not ok:
        sink.push_done(error=reason)
        return
    from . import llm

    try:
        answer = call(
            llm,
            on_token=sink.push_token,
            on_status=sink.push_status,
            cancelled=getattr(sink, "cancelled", None),
        )
    except llm.Cancelled:
        log.info("[vino_ai] %s cancelled", label)
        return
    except Exception as exc:  # noqa: BLE001 - shown to the user, logged in full
        log.exception("[vino_ai] %s failed", label)
        sink.push_done(error=f"Couldn't run the local model: {exc}")
        return
    sink.push_done(text=answer.text, warning=_warning(answer))
