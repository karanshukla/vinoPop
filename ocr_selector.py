"""Frictionless region selector for OCR on Wayland/KDE.

Spectacle 6's region capture always pops its annotate overlay and needs an
explicit Accept. This module replaces that with a PopClip-style flow:

  1. grab the whole screen instantly and silently (spectacle -f -b),
  2. show it as a dimmed full-screen layer-shell overlay,
  3. the user drags one rectangle,
  4. on mouse-up we crop the screenshot to that rectangle and hand the PNG
     back - no buttons, no second app window.

`select_and_capture(callback)` MUST run on the GTK main thread. It calls
`callback(png_path_or_None)` exactly once (None = cancelled / failed).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gtk  # noqa: E402


def _grabber() -> str | None:
    """Name of the first available full-screen grabber, or None."""
    for tool in ("spectacle", "grim", "maim"):
        if shutil.which(tool):
            return tool
    return None


def _capture_fullscreen() -> str | None:
    """Grab the whole screen to a temp PNG with no UI. spectacle -f -b is the
    KWin-native path; grim covers wlroots; maim covers X11.

    The PNG is written to a temporary file in the user's runtime environment."""
    tool = _grabber()
    if not tool:
        return None
    fd, path = tempfile.mkstemp(suffix=".png", prefix="lp-ocr-full-")
    os.close(fd)
    prefix = []
    argv = {
        "spectacle": [*prefix, "spectacle", "-f", "-b", "-n", "-o", path],
        "grim": [*prefix, "grim", path],
        "maim": [*prefix, "maim", path],
    }[tool]
    try:
        subprocess.run(argv, capture_output=True, timeout=15)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        os.unlink(path)
    except OSError:
        pass
    return None


def _layer_shell():
    """The gtk4-layer-shell module if it is installed AND usable on this
    display (it needs a wlr-layer-shell compositor), else None."""
    try:
        gi.require_version("Gtk4LayerShell", "1.0")
        from gi.repository import Gtk4LayerShell as L
    except (ValueError, ImportError):
        return None
    try:
        return L if L.is_supported() else None
    except Exception:
        return None


