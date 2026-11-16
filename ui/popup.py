"""The popup: a small rounded bar of action buttons shown near the selection,
dismissed by a click outside it."""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import NamedTuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

try:
    from Xlib import XK, X  # noqa: E402
    from Xlib import display as xdisplay
    from Xlib import error as xerror
except Exception:  # python-xlib is absent on pure-Wayland (KDE) installs
    X = XK = xdisplay = xerror = None  # type: ignore

from core import actions
from core.classifier import ContentType
from platform_backend import get_backend  # noqa: E402
from platform_backend.monitors import clamp_to_area, monitor_at, usable_area  # noqa: E402
from plugin_system import plugin_loader
from ui import keyboard_nav

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

/* The button Enter/Space would run, when the hotkey opened the popup.
 * The popup never has keyboard focus (paste has to reach the app), so
 * this stands in for GTK's focus ring. */
button.vinopop-action.vinopop-selected {
    background-color: #1f7d42;
    color: #ffffff;
}

/* Text-label mode (popup_text_labels): the action's name instead of its
 * icon. */
button.vinopop-action.vinopop-text {
    padding-left: __PAD_H_TEXT__px;
    padding-right: __PAD_H_TEXT__px;
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
    """The Gdk.Monitor containing (x, y), or the first monitor (GTK 4 has no
    get_monitor_at_point()). Physical X11 coordinates are scaled by each
    monitor's own factor before the test."""
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


_BUTTON_SIZE_MIN = 16
_BUTTON_SIZE_MAX = 32
_BUTTON_SIZE_DEFAULT = 22

_popup_css_provider: Gtk.CssProvider | None = None


def _resolve_button_size() -> int:
    """The popup_button_size setting clamped to 16-32 px (symbolic icons go
    muddy below 16; above 32 is touch-target size), or the default when it's
    unset or not a number (pinned by
    `tests/test_popup_rules.py::test_a_button_size_of_sixteen_is_kept`,
    `tests/test_popup_rules.py::test_a_button_size_under_sixteen_is_raised_to_it`,
    `tests/test_popup_rules.py::test_a_button_size_of_thirty_two_is_kept`,
    `tests/test_popup_rules.py::test_a_button_size_over_thirty_two_is_lowered_to_it`,
    `tests/test_popup_rules.py::test_a_nonsense_button_size_is_the_default`)."""
    try:
        from core.settings import get_settings

        raw = get_settings().get("popup_button_size", _BUTTON_SIZE_DEFAULT)
        size = int(raw or _BUTTON_SIZE_DEFAULT)
    except Exception:
        size = _BUTTON_SIZE_DEFAULT
    return max(_BUTTON_SIZE_MIN, min(_BUTTON_SIZE_MAX, size))


def _text_pad_h(size: int) -> int:
    """Horizontal padding of a text-label button (wider than an icon's)."""
    return max(6, size // 3)


def _text_labels_on() -> bool:
    """The popup_text_labels setting: names instead of icons."""
    try:
        from core.settings import get_settings

        return bool(get_settings().get("popup_text_labels", False))
    except Exception:
        return False


def _label_text(tooltip: str) -> str:
    """Button text for text-label mode: the tooltip without a trailing
    parenthetical, so "Paste (replace selection with clipboard)" reads
    "Paste". The full tooltip still shows on hover."""
    text = (tooltip or "").strip()
    short = re.sub(r"\s*\([^()]*\)$", "", text).strip()
    return short or text


def _theme_is_light() -> bool:
    """Whether the popup follows the light theme (dark when unsure)."""
    try:
        from ui import theme as _theme

        return _theme._resolve_mode((get_settings_func() or {}).get("theme", "dark")) == "light"
    except Exception:
        return False


def _build_css(size: int) -> bytes:
    """The popup CSS for buttons `size` px square: padding and radii scale
    with it, and a light theme gets theme.py's light palette."""
    pad_h = max(3, size // 6)
    bar_gutter = max(2, size // 7)
    bar_corner_radius = max(6, size // 2 - 2)
    button_corner_radius = max(3, size // 4)
    css = (
        _CSS.replace(b"__BTN_SIZE__", str(size).encode())
        .replace(b"__PAD_V__", str(max(1, size // 12)).encode())
        .replace(b"__PAD_H__", str(pad_h).encode())
        .replace(b"__PAD_H_OVERFLOW__", str(max(pad_h, 8)).encode())
        .replace(b"__PAD_H_TEXT__", str(_text_pad_h(size)).encode())
        .replace(b"__WIN_PAD__", str(bar_gutter).encode())
        .replace(b"__WIN_RADIUS__", str(bar_corner_radius).encode())
        .replace(b"__BTN_RADIUS__", str(button_corner_radius).encode())
    )
    if _theme_is_light():
        with contextlib.suppress(Exception):
            from ui import theme as _theme

            css = _theme._apply_remap(css, _theme._LIGHT_REMAP)
    return css


def get_settings_func():
    """The live settings, or {} when they can't be loaded."""
    try:
        from core.settings import get_settings

        return get_settings()
    except Exception:
        return {}


# Above theme.py's APPLICATION-priority accents
_POPUP_CSS_PRIORITY = Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1


def _install_css() -> None:
    """Install the popup CSS for the current button size, replacing any
    earlier provider."""
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
    Gtk.StyleContext.add_provider_for_display(display, provider, _POPUP_CSS_PRIORITY)
    _popup_css_provider = provider


def reinstall_popup_css() -> None:
    """For the settings dialog: the next popup uses the new button size."""
    _install_css()


_BTN_LEFT, _BTN_RIGHT, _BTN_MIDDLE = 0x110, 0x111, 0x112
_BTN_MOUSE = {_BTN_LEFT, _BTN_RIGHT, _BTN_MIDDLE}
_BTN_TOOL_FINGER = 0x145
_BTN_TOUCH = 0x14A
_EV_KEY = 0x01
_INPUT_EVENT = "llHHi"
_TAP_MAX_S = 0.25


def _has_key_bit(bitmap: str, code: int) -> bool:
    """Is `code` set in a sysfs capabilities/key bitmap? The file is
    space-separated hex words, most significant first, each as wide as a
    C long (64 bits on every platform vinoPop runs on)."""
    words = bitmap.split()
    idx = code // 64
    if idx >= len(words):
        return False
    try:
        return bool(int(words[-1 - idx], 16) >> (code % 64) & 1)
    except ValueError:
        return False


def _touch_event_paths() -> set[str]:
    """/dev/input nodes of touchpads (and touchscreens): they report finger
    contact as BTN_TOUCH and may not show up as *-event-mouse."""
    import glob
    import os

    out = set()
    for sys_node in glob.glob("/sys/class/input/event*"):
        try:
            with open(os.path.join(sys_node, "device/capabilities/key")) as f:
                bitmap = f.read()
        except OSError:
            continue
        if _has_key_bit(bitmap, _BTN_TOUCH) and _has_key_bit(bitmap, _BTN_TOOL_FINGER):
            out.add(os.path.join("/dev/input", os.path.basename(sys_node)))
    return out


def _pointer_event_paths() -> set[str]:
    """The mice and touchpads to watch, or every event node when no mouse
    is listed by path."""
    import glob
    import os

    paths = set()
    for p in glob.glob("/dev/input/by-path/*-event-mouse"):
        try:
            paths.add(os.path.realpath(p))
        except OSError:
            pass
    if not paths:
        return set(glob.glob("/dev/input/event*"))
    return paths | _touch_event_paths()


class _ClickWatcher:
    """Wayland fallback for the X11 pointer-button poll. KWin doesn't deliver
    clicks that land outside a no-focus layer-shell surface, so we read the
    raw mouse devices via /dev/input while the popup is up and fire a callback
    on any button press. Needs the user in the 'input' group (same as the
    snippet trigger watcher); silently inert otherwise.

    Touchpad tap-to-click is done by libinput in userspace: the kernel only
    sends BTN_TOUCH down/up, never BTN_LEFT. A contact that lifts within
    _TAP_MAX_S (libinput's tap timeout is 180 ms) counts as a click; a longer
    one is the finger moving the pointer, which must not dismiss (pinned by
    `tests/test_popup_rules.py::test_a_touch_lifted_within_the_tap_time_is_a_click`,
    `tests/test_popup_rules.py::test_a_touch_held_past_the_tap_time_is_not`)."""

    def __init__(self, on_click) -> None:
        self._on_click = on_click
        self._stop = threading.Event()
        self._thread = None
        self._touch_down_at: float | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="popup-clickwatch")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _is_click(self, code: int, val: int, now: float) -> bool:
        """Feed one EV_KEY event (`now` is its timestamp in seconds); True
        when it completes a click or tap."""
        if code in _BTN_MOUSE:
            return val == 1
        if code == _BTN_TOUCH:
            if val == 1:
                self._touch_down_at = now
                return False
            down, self._touch_down_at = self._touch_down_at, None
            return val == 0 and down is not None and now - down <= _TAP_MAX_S
        return False

    def _run(self) -> None:
        import os
        import select as _sel
        import struct

        size = struct.calcsize(_INPUT_EVENT)
        fds = []
        for path in sorted(_pointer_event_paths()):
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
                        sec, usec, et, code, val = struct.unpack_from(_INPUT_EVENT, data, off)
                        if et == _EV_KEY and self._is_click(code, val, sec + usec / 1e6):
                            GLib.idle_add(self._on_click)
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass


_POLL_MS = 150


def _x11_input_state(xdpy, esc_keycode: int) -> tuple[bool, int, int, bool]:
    """(any of buttons 1-3 held, root x, root y, Esc held) via Xlib. Works
    without focus or a seat grab, which ibus/Cinnamon can refuse."""
    data = xdpy.screen().root.query_pointer()
    pressed = bool(data.mask & (X.Button1Mask | X.Button2Mask | X.Button3Mask))
    esc = False
    if esc_keycode:
        keymap = xdpy.query_keymap()
        esc = bool(keymap[esc_keycode // 8] & (1 << (esc_keycode % 8)))
    return pressed, data.root_x, data.root_y, esc


# Keys the popup takes while the hotkey's popup is up (on X11 they're grabbed
# on the root window for that long): keysym name -> also grab with Shift.
_NAV_KEYS = {
    "Left": False,
    "Right": False,
    "Up": False,
    "Down": False,
    "Tab": True,  # Shift+Tab
    "Home": False,
    "End": False,
    "Return": False,
    "KP_Enter": False,
    "space": False,
    "Escape": False,
}
_ACTIVATE_KEYS = ("Return", "KP_Enter", "space")


def _nav_target(rows: list[int], index: int, key: str, shift: bool = False) -> int:
    """Where `key` moves the selection from flat `index`, in a bar whose
    rows hold rows[i] buttons. Left/Right and Tab wrap across the whole
    bar; Up/Down switch rows keeping the column."""
    total = sum(rows)
    if total == 0:
        return 0
    index = max(0, min(index, total - 1))
    if key == "Tab":
        key = "Left" if shift else "Right"
    if key == "Left":
        return (index - 1) % total
    if key == "Right":
        return (index + 1) % total
    if key == "Home":
        return 0
    if key == "End":
        return total - 1
    if key in ("Up", "Down"):
        row, col = 0, index
        while col >= rows[row]:
            col -= rows[row]
            row += 1
        step = -1 if key == "Up" else 1
        new_row = row + step
        while 0 <= new_row < len(rows) and rows[new_row] == 0:
            new_row += step
        if not 0 <= new_row < len(rows):
            return index
        return sum(rows[:new_row]) + min(col, rows[new_row] - 1)
    return index


_kbd_activated_at = 0.0


def activated_by_keyboard(within_s: float = 2.0) -> bool:
    """True if a popup action was run from the keyboard in the last
    `within_s` seconds, so a window it opens (the AI bubble) can take the
    keys too. Any thread: actions run on a worker (pinned by
    `tests/test_popup_ui.py::test_enter_marks_the_run_as_from_the_keyboard`,
    `tests/test_popup_ui.py::test_only_keyboard_activation_counts`)."""
    return time.monotonic() - _kbd_activated_at < within_s


def _announce(widget: Gtk.Widget, message: str) -> None:
    """Have screen readers speak `message` (GTK 4.14+; a no-op before)."""
    if not message or not hasattr(widget, "announce"):
        return
    try:
        widget.announce(message, Gtk.AccessibleAnnouncementPriority.MEDIUM)
    except Exception:
        pass


class _KeyGrab:
    """Grab `keys` (keysym name -> also grab with Shift) on the X root
    window so a window that never takes focus can still be driven from the
    keyboard. on_key(name, shift) runs on the GTK main thread for each press.
    The popup and the AI bubble use it: both keep focus in the source app so
    a paste lands there."""

    def __init__(self, xdpy, keys: dict[str, bool], on_key: Callable[[str, bool], object]) -> None:
        self._xdpy = xdpy
        self._keys = keys
        self._on_key = on_key
        self._grabs: list[tuple[int, int]] = []
        self._names: dict[int, str] = {}
        self._watch_id: int | None = None
        self.active = False

    def start(self) -> None:
        root = self._xdpy.screen().root
        catchers = []
        for name, with_shift in self._keys.items():
            keycode = self._xdpy.keysym_to_keycode(XK.string_to_keysym(name))
            if not keycode:
                continue
            self._names[keycode] = name
            for mods in (0, X.ShiftMask) if with_shift else (0,):
                for lock in (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask):
                    # One catcher per request: CatchError holds a single error.
                    catcher = xerror.CatchError(xerror.BadAccess)
                    root.grab_key(
                        keycode,
                        mods | lock,
                        False,
                        X.GrabModeAsync,
                        X.GrabModeAsync,
                        onerror=catcher,
                    )
                    catchers.append(catcher)
                    self._grabs.append((keycode, mods | lock))
        try:
            self._xdpy.sync()
        except Exception as exc:  # noqa: BLE001
            print(f"[popup] grabbing the navigation keys failed: {exc}")
        if any(c.get_error() for c in catchers):
            print("[popup] some navigation keys are bound by another app")
        self.active = True
        self._watch_id = GLib.io_add_watch(
            self._xdpy.fileno(),
            GLib.PRIORITY_DEFAULT,
            GLib.IOCondition.IN,
            self._on_x_input,
        )

    def stop(self) -> None:
        if self._watch_id is not None:
            GLib.source_remove(self._watch_id)
            self._watch_id = None
        self.active = False
        if self._grabs:
            root = self._xdpy.screen().root
            for keycode, mask in self._grabs:
                try:
                    root.ungrab_key(keycode, mask)
                except Exception:
                    pass
            try:
                self._xdpy.sync()
                self.drain()  # drop presses that raced the ungrab
            except Exception:
                pass
        self._grabs.clear()
        self._names.clear()

    def _on_x_input(self, _fd, _cond) -> bool:
        try:
            self.drain()
        except Exception as exc:  # noqa: BLE001
            print(f"[popup] reading navigation keys failed: {exc}")
        return self._watch_id is not None

    def drain(self) -> None:
        """Handle the grabbed key presses queued on the Xlib connection.
        Whoever else queries that connection must call it too: replies can
        pull events off the socket before the fd watch sees them."""
        while self._xdpy.pending_events():
            event = self._xdpy.next_event()
            if event.type != X.KeyPress or not self.active:
                continue
            name = self._names.get(event.detail)
            if name is not None:
                self._on_key(name, bool(event.state & X.ShiftMask))


class _XInputPoll:
    """The popup's X11 button/Esc poll, for other never-focused windows (the
    AI bubble): fires on_click / on_escape on each new press. start()
    returns False when there's no X server to poll (Wayland, no Xlib)."""

    def __init__(self, on_click, on_escape) -> None:
        self._on_click = on_click
        self._on_escape = on_escape
        self._xdpy = None
        self._esc_keycode = 0
        # (button, esc) held at the last poll: a held input isn't a new press
        self._prev = (True, True)
        self._id: int | None = None

    def start(self) -> bool:
        if xdisplay is None or not get_backend().popup_uses_xlib:
            return False
        try:
            self._xdpy = xdisplay.Display()
            self._esc_keycode = self._xdpy.keysym_to_keycode(XK.string_to_keysym("Escape"))
            pressed, _x, _y, esc = _x11_input_state(self._xdpy, self._esc_keycode)
        except Exception as exc:  # noqa: BLE001
            print(f"[popup] could not open Xlib display: {exc}")
            self._xdpy = None
            return False
        self._prev = (pressed, esc)
        self._id = GLib.timeout_add(_POLL_MS, self._tick)
        return True

    def stop(self) -> None:
        if self._id is not None:
            GLib.source_remove(self._id)
            self._id = None
        if self._xdpy is not None:
            try:
                self._xdpy.close()
            except Exception:
                pass
            self._xdpy = None

    def _tick(self) -> bool:
        if self._xdpy is None:
            self._id = None
            return False
        try:
            pressed, _x, _y, esc = _x11_input_state(self._xdpy, self._esc_keycode)
        except Exception as exc:  # noqa: BLE001
            print(f"[popup] xlib query failed: {exc}")
            return True
        was_pressed, was_esc = self._prev
        self._prev = (pressed, esc)
        if esc and not was_esc:
            self._on_escape()
        elif pressed and not was_pressed:
            self._on_click()
        return self._id is not None


_EDGE_PAD = 4
_POPUP_MARGIN = 12
_CORRIDOR_PAD = 24
_TEXT_ZONE_RADIUS = 80
_SELECTION_GAP = 10
_POINTER_CLEARANCE_ABOVE_BELOW = (28, 32)
_WAYLAND_POINTER_CLEARANCE_ABOVE_BELOW = (64, 56)


_LogicalRect = tuple[int, int, int, int]
"""x, y, width, height in logical pixels."""


class _Spot(NamedTuple):
    """Where a popup goes, in logical pixels: its left edge, its top edge
    above the text and below it, and the point it is for."""

    x: int
    above_y: int
    below_y: int
    origin: tuple[float, float]


def _rect_centre(rect: tuple[int, int, int, int]) -> tuple[float, float]:
    x, y, width, height = rect
    return x + width / 2, y + height / 2


def _spot_over_selection(
    rect: tuple[int, int, int, int], width: int, height: int, pixels_per_unit: float
) -> _Spot:
    """Centred on the selection's box, just above its top edge or just
    below its bottom one (pinned by
    `tests/test_popup_show.py::test_with_a_selection_rect_the_popup_centres_just_above_it`,
    `tests/test_popup_show.py::test_a_selection_at_the_top_edge_gets_the_popup_below_it`)."""
    x, y, rect_width, rect_height = rect
    centre_x = (x + rect_width / 2) / pixels_per_unit
    top, bottom = y / pixels_per_unit, (y + rect_height) / pixels_per_unit
    return _Spot(
        x=int(centre_x - width / 2),
        above_y=int(top - height - _SELECTION_GAP),
        below_y=int(bottom + _SELECTION_GAP),
        origin=(centre_x, (top + bottom) / 2.0),
    )


def _spot_over_pointer(
    x: float, y: float, width: int, height: int, pixels_per_unit: float, is_wayland: bool
) -> _Spot:
    """Centred on the pointer, clear of the line of text it sits in the
    middle of. On Wayland the pointer is the only hint and sits inside or
    under the selection, so the popup is lifted further (pinned by
    `tests/test_popup_show.py::test_on_x11_the_popup_sits_a_line_above_the_pointer`,
    `tests/test_popup_show.py::test_on_wayland_the_popup_is_lifted_further_and_not_scaled`)."""
    above, below = (
        _WAYLAND_POINTER_CLEARANCE_ABOVE_BELOW if is_wayland else _POINTER_CLEARANCE_ABOVE_BELOW
    )
    pointer_x, pointer_y = x / pixels_per_unit, y / pixels_per_unit
    return _Spot(
        x=int(pointer_x - width / 2),
        above_y=int(pointer_y - height - above),
        below_y=int(pointer_y + below),
        origin=(pointer_x, pointer_y),
    )


_DEFAULT_MAX_POPUP_BUTTONS = 10


def _max_popup_buttons() -> int:
    """(pinned by `tests/test_popup_show.py::test_an_unset_cap_is_ten`)"""
    try:
        from core.settings import get_settings

        return get_settings().get_int("max_popup_buttons") or _DEFAULT_MAX_POPUP_BUTTONS
    except Exception:
        return _DEFAULT_MAX_POPUP_BUTTONS


def _new_action_button(tooltip: str) -> Gtk.Button:
    """A flat popup button that never takes focus from the source app."""
    btn = Gtk.Button()
    btn.add_css_class("flat")
    btn.set_focus_on_click(False)
    btn.set_tooltip_text(tooltip)
    btn.add_css_class("vinopop-action")
    return btn


# The popup_force_all_modifier names, as X11 modifier masks (Alt and Super
# as Mod1 and Mod4, the usual mapping).
_FORCE_ALL_MODIFIERS = {"shift": 1 << 0, "ctrl": 1 << 2, "alt": 1 << 3, "super": 1 << 6}

# An icon button's padding and border, slightly over so a row never ends a
# pixel short
_ICON_BUTTON_PADDING_PX = 14
_MIN_ICONS_PER_ROW = 4
_ROW_MAX_PX = 680
_ROW_MAX_MONITOR_FRACTION = 0.5
_FALLBACK_MONITOR_WIDTH = 1920
_ICON_FRACTION_OF_BUTTON = 0.72
_CHEVRON_FRACTION_OF_BUTTON = 0.6
_MIN_ICON_PX = 12

_OVERFLOW_MODES = ("wrap", "expand", "cap")
_DEFAULT_OVERFLOW_MODE = "wrap"


_DARK_GLYPH_COLOURS = ("#fafafa", "#0e0e10")
_LIGHT_GLYPH_COLOURS = ("#1c1d20", "#ffffff")
_GLYPH_BODY = b"#f0f0f0"
# Punch-out holes in older user glyphs (bundled ones use evenodd)
_GLYPH_HOLE = b"#2b2b2b"
_GLYPH_PIN_ACCENT = b"#1d7a3a"


def _recolour_glyph(svg: bytes, fg: str, bg: str) -> bytes:
    """A glyph's body and accent in `fg`, its holes in `bg` (pinned by
    `tests/test_popup_rules.py::test_a_glyph_body_and_accent_take_the_foreground`,
    `tests/test_popup_rules.py::test_a_glyph_hole_takes_the_background`)."""
    for colour, new in ((_GLYPH_BODY, fg), (_GLYPH_HOLE, bg), (_GLYPH_PIN_ACCENT, fg)):
        svg = svg.replace(colour, new.encode()).replace(colour.upper(), new.encode())
    return svg


# The brand tiles' corner radius, as a fraction of the tile
_TILE_CORNER = 0.24


def _stroke_tile_rim(cr, dev: int) -> None:
    """A thin dark rounded rim just inside a `dev` px square tile."""
    import math

    r = dev * _TILE_CORNER
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


class PopupWindow:
    """The popup. `initial_grace_ms` is how long it waits for the pointer to
    first reach it, `leave_grace_ms` how long once the pointer leaves it and
    the text; `on_open_plugin_order` runs from the overflow chip."""

    def __init__(
        self,
        initial_grace_ms: int = 4000,
        leave_grace_ms: int = 600,
        on_open_plugin_order: Callable[[], None] | None = None,
    ) -> None:
        self._on_open_plugin_order = on_open_plugin_order
        _install_css()
        self._text_labels = False
        self._build_window()
        self._build_bar()
        self._attach_controllers()

        self._current_text: str = ""
        # In the backend's coordinates: picks the monitor for row sizing and
        # icon scale before the window maps
        self._anchor: tuple[int, int] = (0, 0)
        self._hide_timeout_id: int | None = None
        self._tracker_id: int | None = None
        self._initial_hide_id: int | None = None
        self._initial_grace_ms = initial_grace_ms
        self._leave_grace_ms = leave_grace_ms
        self._text_zone_radius = _TEXT_ZONE_RADIUS
        self._origin_logical: tuple[float, float] = (0.0, 0.0)
        self._popup_rect: _LogicalRect = (0, 0, 0, 0)
        self._scale: int = 1
        self._open_xlib_display()
        self._prev_button_pressed = False
        self._prev_esc_pressed = False
        # On Wayland this enter/leave flag is all that keeps the popup up:
        # the pointer position there is surface-local
        self._pointer_in_popup = False
        # time.monotonic() of a hotkey show, None for a selection; main.py's
        # hotkey toggle reads it
        self.hotkey_shown_at: float | None = None
        self._kbd_nav = False
        self._kbd_index = 0
        self._kbd_grab: _KeyGrab | None = None

    def _build_window(self) -> None:
        """A plain undecorated window, handed to the platform backend for
        stacking, placement and focus: override-redirect on X11, a
        layer-shell overlay on Wayland. Neither is ever focused, so a paste
        still lands in the app the text came from (docs/how-it-works.md,
        "The popup window")."""
        self.win = Gtk.Window()
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.set_title("vinoPop")
        self.win.add_css_class("vinopop-popup")
        try:
            get_backend().prepare_popup_window(self.win)
        except Exception as exc:  # noqa: BLE001
            print(f"[popup] backend window init failed: {exc}")

    def _build_bar(self) -> None:
        """The rounded bar and its two rows of buttons, inside a 1 px margin.
        The bar is a toolbar to assistive tech (pinned by
        `tests/test_a11y.py::test_popup_bar_is_a_toolbar`)."""
        self._outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._outer.set_margin_top(1)
        self._outer.set_margin_bottom(1)
        self._outer.set_margin_start(1)
        self._outer.set_margin_end(1)
        self.win.set_child(self._outer)

        self._bar = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
            accessible_role=Gtk.AccessibleRole.TOOLBAR,
        )
        self._bar.update_property([Gtk.AccessibleProperty.LABEL], ["vinoPop actions"])
        self._bar.add_css_class("vinopop-bar")
        self._bar.set_hexpand(True)
        self._outer.append(self._bar)
        self._row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._bar.append(self._row1)
        self._bar.append(self._row2)
        # An empty visible row 2 leaves a gap under row 1
        self._row2.set_visible(False)

    def _attach_controllers(self) -> None:
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
        # Capture phase, never claimed: it sees every press's modifiers
        # (Shift+click copies) and presses outside the bar, and the buttons
        # still get their clicks
        click = Gtk.GestureClick()
        click.set_button(0)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._on_button_press)
        self.win.add_controller(click)
        self._last_press_state = Gdk.ModifierType(0)
        self.win.connect("notify::is-active", self._on_active_changed)

    def _open_xlib_display(self) -> None:
        """X11 only: _tick polls the pointer buttons and Esc over it. The
        Wayland backend dismisses through GTK events instead."""
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

    def _clear_buttons(self) -> None:
        for row in (self._row1, self._row2):
            while (button := row.get_first_child()) is not None:
                row.remove(button)
        self._row2.set_visible(False)
        self._bar.set_size_request(-1, -1)  # drop _expand's width hold

    def _add_button(
        self,
        icon_name: str,
        tooltip: str,
        on_click,
        row: Gtk.Box | None = None,
    ) -> None:
        btn = _new_action_button(tooltip)
        if self._text_labels:
            btn.add_css_class("vinopop-text")
            btn.set_child(Gtk.Label(label=_label_text(tooltip)))
        else:
            btn.set_child(self._make_icon_image(icon_name))
        btn.connect("clicked", on_click)
        self._append_to_row(btn, row)

    def _append_to_row(self, btn: Gtk.Button, row: Gtk.Box | None) -> None:
        target = row if row is not None else self._row1
        target.append(btn)
        if target is self._row2:
            self._row2.set_visible(True)

    def _force_all_held(self) -> bool:
        """Whether the popup_force_all_modifier key is held as show_for runs,
        so it has to be held from before the selection ends (pinned by
        `tests/test_popup_rules.py::test_holding_the_force_all_modifier_counts`,
        `tests/test_popup_rules.py::test_holding_another_modifier_does_not`)."""
        try:
            from core.settings import get_settings as _gs

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
        bit = _FORCE_ALL_MODIFIERS.get(mod_name)
        if bit is None:
            return False
        return bool(mask & bit)

    def _max_per_row(self, anchor: tuple[int, int] | None = None) -> int:
        """How many icon buttons fit on one row."""
        icon_button_px = _resolve_button_size() + _ICON_BUTTON_PADDING_PX
        return max(_MIN_ICONS_PER_ROW, self._row_target_px(anchor) // icon_button_px)

    def _row_target_px(self, anchor: tuple[int, int] | None = None) -> int:
        """Widest a row may grow, in logical pixels: half the monitor under
        the anchor (GTK 4 can't query the global pointer), at most
        _ROW_MAX_PX even on a 5K screen."""
        if anchor is None:
            anchor = self._anchor
        try:
            display = self.win.get_display()
            monitor = _monitor_for_point(
                display, anchor[0], anchor[1], get_backend().pointer_is_logical
            )
            monitor_w = monitor.get_geometry().width if monitor else _FALLBACK_MONITOR_WIDTH
        except Exception:
            monitor_w = _FALLBACK_MONITOR_WIDTH
        return min(_ROW_MAX_PX, int(monitor_w * _ROW_MAX_MONITOR_FRACTION))

    def _text_button_px(self, tooltip: str) -> int:
        """Outer width of a text-label button, slightly overestimated."""
        size = _resolve_button_size()
        try:
            text_w, _h = self.win.create_pango_layout(_label_text(tooltip)).get_pixel_size()
        except Exception:
            text_w = 8 * len(_label_text(tooltip))
        return max(size, text_w) + 2 * _text_pad_h(size) + 4

    def _fit_row(self, specs) -> int:
        """How many of `specs` (from the front) fit on one row. Icon
        buttons are all one width; text labels are measured, at least one
        per row even if it's wider than the budget."""
        if not self._text_labels:
            return min(len(specs), self._max_per_row())
        budget = self._row_target_px()
        used = 0
        n = 0
        for _icon, tip, _h in specs:
            w = self._text_button_px(tip)
            if n and used + w > budget:
                break
            used += w
            n += 1
        return n

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
        """A colour logo at device resolution with a thin dark rim hugging the
        tile edge, so it separates from the bar; None on any failure."""
        try:
            import cairo
            from gi.repository import GdkPixbuf

            scale = self._device_scale()
            dev = max(_MIN_ICON_PX, icon_px * scale)
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
            _stroke_tile_rim(cr, dev)
            # Device pixels drawn at a logical pixel_size: 1:1 on HiDPI
            image = Gtk.Image.new_from_paintable(_texture_from_cairo(surf))
            image.set_pixel_size(icon_px)
            return image
        except Exception:
            return None

    def _glyph_colors(self) -> tuple[str, str]:
        """Foreground and background for the mono glyphs in the current theme."""
        return _LIGHT_GLYPH_COLOURS if _theme_is_light() else _DARK_GLYPH_COLOURS

    def _glyph_image(self, icon_name: str, icon_px: int):
        """One of our vinopop-*-symbolic glyphs, recoloured for the theme;
        None on any failure. They hardcode #f0f0f0 rather than use GTK's
        symbolic format, so the popup CSS `color` can't recolour them."""
        try:
            from gi.repository import GdkPixbuf, Gio

            path = _icon_file(icon_name, icon_px, 1)
            if not path or not path.endswith(".svg"):
                return None
            with open(path, "rb") as fh:
                svg = fh.read()
            if _GLYPH_BODY not in svg.lower():
                return None
            svg = _recolour_glyph(svg, *self._glyph_colors())
            scale = self._device_scale()
            dev = max(_MIN_ICON_PX, icon_px * scale)
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
        """The button's icon, in the user's icon_style: a colour logo with a
        rim, one of our glyphs recoloured, or a system symbolic icon left to
        the popup CSS to recolour."""
        icon_px = max(_MIN_ICON_PX, int(_resolve_button_size() * _ICON_FRACTION_OF_BUTTON))
        try:
            from ui import icon_style

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
        via_hotkey: bool = False,
    ) -> None:
        """Offer the plugins for `text` near (x, y) or over `rect`. A
        selection change while the pointer is on the popup is ignored: on
        Wayland it is usually an app re-claiming PRIMARY on focus (pinned by
        `tests/test_popup_show.py::test_a_selection_change_under_the_pointer_keeps_the_buttons`).
        A plugin without a tooltip is named after itself (pinned by
        `tests/test_popup_show.py::test_a_plugin_without_a_tooltip_is_named_after_itself`)."""
        if self.win.get_visible() and self._pointer_in_popup:
            log.info("[popup] ignored a selection change while the pointer is on the popup")
            return
        self._current_text = text
        self._anchor = (int(x), int(y))
        self._clear_buttons()

        plugins = self._plugins_for(text, content_type, editable)
        max_buttons = _max_popup_buttons()
        hidden_count = 0
        if max_buttons > 0 and len(plugins) > max_buttons:
            hidden_count = len(plugins) - max_buttons
            print(f"[popup] capping {len(plugins)} plugins to {max_buttons} (max_popup_buttons)")
            plugins = plugins[:max_buttons]
        if not plugins:
            print(f"[popup] no plugins for {content_type.value} (editable={editable})")
            return

        specs = [(p.icon, p.tooltip or p.name, self._runs_plugin(p)) for p in plugins]
        self._present_buttons(specs, hidden_count, max_buttons)
        self._present_near(x, y, rect=rect, via_hotkey=via_hotkey)

    def _plugins_for(self, text: str, content_type: ContentType, editable: bool) -> list:
        """The plugins to offer, in the user's order. Holding the force-all
        modifier skips the classifier and offers every plugin whose own
        matches() accepts the text, for a selection it got wrong."""
        if self._force_all_held():
            plugins = [p for p in plugin_loader.all_plugins() if p.matches(text)]
            plugins = plugin_loader.in_user_order(plugins)
        else:
            plugins = plugin_loader.for_content_type(content_type, text)
        if not editable:
            plugins = [p for p in plugins if not p.requires_editable]
        return plugins

    def _runs_plugin(self, plugin):
        """A click handler that runs `plugin` on the text the popup is for.
        Off the main thread: a handler may block on the network, a
        subprocess or key injection."""

        def on_click(_btn):
            text = self._current_text
            copy_instead = self._shift_held()

            def run():
                try:
                    if copy_instead:
                        with actions.force_copy_mode():
                            plugin.execute(text)
                    else:
                        plugin.execute(text)
                except Exception:  # noqa: BLE001
                    log.exception("[popup] plugin '%s' failed", plugin.name)

            threading.Thread(target=run, daemon=True, name=f"plugin-{plugin.name}").start()
            self.hide()

        return on_click

    def _add_overflow_chip(
        self,
        hidden: int,
        cap: int,
        row: Gtk.Box | None = None,
    ) -> None:
        """A +N chip for the actions the cap hid; it opens Plugin Manager →
        Order, where they can be moved up."""
        tooltip = (
            f"{hidden} more action{'s' if hidden != 1 else ''} hidden by the "
            f"popup limit ({cap}). Click to reorder in Plugin Manager."
        )

        def _on_click(_btn):
            actions.notify(
                f"{hidden} action{'s' if hidden != 1 else ''} hidden",
                "Open Plugin Manager → Order to reorder, or raise "
                "the popup limit in Settings → Appearance.",
                "dialog-information",
                4000,
            )
            self.hide()
            if self._on_open_plugin_order is not None:
                try:
                    self._on_open_plugin_order()
                except Exception as exc:  # noqa: BLE001
                    print(f"[popup] open plugin order failed: {exc}")

        btn = _new_action_button(tooltip)
        btn.set_label(f"+{hidden}")
        btn.add_css_class("vinopop-overflow")
        btn.connect("clicked", _on_click)
        self._append_to_row(btn, row)

    def _overflow_mode(self) -> str:
        """The popup_overflow_mode setting, "wrap" when unknown (pinned by
        `tests/test_popup_rules.py::test_a_known_overflow_mode_is_used`,
        `tests/test_popup_rules.py::test_an_unknown_overflow_mode_wraps`)."""
        try:
            from core.settings import get_settings as _gs

            m = (_gs().get("popup_overflow_mode") or _DEFAULT_OVERFLOW_MODE).strip().lower()
        except Exception:
            m = _DEFAULT_OVERFLOW_MODE
        return m if m in _OVERFLOW_MODES else _DEFAULT_OVERFLOW_MODE

    def _present_buttons(self, specs, hidden_count: int, max_btns: int) -> None:
        """Lay out (icon, tooltip, handler) specs per the overflow mode.
        `hidden_count` is how many the max_popup_buttons cap already dropped."""
        self._text_labels = _text_labels_on()
        mode = self._overflow_mode()
        if mode == "cap":
            self._lay_out_one_row_and_chip(specs, hidden_count, max_btns)
        elif mode == "expand":
            self._lay_out_one_row_and_chevron(specs, hidden_count, max_btns)
        else:
            self._lay_out_two_rows(specs, hidden_count, max_btns)

    def _lay_out_one_row_and_chip(self, specs, hidden_count: int, max_btns: int) -> None:
        shown = self._fit_row(specs)
        for icon, tip, h in specs[:shown]:
            self._add_button(icon, tip, h, row=self._row1)
        extra = (len(specs) - shown) + hidden_count
        if extra > 0:
            self._add_overflow_chip(extra, max_btns, row=self._row1)

    def _lay_out_one_row_and_chevron(self, specs, hidden_count: int, max_btns: int) -> None:
        first = self._fit_row(specs)
        for icon, tip, h in specs[:first]:
            self._add_button(icon, tip, h, row=self._row1)
        rest = specs[first:]
        if rest or hidden_count > 0:
            self._add_expand_chip(rest, hidden_count, max_btns)

    def _lay_out_two_rows(self, specs, hidden_count: int, max_btns: int) -> None:
        first = self._fit_row(specs)
        shown = first + self._fit_row(specs[first:])
        for i, (icon, tip, h) in enumerate(specs[:shown]):
            self._add_button(icon, tip, h, row=self._row1 if i < first else self._row2)
        extra = (len(specs) - shown) + hidden_count
        if extra > 0:
            self._add_overflow_chip(
                extra, max_btns, row=self._row2 if shown > first else self._row1
            )

    def _add_expand_chip(self, rest, hidden_count: int, max_btns: int) -> None:
        """A chevron at the end of row 1 that reveals row 2 with the rest."""
        count = len(rest) + hidden_count
        btn = _new_action_button(f"Show {count} more action{'s' if count != 1 else ''}")
        btn.add_css_class("vinopop-overflow")
        img = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        img.set_pixel_size(
            max(_MIN_ICON_PX, int(_resolve_button_size() * _CHEVRON_FRACTION_OF_BUTTON))
        )
        btn.set_child(img)

        def _expand(_b):
            # Without the chevron row 1 is a button narrower, which would
            # leave the pointer outside the popup and start the leave hide
            self._bar.set_size_request(self._bar.get_width(), -1)
            self._row1.remove(_b)
            shown2 = self._fit_row(rest)
            for icon, tip, h in rest[:shown2]:
                self._add_button(icon, tip, h, row=self._row2)
            extra = (len(rest) - shown2) + hidden_count
            if extra > 0:
                self._add_overflow_chip(extra, max_btns, row=self._row2)
            self._row2.set_visible(True)
            self._refit()

        btn.connect("clicked", _expand)
        self._row1.append(btn)

    def _refit(self) -> None:
        """Re-measure after row 2 is revealed and keep the popup on its
        monitor. _popup_rect must grow too, or a click on row 2 reads as
        outside (pinned by
        `tests/test_popup_ui.py::test_refit_after_expand_covers_row_two`,
        `tests/test_popup_ui.py::test_refit_pulls_a_grown_bar_back_on_screen`)."""
        _, natural = self.win.get_preferred_size()
        w = max(natural.width, 1)
        h = max(natural.height, 1)
        x, y, _, _ = self._popup_rect
        monitor = _monitor_for_point(self.win.get_display(), x + w / 2, y, True)
        if monitor is not None:
            x, y = clamp_to_area(x, y, w, h, usable_area(monitor))
        if (x, y) != self._popup_rect[:2]:
            get_backend().move_popup_window(self.win, x, y)
        self._popup_rect = (x, y, w, h)
        try:
            self.win.queue_resize()
        except Exception:
            pass

    def show_actions(
        self,
        items: list[tuple[str, str, Callable[[], None]]],
        x: int,
        y: int,
        via_hotkey: bool = False,
    ) -> None:
        """Show (icon, tooltip, callback) actions instead of plugins: the
        popup the hotkey opens with nothing selected. Callbacks take no
        arguments and run on a worker thread."""
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
        self._present_near(x, y, via_hotkey=via_hotkey)

    def _present_near(
        self,
        x: int,
        y: int,
        rect: tuple[int, int, int, int] | None = None,
        via_hotkey: bool = False,
    ) -> None:
        """Show the popup near screen point (x, y), or over `rect` (the
        selection's box from AT-SPI) when there is one, and arm the
        tracking and auto-hide. It is laid out off-screen first (pinned by
        `tests/test_popup_show.py::test_the_window_is_laid_out_off_screen_before_it_is_placed`);
        X11's physical coordinates are scaled to logical, Wayland's are
        logical already (pinned by
        `tests/test_popup_show.py::test_physical_pointer_coordinates_are_scaled_down`,
        `tests/test_popup_show.py::test_on_wayland_the_popup_is_lifted_further_and_not_scaled`);
        it drops below when there's no room above (pinned by
        `tests/test_popup_show.py::test_without_room_above_the_popup_drops_below_the_pointer`)
        and stays on the monitor (pinned by
        `tests/test_popup_show.py::test_the_popup_is_kept_on_the_monitor`)."""
        width, height = self._measure_off_screen()
        is_logical = get_backend().pointer_is_logical
        anchor_x, anchor_y = _rect_centre(rect) if rect is not None else (x, y)
        monitor = _monitor_for_point(self.win.get_display(), anchor_x, anchor_y, is_logical)
        scale = monitor.get_scale_factor() if monitor else 1
        pixels_per_unit = 1 if is_logical else scale

        if rect is not None:
            spot = _spot_over_selection(rect, width, height, pixels_per_unit)
        else:
            spot = _spot_over_pointer(x, y, width, height, pixels_per_unit, is_logical)
        target_x, target_y = spot.x, spot.above_y
        if monitor is not None:
            area = usable_area(monitor)
            if target_y < area.y + _EDGE_PAD:
                target_y = spot.below_y
            target_x, target_y = clamp_to_area(target_x, target_y, width, height, area)

        # Moved while unmapped, so the first frame is already in place.
        # set_visible(), not present(): present() also asks for focus.
        get_backend().move_popup_window(self.win, target_x, target_y)
        self.win.set_visible(True)
        self._reset_hide_zones(spot.origin, (target_x, target_y, width, height))
        self._scale = scale
        self._seed_press_detectors()
        self._start_tracking()
        self._arm_initial_hide()
        self.hotkey_shown_at = time.monotonic() if via_hotkey else None
        self._kbd_stop()
        if via_hotkey:
            self._kbd_start()

    def _measure_off_screen(self) -> tuple[int, int]:
        """The popup's size, laid out while parked off-screen and unmapped,
        so no frame is drawn there."""
        get_backend().move_popup_window(self.win, -10000, -10000)
        self.win.realize()
        _, natural = self.win.get_preferred_size()
        return max(natural.width, 1), max(natural.height, 1)

    def _reset_hide_zones(self, origin: tuple[float, float], popup_rect: _LogicalRect) -> None:
        """The pointer is at the selection, not on the popup, after a show
        (pinned by `tests/test_popup_show.py::test_the_hide_zones_follow_the_placement`)."""
        self._pointer_in_popup = False
        self._origin_logical = origin
        self._popup_rect = popup_rect

    def _seed_press_detectors(self) -> None:
        """_tick acts on a button or Esc going down. Seeded with what is
        held right now: a fixed True swallowed the first click outside, and
        a fixed False would act on a key still held from the last popup."""
        self._prev_button_pressed = True
        self._prev_esc_pressed = True
        if self._xdpy is None:
            return
        try:
            pressed, _rx, _ry, esc = _x11_input_state(self._xdpy, self._esc_keycode)
            self._prev_button_pressed = pressed
            self._prev_esc_pressed = esc
        except Exception:
            pass

    def hide(self) -> None:
        self._cancel_hide_timeout()
        self._cancel_initial_hide()
        self._stop_tracking()
        self._kbd_stop()
        self.hotkey_shown_at = None
        self.win.set_visible(False)

    def _rows(self) -> list[list[Gtk.Widget]]:
        rows = []
        for row in (self._row1, self._row2):
            btns = []
            if row.get_visible():
                child = row.get_first_child()
                while child is not None:
                    if child.get_visible():
                        btns.append(child)
                    child = child.get_next_sibling()
            rows.append(btns)
        return rows

    def _kbd_start(self) -> None:
        if self._xdpy is None or X is None:
            return
        self._kbd_grab = _KeyGrab(self._xdpy, _NAV_KEYS, self._kbd_key)
        self._kbd_grab.start()
        self._kbd_nav = True
        self._kbd_select(0)

    def _kbd_stop(self) -> None:
        was_on, self._kbd_nav = self._kbd_nav, False
        grab, self._kbd_grab = self._kbd_grab, None
        if grab is not None:
            grab.stop()
        if was_on:
            keyboard_nav.clear_highlight(self._buttons())

    def _kbd_drain(self) -> None:
        """_tick calls this: its queries share the grab's Xlib connection."""
        if self._kbd_grab is not None:
            self._kbd_grab.drain()

    def _buttons(self) -> list[Gtk.Widget]:
        return [button for row in self._rows() for button in row]

    def _kbd_select(self, index: int) -> None:
        """Highlight button `index` and announce it: without focus a screen
        reader can't follow the highlight (pinned by
        `tests/test_popup_ui.py::test_the_highlight_is_announced`)."""
        buttons = self._buttons()
        if not buttons:
            return
        self._kbd_index = max(0, min(index, len(buttons) - 1))
        keyboard_nav.highlight_only(buttons, self._kbd_index)
        selected = buttons[self._kbd_index]
        _announce(selected, selected.get_tooltip_text() or "")

    def _kbd_key(self, name: str, shift: bool = False) -> bool:
        """Handle one navigation key; returns whether it was one. A key
        restarts the auto-hide countdown, and activating ignores a Shift
        left from an earlier mouse press (pinned by
        `tests/test_popup_ui.py::test_keyboard_moves_the_highlight_and_enter_runs_it`)."""
        if name == "Escape":
            self.hide()
            return True
        rows = self._rows()
        flat = [b for btns in rows for b in btns]
        if not flat or name not in _NAV_KEYS:
            return False
        self._arm_initial_hide()
        if name in _ACTIVATE_KEYS:
            global _kbd_activated_at
            _kbd_activated_at = time.monotonic()
            index = min(self._kbd_index, len(flat) - 1)
            self._last_press_state = Gdk.ModifierType(0)
            flat[index].emit("clicked")
            if self._kbd_nav:
                # Still up: it was the expand chevron, so highlight the
                # first button it revealed
                self._kbd_select(index)
            return True
        self._kbd_select(_nav_target([len(b) for b in rows], self._kbd_index, name, shift))
        return True

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
        if not self._pointer_in_popup:
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
        # self.hide(), not win.hide(): the tracker tick has to be cancelled
        # too, or it fires once into the next show with a stale _popup_rect
        self.hide()
        return False

    def _start_tracking(self) -> None:
        self._stop_tracking()
        self._tracker_id = GLib.timeout_add(_POLL_MS, self._tick)
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
        """A mouse button was pressed somewhere (Wayland evdev watcher):
        dismiss unless the pointer is on the popup. Decided by the
        enter/leave flag: Wayland's pointer position is surface-local and
        meaningless for a click on another window."""
        if not self.win.get_visible():
            return False
        if not self._pointer_in_popup:
            self.hide()
        return False

    def _tick(self) -> bool:
        """The tracking poll. Only X11 has a screen-space pointer for the
        safe zone; on Wayland enter and leave keep the popup up."""
        if not self.win.get_visible():
            self._tracker_id = None
            return False
        if self._xdpy is None:
            return True
        pointer = None
        try:
            pointer = self._poll_x11_input()
        except Exception as exc:  # noqa: BLE001
            print(f"[popup] xlib query failed: {exc}")
        if not self.win.get_visible():
            return False
        if pointer is None:
            return True
        if self._in_safe_zone(*pointer):
            self._cancel_hide_timeout()
        elif self._hide_timeout_id is None:
            self._arm_hide_timeout(self._leave_grace_ms)
        return True

    def _poll_x11_input(self) -> tuple[float, float]:
        """Hide on a new click outside the popup or a new Esc, and run the
        grabbed navigation keys. Returns the pointer in logical pixels.
        Xlib sees the buttons even when ibus or Cinnamon refuse GTK a seat
        grab."""
        pressed, phys_x, phys_y, esc_pressed = _x11_input_state(self._xdpy, self._esc_keycode)
        pointer = (phys_x / self._scale, phys_y / self._scale)
        was_pressed, self._prev_button_pressed = self._prev_button_pressed, pressed
        if pressed and not was_pressed and not self._point_in_popup(*pointer):
            self.hide()
            return pointer
        if self._kbd_nav:
            self._kbd_drain()
            if not self.win.get_visible():
                return pointer
        was_esc, self._prev_esc_pressed = self._prev_esc_pressed, esc_pressed
        if esc_pressed and not was_esc:
            self.hide()
        return pointer

    def _point_in_popup(self, lx: float, ly: float) -> bool:
        x, y, w, h = self._popup_rect
        return x <= lx <= x + w and y <= ly <= y + h

    def _in_safe_zone(self, px: int, py: int) -> bool:
        """Whether a pointer at (px, py) keeps the popup up: on or just
        around the popup, near the selection, or in the corridor between
        the two (a fast diagonal reach leaves the circle before it arrives)
        (pinned by
        `tests/test_popup_show.py::test_the_pointer_may_be_twelve_pixels_outside_the_popup`,
        `tests/test_popup_show.py::test_the_pointer_may_be_eighty_pixels_from_the_selection`,
        `tests/test_popup_show.py::test_the_path_from_the_selection_to_the_popup_is_safe`)."""
        x, y, w, h = self._popup_rect
        near_popup = (
            x - _POPUP_MARGIN <= px <= x + w + _POPUP_MARGIN
            and y - _POPUP_MARGIN <= py <= y + h + _POPUP_MARGIN
        )
        if near_popup:
            return True
        origin_x, origin_y = self._origin_logical
        dx, dy = px - origin_x, py - origin_y
        if dx * dx + dy * dy <= self._text_zone_radius * self._text_zone_radius:
            return True
        centre_x, centre_y = x + w / 2, y + h / 2
        in_corridor_x = (
            min(origin_x, centre_x) - _CORRIDOR_PAD <= px <= max(origin_x, centre_x) + _CORRIDOR_PAD
        )
        in_corridor_y = (
            min(origin_y, centre_y) - _CORRIDOR_PAD <= py <= max(origin_y, centre_y) + _CORRIDOR_PAD
        )
        return in_corridor_x and in_corridor_y

    def _shift_held(self) -> bool:
        """Whether Shift was held for the press behind the current
        "clicked": the capture-phase gesture's record (GTK 4 has no
        get_current_event_state()), or on X11 the live pointer mask, which
        covers synthetic clicks."""
        if self._last_press_state & Gdk.ModifierType.SHIFT_MASK:
            return True
        if self._xdpy is not None:
            try:
                return bool(self._xdpy.screen().root.query_pointer().mask & X.ShiftMask)
            except Exception:
                return False
        return False

    def _on_active_changed(self, win, _pspec) -> None:
        """Hide when the popup loses focus a window manager gave it anyway."""
        if not win.is_active() and getattr(self, "_was_active", False):
            self.hide()
        self._was_active = win.is_active()

    def _on_enter(self, _controller, _x, _y) -> None:
        self._pointer_in_popup = True
        self._cancel_initial_hide()
        self._cancel_hide_timeout()

    def _on_motion(self, controller, x, y) -> None:
        if not self._pointer_in_popup:
            self._on_enter(controller, x, y)

    def _on_leave(self, _controller) -> None:
        self._pointer_in_popup = False
        # On X11 _tick's safe zone decides (the pointer may be on the text)
        if self._xdpy is None and self.win.get_visible():
            self._arm_hide_timeout(self._leave_grace_ms)

    def _on_key_press(self, _controller, keyval, _keycode, state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        if self._kbd_nav:
            return self._kbd_key(*keyboard_nav.key_from_event(keyval, state))
        return False

    def _on_button_press(self, gesture, _n_press, x, y) -> None:
        self._last_press_state = gesture.get_current_event_state()
        if not (0 <= x <= self.win.get_width() and 0 <= y <= self.win.get_height()):
            self.hide()
