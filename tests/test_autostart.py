from pathlib import Path

from core import autostart


def test_plain_paths_are_left_bare():
    assert autostart._exec_quote("/usr/bin/python3") == "/usr/bin/python3"


def test_spaces_use_double_quotes_not_single():
    quoted = autostart._exec_quote("/home/me/My Code/vinopop/main.py")
    assert quoted == '"/home/me/My Code/vinopop/main.py"'
    assert "'" not in quoted


def test_specials_are_escaped_per_the_desktop_entry_spec():
    assert autostart._exec_quote('a"b') == '"a\\\\"b"'
    assert autostart._exec_quote("$HOME") == '"\\\\$HOME"'
    assert autostart._exec_quote("100%") == '"100%%"'


def test_set_enabled_writes_and_removes_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart, "AUTOSTART_DIR", tmp_path / "autostart")
    monkeypatch.setattr(autostart, "AUTOSTART_FILE", tmp_path / "autostart" / "vinopop.desktop")
    monkeypatch.setattr(
        autostart, "LEGACY_AUTOSTART_FILE", tmp_path / "autostart" / "linuxpop.desktop"
    )
    assert autostart.set_enabled(True, main_py=Path("/opt/my code/main.py"))
    assert autostart.is_enabled()
    text = autostart.AUTOSTART_FILE.read_text()
    assert text.startswith("[Desktop Entry]")
    assert '"/opt/my code/main.py"' in text
    assert autostart.set_enabled(False)
    assert not autostart.is_enabled()


def test_autostart_dir_follows_xdg_config_home():
    from core import xdg_paths

    assert autostart.AUTOSTART_DIR == xdg_paths.CONFIG_HOME / "autostart"


def _point_at(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart, "AUTOSTART_DIR", tmp_path)
    monkeypatch.setattr(autostart, "AUTOSTART_FILE", tmp_path / "vinopop.desktop")
    monkeypatch.setattr(autostart, "LEGACY_AUTOSTART_FILE", tmp_path / "linuxpop.desktop")


def test_set_enabled_defaults_to_the_launcher(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    assert autostart.set_enabled(True)
    exec_line = next(
        ln for ln in autostart.AUTOSTART_FILE.read_text().splitlines() if ln.startswith("Exec=")
    )
    assert exec_line.endswith("/vinopop") or exec_line.endswith('/vinopop"')
    assert autostart.LAUNCHER.is_file()


def test_main_py_entries_are_migrated_once(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    autostart.AUTOSTART_FILE.write_text(
        '[Desktop Entry]\nExec=/usr/bin/python3 "/home/me/My Code/vinoPop/main.py"\n'
    )
    assert autostart.migrate_from_main_py()
    assert "main.py" not in autostart.AUTOSTART_FILE.read_text()
    assert not autostart.migrate_from_main_py()


def test_no_entry_means_nothing_to_migrate(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    assert not autostart.migrate_from_main_py()
    assert not autostart.AUTOSTART_FILE.exists()
