"""Watches the X11 PRIMARY selection (XFixes owner-change events, then an
xclip read) and calls back when it changes."""

from __future__ import annotations

import select as _select
import subprocess
import threading
import time
from collections.abc import Callable

from Xlib import display
from Xlib.ext import xfixes

from platform_backend.base import is_copy_button, take_expected
from platform_backend.gestures import GESTURE_LEAD_S, SelectionGestures
from platform_backend.x11 import XCLIP_TIMEOUT_S

# A new owner often writes the selection lazily, after the owner-change event:
# read again after each of these delays until there is text.
READ_RETRY_DELAYS_S = (0.04, 0.08, 0.15)
IDLE_WAKEUP_S = 5.0
POLL_INTERVAL_S = 0.25


class SelectionWatcher:
    def __init__(
        self,
        on_selection: Callable[[str, int, int], None],
        debounce_ms: int = 300,
        gestures: SelectionGestures | None = None,
    ) -> None:
        self._on_selection = on_selection
        self._gestures = gestures
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_text: str = ""
        # PRIMARY changes on every character of a drag: fire once it's quiet.
        self._debounce_s: float = max(0.0, debounce_ms / 1000.0)
        self._clipboard_atom: int | None = None
        self._clip_changed_at: float | None = None

    def start(self) -> None:
        if self._gestures is None:
            from platform_backend.x11_input import get_xrecord_input

            self._gestures = SelectionGestures(get_xrecord_input())
        self._gestures.start()
        self._thread = threading.Thread(target=self._run, daemon=True, name="vinopop-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._gestures is not None:
            self._gestures.stop()

    def set_debounce_ms(self, ms: int) -> None:
        """Takes effect on the event loop's next iteration."""
        self._debounce_s = max(0.0, ms / 1000.0)

    def _read_primary(self) -> str:
        return self._read("primary")

    def _read_clipboard(self) -> str:
        return self._read("clipboard")

    def _read(self, sel: str) -> str:
        try:
            output = subprocess.run(
                ["xclip", "-selection", sel, "-o"],
                capture_output=True,
                timeout=XCLIP_TIMEOUT_S,
            )
            return output.stdout.decode("utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError):
            return ""

    def _pointer_position(self, dpy: display.Display) -> tuple[int, int]:
        data = dpy.screen().root.query_pointer()
        return data.root_x, data.root_y

    def _note_event(self, ev) -> bool:
        """True for a PRIMARY change. A CLIPBOARD change is only timestamped,
        for the copy-button check."""
        if (
            self._clipboard_atom is not None
            and getattr(ev, "selection", None) == self._clipboard_atom
        ):
            self._clip_changed_at = time.monotonic()
            return False
        return True

    def _should_show(self, text: str, burst_t0: float) -> bool:
        if take_expected(text):
            return True
        if is_copy_button(text, burst_t0, self._clip_changed_at, self._read_clipboard):
            print("[watcher] ignored a copy-button selection (PRIMARY == new CLIPBOARD)")
            return False
        if self._gestures is not None and not self._gestures.selected_since(
            burst_t0 - GESTURE_LEAD_S
        ):
            print("[watcher] ignored a PRIMARY change with no selecting gesture")
            return False
        return True

    def _settle_s(self, burst_t0: float) -> float:
        if self._gestures is None:
            return self._debounce_s
        return self._gestures.settle_s(burst_t0, self._debounce_s)

    def _read_new_primary(self) -> str:
        text = ""
        for delay in READ_RETRY_DELAYS_S:
            time.sleep(delay)
            text = self._read_primary()
            if text and text.strip():
                break
        return text

    def _handle_selection_change(self, dpy: display.Display, burst_t0: float) -> None:
        """Show the popup for the new selection, even when it's the same text
        as last time (pinned by
        `tests/test_x11_watcher.py::test_reselecting_the_same_text_shows_it_again`,
        `tests/test_x11_watcher.py::test_a_blank_selection_is_not_shown`)."""
        text = self._read_new_primary()
        if not text or not text.strip():
            return
        if not self._should_show(text, burst_t0):
            return
        self._last_text = text
        try:
            x, y = self._pointer_position(dpy)
        except Exception as exc:  # noqa: BLE001
            print(f"[watcher] pointer query failed: {exc}")
            x, y = 0, 0
        try:
            self._on_selection(text, x, y)
        except Exception as exc:  # noqa: BLE001
            print(f"[watcher] callback error: {exc}")

    def _run(self) -> None:
        try:
            dpy = display.Display()
        except Exception as exc:  # noqa: BLE001
            print(f"[watcher] cannot open display: {exc}")
            return

        use_xfixes = False
        try:
            dpy.xfixes_query_version()
            root = dpy.screen().root
            primary_atom = dpy.intern_atom("PRIMARY")
            dpy.xfixes_select_selection_input(
                root,
                primary_atom,
                xfixes.XFixesSetSelectionOwnerNotifyMask,
            )
            try:
                clipboard_atom = dpy.intern_atom("CLIPBOARD")
                dpy.xfixes_select_selection_input(
                    root, clipboard_atom, xfixes.XFixesSetSelectionOwnerNotifyMask
                )
                self._clipboard_atom = clipboard_atom
            except Exception as exc:  # noqa: BLE001
                print(f"[watcher] no CLIPBOARD events ({exc}); copy-button filter off")
            dpy.flush()
            use_xfixes = True
            print("[watcher] using XFixes selection events")
        except Exception as exc:  # noqa: BLE001
            print(f"[watcher] XFixes unavailable ({exc}); falling back to polling")

        try:
            if use_xfixes:
                self._event_loop(dpy)
            else:
                self._poll_loop(dpy)
        finally:
            try:
                dpy.close()
            except Exception:
                pass

    def _event_loop(self, dpy: display.Display) -> None:
        fd = dpy.fileno()
        pending = False
        last_event_at = 0.0
        burst_t0 = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if pending:
                timeout = max(0.0, self._settle_s(burst_t0) - (now - last_event_at))
            else:
                timeout = IDLE_WAKEUP_S
            try:
                readable, _, _ = _select.select([fd], [], [], timeout)
            except (OSError, ValueError):
                break
            if readable:
                primary_changed = False
                while dpy.pending_events():
                    primary_changed |= self._note_event(dpy.next_event())
                if primary_changed:
                    if not pending:
                        burst_t0 = time.monotonic()
                    pending = True
                    last_event_at = time.monotonic()
                    continue
            if (
                pending
                and not self._stop.is_set()
                and (time.monotonic() - last_event_at) >= self._settle_s(burst_t0)
            ):
                pending = False
                self._handle_selection_change(dpy, burst_t0)

    def _poll_loop(self, dpy: display.Display) -> None:
        """Fallback without XFixes."""
        while not self._stop.is_set():
            text = self._read_primary()
            if text and text != self._last_text and text.strip():
                self._last_text = text
                try:
                    x, y = self._pointer_position(dpy)
                except Exception:
                    x, y = 0, 0
                try:
                    self._on_selection(text, x, y)
                except Exception as exc:  # noqa: BLE001
                    print(f"[watcher] callback error: {exc}")
            time.sleep(POLL_INTERVAL_S)
