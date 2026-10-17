"""tray.py must never stall the GTK main loop, whatever the Qt child does."""

import time

import pytest

import tray

NOOP = [lambda *a: None] * 7


@pytest.fixture
def fake_tray(tmp_path, monkeypatch):
    monkeypatch.setattr(tray, "SOCKET_DIR", tmp_path)
    monkeypatch.setattr(tray, "_RESTART_PAUSES_S", (0.0, 0.0, 0.0))
    monkeypatch.setattr(tray.Tray, "_notify_gave_up", staticmethod(lambda: None))
    monkeypatch.setattr(tray, "_tray_preexec", lambda: None)

    def use(code: str):
        script = tmp_path / "fake_tray.py"
        script.write_text(code)
        monkeypatch.setattr(tray, "TRAY_SCRIPT", str(script))

    return use


def test_a_tray_process_that_dies_at_once_does_not_block_startup(fake_tray):
    fake_tray("import sys; sys.exit(1)\n")  # what a missing PySide6 looks like
    t0 = time.monotonic()
    t = tray.Tray(*NOOP)
    assert time.monotonic() - t0 < 2.0  # used to wait the full ~3 s
    assert not t._connected


def test_polling_a_dead_tray_never_blocks_and_gives_up(fake_tray):
    fake_tray("import sys; sys.exit(1)\n")
    t = tray.Tray(*NOOP)
    started = []
    real_start = t._start_process
    t._start_process = lambda: (started.append(1), real_start())[1]

    t0 = time.monotonic()
    for _ in range(60):
        t.poll()
        time.sleep(0.02)  # let a respawned child exit between polls
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, "each poll used to block ~3 s"
    assert t._gave_up
    assert len(started) <= len(tray._RESTART_PAUSES_S)  # bounded, not one per poll


def test_restarts_wait_out_their_pause(fake_tray, monkeypatch):
    fake_tray("import sys; sys.exit(1)\n")
    monkeypatch.setattr(tray, "_RESTART_PAUSES_S", (30.0, 30.0))
    t = tray.Tray(*NOOP)
    started = []
    t._start_process = lambda: started.append(1)
    time.sleep(0.2)
    for _ in range(20):
        t.poll()
    assert len(started) == 1  # one restart, then the 30 s pause holds the rest off


def test_a_healthy_tray_connects_and_dispatches(fake_tray, tmp_path):
    fake_tray(
        "import socket, struct, json, time, os\n"
        f"path = {str(tmp_path / 'tray.sock')!r}\n"
        "s = socket.socket(socket.AF_UNIX); s.bind(path); s.listen(1)\n"
        "c, _ = s.accept()\n"
        "raw = json.dumps({'event': 'about'}).encode()\n"
        "c.sendall(struct.pack('!I', len(raw)) + raw)\n"
        "time.sleep(2)\n"
    )
    events = []
    cbs = [lambda *a: None] * 7
    cbs[5] = lambda: events.append("about")
    t = tray.Tray(*cbs)
    try:
        assert t._connected
        for _ in range(50):
            t.poll()
            if events:
                break
            time.sleep(0.05)
        assert events == ["about"]
    finally:
        t.shutdown()
