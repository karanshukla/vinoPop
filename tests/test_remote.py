import os
import threading

import pytest

from core import remote


@pytest.mark.parametrize(
    ("argv", "action"),
    [
        (["--no-tray"], None),
        (["--trigger", "popup"], "popup"),
        (["--debug", "--trigger=ocr"], "ocr"),
        (["--trigger"], ""),
    ],
)
def test_trigger_arg(argv, action):
    assert remote.trigger_arg(argv) == action


def test_unknown_action_is_refused_before_the_bus(capsys):
    assert remote.send("explode") == 2
    assert "one of popup, ocr, clipboard" in capsys.readouterr().err


@pytest.mark.skipif(not os.environ.get("DBUS_SESSION_BUS_ADDRESS"), reason="needs a session bus")
def test_round_trip_over_the_session_bus(monkeypatch):
    gi = pytest.importorskip("gi")
    from gi.repository import GLib

    del gi
    # A name of our own, so a vinoPop running on this desktop isn't poked.
    monkeypatch.setattr(remote, "BUS_NAME", f"io.github.karanshukla.vinoPop.Test{os.getpid()}")
    loop = GLib.MainLoop()
    fired: list[str] = []
    owner = remote.serve({"popup": lambda: (fired.append("popup"), loop.quit())})
    result: list[int] = []

    def client() -> None:
        # Retry until the name is owned (serve() acquires it asynchronously).
        for _ in range(50):
            code = remote.send("popup")
            if code == 0:
                result.append(code)
                return
            threading.Event().wait(0.05)
        result.append(code)
        GLib.idle_add(loop.quit)

    GLib.timeout_add_seconds(10, loop.quit)
    threading.Thread(target=client, daemon=True).start()
    loop.run()
    from gi.repository import Gio

    Gio.bus_unown_name(owner)
    assert result == [0]
    assert fired == ["popup"]
