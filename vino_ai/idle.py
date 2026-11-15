"""What the LLM, the OCR engine and the voice share for unloading after
`ai_idle_unload_minutes`: reading the setting and re-arming the timer."""

from __future__ import annotations

import threading
from collections.abc import Callable

IDLE_UNLOAD_MINUTES = 5.0
# The timer fires just after the deadline, so the idle check it runs passes.
_SECONDS_PAST_DEADLINE = 1


def float_setting(key: str, default: float) -> float:
    try:
        from core.settings import get_settings

        value = get_settings().get(key)
        return default if value is None else float(value)
    except Exception:
        return default


def rearm(
    timer: threading.Timer | None, minutes: float, unload: Callable[[], object]
) -> threading.Timer | None:
    """Cancel `timer` and return a started one that calls `unload` once
    `minutes` have passed, or None when `minutes` is 0 (never unload)."""
    if timer is not None:
        timer.cancel()
    if minutes <= 0:
        return None
    timer = threading.Timer(minutes * 60 + _SECONDS_PAST_DEADLINE, unload)
    timer.daemon = True
    timer.start()
    return timer