class _Selector(Gtk.Window):
    def __init__(self, shot_path: str, callback) -> None:
        super().__init__()
        self._cb = callback
        self._fired = False
        self._pb = GdkPixbuf.Pixbuf.new_from_file(shot_path)
        self._shot_path = shot_path
        self._start = None
        self._cur = None
        self._scaled = None  # screenshot scaled to the window, cached per size

        self.set_decorated(False)

        if not self._init_layer_shell():
            # X11 / non-layer-shell fallback: a fullscreen window.
            self.fullscreen()

        area = Gtk.DrawingArea()
        area.set_hexpand(True)
        area.set_vexpand(True)
        area.set_draw_func(self._on_draw)
        area.set_cursor_from_name("crosshair")
        self.set_child(area)
        self._area = area

        drag = Gtk.GestureDrag()
        drag.set_button(Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        area.add_controller(drag)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    def _init_layer_shell(self) -> bool:
        L = _layer_shell()
        if L is None:
            return False
        try:
            L.init_for_window(self)
            L.set_layer(self, L.Layer.OVERLAY)
            L.set_keyboard_mode(self, L.KeyboardMode.EXCLUSIVE)
            for edge in (L.Edge.LEFT, L.Edge.RIGHT, L.Edge.TOP, L.Edge.BOTTOM):
                L.set_anchor(self, edge, True)
            L.set_exclusive_zone(self, -1)
            return True
        except Exception:
            return False

    # ----- drawing -----
    def _on_draw(self, _area, cr, w: int, h: int) -> None:
        # Dimmed screenshot as the backdrop, scaled to the window.
        if self._scaled is None or self._scaled.get_width() != w or self._scaled.get_height() != h:
            self._scaled = self._pb.scale_simple(
                max(1, w), max(1, h), GdkPixbuf.InterpType.BILINEAR
            )
        scaled = self._scaled
        Gdk.cairo_set_source_pixbuf(cr, scaled, 0, 0)
        cr.paint()
        cr.set_source_rgba(0, 0, 0, 0.45)
        cr.paint()
        if self._start and self._cur:
            x0, y0 = self._start
            x1, y1 = self._cur
            rx, ry = min(x0, x1), min(y0, y1)
            rw, rh = abs(x1 - x0), abs(y1 - y0)
            if rw > 1 and rh > 1:
                # Punch the bright, undimmed screenshot through the selection.
                cr.save()
                cr.rectangle(rx, ry, rw, rh)
                cr.clip()
                Gdk.cairo_set_source_pixbuf(cr, scaled, 0, 0)
                cr.paint()
                cr.restore()
                cr.set_source_rgba(0.36, 0.49, 0.96, 1.0)
                cr.set_line_width(2)
                cr.rectangle(rx, ry, rw, rh)
                cr.stroke()

    # ----- input -----
    def _on_drag_begin(self, _g, x: float, y: float) -> None:
        self._start = (x, y)
        self._cur = (x, y)

    def _on_drag_update(self, _g, dx: float, dy: float) -> None:
        if self._start:
            self._cur = (self._start[0] + dx, self._start[1] + dy)
            self._area.queue_draw()

    def _on_drag_end(self, _g, dx: float, dy: float) -> None:
        if not self._start:
            return
        x0, y0 = self._start
        x1, y1 = x0 + dx, y0 + dy
        self._finish((min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)))

    def _on_key(self, _c, keyval: int, _code: int, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self._finish(None)
        return True

    def _finish(self, rect) -> None:
        if self._fired:
            return
        self._fired = True
        crop_path = None
        try:
            if rect is not None:
                rx, ry, rw, rh = rect
                if rw >= 4 and rh >= 4:
                    # Window coords are logical; the screenshot is at device
                    # resolution. Scale the rectangle up to pixbuf pixels.
                    sx = self._pb.get_width() / max(1, self._area.get_width())
                    sy = self._pb.get_height() / max(1, self._area.get_height())
                    px = max(0, int(rx * sx))
                    py = max(0, int(ry * sy))
                    pw = min(self._pb.get_width() - px, int(rw * sx))
                    ph = min(self._pb.get_height() - py, int(rh * sy))
                    if pw > 0 and ph > 0:
                        sub = self._pb.new_subpixbuf(px, py, pw, ph)
                        fd, crop_path = tempfile.mkstemp(suffix=".png", prefix="lp-ocr-crop-")
                        os.close(fd)
                        sub.savev(crop_path, "png", [], [])
        except Exception as exc:  # noqa: BLE001
            print(f"[ocr] crop failed: {exc}")
            crop_path = None
        self.destroy()
        try:
            os.unlink(self._shot_path)
        except OSError:
            pass
        try:
            self._cb(crop_path)
        except Exception as exc:  # noqa: BLE001
            print(f"[ocr] selector callback error: {exc}")


def select_and_capture(callback) -> bool:
    """Show the overlay and call callback(crop_png_or_None). Main thread only.
    Returns True once the overlay is up; False (and never calls back) if we
    couldn't grab the screen, so the caller can fall back to something else."""
    shot = _capture_fullscreen()
    if not shot:
        return False
    sel = _Selector(shot, callback)
    sel.present()
    return True


def available() -> bool:
    """True if we have a full-screen grabber and layer-shell/X11 to overlay."""
    if not _grabber():
        return False
    if _layer_shell() is not None:
        return True
    # No layer-shell: only safe to overlay on X11 (or XWayland, where GDK
    # was forced onto the X11 backend).
    if bool(os.environ.get("DISPLAY")) and not os.environ.get("WAYLAND_DISPLAY"):
        return True
    display = Gdk.Display.get_default()
    return display is not None and type(display).__name__ == "X11Display"
