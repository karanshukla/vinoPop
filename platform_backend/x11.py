"""The XWayland backend: how vinoPop runs on GNOME, sway, Hyprland and every
other Wayland desktop except KDE Plasma (which has wayland_kde.py). The whole
process, GTK included, runs on XWayland inside the Wayland session. A plain
Xorg session takes the same path, best-effort. The module and the
VINOPOP_BACKEND value keep the name "x11" for compatibility.

Wraps xclip (selection/clipboard; see read_selection for why not
wl-clipboard), Xlib (pointer, popup placement, active window, keystrokes over
XTEST, with xdotool as the fallback) and the X11 watcher / hotkey /
double-click modules.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

from .base import PlatformBackend

# Reused Xlib connection for pointer queries (opening a fresh Display() per
# call costs ~5-15 ms of XOpenDisplay roundtrip, which the hotkey path hits on
# every press). Module-scope; never closed - lives for the process lifetime.
_pointer_dpy = None
# The hotkey path queries the pointer from a worker thread, other callers from
# the GTK thread; python-xlib connections aren't thread-safe by default.
_pointer_lock = threading.Lock()


class X11Backend(PlatformBackend):
    name = "x11"
    popup_uses_xlib = True

    # ---- session ---------------------------------------------------------
    def check_session(self) -> None:
        """XWayland (GNOME, sway ...) is the main path and warns about its
        limits; a plain Xorg session works best-effort. Without a DISPLAY
        there is no XWayland to run on, which is fatal (KDE Plasma Wayland
        gets the wayland_kde backend instead)."""
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

            actions.notify("Limited under XWayland", warning, timeout_ms=6000, urgency="normal")

    # ---- selection / clipboard ------------------------------------------
    # xclip, not wl-clipboard, even with a WAYLAND_DISPLAY: don't "fix" this.
    # wl-copy/wl-paste only work cleanly on a compositor with a data-control
    # protocol (zwlr_data_control_manager_v1 or ext_data_control_manager_v1).
    # Mutter (GNOME) has neither: there wl-copy falls back to a focus-stealing
    # popup surface that flashes a window or fails silently, and
    # wl-paste --primary can hang. This process runs on XWayland, and the
    # compositor bridges the X selections to Wayland apps both ways, so xclip
    # reaches native Wayland apps too. sway and KWin do have data-control,
    # which is why wl-clipboard looks fine there; wayland_kde.py uses it.
    def read_selection(self, source: str) -> str:
        sel = "primary" if source.lower() == "primary" else "clipboard"
        try:
            # Same 2 s as the watcher: 0.5 s read large selections
            # (LibreOffice) as empty and showed the no-selection menu. The
            # hotkey path calls this off the GTK thread, so waiting is safe.
            out = subprocess.run(
                ["xclip", "-selection", sel, "-o"],
                capture_output=True,
                timeout=2.0,
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
                timeout=2.0,
            )
        except subprocess.SubprocessError as exc:
            print(f"[x11] xclip failed: {exc}")

    # ---- pointer ---------------------------------------------------------
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

    # ---- keystroke injection --------------------------------------------
    def send_key(self, combo: str) -> None:
        if send_keys(combo):
            return
        import shutil

        if not shutil.which("xdotool"):
            print(f"[x11] cannot send {combo!r}: no XTEST and xdotool missing")
            from .base import _warn_missing_injector

            _warn_missing_injector(
                "x11",
                "Cut/Paste/Backspace needs the X server's XTEST extension or xdotool.",
            )
            return
        subprocess.run(
            ["xdotool", "key", "--clearmodifiers", combo],
            check=False,
        )

    def can_paste(self) -> bool:
        import shutil

        return xtest_available() or bool(shutil.which("xdotool"))

    def type_text(self, text: str) -> None:
        # Stays on xdotool: typing a character the keyboard layout lacks
        # means remapping a spare keycode first, which XTEST alone can't do.
        import shutil

        if not shutil.which("xdotool"):
            print("[x11] xdotool missing - cannot type text")
            from .base import _warn_missing_injector

            _warn_missing_injector("x11", "Typing text needs xdotool. Install it to enable it.")
            return
        subprocess.run(
            ["xdotool", "type", "--clearmodifiers", "--", text],
            check=False,
        )

    # ---- opening URLs ----------------------------------------------------
    def open_url(self, url: str) -> None:
        import shutil

        if not shutil.which("xdg-open"):
            print("[x11] xdg-open not available")
            return
        # X11 window managers raise the browser on their own.
        subprocess.Popen(["xdg-open", url], start_new_session=True)

    # ---- active window ---------------------------------------------------
    def active_window_haystacks(self) -> list[str]:
        """Window title + the WM_CLASS line in xprop's shape
        (`wm_class(string) = "instance", "class"`, which
        editable_detect._window_class matches on). Read over the shared Xlib
        connection: it runs on every popup, and xdotool + xprop were three
        process spawns."""
        global _pointer_dpy
        from Xlib import X, display
        from Xlib.error import XError

        with _pointer_lock:
            if _pointer_dpy is None:
                _pointer_dpy = display.Display()
            dpy = _pointer_dpy
            try:
                prop = dpy.screen().root.get_full_property(
                    dpy.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType
                )
                wid = prop.value[0] if prop is not None and len(prop.value) else 0
                if not wid:
                    return []
                win = dpy.create_resource_object("window", wid)
                name = win.get_full_property(
                    dpy.intern_atom("_NET_WM_NAME"), dpy.intern_atom("UTF8_STRING")
                )
                title = name.value if name is not None else win.get_wm_name()
                if isinstance(title, bytes):
                    title = title.decode("utf-8", errors="replace")
                cls = win.get_wm_class()
            except XError:
                return []  # the window went away between the two reads
            except Exception as exc:  # noqa: BLE001 - connection gone
                print(f"[x11] active-window query failed: {exc}")
                try:
                    dpy.close()
                except Exception:
                    pass
                _pointer_dpy = None
                return []
        haystacks = [title.lower()] if title else []
        if cls:
            haystacks.append(f'wm_class(string) = "{cls[0]}", "{cls[1]}"'.lower())
        return haystacks

    # ---- component factories --------------------------------------------
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

    # ---- popup positioning ----------------------------------------------
    def prepare_popup_window(self, win) -> None:
        x11_prepare_popup_window(win)

    def move_popup_window(self, win, x: int, y: int) -> None:
        x11_move_popup_window(win, x, y)


# ---------------------------------------------------------------------------
# Keystrokes and window activation in python-xlib, in place of xdotool and
# xprop. Keys go over XTEST, which is what `xdotool key` uses too: let go of
# the modifiers the user is still holding (--clearmodifiers), press the chord,
# then put the held ones back, so a hotkey's Super/Shift doesn't turn Ctrl+V
# into something else. Like xdotool, the events reach X11 clients only
# (XWayland ones on a Wayland desktop). Everything here runs on the shared
# _pointer_dpy connection under _pointer_lock.
# ---------------------------------------------------------------------------

# xdotool's modifier spellings -> X keysym names
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
    """fn(dpy) on the shared connection, or `default` if there's no X
    display or python-xlib, or the connection broke (it's reopened next
    time)."""
    global _pointer_dpy
    with _pointer_lock:
        try:
            from Xlib import display

            if _pointer_dpy is None:
                _pointer_dpy = display.Display()
            return fn(_pointer_dpy)
        except Exception as exc:  # noqa: BLE001
            if _is_xerror(exc):  # e.g. BadWindow: the connection is fine
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
    from Xlib import XK

    if len(name) == 1 and name.isascii() and name.isprintable():
        # 'V' -> 'v' so a chord never asks for an implicit Shift.
        name = name.lower()
        return XK.string_to_keysym(name) or ord(name)
    return XK.string_to_keysym(_MODIFIER_NAMES.get(name.lower(), name))


def _held_modifiers(dpy) -> list[int]:
    """Keycodes of modifier keys (not Caps or Num Lock) that are down now."""
    from Xlib import X

    keymap = dpy.query_keymap()
    held: list[int] = []
    for index, keycodes in enumerate(dpy.get_modifier_mapping()):
        if index in (X.LockMapIndex, X.Mod2MapIndex):  # Caps Lock, Num Lock
            continue
        for kc in keycodes:
            if kc and keymap[kc // 8] & (1 << (kc % 8)) and kc not in held:
                held.append(kc)
    return held


def xtest_send_key(
    dpy, combo: str, repeat: int = 1, delay_s: float = 0.0, clear_modifiers: bool = True
) -> bool:
    """Press and release `combo` ('ctrl+v', 'BackSpace', 'ctrl+shift+v')
    `repeat` times, `delay_s` apart, with XTEST on `dpy`. Returns False,
    having sent nothing, when the server has no XTEST or a key isn't in the
    current keymap (xdotool remaps a spare keycode for those, so callers
    fall back to it)."""
    from Xlib import X
    from Xlib.ext import xtest

    if not dpy.has_extension("XTEST"):
        return False
    parts = [p for p in combo.split("+") if p]
    if not parts:
        return False
    chord: list[int] = []
    for name in parts:
        ks = _keysym(name)
        kc = dpy.keysym_to_keycode(ks) if ks else 0
        if not kc:
            return False
        if kc not in chord:
            chord.append(kc)
    # A key on the Shift level (e.g. 'plus' on a US layout) needs Shift held.
    ks = _keysym(parts[-1])
    if dpy.keycode_to_keysym(chord[-1], 0) != ks and dpy.keycode_to_keysym(chord[-1], 1) == ks:
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
        for kc in chord:
            xtest.fake_input(dpy, X.KeyPress, kc)
        for kc in reversed(chord):
            xtest.fake_input(dpy, X.KeyRelease, kc)
    for kc in held:
        xtest.fake_input(dpy, X.KeyPress, kc)
    dpy.sync()
    return True


def send_keys(
    combo: str, repeat: int = 1, delay_s: float = 0.0, clear_modifiers: bool = True
) -> bool:
    """xtest_send_key on the shared connection; False when it can't."""
    return _on_shared_display(
        lambda dpy: xtest_send_key(dpy, combo, repeat, delay_s, clear_modifiers), False
    )


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
            # source 2 = pager: WMs honour it without focus-stealing checks
            data=(32, [2, X.CurrentTime, _active_window(dpy), 0, 0]),
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
        time.sleep(0.02)


