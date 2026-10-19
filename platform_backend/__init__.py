"""vinoPop platform backend selection.

`get_backend()` returns a cached PlatformBackend chosen by detect():
  - KDE Plasma Wayland  -> WaylandKdeBackend
  - everything else      -> X11Backend (the original behaviour; on other
    Wayland compositors - GNOME, sway, Hyprland - it runs under XWayland)

main.py's _choose_gdk_backend() uses detect() too, so GDK runs on XWayland
whenever the X11 backend is chosen inside a Wayland session.

The package is named `platform_backend`, not `platform`, to avoid shadowing
Python's stdlib `platform` module (imported by gi and others).
"""

from __future__ import annotations

import os

from .base import PlatformBackend

_backend: PlatformBackend | None = None


def is_wayland_session() -> bool:
    """True when there is a Wayland display and either no X11 display or the
    session says it is Wayland (a Wayland session that also exposes XWayland)."""
    if not os.environ.get("WAYLAND_DISPLAY"):
        return False
    session = (os.environ.get("XDG_SESSION_TYPE") or "").lower()
    return not os.environ.get("DISPLAY") or session == "wayland"


def is_kde_desktop() -> bool:
    """True when XDG_CURRENT_DESKTOP (colon-separated, any case) lists KDE."""
    desktops = (os.environ.get("XDG_CURRENT_DESKTOP") or "").split(":")
    return any(d.strip().upper() == "KDE" for d in desktops)


def detect() -> str:
    """Return 'wayland_kde' or 'x11'.

    Wayland KDE is chosen only in a Wayland session (see is_wayland_session)
    whose XDG_CURRENT_DESKTOP lists KDE: the backend needs KWin scripting and
    KGlobalAccel. Every other desktop, Wayland or not, gets the X11 backend,
    which on a non-KDE Wayland compositor runs under XWayland.
    Override with VINOPOP_BACKEND=x11|wayland_kde.
    """
    forced = (os.environ.get("VINOPOP_BACKEND") or "").strip().lower()
    if forced in ("x11", "wayland_kde"):
        return forced
    if is_wayland_session() and is_kde_desktop():
        return "wayland_kde"
    return "x11"


def get_backend() -> PlatformBackend:
    global _backend
    if _backend is None:
        choice = detect()
        if choice == "wayland_kde":
            from .wayland_kde import WaylandKdeBackend

            _backend = WaylandKdeBackend()
        else:
            from .x11 import X11Backend

            _backend = X11Backend()
    return _backend
