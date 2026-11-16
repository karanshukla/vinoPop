"""Global modifier+double-click watcher over XRecord, for the no-selection
edit menu (Paste / Select All / Backspace). The modifier keeps it apart from
the app's own double-click word select. The callback runs on the GLib main
thread.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from gi.repository import GLib

_DOUBLE_CLICK_MS = 300
_POSITION_TOLERANCE_PX = 8
_AFTER_CLICK_MS = 20  # the app under the pointer sees the click first
_STOP_JOIN_S = 0.2
_X_BUTTON_LEFT = 1

# X.h modifier masks
_SHIFT_MASK, _CONTROL_MASK, _MOD1_MASK, _MOD4_MASK = 1, 4, 8, 64
_MODIFIER_MASKS = {
    "ctrl": _CONTROL_MASK,
    "shift": _SHIFT_MASK,
    "alt": _MOD1_MASK,
    "super": _MOD4_MASK,
}
_DEFAULT_MODIFIER = "ctrl"


def _required_modifier_mask() -> int:
    """The X modifier mask of the double_click_modifier setting, read on each
    event so a change applies live."""
    try:
        from core.settings import get_settings

        name = (get_settings().get("double_click_modifier") or _DEFAULT_MODIFIER).lower()
    except Exception:
        name = _DEFAULT_MODIFIER
    return _MODIFIER_MASKS.get(name, _MODIFIER_MASKS[_DEFAULT_MODIFIER])


class DoubleClickWatcher:
    """Listens for global left-button double-clicks on the root window."""

    def __init__(self, on_double_click: Callable[[int, int], None]) -> None:
        self._cb = on_double_click
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._record_display = None
        self._local_display = None
        self._ctx = None
        self._last_ms = 0
        self._last_xy = (0, 0)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="vinopop-dblclick",
        )
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Stop recording and wait briefly for the thread. record_enable_context
        blocks its connection in the RECORD reply loop, so the disable goes
        out on the other one (_local_display)."""
        self._stop.set()
        thread = self._thread
        deadline = time.monotonic() + timeout
        while True:
            # Re-sent until the thread exits: a disable that lands just
            # before the thread's enable would otherwise be lost.
            local, ctx = self._local_display, self._ctx
            try:
                if local is not None and ctx is not None:
                    local.record_disable_context(ctx)
                    local.flush()
            except Exception:
                pass
            if thread is None or thread is threading.current_thread():
                return
            thread.join(timeout=_STOP_JOIN_S)
            if not thread.is_alive() or time.monotonic() >= deadline:
                return

    def _run(self) -> None:
        try:
            from Xlib import X
            from Xlib import display as Xdisplay
            from Xlib.ext import record
            from Xlib.protocol import rq
        except ImportError:
            print("[dblclick] python-xlib record extension missing - disabled")
            return
        try:
            self._record_display = Xdisplay.Display()
            self._local_display = Xdisplay.Display()
            v = self._local_display.record_get_version(0, 0)
            print(f"[dblclick] XRecord {v.major_version}.{v.minor_version} ready")
        except Exception as exc:
            print(f"[dblclick] could not open X displays: {exc}")
            return

        self._ctx = self._local_display.record_create_context(
            0,
            [record.AllClients],
            [
                {
                    "core_requests": (0, 0),
                    "core_replies": (0, 0),
                    "ext_requests": (0, 0, 0, 0),
                    "ext_replies": (0, 0, 0, 0),
                    "delivered_events": (0, 0),
                    "device_events": (X.ButtonPress, X.ButtonRelease),
                    "errors": (0, 0),
                    "client_started": False,
                    "client_died": False,
                }
            ],
        )
        # The server must have the context before the other connection
        # enables it, or stop() disables it.
        self._local_display.sync()

        def handler(reply):
            if self._stop.is_set():
                return
            if reply.category != record.FromServer:
                return
            if reply.client_swapped:
                return
            data = reply.data
            while len(data):
                event, data = rq.EventField(None).parse_binary_value(
                    data,
                    self._record_display.display,
                    None,
                    None,
                )
                if (
                    event.type == X.ButtonPress
                    and event.detail == _X_BUTTON_LEFT
                    and event.state & _required_modifier_mask()
                ):
                    self._on_left_click(event.root_x, event.root_y)

        try:
            if not self._stop.is_set():  # stop() may have run before the context existed
                self._record_display.record_enable_context(self._ctx, handler)
        except Exception as exc:
            print(f"[dblclick] record_enable_context exited: {exc}")
        finally:
            try:
                self._local_display.record_free_context(self._ctx)
                self._local_display.flush()
            except Exception:
                pass
            self._ctx = None
            for dpy in (self._record_display, self._local_display):
                try:
                    dpy.close()
                except Exception:
                    pass
            self._record_display = None
            self._local_display = None

    def _on_left_click(self, x: int, y: int) -> None:
        """A left click with the modifier held. The second within
        _DOUBLE_CLICK_MS and _POSITION_TOLERANCE_PX fires; a third starts
        over (pinned by
        `tests/test_platform_double_click.py::test_a_second_click_just_inside_the_window_fires`,
        `tests/test_platform_double_click.py::test_a_second_click_just_after_the_window_does_not`,
        `tests/test_platform_double_click.py::test_a_wiggle_just_inside_the_tolerance_still_fires`,
        `tests/test_platform_double_click.py::test_a_move_of_the_tolerance_does_not`,
        `tests/test_platform_double_click.py::test_a_third_click_does_not_fire_again`)."""
        now_ms = int(time.monotonic() * 1000)
        elapsed = now_ms - self._last_ms
        dx = abs(x - self._last_xy[0])
        dy = abs(y - self._last_xy[1])
        if (
            elapsed < _DOUBLE_CLICK_MS
            and dx < _POSITION_TOLERANCE_PX
            and dy < _POSITION_TOLERANCE_PX
        ):
            self._last_ms = 0
            self._last_xy = (0, 0)
            GLib.timeout_add(_AFTER_CLICK_MS, self._fire_callback, x, y)
            return
        self._last_ms = now_ms
        self._last_xy = (x, y)

    def _fire_callback(self, x: int, y: int) -> bool:
        try:
            self._cb(x, y)
        except Exception as exc:
            print(f"[dblclick] callback failed: {exc}")
        return False  # GLib.SOURCE_REMOVE
