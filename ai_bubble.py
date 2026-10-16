"""The floating result bubble the AI actions share.

Summarize, rewrite, explain and translate all show their answer the same
way: a small bubble just below the selection that streams the model's
text in as it is generated, with Copy (and, for rewrite, Replace) chips.
The bubble never takes keyboard focus, so the source app keeps its
selection and Replace can paste straight over it.

Placement uses gtk-layer-shell where the compositor has it (KDE, wlroots)
and plain window moves under X11/XWayland.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

log = logging.getLogger("linuxpop")

_CSS = b"""
window.linuxpop-ai {
    background-color: transparent;
}
box.linuxpop-ai-bubble {
    background-color: #1f2330;
    border-radius: 14px;
    border: 1px solid rgba(255,255,255,0.08);
    padding: 12px 14px;
}
label.linuxpop-ai-text {
    color: #f4f6fb;
    font-size: 13pt;
}
label.linuxpop-ai-title {
    color: #8a92a8;
    font-size: 9pt;
}
button.linuxpop-ai-chip {
    padding: 2px 8px;
    min-height: 0;
}
"""

_css_installed = False

# A slow first load (download + compile) can take a while; only an idle
# bubble times out, never one that is still streaming.
_IDLE_TIMEOUT_S = 45


def _install_css() -> None:
    global _css_installed
    if _css_installed:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS)
    screen = Gdk.Screen.get_default()
    if screen is not None:
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
    _css_installed = True


def pointer_position() -> tuple[int, int]:
    try:
        from platform_backend import get_backend

        return get_backend().pointer_position()
    except Exception:
        return 0, 0


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
        self._dismiss_id: int | None = None
        self._pointer_in_bubble = False
        self.keep_open = False  # set while a header dropdown is open
        self._click_watcher = None
        # Set once destroyed, so late idle callbacks from a worker don't
        # touch dead widgets and a timeout + outside click can't race.
        self._dead = False

        _install_css()
        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.win.set_title(f"vinoPop - {title}")
        self.win.set_decorated(False)
        self.win.set_accept_focus(False)
        self.win.set_app_paintable(True)
        self.win.get_style_context().add_class("linuxpop-ai")
        screen = self.win.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self.win.set_visual(visual)

        self._layer_init()

        ev = self.win.get_events()
        self.win.set_events(ev | Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.win.connect("enter-notify-event", self._on_enter)
        self.win.connect("leave-notify-event", self._on_leave)

        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        bubble.get_style_context().add_class("linuxpop-ai-bubble")
        bubble.set_size_request(300, -1)
        self.win.add(bubble)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._title_lbl = Gtk.Label(label=title, xalign=0)
        self._title_lbl.get_style_context().add_class("linuxpop-ai-title")
        header.pack_start(self._title_lbl, False, False, 0)
        if header_widget is not None:
            header.pack_start(header_widget, False, False, 0)
        header.pack_start(Gtk.Label(), True, True, 0)  # spacer

        self._copy_btn = self._chip("Copy", self._on_copy)
        header.pack_start(self._copy_btn, False, False, 0)
        self._replace_btn = None
        if on_replace is not None:
            self._replace_btn = self._chip("Replace", lambda *_: on_replace(self._text))
            header.pack_start(self._replace_btn, False, False, 0)
        header.pack_start(self._chip("✕", lambda *_: self.dismiss()), False, False, 0)
        bubble.pack_start(header, False, False, 0)

        self._text_lbl = Gtk.Label(xalign=0, yalign=0)
        self._text_lbl.set_line_wrap(True)
        self._text_lbl.set_max_width_chars(48)
        self._text_lbl.set_selectable(True)
        self._text_lbl.get_style_context().add_class("linuxpop-ai-text")
        bubble.pack_start(self._text_lbl, True, True, 0)

        self._set_status("…")

    def _chip(self, label: str, cb) -> Gtk.Button:
        btn = Gtk.Button(label=label)
        btn.get_style_context().add_class("linuxpop-ai-chip")
        btn.connect("clicked", cb)
        return btn

    # ---- layer-shell placement (Wayland) --------------------------------
    def _layer_init(self) -> None:
        try:
            gi.require_version("GtkLayerShell", "0.1")
            from gi.repository import GtkLayerShell as L
        except (ValueError, ImportError):
            self._L = None
            return
        self._L = L
        L.init_for_window(self.win)
        L.set_layer(self.win, L.Layer.OVERLAY)
        try:
            L.set_keyboard_mode(self.win, L.KeyboardMode.NONE)
        except Exception as exc:  # older gtk-layer-shell: focus default is fine
            log.debug("[ai_bubble] set_keyboard_mode unavailable: %s", exc)
        L.set_anchor(self.win, L.Edge.LEFT, True)
        L.set_anchor(self.win, L.Edge.TOP, True)

    def _move(self, x: int, y: int) -> None:
        if self._L is not None:
            self._L.set_margin(self.win, self._L.Edge.LEFT, int(x))
            self._L.set_margin(self.win, self._L.Edge.TOP, int(y))
        else:
            self.win.move(int(x), int(y))

    def present(self) -> None:
        # Measure off-screen first so we can clamp to the monitor.
        self._move(-10000, -10000)
        self.win.show_all()
        self.win.realize()
        self._place()
        self.win.present()
        self._pointer_in_bubble = False
        self._start_click_watcher()

    def _place(self) -> None:
        _, nat = self.win.get_preferred_size()
        w, h = max(nat.width, 1), max(nat.height, 1)
        mon = self.win.get_display().get_monitor_at_point(self._x, self._y)
        geom = mon.get_geometry() if mon else None
        tx = int(self._x - w / 2)
        ty = int(self._y + 24)
        if geom is not None:
            tx = max(geom.x + 4, min(tx, geom.x + geom.width - w - 4))
            if ty + h > geom.y + geom.height - 4:
                ty = int(self._y - h - 24)
            ty = max(geom.y + 4, ty)
        self._move(tx, ty)

    def _start_click_watcher(self) -> None:
        try:
            from popup import _ClickWatcher
        except Exception:
            return  # the ✕ chip and the idle timeout still close it
        self._click_watcher = _ClickWatcher(self._on_global_click)
        self._click_watcher.start()

    def _on_enter(self, _w, event):
        if event.detail != Gdk.NotifyType.INFERIOR:
            self._pointer_in_bubble = True
        return False

    def _on_leave(self, _w, event):
        if event.detail != Gdk.NotifyType.INFERIOR:
            self._pointer_in_bubble = False
        return False

    def _on_global_click(self) -> bool:
        if not self.keep_open and not self._pointer_in_bubble:
            self.dismiss()
        return False

    # ---- content (main thread) ------------------------------------------
    def _set_status(self, msg: str) -> bool:
        if not self._dead:
            self._text_lbl.set_text(msg)
            self._sync_buttons()
        return False

    def _append(self, chunk: str) -> bool:
        if not self._dead:
            self._text += chunk
            self._text_lbl.set_text(self._text.lstrip())
        return False

    def _finish(self, text: str | None, error: str | None) -> bool:
        if self._dead:
            return False
        self._busy = False
        if error:
            self._text = ""
            self._text_lbl.set_text(error)
        else:
            self._text = (text if text is not None else self._text).strip()
            self._text_lbl.set_text(self._text or "(no answer)")
        self._sync_buttons()
        self._arm_timeout()
        return False

    def _sync_buttons(self) -> None:
        ready = not self._busy and bool(self._text)
        self._copy_btn.set_sensitive(ready)
        if self._replace_btn is not None:
            self._replace_btn.set_sensitive(ready)

    def restart(self) -> None:
        """Clear for a new run (e.g. the translate target changed)."""
        self._text = ""
        self._busy = True
        if self._dismiss_id is not None:
            GLib.source_remove(self._dismiss_id)
            self._dismiss_id = None
        self._set_status("…")

    # ---- thread-safe feeders --------------------------------------------
    def push_status(self, msg: str) -> None:
        GLib.idle_add(self._set_status, msg)

    def push_token(self, chunk: str) -> None:
        GLib.idle_add(self._append, chunk)

    def push_done(self, text: str | None = None, error: str | None = None) -> None:
        GLib.idle_add(self._finish, text, error)

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
        if self._dismiss_id is not None:
            GLib.source_remove(self._dismiss_id)
            self._dismiss_id = None
        if self._click_watcher is not None:
            self._click_watcher.stop()
            self._click_watcher = None
        self.win.destroy()


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
