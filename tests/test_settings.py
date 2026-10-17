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


def test_invalid_utf8_falls_back_to_defaults_and_is_backed_up(tmp_path):
    path = tmp_path / "settings.json"
    path.write_bytes(b'{"theme": "light", "note": "caf\xe9"}')  # a latin-1 hand edit
    s = Settings(path)
    assert s.get("theme") == DEFAULTS["theme"]
    s.save()
    assert (tmp_path / "settings.json.bak").read_bytes().endswith(b'caf\xe9"}')


def test_non_object_file_is_backed_up_before_a_save_replaces_it(tmp_path):
    for content in ("[]", '"x"', "42"):
        path = tmp_path / "settings.json"
        path.write_text(content)
        s = Settings(path)
        assert s.get("theme") == DEFAULTS["theme"]
        s.save()
        assert (tmp_path / "settings.json.bak").read_text() == content


def test_save_writes_only_what_the_user_set(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(path)
    s.ensure_written()
    assert json.loads(path.read_text()) == {settings.VERSION_KEY: settings.SETTINGS_VERSION}
    s.set("theme", "light")
    s.save()
    assert json.loads(path.read_text()) == {
        "theme": "light",
        settings.VERSION_KEY: settings.SETTINGS_VERSION,
    }


def test_a_changed_default_reaches_users_who_never_set_it(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    s = Settings(path)
    s.ensure_written()
    s.set("theme", "light")
    s.save()
    monkeypatch.setitem(DEFAULTS, "popup_overflow_mode", "wrap")
    assert Settings(path).get("popup_overflow_mode") == "wrap"


def _old_file(path, **overrides):
    """settings.json as every earlier version wrote it: all the defaults."""
    path.write_text(json.dumps({**DEFAULTS, **overrides, "my_note": "keep me"}))


def test_migration_drops_stored_defaults_once(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    _old_file(path, theme="light", hotkey="ctrl+alt+y")
    s = Settings(path)
    s.ensure_written()
    on_disk = json.loads(path.read_text())
    assert on_disk["theme"] == "light"
    assert on_disk["hotkey"] == "ctrl+alt+y"
    assert on_disk["my_note"] == "keep me"
    assert "popup_overflow_mode" not in on_disk
    assert "clipboard_history_size" not in on_disk
    assert on_disk[settings.VERSION_KEY] == settings.SETTINGS_VERSION
    old = json.loads((tmp_path / "settings.json.pre-v1").read_text())
    assert old["popup_overflow_mode"] == DEFAULTS["popup_overflow_mode"]
    # Set to today's default after the migration, it's the user's choice:
    # later loads don't strip it, so a changed default doesn't override it.
    s.set("popup_overflow_mode", DEFAULTS["popup_overflow_mode"])
    s.save()
    monkeypatch.setitem(DEFAULTS, "popup_overflow_mode", "wrap")
    again = Settings(path)
    again.ensure_written()
    assert again.get("popup_overflow_mode") == "expand"


def test_migration_compares_types_not_just_values(tmp_path):
    path = tmp_path / "settings.json"
    _old_file(path, show_on_selection=1, ai_idle_unload_minutes=5)
    s = Settings(path)
    s.ensure_written()
    on_disk = json.loads(path.read_text())
    assert on_disk["show_on_selection"] == 1  # not taken for the default True
    assert on_disk["ai_idle_unload_minutes"] == 5  # the default is 5.0


def test_migration_keeps_consent_and_privacy_switches(tmp_path, monkeypatch):
    # PR #47: a stored ai_prefetch_model True from the old default isn't
    # consent. It stays in the file, so prefetch_consented still sees it
    # without ai_prefetch_asked, and the migration never adds that flag.
    monkeypatch.setitem(DEFAULTS, "openvino_telemetry", False)  # PR #46
    path = tmp_path / "settings.json"
    _old_file(path, ai_prefetch_model=True)
    s = Settings(path)
    s.ensure_written()
    on_disk = json.loads(path.read_text())
    assert on_disk["ai_prefetch_model"] is True
    assert "ai_prefetch_asked" not in on_disk
    assert on_disk["openvino_telemetry"] is False
    for key in ("snippet_shell_enabled", "snippet_triggers_enabled", "clipboard_history_enabled"):
        assert on_disk[key] == DEFAULTS[key]


def test_doctor_reads_migrated_values_without_writing(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    _old_file(path)
    before = path.read_text()
    monkeypatch.setattr(settings, "_singleton", None)
    monkeypatch.setattr(settings, "write_defaults", False)
    monkeypatch.setattr(settings, "Settings", lambda: Settings(path))
    assert settings.get_settings().get("theme") == DEFAULTS["theme"]
    assert path.read_text() == before


def test_current_files_are_not_migrated(tmp_path):
    path = tmp_path / "settings.json"
    stored = {"theme": DEFAULTS["theme"], settings.VERSION_KEY: settings.SETTINGS_VERSION}
    path.write_text(json.dumps(stored))
    s = Settings(path)
    s.ensure_written()
    assert json.loads(path.read_text()) == stored
    assert not (tmp_path / "settings.json.pre-v1").exists()


def test_reset_forgets_everything(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"theme": "light", "my_note": "x"}))
    s = Settings(path)
    s.reset()
    s.save()
    assert json.loads(path.read_text()) == {settings.VERSION_KEY: settings.SETTINGS_VERSION}
    assert Settings(path).get("theme") == DEFAULTS["theme"]


def test_get_int_falls_back_to_the_default_on_a_bad_hand_edit(tmp_path, capsys):
    path = tmp_path / "settings.json"
    stored = {
        "clipboard_history_size": "lots",
        "min_selection_length": True,
        "max_popup_buttons": None,
        "auto_hide_leave_ms": "3000",
        "selection_debounce_ms": 200.7,
    }
    path.write_text(json.dumps(stored))
    s = Settings(path)
    assert s.get_int("clipboard_history_size") == DEFAULTS["clipboard_history_size"]
    assert s.get_int("min_selection_length") == DEFAULTS["min_selection_length"]
    assert s.get_int("max_popup_buttons") == DEFAULTS["max_popup_buttons"]
    assert "clipboard_history_size = 'lots'" in capsys.readouterr().out
    assert s.get_int("auto_hide_leave_ms") == 3000
    assert s.get_int("selection_debounce_ms") == 200


def test_get_float_rejects_nan_and_text(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"ai_idle_unload_minutes": NaN, "clipboard_poll_interval": "fast"}')
    s = Settings(path)
    assert s.get_float("ai_idle_unload_minutes") == DEFAULTS["ai_idle_unload_minutes"]
    assert s.get_float("clipboard_poll_interval") == DEFAULTS["clipboard_poll_interval"]
