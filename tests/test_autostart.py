from pathlib import Path

import autostart


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
    import xdg_paths

    assert autostart.AUTOSTART_DIR == xdg_paths.CONFIG_HOME / "autostart"
