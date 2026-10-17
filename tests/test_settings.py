import json

import settings
from settings import DEFAULTS, Settings


def test_get_returns_copies_of_mutable_defaults(tmp_path):
    s = Settings(tmp_path / "settings.json")
    s.get("disabled_plugins").append("oops")
    s.get("snippet_variables")["a"] = "b"
    assert DEFAULTS["disabled_plugins"] == []
    assert DEFAULTS["snippet_variables"] == {}
    assert s.get("disabled_plugins") == []


def test_save_creates_the_files_own_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "settings.json"
    s = Settings(path)
    s.set("theme", "light")
    s.save()
    assert json.loads(path.read_text())["theme"] == "light"
    assert Settings(path).get("theme") == "light"


def test_unknown_keys_survive_a_save(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"my_note": "keep me"}))
    s = Settings(path)
    s.save()
    assert json.loads(path.read_text())["my_note"] == "keep me"


def test_unreadable_file_is_backed_up_before_defaults_replace_it(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"theme": "light",  /* hand edit */ }')
    s = Settings(path)
    assert s.get("theme") == DEFAULTS["theme"]
    s.save()
    assert "hand edit" in (tmp_path / "settings.json.bak").read_text()


def test_write_defaults_flag_keeps_get_settings_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CONFIG_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_singleton", None)
    monkeypatch.setattr(settings, "write_defaults", False)
    # Settings() binds CONFIG_PATH at definition time, so build one explicitly
    monkeypatch.setattr(settings, "Settings", lambda: Settings(tmp_path / "settings.json"))
    settings.get_settings()
    assert not (tmp_path / "settings.json").exists()
