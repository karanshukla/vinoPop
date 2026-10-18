"""Long-lived KWin query helper (KDE Wayland only).

Reads `cursor` or `active` lines on stdin and answers each with the line the
one-shot _kwin_cursor / _kwin_active helpers print, or an empty line on
failure. One process per vinoPop run: starting Python + dbus + gi is ~55 ms
of a one-shot query's ~61 ms, the KWin round trip itself ~6 ms.

Each query still calls back to a fresh bus name (see _kwin_cursor.py), so a
late callback from a timed-out query can't answer the next one.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib  # only GLib: no GTK in this helper

from . import _kwin_active, _kwin_cursor

_TIMEOUT_MS = 1500
_HELPERS = {"cursor": _kwin_cursor, "active": _kwin_active}


def _one_line(s: str) -> str:
    """Control characters (tab and every line break included) -> spaces."""
    return _CONTROL.sub(" ", s)


_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


class _State:
    def __init__(self) -> None:
        self.loop: GLib.MainLoop | None = None
        self.out: str | None = None

    def report(self, out: str) -> None:
        self.out = out
        if self.loop is not None:
            self.loop.quit()


def _query(bus, scripting, helper, state: _State) -> str | None:
    name = helper.bus_name()
    state.out = None
    if (
        bus.request_name(name, dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
        != dbus.bus.REQUEST_NAME_REPLY_PRIMARY_OWNER
    ):
        return None
    fd, path = tempfile.mkstemp(suffix=".js", prefix="vinopop-kwin-")
    try:
        os.write(fd, helper.script_js(name).encode("utf-8"))
        os.close(fd)
        script_id = int(scripting.loadScript(path))
        script = bus.get_object("org.kde.KWin", f"/Scripting/Script{script_id}")
        state.loop = GLib.MainLoop()
        dbus.Interface(script, "org.kde.kwin.Script").run()
        timed_out = []

        def _give_up() -> bool:
            timed_out.append(True)
            state.loop.quit()
            return False

        src = GLib.timeout_add(_TIMEOUT_MS, _give_up)
        state.loop.run()
        if not timed_out:
            GLib.source_remove(src)
        try:
            scripting.unloadScript(path)  # don't leak resident KWin scripts
        except dbus.DBusException:
            pass
    except dbus.DBusException as exc:
        print(f"[kwin-query] {exc}", file=sys.stderr)
    finally:
        state.loop = None
        try:
            os.unlink(path)
        except OSError:
            pass
        bus.release_name(name)
    return state.out


def main() -> int:
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    state = _State()

    class _Cursor(dbus.service.Object):
        @dbus.service.method(_kwin_cursor.IFACE, in_signature="ii", out_signature="")
        def Report(self, x, y):  # noqa: N802 - DBus method name
            state.report(f"{int(x)} {int(y)}")

    class _Active(dbus.service.Object):
        @dbus.service.method(_kwin_active.IFACE, in_signature="ss", out_signature="")
        def Report(self, cls, caption):  # noqa: N802
            # A page title is attacker-controlled; a line break in it would
            # split the answer and desync every later query.
            state.report(f"{_one_line(str(cls))}\t{_one_line(str(caption))}")

    _objs = (_Cursor(bus, _kwin_cursor.OBJ_PATH), _Active(bus, _kwin_active.OBJ_PATH))
    scripting = dbus.Interface(
        bus.get_object("org.kde.KWin", "/Scripting"), "org.kde.kwin.Scripting"
    )
    for line in sys.stdin:
        helper = _HELPERS.get(line.strip())
        out = _query(bus, scripting, helper, state) if helper else None
        print(out or "", flush=True)
    del _objs
    return 0


if __name__ == "__main__":
    sys.exit(main())
