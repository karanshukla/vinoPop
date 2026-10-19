"""What a press of the popup hotkey does: open the popup, close it, or
nothing (#44: the hotkey that opens the popup also closes it).

Pure logic, no GTK or X11, so it's testable without a display.
"""

from __future__ import annotations

import time
from collections.abc import Callable

OPEN = "open"
CLOSE = "close"
IGNORE = "ignore"


class HotkeyToggle:
    # Autorepeat sends presses 25-50 ms apart: anything closer than this to
    # the previous press is the same key still held.
    REPEAT_GAP_S = 0.25
    # The first autorepeat comes 250-660 ms after the press (X11 defaults to
    # 660, KDE to 600). A popup the hotkey opened isn't closed by a press this
    # soon after it appeared, so holding the key can't open and then close it.
    OPEN_GUARD_S = 0.7

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last_press: float | None = None

    def press(self, popup_visible: bool, hotkey_shown_at: float | None) -> str:
        """`hotkey_shown_at` is when the hotkey last showed the popup (clock
        time), or None when what's on screen came from a selection."""
        now = self._clock()
        last, self._last_press = self._last_press, now
        if last is not None and now - last < self.REPEAT_GAP_S:
            return IGNORE
        if not popup_visible:
            return OPEN
        if hotkey_shown_at is not None and now - hotkey_shown_at < self.OPEN_GUARD_S:
            return IGNORE
        return CLOSE
