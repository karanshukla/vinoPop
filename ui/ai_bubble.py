"""The floating result bubble the AI actions share.

Summarize, rewrite, explain and translate all show their answer the same
way: a small bubble just below the selection that streams the model's
text in as it is generated, with Copy (and, for rewrite, Replace) chips.
The bubble never takes keyboard focus, so the source app keeps its
selection and Replace can paste straight over it.

Placement goes through the platform backend: gtk4-layer-shell where the
compositor has it (KDE, wlroots) and X11 window moves under X11/XWayland.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from platform_backend.monitors import clamp_to_area, monitor_at, to_logical, usable_area  # noqa: E402

log = logging.getLogger("vinopop")

_CSS = """
window.vinopop-ai {
    background-color: transparent;
}
box.vinopop-ai-bubble {
    background-color: @popover_bg_color;
    color: @popover_fg_color;
    border-radius: 14px;
    border: 1px solid alpha(currentColor, 0.12);
    padding: 12px 14px;
}
label.vinopop-ai-text {
    color: @popover_fg_color;
    font-size: 13pt;
}
label.vinopop-ai-warning {
    color: @warning_color;
    font-size: 9pt;
}
label.vinopop-ai-title {
    /* 0.75 keeps 9pt text at 4.5:1 on the light popover (0.6 was 3.7:1) */
    color: alpha(@popover_fg_color, 0.75);
    font-size: 9pt;
}
button.vinopop-ai-chip {
    padding: 2px 8px;
    min-height: 0;
}
"""

_css_installed = False

# A slow first load (download + compile) can take a while; only an idle
# bubble times out, never one that is still streaming.
_IDLE_TIMEOUT_S = 45
# ...but a run that goes quiet this long while busy is hung: give up on it,
# so the bubble can't stay up forever if it never reports done.
_STALL_TIMEOUT_S = 300
# Share of the monitor height the answer may take before it scrolls.
_MAX_HEIGHT_FRACTION = 0.6


def _install_css() -> None:
    global _css_installed
    if _css_installed:
        return
    provider = Gtk.CssProvider()
    if hasattr(provider, "load_from_string"):  # GTK 4.12+
        provider.load_from_string(_CSS)
    else:
        provider.load_from_data(_CSS.encode(), -1)
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
    _css_installed = True


def pointer_position() -> tuple[int, int]:
    try:
        from platform_backend import get_backend

        return get_backend().pointer_position()
    except Exception:
        return 0, 0


def _backend():
    try:
        from platform_backend import get_backend

        return get_backend()
    except Exception:
        log.exception("[ai_bubble] no platform backend for placement")
        return None


def _logical_anchor(x: int, y: int, is_logical: bool) -> tuple[float, float, Gdk.Rectangle | None]:
    """The pointer in logical coords plus the usable area (workarea) of the
    monitor holding it. X11 root coords are physical, so on a HiDPI screen they are
    divided by that monitor's scale (the same rule popup.py uses); KWin's
    coords are already logical."""
    display = Gdk.Display.get_default()
    if display is None:
        return float(x), float(y), None
    mon = monitor_at(display.get_monitors(), x, y, is_logical)
    lx, ly = to_logical(x, y, mon, is_logical)
    return lx, ly, (usable_area(mon) if mon is not None else None)


def _bubble_target(lx: float, ly: float, w: int, h: int, geom) -> tuple[int, int]:
    """Top-left for a w x h bubble just below logical (lx, ly), centred on
    it, flipped above when there's no room and kept inside `geom` (the
    monitor's usable area)."""
    tx = int(lx - w / 2)
    ty = int(ly + 24)
    if geom is not None:
        if ty + h > geom.y + geom.height - 4:
            ty = int(ly - h - 24)
        tx, ty = clamp_to_area(tx, ty, w, h, geom)
    return tx, ty


