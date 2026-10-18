"""Watches the X11 PRIMARY selection and notifies a callback when it changes.

Uses XFixes selection-owner-notify events (event-driven, ~zero CPU when
idle) with a short xclip read after each event to fetch the actual text.
"""

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
        # Wait this long after the last selection-change event before firing.
        # Avoids popup churn while the user is still dragging the selection -
        # X PRIMARY updates on every character as the drag extends.
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
        """Update the debounce window live. Read by the event loop on its
        next iteration - no restart needed."""
        self._debounce_s = max(0.0, ms / 1000.0)

    def _read_primary(self) -> str:
        return self._read("primary")

    def _read_clipboard(self) -> str:
        return self._read("clipboard")

    def _read(self, sel: str) -> str:
        try:
            # 2 s timeout - xclip can take noticeable time to pull a
            # large selection (several MB of pasted text, a Firefox
            # readability-mode paragraph, etc.). 0.5 s was timing out
            # on long selections and we'd silently skip the popup.
            output = subprocess.run(
                ["xclip", "-selection", sel, "-o"],
                capture_output=True,
                timeout=2.0,
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

    def _handle_selection_change(self, dpy: display.Display, burst_t0: float) -> None:
        # Try a few times to read the new content. The xfixes event fires
        # on owner change, but the new owner often hasn't actually written
        # the data yet (esp. apps that build their selection lazily on
        # CONVERT_SELECTION). 270 ms total budget: fast apps break on
        # the very first read (40 ms), only lazy converters spend the
        # whole window. _read_primary's own xclip timeout (2 s) still
        # protects huge selections that take time to transfer.
        text = ""
        for delay in (0.04, 0.08, 0.15):
            time.sleep(delay)
            text = self._read_primary()
            if text and text.strip():
                break
        if not text or not text.strip():
            return
        if not self._should_show(text, burst_t0):
            return
        # Note: we used to also bail when text == self._last_text, to
        # squash spurious owner-change events that didn't carry new
        # content. Removed because it ate the legitimate "user selected
        # the same word again" case - re-highlighting a phrase to bring
        # the popup back must work. XFixes events without a real
        # selection change are rare in practice, and even if one slips
        # through it's cheaper to redraw than to miss a re-select.
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

        # Try to set up XFixes selection-change notifications
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
        # Idle wakeup cadence - keep at 5 s so a quiet daemon barely costs
        # anything. While a selection change is pending, we override the
        # timeout with the remaining debounce window so we fire promptly.
        IDLE_TIMEOUT = 5.0
        pending = False
        last_event_at = 0.0
        burst_t0 = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if pending:
                # Wait at most until the debounce window expires
                timeout = max(0.0, self._settle_s(burst_t0) - (now - last_event_at))
            else:
                timeout = IDLE_TIMEOUT
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
                    # Loop back to start a new debounce wait
                    continue
            # No fd activity in the timeout window. If we were debouncing,
            # the user has stopped extending the selection - fire now.
            if (
                pending
                and not self._stop.is_set()
                and (time.monotonic() - last_event_at) >= self._settle_s(burst_t0)
            ):
                pending = False
                self._handle_selection_change(dpy, burst_t0)

    def _poll_loop(self, dpy: display.Display) -> None:
        # Fallback: 250 ms polling. Less responsive but XFixes-free.
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
            time.sleep(0.25)
