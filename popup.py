"""GTK popup window showing context-aware action buttons.

Visual style is inspired by PopClip on macOS: a small, dark, rounded
floating bar that appears near the selection and disappears when the
user clicks outside it.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

try:
    from Xlib import XK, X  # noqa: E402
    from Xlib import display as xdisplay
except Exception:  # python-xlib is absent on pure-Wayland (KDE) installs
    X = XK = xdisplay = None  # type: ignore

import actions
import plugin_loader
from classifier import ContentType
from platform_backend import get_backend  # noqa: E402
from platform_backend.monitors import monitor_at  # noqa: E402

log = logging.getLogger("vinopop")

_CSS = b"""
/* The window itself is invisible; only the rounded bar paints. GTK 4 needs
 * no RGBA visual for this - a transparent window background is enough. */
window.vinopop-popup {
    background: none;
    background-color: rgba(0, 0, 0, 0);
    box-shadow: none;
    border: none;
}

box.vinopop-bar {
    background-color: #0e0e10;
    border: 1px solid #2a2c30;
    border-radius: __WIN_RADIUS__px;
    padding: __WIN_PAD__px;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55),
                0 0 0 1px rgba(255, 255, 255, 0.06) inset;
}

button.vinopop-action {
    background: transparent;
    border: none;
    box-shadow: none;
    border-radius: __BTN_RADIUS__px;
    padding: __PAD_V__px __PAD_H__px;
    margin: 0;
    min-width: __BTN_SIZE__px;
    min-height: __BTN_SIZE__px;
    color: #fafafa;
    transition: background-color 100ms ease, color 100ms ease;
}

button.vinopop-action:hover {
    background-color: #1f7d42;
    color: #ffffff;
}

button.vinopop-action:active {
    background-color: #1a7040;
    color: #ffffff;
}

button.vinopop-action image {
    color: #fafafa;
}

/* Overflow chip - shown when more plugins matched than the popup
 * limit allows. Renders as a "+N" pill with a softer tone so it
 * reads as informational rather than a normal action. */
button.vinopop-overflow {
    color: #a0a4ac;
    font-weight: 600;
    font-size: 0.85em;
    padding-left: __PAD_H_OVERFLOW__px;
    padding-right: __PAD_H_OVERFLOW__px;
}
button.vinopop-overflow:hover {
    background-color: rgba(128, 128, 128, 0.25);
    color: #fafafa;
}
"""


def _monitor_for_point(display, x: float, y: float, is_logical: bool = True):
    """The Gdk.Monitor containing (x, y), or the first monitor. GTK 4 has no
    get_monitor_at_point(); monitor geometry is logical, so physical X11 root
    coords are scaled down by each monitor's own factor before the test."""
    try:
        return monitor_at(display.get_monitors(), x, y, is_logical)
    except Exception:
        return None


def _texture_from_cairo(surf):
    """Wrap a cairo ARGB32 ImageSurface as a Gdk.Texture (GTK 4 dropped
    Gtk.Image.new_from_surface)."""
    surf.flush()
    return Gdk.MemoryTexture.new(
        surf.get_width(),
        surf.get_height(),
        Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
        GLib.Bytes.new(bytes(surf.get_data())),
        surf.get_stride(),
    )


def _icon_file(icon_name: str, icon_px: int, scale: int) -> str | None:
    """On-disk path of a themed icon, or None (missing / resource-only)."""
    display = Gdk.Display.get_default()
    if display is None:
        return None
    theme = Gtk.IconTheme.get_for_display(display)
    if not theme.has_icon(icon_name):
        return None
    paintable = theme.lookup_icon(
        icon_name, None, icon_px, scale, Gtk.TextDirection.NONE, Gtk.IconLookupFlags(0)
    )
    f = paintable.get_file() if paintable is not None else None
    return f.get_path() if f is not None else None


_BUTTON_SIZE_MIN = 16  # symbolic icons go muddy below this on every screen
_BUTTON_SIZE_MAX = 32  # > 32 is touch-target territory; mouse users don't benefit
_BUTTON_SIZE_DEFAULT = 22

_popup_css_provider: Gtk.CssProvider | None = None


def _resolve_button_size() -> int:
    """Clamp the popup_button_size setting into the supported range.
    Falls back to the default if the key isn't set or has been hand-
    edited to nonsense in settings.json."""
    try:
        from settings import get_settings

        raw = get_settings().get("popup_button_size", _BUTTON_SIZE_DEFAULT)
        size = int(raw or _BUTTON_SIZE_DEFAULT)
    except Exception:
        size = _BUTTON_SIZE_DEFAULT
    return max(_BUTTON_SIZE_MIN, min(_BUTTON_SIZE_MAX, size))


