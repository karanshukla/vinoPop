"""Frictionless region selector for OCR on Wayland/KDE.

Spectacle 6's region capture always pops its annotate overlay and needs an
explicit Accept. This module replaces that with a PopClip-style flow:

  1. grab the screen instantly and silently (spectacle -f -b; with several
     monitors only the one under the pointer, spectacle -m -b),
  2. show it as a dimmed full-screen layer-shell overlay on that output,
  3. the user drags one rectangle,
  4. on mouse-up we crop the screenshot to that rectangle and hand the PNG
     back - no buttons, no second app window.

The steps are separate functions so a worker thread can drive them and hop
to the GTK main thread only for the GTK parts (screen_ocr._capture_via_overlay
does): `monitor_count()` and `pick_monitor()` and `show()` MUST run on the
GTK main thread; `pointer_hint()` and `grab()` block on a subprocess and
belong on a worker, or the daemon's main loop freezes for the length of the
screenshot. `show()` arranges for `callback(png_path_or_None)` to be called
exactly once (None = cancelled / failed). `select_and_capture(callback)` runs
all of them on the main thread, for callers that have no worker.
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

from platform_backend.monitors import monitor_at  # noqa: E402


def _grabber() -> str | None:
    """Name of the first available full-screen grabber, or None."""
    for tool in ("spectacle", "grim", "maim"):
        if shutil.which(tool):
            return tool
    return None


def monitor_count() -> int:
    """How many outputs GDK sees. Main thread only."""
    display = Gdk.Display.get_default()
    return display.get_monitors().get_n_items() if display is not None else 0


def pointer_hint() -> tuple[int, int, bool] | None:
    """(x, y, is_logical) of the pointer from the platform backend, or None
    when it can't say. Any thread; on KDE it's a KWin round-trip, so not the
    GTK one."""
    try:
        from platform_backend import get_backend

        backend = get_backend()
        x, y = backend.pointer_position()
        return x, y, backend.pointer_is_logical
    except Exception as exc:  # noqa: BLE001
        print(f"[ocr] no pointer position, grabbing the whole desktop: {exc}")
        return None


def pick_monitor(pointer: tuple[int, int, bool] | None):
    """The Gdk.Monitor under `pointer` when there's more than one, else
    None (one screen: grab and overlay the whole desktop as before). The
    overlay is a single surface on one output, so with several monitors a
    whole-desktop grab got squashed onto it. Main thread only."""
    display = Gdk.Display.get_default()
    if display is None or pointer is None:
        return None
    monitors = display.get_monitors()
    if monitors.get_n_items() <= 1:
        return None
    x, y, is_logical = pointer
    return monitor_at(monitors, x, y, is_logical)


def _target_monitor():
    """pick_monitor for the pointer, all on the main thread."""
    return pick_monitor(pointer_hint()) if monitor_count() > 1 else None


def _capture_argv(tool: str, path: str, monitor=None) -> list[str]:
    """Grabber command line: the whole desktop, or just `monitor`.
    spectacle -m grabs the monitor under the pointer (the one we picked);
    grim takes an output name or a logical region; maim takes a physical
    X11 region."""
    if monitor is None:
        return {
            "spectacle": ["spectacle", "-f", "-b", "-n", "-o", path],
            "grim": ["grim", path],
            "maim": ["maim", path],
        }[tool]
    g = monitor.get_geometry()
    if tool == "spectacle":
        return ["spectacle", "-m", "-b", "-n", "-o", path]
    if tool == "grim":
        connector = monitor.get_connector() if hasattr(monitor, "get_connector") else None
        if connector:
            return ["grim", "-o", connector, path]
        return ["grim", "-g", f"{g.x},{g.y} {g.width}x{g.height}", path]
    s = max(1, int(monitor.get_scale_factor()))
    return ["maim", "-g", f"{g.width * s}x{g.height * s}+{g.x * s}+{g.y * s}", path]


def grab(monitor=None) -> str | None:
    """Grab the screen (or just `monitor`) to a temp PNG with no UI.
    spectacle is the KWin-native path; grim covers wlroots; maim covers X11.
    Blocks for the length of the screenshot (spectacle takes a good part of
    a second), so call it from a worker thread.

    The PNG is written to a temporary file in the user's runtime environment."""
    tool = _grabber()
    if not tool:
        return None
    fd, path = tempfile.mkstemp(suffix=".png", prefix="lp-ocr-full-")
    os.close(fd)
    argv = _capture_argv(tool, path, monitor)
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
    def __init__(self, shot_path: str, callback, monitor=None) -> None:
        super().__init__()
        self._monitor = monitor
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
            if self._monitor is not None:
                self.fullscreen_on_monitor(self._monitor)
            else:
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
            if self._monitor is not None:
                L.set_monitor(self, self._monitor)
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


def show(shot_path: str, callback, monitor=None) -> None:
    """Put the overlay for the screenshot at `shot_path` up on `monitor` (or
    the whole desktop). Main thread only. The overlay deletes the screenshot
    and calls callback(crop_png_or_None) once the drag ends."""
    _Selector(shot_path, callback, monitor).present()


_capture_fullscreen = grab


def select_and_capture(callback) -> bool:
    """Show the overlay and call callback(crop_png_or_None), all on the main
    thread (it blocks on the screen grab; workers should drive the steps
    separately, see the module docstring). Returns True once the overlay is
    up; False (and never calls back) if we couldn't grab the screen, so the
    caller can fall back to something else."""
    monitor = _target_monitor()
    shot = grab(monitor)
    if not shot:
        return False
    show(shot, callback, monitor)
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
