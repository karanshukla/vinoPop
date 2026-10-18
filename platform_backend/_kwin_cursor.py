"""Print the KWin global pointer position as "x y" and exit (KDE Wayland only).

Wayland exposes no protocol for a client to query the global pointer. KDE's
KWin scripting API does (`workspace.cursorPos`), reachable over DBus. We load a
one-line KWin script that pushes the position back to us via callDBus - far
faster than the journalctl route - then print and exit.

The app asks through the long-lived _kwin_query.py process (kept out of the
main app so its GLib main loop never tangles with ours); this one-shot form
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

# The interface is fixed; the bus NAME is unique per helper run. A shared
# name made concurrent queries collide: the second helper sat in the name's
# queue, KWin delivered its script's callback to the first, and it timed
# out with no coords (the popup then landed at (0, 0)).
IFACE = "io.github.karanshukla.vinoPop.Cursor"
OBJ_PATH = "/cursor"
_counter = itertools.count()


def bus_name() -> str:
    """A well-known name only this run owns (pid + per-process counter)."""
    return f"{IFACE}.p{os.getpid()}_{next(_counter)}"


def script_js(name: str) -> str:
    """The KWin script, calling back to `name` rather than a shared name."""
    return (
        f'callDBus("{name}", "{OBJ_PATH}", "{IFACE}", '
        '"Report", workspace.cursorPos.x, workspace.cursorPos.y);'
    )


def main() -> int:
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    loop = GLib.MainLoop()
    got: dict[str, tuple[int, int]] = {}

    class _Svc(dbus.service.Object):
        @dbus.service.method(IFACE, in_signature="ii", out_signature="")
        def Report(self, x, y):  # noqa: N802 - DBus method name
            got["xy"] = (int(x), int(y))
            print(f"{int(x)} {int(y)}", flush=True)
            loop.quit()

    unique = bus_name()
    # do_not_queue: fail fast rather than wait behind another owner.
    name = dbus.service.BusName(unique, bus, do_not_queue=True)
    _svc = _Svc(name, OBJ_PATH)

    fd, path = tempfile.mkstemp(suffix=".js", prefix="vinopop-cursor-")
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
        # CRUCIAL: unload the script. KWin keeps every loaded script resident;
        # loading one per cursor read (i.e. per popup) without unloading leaks
        # them and eventually destabilises the compositor.
        try:
            scripting.unloadScript(path)
        except Exception:
            pass
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return 0 if "xy" in got else 1


if __name__ == "__main__":
    sys.exit(main())
