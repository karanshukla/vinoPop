"""The XWayland backend, for every Wayland desktop except KDE Plasma (which
has wayland_kde.py) and, best-effort, plain Xorg. The whole process, GTK
included, runs on XWayland. The name stays "x11" for VINOPOP_BACKEND.

Wraps xclip (selection/clipboard), Xlib (pointer, popup placement, active
window, keystrokes and typing over XTEST) and the X11 trigger modules.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

from .base import PlatformBackend

# One Xlib connection for the process, reopened after a failure: opening one
# costs 5-15 ms (measured 2026-06), paid on every hotkey press otherwise.
_pointer_dpy = None
# python-xlib connections aren't thread-safe; the hotkey worker thread and
# the GTK thread both use _pointer_dpy.
_pointer_lock = threading.Lock()

# Large selections (LibreOffice) read as empty with 0.5 s. Callers wait off
# the GTK thread.
XCLIP_TIMEOUT_S = 2.0
XWAYLAND_WARNING_MS = 6000


class X11Backend(PlatformBackend):
    name = "x11"
    popup_uses_xlib = True

    def check_session(self) -> None:
        """Exit without a DISPLAY; warn about XWayland's limits under Wayland."""
        has_x11 = bool(os.environ.get("DISPLAY"))
        has_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        if not has_x11:
            print("DISPLAY is not set - the X11 backend needs an X11 display.", file=sys.stderr)
            sys.exit(2)
        if has_wayland and has_x11:
            warning = (
                "Running under XWayland: depending on the compositor, the "
                "auto-popup may only fire for selections in X11 apps. The "
                "global hotkey works everywhere the X11 grab is honoured. "
                "KDE Plasma Wayland has a native backend."
            )
            print(f"[vinopop] WARNING: {warning}", file=sys.stderr)
            from core import actions

            actions.notify(
                "Limited under XWayland", warning, timeout_ms=XWAYLAND_WARNING_MS, urgency="normal"
            )

    def read_selection(self, source: str) -> str:
        """Read with xclip even under Wayland. wl-clipboard needs a
        data-control protocol, which Mutter lacks (wl-copy flashes a window
        or fails, wl-paste --primary can hang); the compositor bridges X
        selections to Wayland apps both ways."""
        sel = "primary" if source.lower() == "primary" else "clipboard"
        try:
            out = subprocess.run(
                ["xclip", "-selection", sel, "-o"],
                capture_output=True,
                timeout=XCLIP_TIMEOUT_S,
            )
            return out.stdout.decode("utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError):
            return ""

    def set_clipboard(self, text: str) -> None:
        import shutil

        if not shutil.which("xclip"):
            print("[x11] xclip not installed, cannot set clipboard")
            return
        try:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text.encode("utf-8"),
                check=False,
                timeout=XCLIP_TIMEOUT_S,
            )
        except subprocess.SubprocessError as exc:
            print(f"[x11] xclip failed: {exc}")

    def pointer_position(self) -> tuple[int, int]:
        with _pointer_lock:
            return self._pointer_position_locked()

    def _pointer_position_locked(self) -> tuple[int, int]:
        global _pointer_dpy
        from Xlib import display

        if _pointer_dpy is None:
            _pointer_dpy = display.Display()
        try:
            data = _pointer_dpy.screen().root.query_pointer()
            return data.root_x, data.root_y
        except Exception:
            try:
                _pointer_dpy.close()
            except Exception:
                pass
            _pointer_dpy = None
            raise

    def send_key(self, combo: str) -> None:
        if send_keys(combo):
            return
        print(f"[x11] cannot send {combo!r}: no XTEST")
        from .base import _warn_missing_injector

        _warn_missing_injector(
            "x11",
            "Cut/Paste/Backspace needs the X server's XTEST extension, which this one lacks.",
        )

    def can_paste(self) -> bool:
        return xtest_available()

    def type_text(self, text: str) -> None:
        if type_text(text):
            return
        print("[x11] cannot type text: no XTEST")
        from .base import _warn_missing_injector

        _warn_missing_injector(
            "x11", "Typing text needs the X server's XTEST extension, which this one lacks."
        )

    def open_url(self, url: str) -> None:
        import shutil

        if not shutil.which("xdg-open"):
            print("[x11] xdg-open not available")
            return
        subprocess.Popen(["xdg-open", url], start_new_session=True)

    def active_window_haystacks(self) -> list[str]:
        """Window title and the WM_CLASS line in xprop's shape, which
        editable_detect._window_class matches on."""

        def read(dpy) -> list[str]:
            wid = _active_window(dpy)
            if not wid:
                return []
            win = dpy.create_resource_object("window", wid)
            name = win.get_full_property(
                dpy.intern_atom("_NET_WM_NAME"), dpy.intern_atom("UTF8_STRING")
            )
            title = name.value if name is not None else win.get_wm_name()
            if isinstance(title, bytes):
                title = title.decode("utf-8", errors="replace")
            haystacks = [title.lower()] if title else []
            wm_class = _xprop_wm_class(win.get_wm_class())
            return haystacks + [wm_class] if wm_class else haystacks

        return _on_shared_display(read, [])

    def make_selection_watcher(self, on_selection, debounce_ms):
        from triggers.watcher import SelectionWatcher

        return SelectionWatcher(on_selection, debounce_ms=debounce_ms)

    def make_hotkey(self, hotkey_str, on_trigger, use_polling=False, role=None):
        from triggers.hotkey import Hotkey

        return Hotkey(hotkey_str, on_trigger, use_polling=use_polling)

    def can_bind_hotkey(self, hotkey_str):
        from triggers.hotkey import _parse, _resolve_keysym

        try:
            _, keysym_name = _parse(hotkey_str)
        except ValueError:
            return False
        return _resolve_keysym(keysym_name) != 0

    def make_double_click_watcher(self, on_double_click):
        from triggers.mouse_watcher import DoubleClickWatcher

        return DoubleClickWatcher(on_double_click)

    def prepare_popup_window(self, win) -> None:
        x11_prepare_popup_window(win)

    def move_popup_window(self, win, x: int, y: int) -> None:
        x11_move_popup_window(win, x, y)