class ResultBubble:
    """Build and show on the GTK main thread; feed it from any thread with
    the thread-safe `push_*` methods."""

    def __init__(
        self,
        title: str,
        x: int,
        y: int,
        *,
        header_widget: Gtk.Widget | None = None,
        on_replace: Callable[[str], None] | None = None,
    ) -> None:
        self._x, self._y = x, y
        self._text = ""
        self._busy = True
        # Set when the answer is incomplete (input cut or output capped):
        # Replace would paste a partial text over the whole selection.
        self._incomplete = False
        self._dismiss_id: int | None = None
        self._pointer_in_bubble = False
        self.keep_open = False  # set while a header dropdown is open
        self._click_watcher = None
        self._x_poll = None
        self._stall_id: int | None = None
        self._last_activity = time.monotonic()
        self._replace_id: int | None = None
        self._placed_size = (0, 0)
        # Set once destroyed, so late idle callbacks from a worker don't
        # touch dead widgets and a timeout + outside click can't race.
        self._dead = False
        # Bumped by restart(); a run's sink (for_run) is ignored once stale.
        self._gen = 0

        _install_css()
        self.win = Gtk.Window()
        self.win.set_title(f"vinoPop - {title}")
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.add_css_class("vinopop-ai")

        # "leave" only fires when the pointer leaves the whole window, so
        # crossings between child widgets need no filtering.
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self._on_enter)
        motion.connect("leave", self._on_leave)
        # Same stale-crossing problem as the popup: motion still fires.
        motion.connect("motion", lambda _c, x, y: self._on_enter(_c, x, y))
        self.win.add_controller(motion)
        # Only reaches us if the compositor focused the bubble anyway; the
        # usual unfocused case is covered by the X11 poll (_start_watchers).
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.win.add_controller(keys)

        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        bubble.add_css_class("vinopop-ai-bubble")
        bubble.set_size_request(300, -1)
        self.win.set_child(bubble)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._title_lbl = Gtk.Label(label=title, xalign=0)
        self._title_lbl.add_css_class("vinopop-ai-title")
        header.append(self._title_lbl)
        # Spins until the first word arrives: a model can take seconds
        # before it says anything (~5 s for an image on the NPU).
        self._spinner = Gtk.Spinner()
        header.append(self._spinner)
        if header_widget is not None:
            header.append(header_widget)
        spacer = Gtk.Label()
        spacer.set_hexpand(True)
        header.append(spacer)

        self._copy_btn = self._chip("Copy", self._on_copy)
        header.append(self._copy_btn)
        self._replace_btn = None
        if on_replace is not None:
            self._replace_btn = self._chip("Replace", lambda *_: on_replace(self._text))
            header.append(self._replace_btn)
        close = self._chip("✕", lambda *_: self.dismiss())
        close.set_tooltip_text("Close")
        # A labelled button is named by its label (the glyph) whatever LABEL
        # says, so drop that relation for "Close" to be what's read.
        close.reset_relation(Gtk.AccessibleRelation.LABELLED_BY)
        close.update_property([Gtk.AccessibleProperty.LABEL], ["Close"])
        header.append(close)
        bubble.append(header)

        self._text_lbl = Gtk.Label(xalign=0, yalign=0)
        self._text_lbl.set_wrap(True)
        self._text_lbl.set_max_width_chars(48)
        self._text_lbl.set_selectable(True)
        # A selectable label is focusable; keep it from grabbing focus.
        self._text_lbl.set_focusable(False)
        self._text_lbl.add_css_class("vinopop-ai-text")
        self._text_lbl.set_vexpand(True)
        # A long answer (an 800-token translation is ~60 lines) scrolls
        # instead of running off the screen; _place caps the height.
        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroll.set_propagate_natural_width(True)
        self._scroll.set_propagate_natural_height(True)
        self._scroll.set_max_content_height(400)
        self._scroll.set_child(self._text_lbl)
        bubble.append(self._scroll)

        self._warn_lbl = Gtk.Label(xalign=0)
        self._warn_lbl.set_wrap(True)
        self._warn_lbl.set_max_width_chars(48)
        self._warn_lbl.add_css_class("vinopop-ai-warning")
        self._warn_lbl.set_visible(False)
        bubble.append(self._warn_lbl)

        self._set_status("…")

    def _chip(self, label: str, cb) -> Gtk.Button:
        btn = Gtk.Button(label=label)
        btn.add_css_class("vinopop-ai-chip")
        btn.set_focusable(False)
        btn.connect("clicked", cb)
        return btn

    # ---- placement (via platform_backend) --------------------------------
    def present(self) -> None:
        backend = _backend()
        if backend is not None:
            backend.prepare_popup_window(self.win)
        # GTK 4 measures unrealized windows, so place before mapping.
        self._place(backend)
        self.win.present()
        self._pointer_in_bubble = False
        self._start_watchers()
        self._stall_id = GLib.timeout_add_seconds(30, self._check_stall)

    def _place(self, backend) -> None:
        is_logical = bool(getattr(backend, "pointer_is_logical", True))
        lx, ly, geom = _logical_anchor(self._x, self._y, is_logical)
        if geom is not None:
            self._scroll.set_max_content_height(int(geom.height * _MAX_HEIGHT_FRACTION))
        _, nat = self.win.get_preferred_size()
        w, h = max(nat.width, 1), max(nat.height, 1)
        self._placed_size = (w, h)
        tx, ty = _bubble_target(lx, ly, w, h, geom)
        if backend is not None:
            backend.move_popup_window(self.win, tx, ty)

    def _queue_replace(self) -> None:
        """Re-place once the streamed text has grown the bubble, so it stays
        on screen and, when it sits above the pointer, grows up rather than
        down over the selection. Width counts too: the window grows right
        from its left edge, so a one-line answer that widens the bubble near
        the right edge ran off screen (#97). Coalesced: tokens arrive many
        per frame."""
        if self._replace_id is None and not self._dead:
            self._replace_id = GLib.idle_add(self._replace_now)

    def _replace_now(self) -> bool:
        self._replace_id = None
        if self._dead:
            return False
        _, nat = self.win.get_preferred_size()
        if (max(nat.width, 1), max(nat.height, 1)) != self._placed_size:
            self._place(_backend())
        return False

    def _start_watchers(self) -> None:
        """Close on a click outside or Esc. X11: the popup's Xlib poll, which
        needs no permissions. Wayland: the /dev/input watcher (needs the
        'input' group); without it the ✕ chip and the timeouts still close."""
        try:
            from ui.popup import _ClickWatcher, _XInputPoll
        except Exception:
            return
        poll = _XInputPoll(self._on_global_click, self.dismiss)
        if poll.start():
            self._x_poll = poll
            return
        self._click_watcher = _ClickWatcher(self._on_global_click)
        self._click_watcher.start()

    def _on_key(self, _ctrl, keyval, _code, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.dismiss()
            return True
        return False

    def _on_enter(self, _ctrl, _x, _y) -> None:
        self._pointer_in_bubble = True

    def _on_leave(self, _ctrl) -> None:
        self._pointer_in_bubble = False

    def _on_global_click(self) -> bool:
        if not self.keep_open and not self._pointer_in_bubble:
            self.dismiss()
        return False

    # ---- content (main thread) ------------------------------------------
    def _set_status(self, msg: str) -> bool:
        if not self._dead:
            self._last_activity = time.monotonic()
            self._text_lbl.set_text(msg)
            self._sync_buttons()
            self._queue_replace()
        return False

    def _append(self, chunk: str) -> bool:
        if not self._dead:
            self._last_activity = time.monotonic()
            self._text += chunk
            self._text_lbl.set_text(self._text.lstrip())
            self._sync_spinner()
            self._queue_replace()
        return False

    def _finish(self, text: str | None, error: str | None, warning: str | None = None) -> bool:
        if self._dead:
            return False
        self._busy = False
        self._incomplete = bool(warning) and not error
        self._warn_lbl.set_text(warning or "")
        self._warn_lbl.set_visible(self._incomplete)
        if error:
            self._text = ""
            self._text_lbl.set_text(error)
        else:
            self._text = (text if text is not None else self._text).strip()
            self._text_lbl.set_text(self._text or "(no answer)")
        self._sync_buttons()
        self._queue_replace()
        self._arm_timeout()
        # The bubble never takes focus, so tell a screen reader the answer is in
        spoken = self._text_lbl.get_text()
        self._announce(f"{spoken} {warning}" if self._incomplete and warning else spoken)
        return False

    def _announce(self, message: str) -> None:
        """Have screen readers speak `message` (GTK 4.14+; a no-op before)."""
        if not hasattr(self.win, "announce"):
            return
        try:
            self.win.announce(
                f"{self._title_lbl.get_text()}: {message}",
                Gtk.AccessibleAnnouncementPriority.MEDIUM,
            )
        except Exception:
            pass

    def _check_stall(self) -> bool:
        """Give up on a run that has said nothing for _STALL_TIMEOUT_S: drop
        whatever it sends later (as restart() does) and show why, which arms
        the normal idle timeout."""
        if self._dead:
            self._stall_id = None
            return False
        if self._busy and time.monotonic() - self._last_activity > _STALL_TIMEOUT_S:
            self._gen += 1
            self._finish(None, "No answer from the model, so this was stopped.")
        return True

    def _sync_spinner(self) -> None:
        waiting = self._busy and not self._text
        self._spinner.set_spinning(waiting)
        self._spinner.set_visible(waiting)

    def _sync_buttons(self) -> None:
        self._sync_spinner()
        ready = not self._busy and bool(self._text)
        self._copy_btn.set_sensitive(ready)
        if self._replace_btn is not None:
            self._replace_btn.set_sensitive(ready and not self._incomplete)
            self._replace_btn.set_tooltip_text(
                "The answer is incomplete, so it can't replace the selection; use Copy"
                if self._incomplete
                else None
            )

    def restart(self) -> None:
        """Clear for a new run (e.g. the translate target changed). Feed the
        new run through `for_run()`: the previous run stops at its next token
        (see _RunSink.cancelled), and output already queued must not land in
        this one."""
        self._gen += 1
        self._text = ""
        self._busy = True
        self._incomplete = False
        self._warn_lbl.set_visible(False)
        if self._dismiss_id is not None:
            GLib.source_remove(self._dismiss_id)
            self._dismiss_id = None
        self._set_status("…")

    # ---- thread-safe feeders --------------------------------------------
    def push_status(self, msg: str) -> None:
        GLib.idle_add(self._set_status, msg)

    def push_token(self, chunk: str) -> None:
        GLib.idle_add(self._append, chunk)

    def push_done(
        self, text: str | None = None, error: str | None = None, warning: str | None = None
    ) -> None:
        GLib.idle_add(self._finish, text, error, warning)

    def cancelled(self) -> bool:
        """True once the bubble is closed: the run feeding it can stop.
        Polled from the worker thread."""
        return self._dead

    def for_run(self) -> _RunSink:
        """A feeder for the run starting now; once restart() is called it goes
        quiet, so a superseded run can't write into the next one."""
        return _RunSink(self, self._gen)

    # ---- actions ---------------------------------------------------------
    def _on_copy(self, *_):
        try:
            from platform_backend import get_backend

            get_backend().set_clipboard(self._text)
        except Exception:
            log.exception("[ai_bubble] copy to clipboard failed")

    def _arm_timeout(self) -> None:
        if self._dismiss_id is None:
            self._dismiss_id = GLib.timeout_add_seconds(_IDLE_TIMEOUT_S, self._on_timeout)

    def _on_timeout(self) -> bool:
        self._dismiss_id = None
        self.dismiss()
        return False

    def dismiss(self) -> None:
        if self._dead:
            return
        self._dead = True
        for attr in ("_dismiss_id", "_stall_id", "_replace_id"):
            source = getattr(self, attr)
            if source is not None:
                GLib.source_remove(source)
                setattr(self, attr, None)
        if self._x_poll is not None:
            self._x_poll.stop()
            self._x_poll = None
        if self._click_watcher is not None:
            self._click_watcher.stop()
            self._click_watcher = None
        self.win.destroy()


class _RunSink:
    """push_* feeder for one run of a bubble (see ResultBubble.for_run).
    Staleness is checked on the main thread, when the update would apply."""

    def __init__(self, bubble: ResultBubble, gen: int) -> None:
        self._bubble = bubble
        self._gen = gen

    def _current(self) -> bool:
        return self._gen == self._bubble._gen

    def push_status(self, msg: str) -> None:
        GLib.idle_add(lambda: self._current() and self._bubble._set_status(msg))

    def push_token(self, chunk: str) -> None:
        GLib.idle_add(lambda: self._current() and self._bubble._append(chunk))

    def push_done(
        self, text: str | None = None, error: str | None = None, warning: str | None = None
    ) -> None:
        GLib.idle_add(lambda: self._current() and self._bubble._finish(text, error, warning))

    def cancelled(self) -> bool:
        """True once this run is superseded or its bubble is closed."""
        return not self._current() or self._bubble._dead


def replace_selection(text: str) -> None:
    """Paste `text` over the source app's selection: the bubble never took
    focus, so Ctrl+V lands in the app the text came from."""
    if not text:
        return
    from platform_backend import get_backend

    backend = get_backend()
    backend.set_clipboard(text)
    backend.send_key("ctrl+v")


def show(
    title: str,
    worker: Callable[[ResultBubble], None],
    *,
    header_widget_factory: Callable[[], Gtk.Widget] | None = None,
    on_replace: Callable[[str], None] | None = None,
) -> None:
    """Open a bubble at the pointer and run `worker(bubble)` on a thread.
    Callable from any thread; `worker` must end with `bubble.push_done`."""
    import threading

    x, y = pointer_position()

    def _build() -> bool:
        header = header_widget_factory() if header_widget_factory else None
        bubble = ResultBubble(title, x, y, header_widget=header, on_replace=on_replace)
        bubble.present()
        threading.Thread(target=worker, args=(bubble,), daemon=True, name="ai-bubble").start()
        return False

    GLib.idle_add(_build)
