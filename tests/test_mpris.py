import os
import threading

import pytest

from core import mpris


def test_properties_say_it_can_stop_but_not_seek():
    pytest.importorskip("gi")
    player = mpris.properties()["org.mpris.MediaPlayer2.Player"]
    assert player["PlaybackStatus"].unpack() == "Playing"
    assert player["CanPause"].unpack() and player["CanControl"].unpack()
    assert not player["CanSeek"].unpack() and not player["CanGoNext"].unpack()


def test_the_title_never_shows_the_selection():
    # The lock screen and media widgets show it to anyone at the machine.
    pytest.importorskip("gi")
    metadata = mpris.properties()["org.mpris.MediaPlayer2.Player"]["Metadata"].unpack()
    assert metadata["xesam:title"] == "Reading aloud"


@pytest.mark.skipif(not os.environ.get("DBUS_SESSION_BUS_ADDRESS"), reason="needs a session bus")
def test_a_media_key_stop_reaches_the_voice(monkeypatch):
    gi = pytest.importorskip("gi")
    from gi.repository import Gio, GLib

    del gi
    # A name of our own, so a player running on this desktop isn't poked.
    name = f"org.mpris.MediaPlayer2.vinoPopTest{os.getpid()}"
    monkeypatch.setattr(mpris, "BUS_NAME", name)
    loop = GLib.MainLoop()
    stops: list[bool] = []
    end = mpris.playing(lambda: (stops.append(True), loop.quit()))
    seen: dict = {}

    def client() -> None:
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        for _ in range(50):
            try:
                reply = conn.call_sync(
                    name,
                    mpris.OBJECT_PATH,
                    "org.freedesktop.DBus.Properties",
                    "Get",
                    GLib.Variant("(ss)", ("org.mpris.MediaPlayer2", "Identity")),
                    None,
                    Gio.DBusCallFlags.NO_AUTO_START,
                    1000,
                    None,
                )
                seen["identity"] = reply.unpack()[0]
                break
            except GLib.Error:
                threading.Event().wait(0.05)
        conn.call_sync(
            name,
            mpris.OBJECT_PATH,
            "org.mpris.MediaPlayer2.Player",
            "PlayPause",
            None,
            None,
            Gio.DBusCallFlags.NO_AUTO_START,
            1000,
            None,
        )

    GLib.timeout_add_seconds(10, loop.quit)
    thread = threading.Thread(target=client, daemon=True)
    thread.start()
    loop.run()
    thread.join(timeout=5)
    end()
    GLib.MainContext.default().iteration(False)
    assert seen == {"identity": "vinoPop"}
    assert stops == [True]
