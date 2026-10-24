"""A second `vinopop` (e.g. from the app launcher) opens the running
instance's Settings, and only says "already running" when it can't."""

import fcntl
import os

import pytest

pytest.importorskip("gi")
pytest.importorskip("Xlib")


@pytest.fixture(scope="module")
def main_mod():
    mp = pytest.MonkeyPatch()
    mp.setenv("GDK_BACKEND", "x11")
    try:
        import main
    finally:
        mp.undo()
    return main


@pytest.fixture
def held_lock(main_mod, tmp_path, monkeypatch):
    lock = tmp_path / "vinopop.lock"
    monkeypatch.setattr(main_mod, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(main_mod, "LOCK_FILE", lock)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    yield
    os.close(fd)


def _run_second_copy(main_mod, monkeypatch, send_code):
    from core import actions, remote

    sent, notified = [], []
    monkeypatch.setattr(remote, "send", lambda action: (sent.append(action), send_code)[1])
    monkeypatch.setattr(actions, "notify", lambda *a, **k: notified.append(a))
    with pytest.raises(SystemExit) as exit_info:
        main_mod._acquire_single_instance_lock()
    return exit_info.value.code, sent, notified


def test_second_copy_opens_settings_in_the_running_one(main_mod, held_lock, monkeypatch):
    code, sent, notified = _run_second_copy(main_mod, monkeypatch, send_code=0)
    assert (code, sent, notified) == (0, ["settings"], [])


def test_second_copy_notifies_when_the_running_one_does_not_answer(
    main_mod, held_lock, monkeypatch
):
    code, sent, notified = _run_second_copy(main_mod, monkeypatch, send_code=1)
    assert (code, sent) == (0, ["settings"])
    assert notified[0][0] == "vinoPop is already running"
