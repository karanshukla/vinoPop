"""Each JSON file the app rewrites in place: the bytes it holds, the mode
it is created with, and the temp file it is swapped in from. They differ
per file on purpose, and one helper (core/json_file.py) writes all three."""

from __future__ import annotations

import json
import os
import stat

import pytest

from core.settings import Settings
from plugin_system import recipe_loader
from vino_ai import failures

DATA = {"name": "søk", "n": [1, 2]}


@pytest.fixture
def swaps(monkeypatch):
    """(temp name, final name, temp bytes, temp mode) of every os.replace."""
    seen = []
    real = os.replace

    def replace(src, dst):
        mode = stat.S_IMODE(os.stat(src).st_mode)
        with open(src, "rb") as f:
            seen.append((os.path.basename(src), os.path.basename(dst), f.read(), mode))
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    return seen


@pytest.fixture
def umask_022():
    old = os.umask(0o022)
    yield
    os.umask(old)


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_settings_are_swapped_in_from_a_private_dot_tmp(tmp_path, swaps, umask_022):
    path = tmp_path / "settings.json"
    s = Settings(path)
    s.set("theme", "søk")
    s.save()
    body = '{\n  "theme": "søk",\n  "_settings_version": 1\n}\n'
    assert swaps == [("settings.json.tmp", "settings.json", body.encode(), 0o600)]
    assert _mode(path) == 0o600
    assert os.listdir(tmp_path) == ["settings.json"]


def test_a_recipe_is_swapped_in_from_dot_tmp_with_the_umasks_mode(
    tmp_path, swaps, umask_022, monkeypatch
):
    monkeypatch.setattr(recipe_loader, "RECIPES_DIR", tmp_path)
    path = recipe_loader.save_recipe(DATA, target_path=tmp_path / "search.json")
    body = '{\n  "name": "søk",\n  "n": [\n    1,\n    2\n  ]\n}\n'
    assert swaps == [("search.json.tmp", "search.json", body.encode(), 0o644)]
    assert _mode(path) == 0o644
    assert os.listdir(tmp_path) == ["search.json"]


def test_device_failures_are_swapped_in_from_a_per_process_tmp(tmp_path, swaps, umask_022):
    path = tmp_path / "state" / "device-failures.json"
    failures._write({"NPU": DATA}, path)
    body = json.dumps({"NPU": DATA}, indent=2)
    assert "s\\u00f8k" in body and not body.endswith("\n")
    tmp = f"device-failures.json.{os.getpid()}.tmp"
    assert swaps == [(tmp, "device-failures.json", body.encode(), 0o644)]
    assert _mode(path) == 0o644
    assert os.listdir(path.parent) == ["device-failures.json"]


def test_device_failures_that_cannot_be_saved_are_logged_not_raised(tmp_path, caplog):
    blocker = tmp_path / "state"
    blocker.write_text("a file where the folder should be")
    with caplog.at_level("WARNING", logger="vinopop"):
        failures._write({"NPU": DATA}, blocker / "device-failures.json")
    assert "couldn't save device failures" in caplog.text


def test_settings_that_cannot_be_swapped_in_raise_and_keep_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    s = Settings(path)
    s.set("theme", "light")
    s.save()
    before = path.read_bytes()

    def refuse(_src, _dst):
        raise OSError("read-only")

    monkeypatch.setattr(os, "replace", refuse)
    s.set("theme", "dark")
    with pytest.raises(OSError):
        s.save()
    assert path.read_bytes() == before


@pytest.mark.parametrize("save", ["settings", "recipe"])
def test_a_save_survives_a_filesystem_without_fsync(tmp_path, monkeypatch, save):
    def no_fsync(_fd):
        raise OSError("not supported")

    monkeypatch.setattr(os, "fsync", no_fsync)
    if save == "settings":
        Settings(tmp_path / "settings.json").save()
        assert (tmp_path / "settings.json").is_file()
    else:
        monkeypatch.setattr(recipe_loader, "RECIPES_DIR", tmp_path)
        recipe_loader.save_recipe(DATA, target_path=tmp_path / "r.json")
        assert json.loads((tmp_path / "r.json").read_text()) == DATA


def test_settings_and_recipes_are_fsynced_and_device_failures_are_not(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    failures._write({}, tmp_path / "device-failures.json")
    assert synced == []
    Settings(tmp_path / "settings.json").save()
    assert len(synced) == 1
    monkeypatch.setattr(recipe_loader, "RECIPES_DIR", tmp_path)
    recipe_loader.save_recipe(DATA, target_path=tmp_path / "r.json")
    assert len(synced) == 2
