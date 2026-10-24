"""Screenshots through xdg-desktop-portal, for desktops with no grabber CLI.

spectacle is KDE's, grim needs wlr-screencopy (sway, Hyprland, COSMIC), and
maim is X11: under XWayland it gets XWayland's windows and black. GNOME has
none of them, but every desktop's portal backend implements
org.freedesktop.portal.Screenshot (GNOME, KDE, wlr, COSMIC).

Blocking, and only GLib/Gio: call it from a worker thread (screen_ocr's
capture thread), never the GTK one. The portal answers through a Response
signal on a request object, so the call runs a private main loop on this
thread's own context until the answer arrives.
"""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from urllib.parse import unquote, urlparse

from gi.repository import Gio, GLib

_BUS = "org.freedesktop.portal.Desktop"
_PATH = "/org/freedesktop/portal/desktop"
_IFACE = "org.freedesktop.portal.Screenshot"


def _bus():
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def available() -> bool:
    """True when the session's portal exports the Screenshot interface."""
    try:
        reply = _bus().call_sync(
            _BUS,
            _PATH,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (_IFACE, "version")),
            None,
            Gio.DBusCallFlags.NONE,
            1000,
            None,
        )
        return reply is not None
    except GLib.Error:
        return False


def screenshot(interactive: bool = False, timeout_s: float = 120.0) -> str | None:
    """Take a screenshot and return a temp PNG the caller owns, or None
    (cancelled, denied, no portal). interactive=True lets the desktop show
    its own picker (GNOME's region UI); False grabs the whole desktop."""
    ctx = GLib.MainContext.new()
    ctx.push_thread_default()
    try:
        return _screenshot(ctx, interactive, timeout_s)
    finally:
        ctx.pop_thread_default()


def _screenshot(ctx, interactive: bool, timeout_s: float) -> str | None:
    try:
        conn = _bus()
    except GLib.Error as exc:
        print(f"[portal] no session bus: {exc.message}")
        return None
    token = f"vinopop{secrets.token_hex(4)}"
    sender = (conn.get_unique_name() or "").lstrip(":").replace(".", "_")
    handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    loop = GLib.MainLoop.new(ctx, False)
    answer: list = []

    def on_response(_conn, _sender, _path, _iface, _signal, params) -> None:
        answer.append(params.unpack())
        loop.quit()

    # Subscribed before the call, on the handle we're about to be given, so
    # a fast answer can't arrive before anyone listens.
    sub = conn.signal_subscribe(
        _BUS,
        "org.freedesktop.portal.Request",
        "Response",
        handle,
        None,
        Gio.DBusSignalFlags.NONE,
        on_response,
    )
    try:
        options = {
            "handle_token": GLib.Variant("s", token),
            "interactive": GLib.Variant("b", interactive),
            "modal": GLib.Variant("b", False),
        }
        try:
            conn.call_sync(
                _BUS,
                _PATH,
                _IFACE,
                "Screenshot",
                GLib.Variant("(sa{sv})", ("", options)),
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except GLib.Error as exc:
            print(f"[portal] Screenshot failed: {exc.message}")
            return None
        timer = GLib.timeout_source_new(int(timeout_s * 1000))
        timer.set_callback(lambda *_: (loop.quit(), False)[1])
        timer.attach(ctx)
        loop.run()
        timer.destroy()
    finally:
        conn.signal_unsubscribe(sub)
    if not answer:
        print("[portal] no answer to Screenshot")
        return None
    code, results = answer[0]
    if code != 0:
        return None  # 1 = cancelled, 2 = denied or failed
    return _take(results.get("uri", ""))


def _take(uri: str) -> str | None:
    """Move the portal's file to a temp path we own: backends write it to
    /tmp, or ~/Pictures on some, and it's ours to clean up either way."""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    src = unquote(parsed.path)
    fd, dst = tempfile.mkstemp(suffix=".png", prefix="lp-ocr-portal-")
    os.close(fd)
    try:
        shutil.move(src, dst)
    except OSError as exc:
        print(f"[portal] could not take {src}: {exc}")
        try:
            os.unlink(dst)
        except OSError:
            pass
        return None
    return dst
