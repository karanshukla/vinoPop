"""Mouse buttons and keys over XRecord, in the evdev hub's shape, so the
selection-gesture tracker (gestures.py) works on X11 too.

Same XRecord setup as mouse_watcher.DoubleClickWatcher: the context is
enabled on one connection and disabled from another, since enable blocks
inside the RECORD reply loop. Nothing is stored beyond what the tracker
keeps (which modifiers are held, the last press time).

X keycodes on Linux are evdev codes + 8; button 1 is BTN_LEFT. Times are
the server's milliseconds, only ever compared with each other.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .evdev_input import BTN_LEFT, KEYBOARD, POINTER, InputEvent

_X_KEYCODE_OFFSET = 8
_POINTER = frozenset({POINTER})
_KEYBOARD = frozenset({KEYBOARD})


class XRecordInput:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[int, Callable[[InputEvent], None]] = {}
        self._next_id = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._record_display = None
        self._local_display = None
        self._ctx = None
        self._recording = False

    # ---- the evdev hub's interface ---------------------------------------
    def subscribe(self, callback: Callable[[InputEvent], None]) -> int:
        with self._lock:
            self._next_id += 1
            self._subs[self._next_id] = callback
            handle = self._next_id
            if self._thread is None or not self._thread.is_alive():
                self._stop = threading.Event()
                self._thread = threading.Thread(
                    target=self._run, args=(self._stop,), daemon=True, name="vinopop-xrecord"
                )
                self._thread.start()
        return handle

    def unsubscribe(self, handle: int) -> None:
        with self._lock:
            self._subs.pop(handle, None)
            if self._subs:
                return
            stop, thread = self._stop, self._thread
        stop.set()
        deadline = time.monotonic() + 1.0
        while thread is not None and thread.is_alive() and time.monotonic() < deadline:
            # Re-sent until the thread exits: a disable that lands just
            # before the thread's enable would otherwise be lost.
            local, ctx = self._local_display, self._ctx
            try:
                if local is not None and ctx is not None:
                    local.record_disable_context(ctx)
                    local.flush()
            except Exception:
                pass
            thread.join(timeout=0.2)

    def devices(self) -> list[str]:
        """Non-empty while recording works, like the hub's open devices."""
        return ["xrecord"] if self._recording else []

    # ---- reader ------------------------------------------------------------
    def dispatch(self, event: InputEvent) -> None:
        with self._lock:
            subs = list(self._subs.values())
        for cb in subs:
            try:
                cb(event)
            except Exception as exc:  # noqa: BLE001
                print(f"[xrecord] subscriber failed: {exc}")

    def on_x_event(self, etype: int, detail: int, server_ms: int) -> None:
        from Xlib import X

        t = server_ms / 1000.0
        if etype in (X.ButtonPress, X.ButtonRelease):
            if detail == 1:
                self.dispatch(InputEvent(BTN_LEFT, int(etype == X.ButtonPress), _POINTER, t))
        elif etype in (X.KeyPress, X.KeyRelease):
            code = detail - _X_KEYCODE_OFFSET
            self.dispatch(InputEvent(code, int(etype == X.KeyPress), _KEYBOARD, t))

    def _run(self, stop: threading.Event) -> None:
        try:
            from Xlib import X
            from Xlib import display as Xdisplay
            from Xlib.ext import record
            from Xlib.protocol import rq
        except ImportError:
            print("[xrecord] python-xlib record extension missing - gesture check off")
            return
        try:
            self._record_display = Xdisplay.Display()
            self._local_display = Xdisplay.Display()
            self._local_display.record_get_version(0, 0)
        except Exception as exc:  # noqa: BLE001
            print(f"[xrecord] unavailable ({exc}) - gesture check off")
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
                    "device_events": (X.KeyPress, X.ButtonRelease),
                    "errors": (0, 0),
                    "client_started": False,
                    "client_died": False,
                }
            ],
        )
        self._local_display.sync()

        def handler(reply):
            if stop.is_set() or reply.category != record.FromServer or reply.client_swapped:
                return
            data = reply.data
            while len(data):
                event, data = rq.EventField(None).parse_binary_value(
                    data, self._record_display.display, None, None
                )
                self.on_x_event(event.type, event.detail, event.time)

        try:
            if not stop.is_set():
                self._recording = True
                self._record_display.record_enable_context(self._ctx, handler)
        except Exception as exc:  # noqa: BLE001
            print(f"[xrecord] record_enable_context exited: {exc}")
        finally:
            self._recording = False
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


_INPUT: XRecordInput | None = None


def get_xrecord_input() -> XRecordInput:
    global _INPUT
    if _INPUT is None:
        _INPUT = XRecordInput()
    return _INPUT
