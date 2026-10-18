"""Print the active window's "resourceClass\\tcaption" and exit (KDE Wayland).

Same KWin-scripting-over-DBus trick as _kwin_cursor.py, used by
WaylandKdeBackend.active_window_haystacks for the popup blocklist and the
editable fallback. The app asks through _kwin_query.py; this one-shot form
is for debugging.
"""

from __future__ import annotations

import itertools
import os
import sys
import tempfile

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib  # only GLib: no GTK in this helper

# Unique bus name per run, fixed interface: see _kwin_cursor.py for why.
IFACE = "io.github.karanshukla.vinoPop.ActiveWin"
OBJ_PATH = "/win"
_counter = itertools.count()


def bus_name() -> str:
    return f"{IFACE}.p{os.getpid()}_{next(_counter)}"


def script_js(name: str) -> str:
    # KWin 6 renamed activeClient -> activeWindow; fall back for older builds.
    return (
        "var c = workspace.activeWindow || workspace.activeClient;"
        f'callDBus("{name}", "{OBJ_PATH}", "{IFACE}", '
        '"Report", c ? String(c.resourceClass) : "", c ? String(c.caption) : "");'
    )


def main() -> int:
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    loop = GLib.MainLoop()
    got: dict[str, tuple[str, str]] = {}

    class _Svc(dbus.service.Object):
        @dbus.service.method(IFACE, in_signature="ss", out_signature="")
        def Report(self, cls, caption):  # noqa: N802
            got["v"] = (str(cls), str(caption))
            print(f"{cls}\t{caption}", flush=True)
            loop.quit()

    unique = bus_name()
    # do_not_queue: fail fast rather than wait behind another owner.
    name = dbus.service.BusName(unique, bus, do_not_queue=True)
    _svc = _Svc(name, OBJ_PATH)

    fd, path = tempfile.mkstemp(suffix=".js", prefix="vinopop-active-")
    try:
        os.write(fd, script_js(unique).encode("utf-8"))
        os.close(fd)
        scripting = dbus.Interface(
            bus.get_object("org.kde.KWin", "/Scripting"),
            "org.kde.kwin.Scripting",
        )
        script_id = int(scripting.loadScript(path))
        script = bus.get_object("org.kde.KWin", f"/Scripting/Script{script_id}")
        dbus.Interface(script, "org.kde.kwin.Script").run()
        GLib.timeout_add(1500, lambda: (loop.quit(), False)[1])
        loop.run()
        try:
            scripting.unloadScript(path)  # don't leak resident KWin scripts
        except Exception:
            pass
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return 0 if "v" in got else 1


if __name__ == "__main__":
    sys.exit(main())
