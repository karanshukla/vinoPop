"""Read aloud as an MPRIS media player, while it talks.

The desktop's media controls (the keyboard's play/pause and stop keys,
KDE's media widget, GNOME's notification-area player, the lock screen)
find players on the session bus by their org.mpris.MediaPlayer2.* name.
vinoPop owns one only for as long as the voice plays. vinoSpeak can stop
but not pause, so Pause, PlayPause and Stop all stop it.

Only GLib/Gio here, like core/remote.py.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("vinopop")

BUS_NAME = "org.mpris.MediaPlayer2.vinoPop"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
TRACK_ID = "/io/github/karanshukla/vinoPop/speech"
IDENTITY = "vinoPop"
DESKTOP_ENTRY = "io.github.karanshukla.vinoPop"
ART = Path(__file__).resolve().parent.parent / "icons" / "vinopop-vinospeak.svg"
TITLE_CHARS = 80

_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg type="x" name="Offset" direction="in"/></method>
    <method name="SetPosition">
      <arg type="o" name="TrackId" direction="in"/>
      <arg type="x" name="Position" direction="in"/>
    </method>
    <method name="OpenUri"><arg type="s" name="Uri" direction="in"/></method>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Rate" type="d" access="read"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="read"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""

_STOPS = {"Pause", "PlayPause", "Stop"}


def title(text: str) -> str:
    """The track title: the start of what's being read, on one line."""
    text = " ".join(text.split())
    return text if len(text) <= TITLE_CHARS else text[: TITLE_CHARS - 1].rstrip() + "…"


def properties(text: str) -> dict[str, dict[str, object]]:
    """Every property, by interface, as GLib.Variants."""
    from gi.repository import GLib

    v = GLib.Variant
    metadata = {
        "mpris:trackid": v("o", TRACK_ID),
        "xesam:title": v("s", title(text)),
        "xesam:artist": v("as", ["vinoSpeak"]),
    }
    if ART.is_file():
        metadata["mpris:artUrl"] = v("s", ART.as_uri())
    return {
        "org.mpris.MediaPlayer2": {
            "CanQuit": v("b", False),
            "CanRaise": v("b", False),
            "HasTrackList": v("b", False),
            "Identity": v("s", IDENTITY),
            "DesktopEntry": v("s", DESKTOP_ENTRY),
            "SupportedUriSchemes": v("as", []),
            "SupportedMimeTypes": v("as", []),
        },
        "org.mpris.MediaPlayer2.Player": {
            "PlaybackStatus": v("s", "Playing"),
            "Rate": v("d", 1.0),
            "Metadata": v("a{sv}", metadata),
            "Volume": v("d", 1.0),
            "Position": v("x", 0),
            "MinimumRate": v("d", 1.0),
            "MaximumRate": v("d", 1.0),
            "CanGoNext": v("b", False),
            "CanGoPrevious": v("b", False),
            "CanPlay": v("b", False),
            "CanPause": v("b", True),
            "CanSeek": v("b", False),
            "CanControl": v("b", True),
        },
    }


def playing(text: str, on_stop: Callable[[], None]) -> Callable[[], None]:
    """Show `text` as the playing track until the returned function is
    called; a stop from the desktop's media controls calls on_stop. Runs on
    the GLib main loop, so it is safe from any thread."""
    from gi.repository import Gio, GLib

    props = properties(text)
    node = Gio.DBusNodeInfo.new_for_xml(_XML)
    state: dict = {"ended": False}

    def on_call(_conn, _sender, _path, _iface, method, _params, invocation) -> None:
        invocation.return_value(None)
        if method in _STOPS:
            print(f"[mpris] {method}")
            on_stop()

    def get_property(_conn, _sender, _path, iface, name):
        return props[iface][name]

    def on_bus(conn, _name) -> None:
        ids = []
        for info in node.interfaces:
            try:
                ids.append(conn.register_object(OBJECT_PATH, info, on_call, get_property, None))
            except Exception as exc:  # noqa: BLE001
                log.warning("[mpris] could not export %s: %s", info.name, exc)
        state["conn"], state["ids"] = conn, ids

    def _own() -> bool:
        if state["ended"]:
            return False
        try:
            state["owner"] = Gio.bus_own_name(
                Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE, on_bus, None, None
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("[mpris] no session bus: %s", exc)
        return False

    def _unown() -> bool:
        state["ended"] = True
        conn = state.pop("conn", None)
        for object_id in state.pop("ids", []):
            conn.unregister_object(object_id)
        owner = state.pop("owner", 0)
        if owner:
            Gio.bus_unown_name(owner)
        return False

    GLib.idle_add(_own)
    return lambda: GLib.idle_add(_unown)
