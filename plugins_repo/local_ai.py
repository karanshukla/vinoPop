# vinopop-plugin-version: 1
"""Summarize, rewrite and explain the selection with a local model.

Everything runs on this machine through OpenVINO (NPU first, then iGPU,
then CPU). The first click downloads the model once; after that it works
offline. Answers stream into a bubble over the selection with Copy, and
Rewrite adds Replace, which pastes the new text over the selection; it is
off when the selection was cut or the answer hit its length cap, since a
partial rewrite would overwrite the whole selection.
"""

from __future__ import annotations

from plugin_base import Plugin

# Shorter selections don't need summarizing
_SUMMARY_MIN_WORDS = 40


def _open(title: str, task: str, text: str, *, replace: bool = False) -> None:
    text = text.strip()
    if not text:
        return
    import ai_bubble
    from vino_ai import ai_worker

    ai_bubble.show(
        title,
        lambda bubble: ai_worker.run_into(bubble, task, text),
        on_replace=ai_bubble.replace_selection if replace else None,
    )


def _long_enough(text: str) -> bool:
    return len(text.split()) >= _SUMMARY_MIN_WORDS


def _has_words(text: str) -> bool:
    return len(text.split()) >= 2


def register(register_plugin) -> None:
    register_plugin(
        Plugin(
            name="ai-summarize",
            icon="view-list-symbolic",
            tooltip="Summarize",
            handler=lambda t: _open("Summary", "summarize", t),
            predicate=_long_enough,
            priority=40,
        )
    )
    register_plugin(
        Plugin(
            name="ai-rewrite",
            icon="document-edit-symbolic",
            tooltip="Rewrite",
            handler=lambda t: _open("Rewrite", "rewrite", t, replace=True),
            predicate=_has_words,
            priority=41,
        )
    )
    register_plugin(
        Plugin(
            name="ai-explain",
            icon="dialog-question-symbolic",
            tooltip="Explain",
            handler=lambda t: _open("Explain", "explain", t),
            priority=42,
        )
    )