# Keystrokes, typing and window activation over XTEST on _pointer_dpy
# (docs/how-it-works.md#keystrokes-on-x11).

# A scratch keycode stays bound this long after its key events before it's
# rebound, so the client reads the events before the binding changes.
_REMAP_SETTLE_S = 0.012
# And this long after the last key event before it's given back: a client
# behind on its event queue drops every character it hasn't read yet.
_RESTORE_SETTLE_S = 0.25
_ACTIVATE_POLL_S = 0.02
# _NET_ACTIVE_WINDOW source indication "pager": WMs skip focus-stealing checks.
_EWMH_SOURCE_PAGER = 2

_MODIFIER_NAMES = {
    "ctrl": "Control_L",
    "control": "Control_L",
    "shift": "Shift_L",
    "alt": "Alt_L",
    "super": "Super_L",
    "win": "Super_L",
    "meta": "Super_L",
}
_xtest_ok: bool | None = None


def _on_shared_display(fn, default):
    """fn(dpy) on the shared connection, or `default` when there's no X
    display, no python-xlib, an X error, or a broken connection (reopened
    next time)."""
    global _pointer_dpy
    with _pointer_lock:
        try:
            from Xlib import display

            if _pointer_dpy is None:
                _pointer_dpy = display.Display()
            return fn(_pointer_dpy)
        except Exception as exc:  # noqa: BLE001
            if _is_xerror(exc):
                return default
            print(f"[x11] Xlib request failed: {exc}")
            try:
                if _pointer_dpy is not None:
                    _pointer_dpy.close()
            except Exception:
                pass
            _pointer_dpy = None
            return default