def _build_css(size: int) -> bytes:
    """Substitute the per-size measurements into the static CSS template.
    Padding, radii and inner gutters scale with button size so the
    popup shrinks/grows as a single visual unit. Colours are remapped
    through theme._LIGHT_REMAP when the user's theme is light, so the
    popup background/border/text follow the same palette as the rest
    of the app."""
    pad_v = max(1, size // 12)
    pad_h = max(3, size // 6)
    win_pad = max(2, size // 7)  # gutter between bar and frame
    win_radius = max(6, size // 2 - 2)  # outer corner roundness
    btn_radius = max(3, size // 4)  # button corner roundness
    css = (
        _CSS.replace(b"__BTN_SIZE__", str(size).encode())
        .replace(b"__PAD_V__", str(pad_v).encode())
        .replace(b"__PAD_H__", str(pad_h).encode())
        .replace(b"__PAD_H_OVERFLOW__", str(max(pad_h, 8)).encode())
        .replace(b"__WIN_PAD__", str(win_pad).encode())
        .replace(b"__WIN_RADIUS__", str(win_radius).encode())
        .replace(b"__BTN_RADIUS__", str(btn_radius).encode())
    )
    # Apply the same dark->light palette swap the main theme uses, so
    # the popup doesn't end up as a dark island in a light window.
    try:
        import theme as _theme

        mode = _theme._resolve_mode((get_settings_func() or {}).get("theme", "dark"))
        if mode == "light":
            css = _theme._apply_remap(css, _theme._LIGHT_REMAP)
    except Exception:
        # If theme can't be imported, fall back to dark - it was the
        # default for a long time and won't surprise anyone.
        pass
    return css


def get_settings_func():
    """Single point that yields the live settings dict-like, or {} if
    settings can't be reached. Wrapped so _build_css doesn't have to
    care about import-time ordering."""
    try:
        from settings import get_settings

        return get_settings()
    except Exception:
        return {}


def _install_css() -> None:
    """Install (or reinstall) the popup CSS provider with the current
    button-size setting. Safe to call repeatedly; the old provider is
    removed before the new one is added."""
    global _popup_css_provider
    display = Gdk.Display.get_default()
    if display is None:
        return
    if _popup_css_provider is not None:
        try:
            Gtk.StyleContext.remove_provider_for_display(display, _popup_css_provider)
        except Exception:
            pass
    provider = Gtk.CssProvider()
    css = _build_css(_resolve_button_size())
    if hasattr(provider, "load_from_string"):  # GTK 4.12+
        provider.load_from_string(css.decode("utf-8"))
    else:
        provider.load_from_data(css, -1)
    # +1 over APPLICATION so the bar wins over theme.py's own
    # APPLICATION-priority accents (both sit above libadwaita's stylesheet).
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1,
    )
    _popup_css_provider = provider


def reinstall_popup_css() -> None:
    """Public hook for the settings dialog to call when the user
    changes popup_button_size - rebuilds the CSS so the next popup
    renders at the new size without a daemon restart."""
    _install_css()


class _ClickWatcher:
    """Wayland fallback for the X11 pointer-button poll. KWin doesn't deliver
    clicks that land outside a no-focus layer-shell surface, so we read the
    raw mouse devices via /dev/input while the popup is up and fire a callback
    on any button press. Needs the user in the 'input' group (same as the
    snippet trigger watcher); silently inert otherwise."""

    def __init__(self, on_click) -> None:
        self._on_click = on_click
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="popup-clickwatch")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        import glob
        import os
        import select as _sel
        import struct

        EV_KEY = 0x01
        BTNS = {0x110, 0x111, 0x112}  # BTN_LEFT / RIGHT / MIDDLE
        fmt = "llHHi"
        size = struct.calcsize(fmt)
        paths = set()
        for p in glob.glob("/dev/input/by-path/*-event-mouse"):
            try:
                paths.add(os.path.realpath(p))
            except OSError:
                pass
        if not paths:
            paths = set(glob.glob("/dev/input/event*"))
        fds = []
        for path in sorted(paths):
            try:
                fds.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
            except OSError:
                pass
        if not fds:
            return
        try:
            while not self._stop.is_set():
                r, _, _ = _sel.select(fds, [], [], 0.3)
                for fd in r:
                    try:
                        data = os.read(fd, size * 32)
                    except (BlockingIOError, OSError):
                        continue
                    for off in range(0, len(data) - size + 1, size):
                        _s, _us, et, code, val = struct.unpack_from(fmt, data, off)
                        if et == EV_KEY and code in BTNS and val == 1:
                            GLib.idle_add(self._on_click)
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass


class PopupWindow:
    def __init__(
        self,
        initial_grace_ms: int = 4000,
        leave_grace_ms: int = 600,
        on_open_plugin_order: Callable[[], None] | None = None,
    ) -> None:
        # on_open_plugin_order is invoked when the user clicks the
        # overflow chip - lets us route them straight to Plugin Manager
        # → Order so they can fix what's been hidden.
        self._on_open_plugin_order = on_open_plugin_order
        _install_css()

        # GTK 4 has no window types, type hints, keep-above or accept-focus,
        # so a plain undecorated Gtk.Window is handed to the platform backend,
        # which owns stacking/placement/focus: on X11 it becomes an
        # override-redirect window (never focused, so paste still goes to the
        # text-source app; re-raised on every move so it can't end up under
        # an open Settings / Plugin Manager window, the old Cinnamon bug the
        # GTK 3 TOPLEVEL + POPUP_MENU hint worked around); on Wayland a
        # gtk4-layer-shell OVERLAY surface with keyboard mode NONE.
        # Esc-to-dismiss is handled via the Xlib keymap poll in _tick(),
        # not via GTK key events, so we don't lose that flow.
        self.win = Gtk.Window()
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.set_title("vinoPop")
        self.win.add_css_class("vinopop-popup")

        # Let the platform backend prepare the window before it's ever shown
        # (X11: override-redirect on realize; Wayland/KDE: gtk4-layer-shell).
        try:
            get_backend().prepare_popup_window(self.win)
        except Exception as exc:  # noqa: BLE001
            print(f"[popup] backend window init failed: {exc}")

        # Outer container so the rounded HBox has breathing room from window edges
        self._outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._outer.set_margin_top(1)
        self._outer.set_margin_bottom(1)
        self._outer.set_margin_start(1)
        self._outer.set_margin_end(1)
        self.win.set_child(self._outer)

        # _bar is the rounded pill background. It stacks one or two
        # rows vertically depending on how many actions matched.
        self._bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._bar.add_css_class("vinopop-bar")
        self._bar.set_hexpand(True)
        self._outer.append(self._bar)
        self._row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._bar.append(self._row1)
        self._bar.append(self._row2)
        # Row 2 stays out of the layout entirely until needed; without
        # this it would leave a faint baseline gap below row 1 even when
        # empty.
        self._row2.set_visible(False)

        # Enter/leave + key + click tracking via GTK 4 event controllers.
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self._on_enter)
        motion.connect("leave", self._on_leave)
        # Hiding the popup with the pointer on it (every button click does)
        # leaves GTK thinking the pointer never left, so the next show gets
        # no "enter" and clicks read as outside. Motion still fires.
        motion.connect("motion", self._on_motion)
        self.win.add_controller(motion)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_press)
        self.win.add_controller(keys)
        # Capture-phase click gesture: sees every press before the button
        # under it, records the modifier state (for Shift-click = copy) and
        # handles presses that land outside the bar. Never claims, so the
        # buttons still get their clicks.
        click = Gtk.GestureClick()
        click.set_button(0)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._on_button_press)
        self.win.add_controller(click)
        self._last_press_state = Gdk.ModifierType(0)
        # Focus loss (GTK 3's focus-out-event). The backend keeps the popup
        # unfocused, so this only fires if a WM focused it anyway.
        self.win.connect("notify::is-active", self._on_active_changed)

        self._current_text: str = ""
        # Raw (backend-space) coords the popup is anchored to; used to pick
        # the monitor for row sizing / icon scale before the window maps.
        self._anchor: tuple[int, int] = (0, 0)
        self._hide_timeout_id: int | None = None
        self._tracker_id: int | None = None
        # Absolute ceiling on time the popup will linger if the user
        # never moves the mouse into it. Armed in show_for, cancelled
        # the moment the pointer enters the popup. Independent of the
        # leave-grace timer so a stationary cursor still triggers hide.
        self._initial_hide_id: int | None = None
        self._initial_grace_ms = initial_grace_ms  # before mouse enters
        self._leave_grace_ms = leave_grace_ms  # after mouse leaves popup AND text zone
        # Logical-pixel radius around the original selection cursor that counts
        # as "still over the text" - keeps popup alive while user looks at it.
        self._text_zone_radius = 80
        self._origin_logical: tuple[float, float] = (0.0, 0.0)
        self._popup_rect: tuple[int, int, int, int] = (0, 0, 0, 0)  # x, y, w, h logical
        self._scale: int = 1
        # Xlib display used for polling pointer button state + Esc key.
        # X11 only - the Wayland/KDE backend dismisses via GTK events
        # (leave/focus-out/key-press), so we never open an Xlib display there.
        self._xdpy = None
        self._esc_keycode = 0
        if get_backend().popup_uses_xlib and xdisplay is not None:
            try:
                self._xdpy = xdisplay.Display()
                self._esc_keycode = self._xdpy.keysym_to_keycode(XK.string_to_keysym("Escape"))
            except Exception as exc:  # noqa: BLE001
                print(f"[popup] could not open Xlib display: {exc}")
                self._xdpy = None
                self._esc_keycode = 0
        self._prev_button_pressed = False
        self._prev_esc_pressed = False
        # True while the pointer is over the popup. The popup must never
        # auto-hide while this holds (user is reading / about to click).
        # On Wayland this enter/leave signal is reliable; the tick's
        # screen-space safe-zone math is not (pointer.get_position() is
        # surface-local there), so Wayland persistence is driven from here.
        self._pointer_in_popup = False

    def _clear_buttons(self) -> None:
        # remove() drops the box's (only) GTK reference, which releases the
        # button's signal-handler GClosures and its child image. GTK 4 has no
        # Gtk.Widget.destroy(); unparenting is the whole teardown.
        for row in (self._row1, self._row2):
            child = row.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                row.remove(child)
                child = nxt
        # Collapse row 2 until something gets added to it.
        self._row2.set_visible(False)

    def _add_button(
        self,
        icon_name: str,
        tooltip: str,
        on_click,
        row: Gtk.Box | None = None,
    ) -> None:
        target = row if row is not None else self._row1
        btn = Gtk.Button()
        btn.add_css_class("flat")
        btn.set_focus_on_click(False)
        btn.set_tooltip_text(tooltip)
        btn.add_css_class("vinopop-action")
        btn.set_child(self._make_icon_image(icon_name))
        btn.connect("clicked", on_click)
        target.append(btn)
        if target is self._row2:
            # Once anything lands in row 2, allow it to show.
            self._row2.set_visible(True)

    def _force_all_held(self) -> bool:
        """True if the popup_force_all_modifier setting names a modifier
        that's currently pressed. Read at the moment show_for runs - the
        user must hold the key from before the selection finishes until
        the popup renders (so the modifier survives the 150 ms debounce).
        Returning False is the safe default; misreads just give the
        normal filtered popup."""
        try:
            from settings import get_settings as _gs

            mod_name = (_gs().get("popup_force_all_modifier") or "").strip().lower()
        except Exception:
            return False
        if not mod_name:
            return False
        if self._xdpy is None:
            return False
        try:
            root = self._xdpy.screen().root
            mask = root.query_pointer().mask
        except Exception:
            return False
        # X11 mod bitmasks. Mod1Mask is Alt on essentially every modern
        # X11 desktop; Mod4Mask is Super/Win on Cinnamon, GNOME, KDE,
        # XFCE, MATE. The rare exception (some i3/sway setups remap)
        # rarely runs vinoPop anyway.
        mod_bits = {
            "shift": 1 << 0,  # ShiftMask
            "ctrl": 1 << 2,  # ControlMask
            "alt": 1 << 3,  # Mod1Mask
            "super": 1 << 6,  # Mod4Mask
        }
        bit = mod_bits.get(mod_name)
        if bit is None:
            return False
        return bool(mask & bit)

    def _max_per_row(self, anchor: tuple[int, int] | None = None) -> int:
        """How many buttons fit in a single row before we wrap.

        Uses logical pixels and the user's chosen button size, capped by
        a "comfortable reading width" constant so even a 5K screen
        doesn't grow the popup into a freeway-sized strip.
        """
        size = _resolve_button_size()
        # Button outer width: min-width + horizontal padding on each side
        # (kept in sync with vinopop.css). Slight overestimate so we
        # don't push a button half-off the line by one pixel.
        approx_button_width = size + 14
        # GTK 4 can't query the global pointer, so size against the monitor
        # under the popup's anchor (the selection / pointer coords we got).
        if anchor is None:
            anchor = self._anchor
        try:
            display = self.win.get_display()
            monitor = _monitor_for_point(
                display, anchor[0], anchor[1], get_backend().pointer_is_logical
            )
            monitor_w = monitor.get_geometry().width if monitor else 1920
        except Exception:
            monitor_w = 1920
        target_w = min(680, int(monitor_w * 0.5))
        return max(4, target_w // approx_button_width)

    def _device_scale(self) -> int:
        """The monitor's device-pixel scale (2 on a HiDPI screen), read from
        the monitor rather than the popup window (a layer-shell surface can
        under-report its own scale)."""
        try:
            display = Gdk.Display.get_default()
            monitor = None
            surface = self.win.get_surface() if self.win is not None else None
            if surface is not None and self.win.get_mapped():
                monitor = display.get_monitor_at_surface(surface)
            if monitor is None:
                monitor = _monitor_for_point(
                    display, self._anchor[0], self._anchor[1], get_backend().pointer_is_logical
                )
            return monitor.get_scale_factor() if monitor else 1
        except Exception:
            return 1

    def _colored_icon_with_rim(self, icon_name: str, icon_px: int):
        """A colour logo composited onto a surface at device resolution with a
        thin dark contrast rim hugging the tile edge, so the bright icon reads
        cleanly against the dark popup bar. Returns None on any failure so the
        caller can fall back to the plain icon-name path."""
        try:
            import math

            import cairo
            from gi.repository import GdkPixbuf

            scale = self._device_scale()
            dev = max(12, icon_px * scale)
            path = _icon_file(icon_name, icon_px, scale)
            if not path:
                return None
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, dev, dev, False)
            if pb is None:
                return None
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, dev, dev)
            cr = cairo.Context(surf)
            Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
            cr.paint()
            # Thin dark rounded rim, matched to the brand tiles' ~24% corner.
            r = dev * 0.24
            lw = max(1.0, dev * 0.045)
            o = lw / 2 + 0.4
            x, y, w, h = o, o, dev - 2 * o, dev - 2 * o
            cr.new_sub_path()
            cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
            cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
            cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
            cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
            cr.close_path()
            cr.set_line_width(lw)
            cr.set_source_rgba(0, 0, 0, 0.6)
            cr.stroke()
            # Texture holds device pixels; pixel_size is logical, so GTK draws
            # it 1:1 on a scale-`scale` monitor (crisp on HiDPI).
            image = Gtk.Image.new_from_paintable(_texture_from_cairo(surf))
            image.set_pixel_size(icon_px)
            return image
        except Exception:
            return None

    def _glyph_colors(self) -> tuple[str, str]:
        """Foreground + background hex for our custom mono glyphs, following
        the popup's resolved light/dark theme. Dark-theme defaults: a light
        glyph on the dark bar."""
        fg, bg = "#fafafa", "#0e0e10"
        try:
            import theme as _theme

            mode = _theme._resolve_mode((get_settings_func() or {}).get("theme", "dark"))
            if mode == "light":
                fg, bg = "#1c1d20", "#ffffff"
        except Exception:
            pass
        return fg, bg

    def _glyph_image(self, icon_name: str, icon_px: int):
        """Load one of our custom vinopop-*-symbolic glyphs, recolouring its
        hardcoded palette to the current theme.

        These SVGs hardcode #f0f0f0 strokes (tuned for the dark bar) and are
        NOT in GTK's recolourable symbolic format, so GTK never recolours them
        via the popup CSS `color` property. On a light popup they'd render
        near-white-on-white (invisible) - the "faint glyph" bug. We recolour
        them ourselves at load time instead. Returns None on any failure so the
        caller can fall back to the plain icon-name path."""
        try:
            from gi.repository import GdkPixbuf, Gio

            path = _icon_file(icon_name, icon_px, 1)
            if not path or not path.endswith(".svg"):
                return None
            with open(path, "rb") as fh:
                svg = fh.read()
            if b"#f0f0f0" not in svg.lower():
                return None  # not one of our hardcoded glyphs - leave to GTK
            fg, bg = self._glyph_colors()
            # Body strokes/fills -> foreground; punch-out holes (#2b2b2b) ->
            # background so negative space matches the bar in both themes; the
            # lone map-pin green accent -> foreground (mono glyph).
            for old, new in (
                (b"#f0f0f0", fg),
                (b"#F0F0F0", fg),
                (b"#2b2b2b", bg),
                (b"#2B2B2B", bg),
                (b"#1d7a3a", fg),
                (b"#1D7A3A", fg),
            ):
                svg = svg.replace(old, new.encode())
            scale = self._device_scale()
            dev = max(12, icon_px * scale)
            stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(svg))
            pb = GdkPixbuf.Pixbuf.new_from_stream_at_scale(stream, dev, dev, True, None)
            if pb is None:
                return None
            image = Gtk.Image.new_from_paintable(Gdk.Texture.new_for_pixbuf(pb))
            image.set_pixel_size(icon_px)
            return image
        except Exception:
            return None

    def _make_icon_image(self, icon_name: str) -> Gtk.Image:
        """Render an icon scaled to ~72% of the configured button size, so it
        has a comfortable halo of padding inside the button.

        Colour logos (non-symbolic) are composited with a thin dark contrast
        rim at device resolution so they stay crisp on HiDPI and separate
        cleanly from the dark bar. Our custom vinopop-*-symbolic glyphs are
        recoloured to the theme foreground at load time (GTK can't recolour
        them via CSS). System symbolic icons stay on the icon-name path so the
        popup's CSS can recolour them to follow the text."""
        size = _resolve_button_size()
        icon_px = max(12, int(size * 0.72))
        # Swap to the colour tile or the mono glyph per the user's icon_style.
        try:
            import icon_style

            icon_name = icon_style.resolve(icon_name)
        except Exception:
            pass
        if icon_name and not icon_name.endswith("-symbolic"):
            img = self._colored_icon_with_rim(icon_name, icon_px)
            if img is not None:
                return img
        elif icon_name and icon_name.startswith("vinopop-"):
            img = self._glyph_image(icon_name, icon_px)
            if img is not None:
                return img
        image = Gtk.Image.new_from_icon_name(icon_name)
        image.set_pixel_size(icon_px)
        return image

    def show_for(
        self,
        text: str,
        x: int,
        y: int,
        content_type: ContentType,
        editable: bool = True,
        rect: tuple[int, int, int, int] | None = None,
    ) -> None:
        if self.win.get_visible() and self._pointer_in_popup:
            # The user is reaching for a button, so this isn't a selection
            # they just made. On Wayland it's usually an app re-claiming
            # PRIMARY on focus (a terminal with old text selected), and
            # swapping the buttons now would run them on that text.
            log.info("[popup] ignored a selection change while the pointer is on the popup")
            return
        self._current_text = text
        self._anchor = (int(x), int(y))
        self._clear_buttons()

        # Force-all escape hatch: when the user is holding the configured
        # modifier (default Alt) at the moment we render the popup, skip
        # the classifier's content-type filter and surface every plugin
        # that matches the text - lets the user override a misclassified
        # selection ("Channel | Title" wrongly tagged as COMMAND, etc.)
        # without changing settings. Editable filter still applies so
        # Cut/Paste don't appear in read-only contexts.
        force_all = self._force_all_held()
        if force_all:
            plugins = plugin_loader.all_plugins()
            # Still keep only plugins that match this text via their
            # matches() method - e.g. calculator filters out non-math
            # selections by content. That's intent-preserving, not
            # classifier-based, so it's fine to leave.
            plugins = [p for p in plugins if p.matches(text)]
            # Sort the same way for_content_type does so plugin_order
            # is honoured in the expanded view too.
            try:
                from settings import get_settings as _gs

                order = list(_gs().get("plugin_order") or [])
            except Exception:
                order = []
            order_index = {n: i for i, n in enumerate(order)}
            big = len(order) + 1_000_000
            plugins.sort(
                key=lambda p: (
                    (0, order_index[p.name], p.priority)
                    if p.name in order_index
                    else (1, big, p.priority)
                )
            )
        else:
            plugins = plugin_loader.for_content_type(content_type, text)
        # Strip out plugins that only make sense in editable widgets
        # (Cut/Paste/Backspace/Bold/Italic/Underline) when the focused
        # context is read-only. Callers pass editable=False after probing
        # the focused widget - see main.py / editable_detect.py.
        if not editable:
            plugins = [p for p in plugins if not p.requires_editable]
        # Hard cap so the popup doesn't grow to 25 icons across when
        # many plugins are installed. for_content_type already returns
        # in priority/custom-order, so the first N are the highest-
        # ranked. Users who want everything raise max_popup_buttons.
        try:
            from settings import get_settings as _gs

            max_btns = _gs().get_int("max_popup_buttons") or 10
        except Exception:
            max_btns = 10
        hidden_count = 0
        if max_btns > 0 and len(plugins) > max_btns:
            hidden_count = len(plugins) - max_btns
            print(f"[popup] capping {len(plugins)} plugins to {max_btns} (max_popup_buttons)")
            plugins = plugins[:max_btns]
        if not plugins:
            print(f"[popup] no plugins for {content_type.value} (editable={editable})")
            return

        def make_handler(p):
            def _on_click(_btn):
                # Snapshot text at click time + run on a worker thread so
                # blocking handlers (network, subprocess, xdotool) don't
                # freeze the GTK main loop.
                text_snapshot = self._current_text
                plugin_name = p.name
                # Read the modifier state on the GTK event that
                # triggered this click. Shift means 'copy the
                # result instead of pasting it back' (PopClip
                # convention) - relevant when the plugin uses
                # actions.replace_selection() for an in-place
                # transform. Plugins that don't paste are
                # unaffected by the flag.
                shift_held = self._shift_held()

                def _worker():
                    try:
                        if shift_held:
                            with actions.force_copy_mode():
                                p.execute(text_snapshot)
                        else:
                            p.execute(text_snapshot)
                    except Exception:  # noqa: BLE001
                        log.exception("[popup] plugin '%s' failed", plugin_name)

                threading.Thread(
                    target=_worker,
                    daemon=True,
                    name=f"plugin-{plugin_name}",
                ).start()
                self.hide()

            return _on_click

        specs = [(p.icon, p.tooltip, make_handler(p)) for p in plugins]
        self._present_buttons(specs, hidden_count, max_btns)
        self._present_near(x, y, rect=rect)

    def _add_overflow_chip(
        self,
        hidden: int,
        cap: int,
        row: Gtk.Box | None = None,
    ) -> None:
        """Append a +N chip at the end of the bar when plugins were
        capped. Click takes the user to Plugin Manager → Order so the
        hidden ones can be promoted (or the cap raised). Without this
        the missing buttons are silent - users wonder why their enabled
        AI service doesn't show up and never connect that to a global
        cap they set in Settings."""
        tooltip = (
            f"{hidden} more action{'s' if hidden != 1 else ''} hidden by the "
            f"popup limit ({cap}). Click to reorder in Plugin Manager."
        )

        def _on_click(_btn):
            try:
                subprocess.run(
                    [
                        "notify-send",
                        "--hint=byte:transient:1",
                        "-t",
                        "4000",
                        "-i",
                        "dialog-information",
                        f"{hidden} action{'s' if hidden != 1 else ''} hidden",
                        "Open Plugin Manager → Order to reorder, or raise "
                        "the popup limit in Settings → Appearance.",
                    ],
                    check=False,
                )
            except OSError:
                pass
            self.hide()
            if self._on_open_plugin_order is not None:
                try:
                    self._on_open_plugin_order()
                except Exception as exc:  # noqa: BLE001
                    print(f"[popup] open plugin order failed: {exc}")

        btn = Gtk.Button(label=f"+{hidden}")
        btn.add_css_class("flat")
        btn.set_focus_on_click(False)
        btn.set_tooltip_text(tooltip)
        btn.add_css_class("vinopop-action")
        btn.add_css_class("vinopop-overflow")
        btn.connect("clicked", _on_click)
        target = row if row is not None else self._row1
        target.append(btn)
        if target is self._row2:
            self._row2.set_visible(True)

    # ---- overflow layout --------------------------------------------------
    def _overflow_mode(self) -> str:
        """How to handle more actions than fit on one line:
        'wrap'   -> spill onto a second row (default),
        'expand' -> one row + a chevron that reveals the rest on click,
        'cap'    -> one row only; the rest go behind a +N chip.
        """
        try:
            from settings import get_settings as _gs

            m = (_gs().get("popup_overflow_mode") or "wrap").strip().lower()
        except Exception:
            m = "wrap"
        return m if m in ("wrap", "expand", "cap") else "wrap"

    def _present_buttons(self, specs, hidden_count: int, max_btns: int) -> None:
        """Lay out (icon, tooltip, handler) specs into the bar per the user's
        overflow mode, adding the matching overflow affordance. `hidden_count`
        is how many were already dropped by the max_popup_buttons cap."""
        per_row = self._max_per_row()
        n = len(specs)
        mode = self._overflow_mode()
        if mode == "cap":
            shown = min(n, per_row)
            for icon, tip, h in specs[:shown]:
                self._add_button(icon, tip, h, row=self._row1)
            extra = (n - shown) + hidden_count
            if extra > 0:
                self._add_overflow_chip(extra, max_btns, row=self._row1)
        elif mode == "expand":
            first = min(n, per_row)
            for icon, tip, h in specs[:first]:
                self._add_button(icon, tip, h, row=self._row1)
            rest = specs[first:]
            if rest or hidden_count > 0:
                self._add_expand_chip(rest, hidden_count, max_btns)
        else:  # 'wrap' - two rows
            shown = min(n, 2 * per_row)
            for i, (icon, tip, h) in enumerate(specs[:shown]):
                self._add_button(icon, tip, h, row=self._row1 if i < per_row else self._row2)
            extra = (n - shown) + hidden_count
            if extra > 0:
                self._add_overflow_chip(
                    extra, max_btns, row=self._row2 if shown > per_row else self._row1
                )

    def _add_expand_chip(self, rest, hidden_count: int, max_btns: int) -> None:
        """A chevron at the end of row 1; clicking it reveals row 2 with the
        actions that didn't fit on the first line ('expand' overflow mode)."""
        count = len(rest) + hidden_count
        btn = Gtk.Button()
        btn.add_css_class("flat")
        btn.set_focus_on_click(False)
        btn.set_tooltip_text(f"Show {count} more action{'s' if count != 1 else ''}")
        btn.add_css_class("vinopop-action")
        btn.add_css_class("vinopop-overflow")
        img = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        img.set_pixel_size(max(12, int(_resolve_button_size() * 0.6)))
        btn.set_child(img)

        def _expand(_b):
            self._row1.remove(_b)
            per_row = self._max_per_row()
            shown2 = min(len(rest), per_row)
            for icon, tip, h in rest[:shown2]:
                self._add_button(icon, tip, h, row=self._row2)
            extra = (len(rest) - shown2) + hidden_count
            if extra > 0:
                self._add_overflow_chip(extra, max_btns, row=self._row2)
            self._row2.set_visible(True)
            try:
                self.win.queue_resize()
            except Exception:
                pass

        btn.connect("clicked", _expand)
        self._row1.append(btn)

    def show_actions(
        self,
        items: list[tuple[str, str, Callable[[], None]]],
        x: int,
        y: int,
    ) -> None:
        """Show the popup with a hard-coded list of (icon, tooltip, callback)
        actions instead of going through plugin_loader. Used for the
        no-selection popup that surfaces paste-oriented entry points
        when the hotkey fires without highlighted text.

        Each callback takes no arguments - it should encapsulate whatever
        the click needs to do (open a picker, paste, etc.). Callbacks run
        on a worker thread so a slow handler can't freeze the GTK loop.
        """
        self._current_text = ""
        self._anchor = (int(x), int(y))
        self._clear_buttons()
        if not items:
            return

        def make_handler(cb, name):
            def _on_click(_btn):
                def _worker():
                    try:
                        cb()
                    except Exception as exc:  # noqa: BLE001
                        print(f"[popup] action '{name}' failed: {exc}")

                threading.Thread(
                    target=_worker,
                    daemon=True,
                    name=f"action-{name}",
                ).start()
                self.hide()

            return _on_click

        specs = [
            (icon, tooltip, make_handler(callback, tooltip)) for (icon, tooltip, callback) in items
        ]
        self._present_buttons(specs, 0, 0)
        self._present_near(x, y)

    def _present_near(
        self,
        x: int,
        y: int,
        rect: tuple[int, int, int, int] | None = None,
    ) -> None:
        """Render the popup near physical screen coords (x, y) and arm
        the tracking/auto-hide machinery. Extracted from show_for so
        both show_for() and show_actions() share the same positioning
        + lifecycle code path.

        When `rect` (x, y, w, h in screen pixels - the selected-text
        bounding box from AT-SPI) is supplied, the popup centres
        horizontally on the selection and sits just above its top edge
        (dropping below the bottom edge when there's no room above),
        instead of being placed relative to the mouse pointer. Falls back
        to the (x, y) pointer behaviour when `rect` is None."""
        # Park the window off-screen and realize it so the backend has its
        # surface (X11: override-redirect set) and GTK can do a full layout
        # pass: get_preferred_size() then returns the real size. Nothing is
        # mapped until the real position is set below, so the compositor
        # never renders a frame at (-10000, -10000) - the first visible
        # frame is already at the correct position.
        get_backend().move_popup_window(self.win, -10000, -10000)
        self.win.realize()

        _, natural = self.win.get_preferred_size()
        w = max(natural.width, 1)
        h = max(natural.height, 1)

        # X11 root coords come in physical pixels; GTK move() takes logical.
        # Convert via the monitor's scale factor before doing layout math.
        # Wayland/KDE pointer + AT-SPI screen coords are already logical
        # (same space as the monitor geometry), so don't divide by scale -
        # doing so would place the popup at half the real position.
        is_logical = get_backend().pointer_is_logical
        display = self.win.get_display()

        # Anchor point: the selection centre when we have its rect,
        # otherwise the mouse pointer.
        if rect is not None:
            rx, ry, rw, rh = rect
            anchor_x, anchor_y = rx + rw / 2, ry + rh / 2
        else:
            anchor_x, anchor_y = x, y
        monitor = _monitor_for_point(display, anchor_x, anchor_y, is_logical)
        scale = monitor.get_scale_factor() if monitor else 1

        def _lg(v: float) -> float:
            return float(v) if is_logical else v / scale

        if rect is not None:
            # Anchor to the selection box: centre horizontally on it and
            # sit just above its top edge. We know the real edge here, so a
            # small gap is enough (no need for the pointer path's guesswork
            # lift). `below_y` drops past the bottom edge if there's no room.
            lcx = _lg(rx + rw / 2)
            ltop = _lg(ry)
            lbot = _lg(ry + rh)
            _GAP = 10
            target_x = int(lcx - w / 2)
            target_y = int(ltop - h - _GAP)
            below_y = int(lbot + _GAP)
            lx, ly = lcx, (ltop + lbot) / 2.0
        else:
            lx = _lg(x)
            ly = _lg(y)
            # Cursor Y is the mouse position, which sits in the middle of the
            # selected line. We need to clear the whole line (~24 px tall in
            # most fonts) plus a small visual gap, otherwise the popup lands
            # on top of the text it's supposed to act on.
            _LINE_CLEARANCE = 28
            _BELOW_GAP = 32  # used when there isn't room above
            if is_logical:
                # Wayland: we only have the pointer, which sits inside/under
                # the selection rather than above it. Lift the popup further
                # so it clears the copied text instead of covering it.
                _LINE_CLEARANCE = 64
                _BELOW_GAP = 56
            # Place the popup horizontally centered on (lx,ly), above ly
            target_x = int(lx - w / 2)
            target_y = int(ly - h - _LINE_CLEARANCE)
            below_y = int(ly + _BELOW_GAP)

        # Keep on-screen (geom is also in logical coords)
        if monitor is not None:
            geom = monitor.get_geometry()
            target_x = max(geom.x + 4, min(target_x, geom.x + geom.width - w - 4))
            if target_y < geom.y + 4:
                # Not enough room above: show below the selection/pointer
                # instead, with a gap so we don't overlap downward.
                target_y = below_y
            target_y = min(target_y, geom.y + geom.height - h - 4)

        # Move to the real position, then map. X11: XMoveWindow on the
        # still-unmapped override-redirect window; Wayland: layer-shell
        # margins, applied with the first commit - so the window appears
        # directly at the correct spot. set_visible() rather than present():
        # present() also asks for keyboard focus, which the popup must not
        # take. Already visible (re-shown for a new selection): the move
        # alone repositions it and GTK resizes it to the new buttons.
        get_backend().move_popup_window(self.win, target_x, target_y)
        self.win.set_visible(True)
        # Fresh show: the pointer is at the selection, not on the popup yet.
        # Reset so a stale True from a prior show can't suppress dismissal.
        self._pointer_in_popup = False

        # Record safe zones for the cursor-tracking loop
        self._origin_logical = (lx, ly)
        self._popup_rect = (target_x, target_y, w, h)
        self._scale = scale
        # Edge-detector seed: read the actual button/Esc state right now.
        # The old "always True" seed swallowed the very first click-outside
        # if no key was actually held, because the rising-edge detector
        # never saw a 0→1 transition (state started at 1). Reading the
        # real state means stuck-held inputs from a previous popup are
        # still suppressed, but a truly idle pointer is correctly tracked.
        if self._xdpy is not None:
            try:
                data = self._xdpy.screen().root.query_pointer()
                btn_mask = X.Button1Mask | X.Button2Mask | X.Button3Mask
                self._prev_button_pressed = bool(data.mask & btn_mask)
                if self._esc_keycode:
                    keymap = self._xdpy.query_keymap()
                    byte_idx = self._esc_keycode // 8
                    bit_idx = self._esc_keycode % 8
                    self._prev_esc_pressed = bool(keymap[byte_idx] & (1 << bit_idx))
                else:
                    self._prev_esc_pressed = False
            except Exception:
                # Fall back to the old conservative seed if Xlib query fails.
                self._prev_button_pressed = True
                self._prev_esc_pressed = True
        else:
            self._prev_button_pressed = True
            self._prev_esc_pressed = True
        self._start_tracking()
        # Arm the initial-grace timer: if the user never moves the
        # pointer into the popup, hide it after _initial_grace_ms
        # regardless of where the cursor is sitting. Cancelled by
        # _on_enter when the pointer actually reaches the popup.
        self._arm_initial_hide()

    def hide(self) -> None:
        self._cancel_hide_timeout()
        self._cancel_initial_hide()
        self._stop_tracking()
        self.win.set_visible(False)

    def _arm_initial_hide(self) -> None:
        self._cancel_initial_hide()
        if self._initial_grace_ms <= 0:
            return
        self._initial_hide_id = GLib.timeout_add(
            self._initial_grace_ms,
            self._on_initial_timeout,
        )

    def _cancel_initial_hide(self) -> None:
        if self._initial_hide_id is not None:
            GLib.source_remove(self._initial_hide_id)
            self._initial_hide_id = None

    def _on_initial_timeout(self) -> bool:
        self._initial_hide_id = None
        # Never give up while the pointer is actually on the popup - the
        # user is reading it / reaching for a button. _on_leave will arm
        # the leave-grace once they move off it.
        if self._pointer_in_popup:
            return False
        # Otherwise honour the contract: after N ms without pointer entry,
        # give up.
        self.hide()
        return False

    def _arm_hide_timeout(self, ms: int) -> None:
        self._cancel_hide_timeout()
        self._hide_timeout_id = GLib.timeout_add(ms, self._on_hide_timeout)

    def _cancel_hide_timeout(self) -> None:
        if self._hide_timeout_id is not None:
            GLib.source_remove(self._hide_timeout_id)
            self._hide_timeout_id = None

    def _on_hide_timeout(self) -> bool:
        self._hide_timeout_id = None
        # Hide via the full path so the tracker tick is cancelled too.
        # Calling self.win.hide() alone leaves _tracker_id pointing at a
        # GLib source that fires once after the next show_for, racing
        # the new tick's setup and reading stale _popup_rect.
        self.hide()
        return False

    def _start_tracking(self) -> None:
        self._stop_tracking()
        self._tracker_id = GLib.timeout_add(150, self._tick)
        # Wayland has no Xlib button poll, so watch the mouse via /dev/input to
        # dismiss the popup the moment the user clicks anywhere outside it.
        if self._xdpy is None:
            self._click_watcher = _ClickWatcher(self._on_global_click)
            self._click_watcher.start()

    def _stop_tracking(self) -> None:
        if self._tracker_id is not None:
            GLib.source_remove(self._tracker_id)
            self._tracker_id = None
        cw = getattr(self, "_click_watcher", None)
        if cw is not None:
            cw.stop()
            self._click_watcher = None

    def _on_global_click(self) -> bool:
        """A mouse button was pressed somewhere (Wayland evdev watcher).
        Dismiss the popup unless the click landed on it. Runs on the GLib
        main loop.

        We decide inside-vs-outside from the enter/leave flag, NOT from
        pointer.get_position(): on Wayland that returns surface-local
        coordinates that are stale/meaningless when the click lands on
        another window, so a click well outside used to read as 'inside'
        and fail to dismiss. The pointer must be over the popup to click
        anything on it, so _pointer_in_popup is the reliable signal."""
        if not self.win.get_visible():
            return False
        if not self._pointer_in_popup:
            self.hide()
        return False

    def _tick(self) -> bool:
        # Stop tracking once the window is hidden
        if not self.win.get_visible():
            self._tracker_id = None
            return False

        # GTK 4 can't query the global pointer; on X11 it comes from the
        # Xlib poll below (physical root coords -> logical via the scale).
        px = py = None

        # Poll physical button state + Esc via Xlib (works even when GTK seat-grab
        # is denied by ibus/cinnamon). Edge-detect 0→1 transitions.
        if self._xdpy is not None:
            try:
                data = self._xdpy.screen().root.query_pointer()
                button_mask = X.Button1Mask | X.Button2Mask | X.Button3Mask
                pressed = bool(data.mask & button_mask)
                phys_x, phys_y = data.root_x, data.root_y
                click_lx = phys_x / self._scale
                click_ly = phys_y / self._scale
                px, py = click_lx, click_ly
                if pressed and not self._prev_button_pressed:
                    if not self._point_in_popup(click_lx, click_ly):
                        self._prev_button_pressed = pressed
                        self.hide()
                        return False
                self._prev_button_pressed = pressed

                if self._esc_keycode:
                    keymap = self._xdpy.query_keymap()
                    byte_idx = self._esc_keycode // 8
                    bit_idx = self._esc_keycode % 8
                    esc_pressed = bool(keymap[byte_idx] & (1 << bit_idx))
                    if esc_pressed and not self._prev_esc_pressed:
                        self._prev_esc_pressed = esc_pressed
                        self.hide()
                        return False
                    self._prev_esc_pressed = esc_pressed
            except Exception as exc:  # noqa: BLE001
                print(f"[popup] xlib query failed: {exc}")

        # Wayland: pointer.get_position() is surface-local, so the screen-
        # space safe-zone math below is wrong (it would mis-fire and hide
        # the popup while the cursor is right on it). Persistence there is
        # driven entirely by enter/leave (_pointer_in_popup + _on_leave's
        # grace), so skip this block.
        if self._xdpy is None or px is None:
            return True
        in_safe = self._in_safe_zone(px, py)
        if in_safe:
            # Reset/cancel any countdown - user is still on text or popup
            self._cancel_hide_timeout()
        else:
            # Arm leave-grace if not already counting down
            if self._hide_timeout_id is None:
                self._arm_hide_timeout(self._leave_grace_ms)
        return True  # keep ticking

    def _point_in_popup(self, lx: float, ly: float) -> bool:
        x, y, w, h = self._popup_rect
        return x <= lx <= x + w and y <= ly <= y + h

    def _in_safe_zone(self, px: int, py: int) -> bool:
        # Three overlapping regions that all keep the popup alive:
        #   (a) the popup window itself (with a small fudge margin so a
        #       diagonal approach to a corner doesn't trip the leave-grace
        #       just because the pointer is 1-2 px outside the frame).
        #   (b) a generous circle around the original selection cursor.
        #   (c) the bounding rectangle spanning both - i.e. the corridor
        #       between text and popup. This is what was missing: moving
        #       the cursor from selection to popup along the natural
        #       diagonal path used to leave the radius briefly, arming
        #       the hide timer; users on faster mice saw the popup vanish
        #       mid-reach.
        x, y, w, h = self._popup_rect
        fudge = 12
        if (x - fudge) <= px <= (x + w + fudge) and (y - fudge) <= py <= (y + h + fudge):
            return True
        ox, oy = self._origin_logical
        dx = px - ox
        dy = py - oy
        if (dx * dx + dy * dy) <= (self._text_zone_radius * self._text_zone_radius):
            return True
        # Corridor: rect from selection cursor to popup centre, padded.
        cx = x + w / 2
        cy = y + h / 2
        pad = 24
        lo_x = min(ox, cx) - pad
        hi_x = max(ox, cx) + pad
        lo_y = min(oy, cy) - pad
        hi_y = max(oy, cy) + pad
        return lo_x <= px <= hi_x and lo_y <= py <= hi_y

    def _shift_held(self) -> bool:
        """True if Shift was held for the press that triggered the current
        'clicked'. GTK 4 has no Gtk.get_current_event_state(); the capture-
        phase gesture stores the press's modifier state, and on X11 the
        live pointer mask is checked as well (covers synthetic clicks)."""
        if self._last_press_state & Gdk.ModifierType.SHIFT_MASK:
            return True
        if self._xdpy is not None:
            try:
                return bool(self._xdpy.screen().root.query_pointer().mask & X.ShiftMask)
            except Exception:
                return False
        return False

    def _on_active_changed(self, win, _pspec) -> None:
        # Hide when the popup loses focus (it normally never has it).
        if not win.is_active() and getattr(self, "_was_active", False):
            self.hide()
        self._was_active = win.is_active()

    def _on_enter(self, _controller, _x, _y) -> None:
        # The motion controller only reports crossings of the window as a
        # whole (GTK 3 needed the NotifyType.INFERIOR filter for that).
        # User reached the popup - it must not auto-hide while they're on
        # it. Cancel BOTH the initial-grace and any leave-grace countdown.
        self._pointer_in_popup = True
        self._cancel_initial_hide()
        self._cancel_hide_timeout()

    def _on_motion(self, controller, x, y) -> None:
        if not self._pointer_in_popup:
            self._on_enter(controller, x, y)

    def _on_leave(self, _controller) -> None:
        self._pointer_in_popup = False
        # On X11 the tracker tick (screen-space safe-zone) governs hiding,
        # so leaving the popup alone isn't enough - the cursor may still be
        # over the selected text. On Wayland the tick can't do that math, so
        # arm the leave-grace here: the popup lingers briefly (covering a
        # cursor that's still very near) then hides.
        if self._xdpy is None and self.win.get_visible():
            self._arm_hide_timeout(self._leave_grace_ms)

    def _on_key_press(self, _controller, keyval, _keycode, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        return False

    def _on_button_press(self, gesture, _n_press, x, y) -> None:
        self._last_press_state = gesture.get_current_event_state()
        # Clicks on our own buttons go through normal child dispatch; a
        # press reported outside the window's bounds dismisses.
        win_w = self.win.get_width()
        win_h = self.win.get_height()
        # x/y are relative to the popup window's origin
        if x < 0 or y < 0 or x > win_w or y > win_h:
            self.hide()
