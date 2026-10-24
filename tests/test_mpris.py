import os
import threading

import pytest

from core import mpris


def test_title_is_the_start_of_the_text_on_one_line():
    assert mpris.title("Hello\n  there.") == "Hello there."
    long = mpris.title("word " * 40)
    assert len(long) == mpris.TITLE_CHARS and long.endswith("…")


def test_properties_say_it_can_stop_but_not_seek():
    pytest.importorskip("gi")
    player = mpris.properties("Hello.")["org.mpris.MediaPlayer2.Player"]
    assert player["PlaybackStatus"].unpack() == "Playing"
    assert player["CanPause"].unpack() and player["CanControl"].unpack()
    assert not player["CanSeek"].unpack() and not player["CanGoNext"].unpack()
    assert player["Metadata"].unpack()["xesam:title"] == "Hello."


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
    end = mpris.playing("Hello there.", lambda: (stops.append(True), loop.quit()))
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