def window_wm_class(wid: int | str) -> str:
    """WM_CLASS of X window `wid` in xprop's shape, lowercased
    (`wm_class(string) = "instance", "class"`), or "" when it has none or is
    gone. For callers that used to spawn `xprop -id <wid> WM_CLASS`."""

    try:
        xid = int(wid)
    except ValueError:
        return ""

    def run(dpy) -> str:
        cls = dpy.create_resource_object("window", xid).get_wm_class()
        return f'wm_class(string) = "{cls[0]}", "{cls[1]}"'.lower() if cls else ""

    return _on_shared_display(run, "")


# ---------------------------------------------------------------------------
# Floating-window placement on X11 / XWayland.
#
# GTK 4 has no Gtk.Window.move(), no POPUP window type and no type hints, so
# we do what GTK 3's POPUP/POPUP_MENU windows did ourselves with python-xlib:
# mark the X window override-redirect right after GTK creates it (before it is
# ever mapped), so the WM neither decorates, focuses, places nor lists it, and
# move it with XMoveWindow. Override-redirect windows never receive focus from
# the WM, which also gives us GTK 3's accept_focus=False for free - paste keeps
# going to the app the text came from. Shared with WaylandKdeBackend, which
# falls back to this when the process runs on XWayland (no gtk4-layer-shell).
# ---------------------------------------------------------------------------

# Own Xlib connection for window attribute/move requests (main thread only).
_win_dpy = None
# id(win) -> (x, y) logical position requested before the window was realized.
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
        # GDK created the X window on its own connection; make sure the
        # server has processed that before we reference the XID on ours.
        win.get_display().sync()
        xwin = _xwindow(win)
        if xwin is None:
            return
        dpy = _xlib_display()
        from Xlib import Xatom

        xwin.change_attributes(override_redirect=True)
        # Compositors (picom, muffin, mutter) still read the window type of an
        # override-redirect window for shadows/animations - same hint GTK 3
        # set for us via Gdk.WindowTypeHint.POPUP_MENU.
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
        # Round-trip so all of this lands before GTK maps the window.
        dpy.sync()
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
            # Override-redirect windows go on top when mapped; re-raise on
            # every move so a dialog raised meanwhile can't cover the popup.
            xwin.configure(x=int(x * s), y=int(y * s), stack_mode=X.Above)
        else:
            xwin.configure(x=int(x * s), y=int(y * s))
        _xlib_display().sync()
    except Exception as exc:  # noqa: BLE001
        print(f"[x11] popup move failed: {exc}")
