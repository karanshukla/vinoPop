"""The Open path action: a selected path reaches xdg-open as one argv
element, and a program or launcher opens its folder rather than running."""

import os

import pytest

from core import actions


@pytest.fixture
def spawned(monkeypatch):
    calls = []

    def fake_popen(argv, **kwargs):
        assert not kwargs.get("shell")
        calls.append(argv)

    monkeypatch.setattr(actions.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(actions, "notify", lambda *a, **k: None)
    return calls


def test_a_plain_file_opens_itself(spawned, tmp_path):
    doc = tmp_path / "notes.txt"
    doc.write_text("hi")
    actions.open_path(str(doc))
    assert spawned == [["xdg-open", str(doc)]]


def test_a_folder_opens_itself(spawned, tmp_path):
    actions.open_path(str(tmp_path))
    assert spawned == [["xdg-open", str(tmp_path)]]


def test_tilde_is_expanded_and_shell_characters_stay_in_one_argument(
    spawned, tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    odd = tmp_path / "a b; $(touch pwned) `x`.txt"
    odd.write_text("")
    actions.open_path("~/" + odd.name)
    assert spawned == [["xdg-open", str(odd)]]
    assert not (tmp_path / "pwned").exists()


@pytest.mark.parametrize("name", ["x.desktop", "setup.EXE", "app.AppImage", "run.bat", "a.jar"])
def test_a_launcher_opens_its_folder(spawned, tmp_path, name):
    launcher = tmp_path / name
    launcher.write_text("[Desktop Entry]\nExec=sh -c 'touch /tmp/pwned'\n")
    actions.open_path(str(launcher))
    assert spawned == [["xdg-open", str(tmp_path)]]


def test_an_executable_file_opens_its_folder(spawned, tmp_path):
    script = tmp_path / "install"
    script.write_text("#!/bin/sh\necho hi\n")
    os.chmod(script, 0o755)
    actions.open_path(str(script))
    assert spawned == [["xdg-open", str(tmp_path)]]


def test_a_launcher_named_relative_to_home_opens_its_folder(spawned, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Downloads" / "x.desktop").write_text("[Desktop Entry]\n")
    actions.open_path("~/Downloads/x.desktop")
    assert spawned == [["xdg-open", str(tmp_path / "Downloads")]]


def test_the_user_is_told_why(spawned, tmp_path, monkeypatch):
    told = []
    monkeypatch.setattr(actions, "notify", lambda *a, **k: told.append(a))
    (tmp_path / "x.desktop").write_text("")
    actions.open_path(str(tmp_path / "x.desktop"))
    assert len(told) == 1
    assert "x.desktop" in told[0][1]


def test_a_missing_path_still_goes_to_xdg_open(spawned, tmp_path):
    # Nothing to run; xdg-open reports the missing file itself
    actions.open_path(str(tmp_path / "gone.desktop"))
    assert spawned == [["xdg-open", str(tmp_path / "gone.desktop")]]


def test_no_xdg_open_is_not_a_crash(monkeypatch, tmp_path):
    def missing(*a, **k):
        raise FileNotFoundError("xdg-open")

    monkeypatch.setattr(actions.subprocess, "Popen", missing)
    actions.open_path(str(tmp_path))
