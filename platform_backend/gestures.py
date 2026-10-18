"""Was a selection change caused by the user selecting something?

Shared by both selection watchers. Apps also set PRIMARY on their own (a
terminal re-claims it on focus, a page selects a hidden field for its Copy
button), so the watchers ask this before popping up, and use the gesture's
end to stop waiting for the selection to settle.
"""

from __future__ import annotations

import time

from .evdev_input import (
    BTN_LEFT,
    KEY_LEFTCTRL,
    KEY_LEFTSHIFT,
    KEY_RIGHTCTRL,
    KEY_RIGHTSHIFT,
    MODIFIER_KEYS,
    InputEvent,
    get_input_hub,
)

# A selecting gesture this long before a PRIMARY burst still counts for it
# (Shift+arrow lands a few ms before the ping it causes).
GESTURE_LEAD_S = 0.5
# Quiet time after the gesture ends before reading PRIMARY. Apps that set it
# on button release do so within a frame or two.
AFTER_GESTURE_S = 0.05


class SelectionGestures:
    """Remembers when the user last did something that selects text: a drag,
    a double/triple click, Shift+click, Shift+key or Ctrl+A. Apps also set
    PRIMARY on their own (a terminal re-claims it on focus), and a plain
    click to focus that window is not a selection.

    Events come from a source with subscribe/unsubscribe/devices: the evdev
    hub on Wayland, XRecord on X11 (x11_input.py). Neither gives button
    coordinates, so a drag is a press held past _DRAG_S. With no readable
    device every change counts."""

    _DRAG_S = 0.15
    _MULTI_CLICK_S = 0.4
    _SHIFT = {KEY_LEFTSHIFT, KEY_RIGHTSHIFT}
    _CTRL = {KEY_LEFTCTRL, KEY_RIGHTCTRL}
    _KEY_A = 30

    def __init__(self, hub=None) -> None:
        self._hub = hub
        self._handle: int | None = None
        self._held: set[int] = set()
        self._press_t = 0.0
        self._release_t = 0.0
        self._multi = False
        self.button_down = False
        self.last = 0.0  # monotonic time of the last selecting gesture

    def start(self) -> None:
        if self._handle is not None:
            return
        if self._hub is None:
            self._hub = get_input_hub()
        self._handle = self._hub.subscribe(self._on_event)

    def stop(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None and self._hub is not None:
            self._hub.unsubscribe(handle)

    def settle_s(self, burst_t0: float, default: float) -> float:
        """How long the PRIMARY stream must stay quiet before the selection
        counts as done. The full debounce exists to wait out a drag; once
        the gesture behind this burst has ended (button up, key pressed),
        a short pause is enough."""
        if self.button_down or self.last < burst_t0 - GESTURE_LEAD_S:
            return default
        return min(default, max(AFTER_GESTURE_S, self.last + AFTER_GESTURE_S - time.monotonic()))

    def selected_since(self, t: float) -> bool:
        if self._hub is None or not self._hub.devices():
            return True
        return self.last >= t

    def _mark(self) -> None:
        self.last = time.monotonic()

    def _on_event(self, ev: InputEvent) -> None:
        if ev.code == BTN_LEFT:
            if ev.value == 1:
                self._multi = ev.time - self._release_t < self._MULTI_CLICK_S
                self._press_t = ev.time
                self.button_down = True
            elif ev.value == 0:
                self.button_down = False
                if (
                    self._multi
                    or self._held & self._SHIFT
                    or ev.time - self._press_t >= self._DRAG_S
                ):
                    self._mark()
                self._release_t = ev.time
            return
        if ev.code in MODIFIER_KEYS:
            if ev.value == 1:
                self._held.add(ev.code)
            elif ev.value == 0:
                self._held.discard(ev.code)
            return
        if ev.value in (1, 2) and (
            self._held & self._SHIFT or (self._held & self._CTRL and ev.code == self._KEY_A)
        ):
            self._mark()
