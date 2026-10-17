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


# ---- the real Qt child (offscreen) -------------------------------------------


@pytest.fixture
def real_tray(tmp_path, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    # The child derives its socket dir from XDG_CACHE_HOME (xdg_paths adds "vinopop")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(tray, "SOCKET_DIR", tmp_path / "vinopop")
    t = tray.Tray(*NOOP)
    if not t._connected:
        t.shutdown()
        pytest.skip("Qt tray process did not start in this environment")
    yield t
    t.shutdown()


def _poll_until(t, predicate, seconds=8.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        t.poll()
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_a_crashed_tray_process_is_noticed_and_restarted(real_tray):
    import os
    import signal

    old_pid = real_tray._proc.pid
    os.kill(old_pid, signal.SIGKILL)
    real_tray._proc.wait()
    assert _poll_until(real_tray, lambda: real_tray._connected and real_tray._proc.pid != old_pid)


def test_the_tray_process_accepts_a_new_connection_after_the_daemon_hangs_up(real_tray):
    pid = real_tray._proc.pid
    real_tray._sock.close()  # the daemon side goes away; the child must free its slot
    assert _poll_until(real_tray, lambda: real_tray._connected)
    assert real_tray._proc.pid == pid  # same child, new connection
    # ...and the new connection works: the child answers a ping
    events = []
    real_tray._dispatch = lambda event, value: events.append(event)
    tray._send_message(real_tray._sock, {"cmd": "ping"})
    assert _poll_until(real_tray, lambda: "pong" in events)