def _is_xerror(exc: Exception) -> bool:
    try:
        from Xlib.error import XError
    except ImportError:
        return False
    return isinstance(exc, XError)


def _keysym(name: str) -> int:
    """X keysym for an xdotool key name. A capital letter means its key, not
    Shift+key (pinned by `tests/test_x11_keys.py::test_named_keys_and_capitals`)."""
    from Xlib import XK

    if len(name) == 1 and name.isascii() and name.isprintable():
        name = name.lower()
        return XK.string_to_keysym(name) or ord(name)
    return XK.string_to_keysym(_MODIFIER_NAMES.get(name.lower(), name))


def _key_is_down(keymap, keycode: int) -> bool:
    """Whether `keycode` is set in a QueryKeymap bit vector."""
    return bool(keymap[keycode // 8] & (1 << (keycode % 8)))


def _held_modifiers(dpy) -> list[int]:
    """Keycodes of modifier keys that are down now, except Caps and Num Lock
    (pinned by `tests/test_x11_keys.py::test_held_modifiers_are_lifted_and_put_back`)."""
    from Xlib import X

    caps_and_num_lock = (X.LockMapIndex, X.Mod2MapIndex)
    keymap = dpy.query_keymap()
    held: list[int] = []
    for index, keycodes in enumerate(dpy.get_modifier_mapping()):
        if index in caps_and_num_lock:
            continue
        for kc in keycodes:
            if kc and _key_is_down(keymap, kc) and kc not in held:
                held.append(kc)
    return held


class _Scratch:
    """Spare keycodes (no keysyms) that keys missing from the layout are
    bound to while typed, one per keysym, highest first, and all restored to
    empty afterwards. Only when missing keysyms outnumber spare keycodes is
    the least recently used one rebound (pinned by
    `tests/test_x11_keys.py::test_a_key_the_layout_lacks_borrows_a_spare_keycode`,
    `tests/test_x11_keys.py::test_typing_binds_each_missing_character_to_its_own_spare_keycode`,
    `tests/test_x11_keys.py::test_more_missing_characters_than_spare_keycodes_rebinds_the_oldest`)."""

    NOT_LOOKED_UP = None

    def __init__(self, dpy) -> None:
        self._dpy = dpy
        self._free: list[int] | None = self.NOT_LOOKED_UP
        self._bound: dict[int, int] = {}  # keysym -> keycode, least recent first

    def _find(self) -> list[int]:
        info = self._dpy.display.info
        lo, hi = info.min_keycode, info.max_keycode
        mapping = self._dpy.get_keyboard_mapping(lo, hi - lo + 1)
        return [lo + offset for offset, syms in enumerate(mapping) if not any(syms)]

    def _bind(self, keycode: int, keysym: int) -> None:
        self._dpy.change_keyboard_mapping(keycode, [(keysym,)])
        self._dpy.sync()
        self._bound[keysym] = keycode

    def reserve(self, keysyms) -> None:
        """Bind `keysyms` up front, while spare keycodes last, so the
        keymap doesn't change in the middle of what's typed."""
        if self._free is None:
            self._free = self._find()
        for keysym in keysyms:
            if not self._free:
                return
            if keysym not in self._bound:
                self._bind(self._free.pop(), keysym)

    def keycode_for(self, keysym: int) -> int:
        """The scratch keycode bound to `keysym`, binding it if it isn't
        yet; 0 if the keyboard has no free keycode."""
        if keysym in self._bound:
            self._bound[keysym] = self._bound.pop(keysym)
            return self._bound[keysym]
        if self._free is None:
            self._free = self._find()
        if self._free:
            self._bind(self._free.pop(), keysym)
        elif self._bound:
            oldest = next(iter(self._bound))
            keycode = self._bound.pop(oldest)
            self._dpy.sync()
            time.sleep(_REMAP_SETTLE_S)
            self._bind(keycode, keysym)
        else:
            return 0
        return self._bound[keysym]

    def restore(self) -> None:
        if not self._bound:
            return
        self._dpy.sync()
        time.sleep(_RESTORE_SETTLE_S)
        for keycode in sorted(self._bound.values()):
            self._dpy.change_keyboard_mapping(keycode, [(0,)])
        self._dpy.sync()
        self._free = (self._free or []) + sorted(self._bound.values())
        self._bound = {}


def _keycode_for(dpy, keysym: int, scratch: _Scratch) -> tuple[int, bool]:
    """(keycode, needs Shift) that produces `keysym` on this keyboard, via
    the scratch keycode when the layout lacks it. (0, False) when it can't."""
    if not keysym:
        return 0, False
    kc = dpy.keysym_to_keycode(keysym)
    if kc:
        shifted = dpy.keycode_to_keysym(kc, 0) != keysym and dpy.keycode_to_keysym(kc, 1) == keysym
        return kc, shifted
    return scratch.keycode_for(keysym), False


def _press(dpy, chord: list[int]) -> None:
    from Xlib import X
    from Xlib.ext import xtest

    for kc in chord:
        xtest.fake_input(dpy, X.KeyPress, kc)
    for kc in reversed(chord):
        xtest.fake_input(dpy, X.KeyRelease, kc)


def xtest_send_key(
    dpy, combo: str, repeat: int = 1, delay_s: float = 0.0, clear_modifiers: bool = True
) -> bool:
    """Press and release `combo` ('ctrl+v', 'BackSpace', 'ctrl+shift+v')
    `repeat` times, `delay_s` apart, with XTEST on `dpy`. A key the current
    layout lacks goes through a scratch keycode. Returns False, having sent
    nothing, when the server has no XTEST, the combo is empty, or a key has
    no keysym at all. A key on the Shift level gets Shift held (pinned by
    `tests/test_x11_keys.py::test_a_shift_level_key_gets_shift`)."""
    from Xlib import X
    from Xlib.ext import xtest

    if not dpy.has_extension("XTEST"):
        return False
    parts = [p for p in combo.split("+") if p]
    if not parts:
        return False
    scratch = _Scratch(dpy)
    chord: list[int] = []
    shifted = False
    for name in parts:
        kc, shifted = _keycode_for(dpy, _keysym(name), scratch)
        if not kc:
            scratch.restore()
            return False
        if kc not in chord:
            chord.append(kc)
    if shifted:
        shift = dpy.keysym_to_keycode(_keysym("shift"))
        if shift and shift not in chord:
            chord.insert(0, shift)
    held = [kc for kc in _held_modifiers(dpy) if kc not in chord] if clear_modifiers else []
    for kc in held:
        xtest.fake_input(dpy, X.KeyRelease, kc)
    for i in range(max(1, repeat)):
        if i and delay_s > 0:
            dpy.sync()
            time.sleep(delay_s)
        _press(dpy, chord)
    for kc in held:
        xtest.fake_input(dpy, X.KeyPress, kc)
    dpy.sync()
    scratch.restore()
    return True


def xtest_type_text(dpy, text: str, delay_s: float = 0.0) -> bool:
    """Type `text` with XTEST, one press and release per character (what
    `xdotool type` did), `delay_s` between characters. Characters the layout
    lacks are typed through a scratch keycode; the modifiers the user is
    holding are lifted first and put back. False, having typed nothing, when
    the server has no XTEST."""
    from Xlib import X
    from Xlib.ext import xtest

    from . import xkb

    if not dpy.has_extension("XTEST"):
        return False
    scratch = _Scratch(dpy)
    held = _held_modifiers(dpy)
    for kc in held:
        xtest.fake_input(dpy, X.KeyRelease, kc)
    shift = dpy.keysym_to_keycode(_keysym("shift"))
    keysyms = [xkb.keysym_for_char(ch) for ch in text]
    scratch.reserve(dict.fromkeys(ks for ks in keysyms if ks and not dpy.keysym_to_keycode(ks)))
    skipped = 0
    for i, keysym in enumerate(keysyms):
        if i and delay_s > 0:
            dpy.sync()
            time.sleep(delay_s)
        kc, shifted = _keycode_for(dpy, keysym, scratch)
        if not kc:
            skipped += 1
            continue
        _press(dpy, [shift, kc] if shifted and shift else [kc])
    for kc in held:
        xtest.fake_input(dpy, X.KeyPress, kc)
    dpy.sync()
    scratch.restore()
    if skipped:
        print(f"[x11] typed {len(text) - skipped} of {len(text)} characters: no free keycode")
    return True


def send_keys(
    combo: str, repeat: int = 1, delay_s: float = 0.0, clear_modifiers: bool = True
) -> bool:
    """xtest_send_key on the shared connection; False when it can't."""
    return _on_shared_display(
        lambda dpy: xtest_send_key(dpy, combo, repeat, delay_s, clear_modifiers), False
    )


def type_text(text: str, delay_s: float = 0.0) -> bool:
    """xtest_type_text on the shared connection; False when it can't."""
    return _on_shared_display(lambda dpy: xtest_type_text(dpy, text, delay_s), False)


def release_keys(names: list[str]) -> bool:
    """Send a key release for each key name, held or not (`xdotool keyup`):
    insurance against a modifier left logically down after injected keys."""

    def run(dpy) -> bool:
        from Xlib import X
        from Xlib.ext import xtest

        if not dpy.has_extension("XTEST"):
            return False
        for name in names:
            ks = _keysym(name)
            kc = dpy.keysym_to_keycode(ks) if ks else 0
            if kc:
                xtest.fake_input(dpy, X.KeyRelease, kc)
        dpy.sync()
        return True

    return _on_shared_display(run, False)


def xtest_available() -> bool:
    """Whether the X server offers XTEST (checked once)."""
    global _xtest_ok
    if _xtest_ok is None:
        _xtest_ok = bool(_on_shared_display(lambda dpy: dpy.has_extension("XTEST"), False))
    return _xtest_ok


def _active_window(dpy) -> int:
    from Xlib import X

    prop = dpy.screen().root.get_full_property(
        dpy.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType
    )
    return int(prop.value[0]) if prop is not None and len(prop.value) else 0


def active_window_id() -> int:
    """The window manager's active window (EWMH _NET_ACTIVE_WINDOW), 0 when
    there is none or no X display (`xdotool getactivewindow`)."""
    return _on_shared_display(_active_window, 0)


def activate_window(wid: int | str, timeout_s: float = 1.0) -> bool:
    """Ask the window manager to activate `wid` and wait up to timeout_s for
    it to become the active window (`xdotool windowactivate --sync`). True
    once it is."""
    wid = int(wid)

    def ask(dpy) -> bool:
        from Xlib import X
        from Xlib.protocol import event

        root = dpy.screen().root
        msg = event.ClientMessage(
            window=dpy.create_resource_object("window", wid),
            client_type=dpy.intern_atom("_NET_ACTIVE_WINDOW"),
            data=(32, [_EWMH_SOURCE_PAGER, X.CurrentTime, _active_window(dpy), 0, 0]),
        )
        root.send_event(msg, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        dpy.sync()
        return True

    if not _on_shared_display(ask, False):
        return False
    deadline = time.monotonic() + timeout_s
    while True:
        if active_window_id() == wid:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_ACTIVATE_POLL_S)


def window_wm_class(wid: int | str) -> str:
    """WM_CLASS of X window `wid` as _xprop_wm_class gives it."""
    try:
        xid = int(wid)
    except ValueError:
        return ""

    def run(dpy) -> str:
        return _xprop_wm_class(dpy.create_resource_object("window", xid).get_wm_class())

    return _on_shared_display(run, "")


def _xprop_wm_class(cls) -> str:
    """A WM_CLASS pair as xprop prints it, lowercased
    (`wm_class(string) = "instance", "class"`), or "" for none."""
    return f'wm_class(string) = "{cls[0]}", "{cls[1]}"'.lower() if cls else ""


# Popup placement: override-redirect before mapping, then XMoveWindow
# (docs/how-it-works.md#popup-placement-on-x11).

_win_dpy = None  # GTK thread only, so separate from _pointer_dpy
# id(win) -> logical (x, y) asked for before the window was realized
_pending_xy: dict[int, tuple[int, int]] = {}
_warned_not_x11 = False


def gdk_display_is_x11(display=None) -> bool:
    """True when GDK runs on X11 (a real Xorg session or XWayland)."""
    try:
        import gi

        gi.require_version("Gdk", "4.0")
        gi.require_version("GdkX11", "4.0")
        from gi.repository import Gdk, GdkX11

        display = display or Gdk.Display.get_default()
        return isinstance(display, GdkX11.X11Display)
    except (ImportError, ValueError):
        return False


def _xlib_display():
    global _win_dpy
    if _win_dpy is None:
        from Xlib import display

        _win_dpy = display.Display()
    return _win_dpy


def _xwindow(win):
    """The python-xlib Window for a realized Gtk.Window, or None."""
    surface = win.get_surface()
    if surface is None:
        return None
    from gi.repository import GdkX11

    if not isinstance(surface, GdkX11.X11Surface):
        return None
    return _xlib_display().create_resource_object("window", surface.get_xid())


def _scale_of(win) -> int:
    """GDK's X11 surface scale (GDK_SCALE): X coordinates are device pixels,
    the backend API takes logical pixels."""
    try:
        surface = win.get_surface()
        return max(1, int(surface.get_scale_factor())) if surface else 1
    except Exception:
        return 1


def _on_popup_realize(win) -> None:
    try:
        # GDK made the X window on its own connection: sync before using the XID.
        win.get_display().sync()
        xwin = _xwindow(win)
        if xwin is None:
            return
        dpy = _xlib_display()
        from Xlib import Xatom

        xwin.change_attributes(override_redirect=True)
        # Compositors still read an override-redirect window's type for
        # shadows and animations.
        xwin.change_property(
            dpy.intern_atom("_NET_WM_WINDOW_TYPE"),
            Xatom.ATOM,
            32,
            [dpy.intern_atom("_NET_WM_WINDOW_TYPE_POPUP_MENU")],
        )
        pending = _pending_xy.pop(id(win), None)
        if pending is not None:
            s = _scale_of(win)
            xwin.configure(x=int(pending[0] * s), y=int(pending[1] * s))
        dpy.sync()  # before GTK maps the window
    except Exception as exc:  # noqa: BLE001
        print(f"[x11] popup window setup failed: {exc}")


def x11_prepare_popup_window(win) -> None:
    global _warned_not_x11
    if not gdk_display_is_x11(win.get_display()):
        if not _warned_not_x11:
            _warned_not_x11 = True
            print("[x11] GDK is not on X11 - popup windows cannot be positioned")
        return
    win.connect("realize", _on_popup_realize)
    if win.get_realized():
        _on_popup_realize(win)


def x11_move_popup_window(win, x: int, y: int) -> None:
    if not win.get_realized():
        _pending_xy[id(win)] = (int(x), int(y))
        return
    try:
        xwin = _xwindow(win)
        if xwin is None:
            return
        from Xlib import X

        s = _scale_of(win)
        if win.get_mapped():
            # Re-raise: a window raised since mapping could cover the popup.
            xwin.configure(x=int(x * s), y=int(y * s), stack_mode=X.Above)
        else:
            xwin.configure(x=int(x * s), y=int(y * s))
        _xlib_display().sync()
    except Exception as exc:  # noqa: BLE001
        print(f"[x11] popup move failed: {exc}")
