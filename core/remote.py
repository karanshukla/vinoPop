"""`vinopop --trigger popup|ocr|clipboard`: run a hotkey's action in the
running daemon, so any desktop's own shortcut settings can bind it.

The in-app hotkeys need KGlobalAccel (KDE) or an X11 key grab. Under
XWayland a grab only fires while an X11 window has focus, so on GNOME,
sway, Hyprland or COSMIC it misses most native Wayland apps. A shortcut the
compositor itself runs a command for works with any app focused.

The daemon owns BUS_NAME on the session bus and exports one method,
Trigger(s action). Only GLib/Gio here: the client side runs from the
launcher before GTK is imported, so a key press doesn't pay for GTK.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

BUS_NAME = "io.github.karanshukla.vinoPop"
OBJECT_PATH = "/io/github/karanshukla/vinoPop"
INTERFACE = "io.github.karanshukla.vinoPop.Remote"
ACTIONS = ("popup", "ocr", "clipboard")

_XML = f"""
<node>
  <interface name="{INTERFACE}">
    <method name="Trigger">
      <arg type="s" name="action" direction="in"/>
    </method>
  </interface>
</node>
"""

_ERROR_UNKNOWN = f"{BUS_NAME}.Error.UnknownAction"


def serve(handlers: dict[str, Callable[[], None]]) -> int:
    """Own BUS_NAME and run handlers[action] on the GLib main loop for each
    Trigger call. Returns the bus_own_name id (0 when there's no session
    bus). Losing the name is only logged: the hotkeys still work."""
    from gi.repository import Gio

    node = Gio.DBusNodeInfo.new_for_xml(_XML)
    # Read now: on_bus runs later, from whichever main loop is spinning then.
    path = OBJECT_PATH

    def on_call(_conn, _sender, _path, _iface, method, params, invocation) -> None:
        action = params.unpack()[0] if method == "Trigger" else ""
        handler = handlers.get(action)
        if handler is None:
            invocation.return_dbus_error(
                _ERROR_UNKNOWN, f"unknown action {action!r}; one of {', '.join(handlers)}"
            )
            return
        invocation.return_value(None)
        print(f"[remote] --trigger {action}")
        handler()

    def on_bus(conn, _name) -> None:
        try:
            conn.register_object(path, node.interfaces[0], on_call, None, None)
        except Exception as exc:  # noqa: BLE001
            print(f"[remote] could not export {path}: {exc}")

    def on_lost(_conn, name) -> None:
        print(f"[remote] don't own {name}: vinopop --trigger won't reach this instance")

    try:
        return Gio.bus_own_name(
            Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE, on_bus, None, on_lost
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[remote] no session bus: {exc}")
        return 0


def send(action: str) -> int:
    """Client side of --trigger. Returns the process exit code."""
    if action not in ACTIONS:
        print(f"vinopop: --trigger takes one of {', '.join(ACTIONS)}", file=sys.stderr)
        return 2
    from gi.repository import Gio, GLib

    try:
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        conn.call_sync(
            BUS_NAME,
            OBJECT_PATH,
            INTERFACE,
            "Trigger",
            GLib.Variant("(s)", (action,)),
            None,
            Gio.DBusCallFlags.NO_AUTO_START,
            3000,
            None,
        )
    except GLib.Error as exc:
        remote = Gio.DBusError.get_remote_error(exc) or ""
        if remote in (
            "org.freedesktop.DBus.Error.ServiceUnknown",
            "org.freedesktop.DBus.Error.NameHasNoOwner",
        ):
            print("vinopop: vinoPop isn't running", file=sys.stderr)
        else:
            print(f"vinopop: --trigger {action} failed: {exc.message}", file=sys.stderr)
        return 1
    return 0


def trigger_arg(argv: list[str]) -> str | None:
    """The action after --trigger (or --trigger=action), "" when it's
    missing, None when there's no --trigger."""
    for i, arg in enumerate(argv):
        if arg == "--trigger":
            return argv[i + 1] if i + 1 < len(argv) else ""
        if arg.startswith("--trigger="):
            return arg.partition("=")[2]
    return None
