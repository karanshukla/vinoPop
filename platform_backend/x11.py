"""X11 platform backend - the original vinoPop behaviour.

Wraps xclip (selection/clipboard), Xlib (pointer, popup placement), xdotool
(keystrokes / active window) and the existing X11 watcher / hotkey /
double-click modules.
Behaviour here must stay byte-for-byte what vinoPop did before the
platform_backend refactor, so X11 users see no change.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

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
        """Pure X11 fully supported; XWayland warns but starts; pure Wayland
        is handled by the wayland_kde backend, so reaching here without a
        DISPLAY is fatal."""
        has_x11 = bool(os.environ.get("DISPLAY"))
        has_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        if not has_x11:
            print("DISPLAY is not set - the X11 backend needs an X11 display.", file=sys.stderr)
            sys.exit(2)
        if has_wayland and has_x11:
            warning = (
                "Running under XWayland: PRIMARY-selection auto-popup will only "
                "fire for legacy X11 apps. The global hotkey still works "
                "everywhere the X11 grab is honoured. For full functionality, "
                "use an Xorg session - or a KDE Plasma Wayland session, which "
                "vinoPop now supports natively."
            )
            print(f"[vinopop] WARNING: {warning}", file=sys.stderr)
            try:
                subprocess.run(
                    [
                        "notify-send",
                        "--hint=byte:transient:1",
                        "-t",
                        "6000",
                        "-u",
                        "normal",
                        "-i",
                        "vinopop",
                        "vinoPop: limited under XWayland",
                        warning,
                    ],
                    check=False,
                )
            except FileNotFoundError:
                pass

    # ---- selection / clipboard ------------------------------------------
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
        import shutil

        if not shutil.which("xdotool"):
            print("[x11] xdotool missing - cannot send key")
            from .base import _warn_missing_injector

            _warn_missing_injector(
                "x11",
                "Cut/Paste/Backspace needs xdotool. Install it to enable keystroke actions.",
            )
            return
        subprocess.run(
            ["xdotool", "key", "--clearmodifiers", combo],
            check=False,
        )

    def can_paste(self) -> bool:
        import shutil

        return bool(shutil.which("xdotool"))

    def type_text(self, text: str) -> None:
        import shutil

        if not shutil.which("xdotool"):
            print("[x11] xdotool missing - cannot type text")
            from .base import _warn_missing_injector

            _warn_missing_injector(
                "x11",
                "Cut/Paste/Backspace needs xdotool. Install it to enable keystroke actions.",
            )
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
        """Window title (xdotool) + WM_CLASS (xprop). xprop is used for the
        class because xdotool getwindowclassname is missing in several distro
        builds."""
        haystacks: list[str] = []
        try:
            wid = subprocess.run(
                ["xdotool", "getactivewindow"],
                capture_output=True,
                text=True,
                timeout=0.3,
            ).stdout.strip()
            if not wid:
                return []
            try:
                out = subprocess.run(
                    ["xdotool", "getwindowname", wid],
                    capture_output=True,
                    text=True,
                    timeout=0.3,
                ).stdout.strip()
                if out:
                    haystacks.append(out.lower())
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                out = subprocess.run(
                    ["xprop", "-id", wid, "WM_CLASS"],
                    capture_output=True,
                    text=True,
                    timeout=0.3,
                ).stdout.strip()
                if out:
                    haystacks.append(out.lower())
            except (OSError, subprocess.SubprocessError):
                pass
        except (OSError, subprocess.SubprocessError):
            return []
        return haystacks

    # ---- component factories --------------------------------------------
    def make_selection_watcher(self, on_selection, debounce_ms):
        from watcher import SelectionWatcher

        return SelectionWatcher(on_selection, debounce_ms=debounce_ms)

    def make_hotkey(self, hotkey_str, on_trigger, use_polling=False):
        from hotkey import Hotkey

        return Hotkey(hotkey_str, on_trigger, use_polling=use_polling)

    def make_double_click_watcher(self, on_double_click):
        from mouse_watcher import DoubleClickWatcher

        return DoubleClickWatcher(on_double_click)

    # ---- popup positioning ----------------------------------------------
    def prepare_popup_window(self, win) -> None:
        x11_prepare_popup_window(win)

    def move_popup_window(self, win, x: int, y: int) -> None:
        x11_move_popup_window(win, x, y)


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
