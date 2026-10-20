"""install.sh against a checkout path full of characters the launchers must escape."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
pytest.importorskip("Xlib")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Gtk  # noqa: F401
except (ImportError, ValueError):
    pytest.skip("needs GTK 4 + libadwaita", allow_module_level=True)

REPO = Path(__file__).resolve().parent.parent
APP_ID = "io.github.karanshukla.vinoPop"


@pytest.fixture
def installed(tmp_path):
    checkout = tmp_path / 'my $HOME `x` "q" it\'s 100%'
    skip = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".*_cache", "tests", "docs")
    shutil.copytree(REPO, checkout, ignore=skip)

    # The clipboard tools install.sh checks for, and a python reached by a
    # bare name on PATH (VINOPOP_PYTHON=python3.12 style)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for tool in ("xclip", "xdotool", "xdg-open", "wl-copy", "wl-paste"):
        (stubs / tool).write_text("#!/bin/sh\n")
        (stubs / tool).chmod(0o755)
    (stubs / "vinopop-test-python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (stubs / "vinopop-test-python").chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "VINOPOP_PYTHON": "vinopop-test-python",
    }
    subprocess.run(["bash", str(checkout / "install.sh")], env=env, check=True, capture_output=True)
    return checkout, home, stubs


def _exec_argv(desktop_file: Path) -> list[str]:
    kf = GLib.KeyFile()
    kf.load_from_file(str(desktop_file), GLib.KeyFileFlags.NONE)
    _, argv = GLib.shell_parse_argv(kf.get_string("Desktop Entry", "Exec").replace("%%", "%"))
    return argv


def test_desktop_exec_lines_survive_special_characters(installed):
    checkout, home, stubs = installed
    for f in (
        home / ".config/autostart/vinopop.desktop",
        home / f".local/share/applications/{APP_ID}.desktop",
    ):
        assert _exec_argv(f) == [str(stubs / "vinopop-test-python"), str(checkout / "vinopop")]


def test_autostart_icon_is_the_installed_one(installed):
    _, home, _ = installed
    text = (home / ".config/autostart/vinopop.desktop").read_text()
    assert f"Icon={APP_ID}\n" in text
    assert (home / f".local/share/icons/hicolor/scalable/apps/{APP_ID}.svg").is_file()


def test_sh_launcher_runs_from_the_odd_path(installed):
    _, home, _ = installed
    out = subprocess.run(
        [str(home / ".local/bin/vinopop"), "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert out.stdout.startswith("vinoPop ")


def test_uninstall_purge_leaves_nothing_behind(installed):
    checkout, home, stubs = installed
    env = {
        "HOME": str(home),
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "VINOPOP_PYTHON": "vinopop-test-python",
    }
    subprocess.run(
        ["bash", str(checkout / "install.sh"), "--uninstall", "--purge", "--yes"],
        env=env,
        check=True,
        capture_output=True,
    )
    # update-desktop-database's index of every app's launchers isn't ours to
    # delete, but it mustn't still point at vinoPop
    mime_cache = home / ".local/share/applications/mimeinfo.cache"
    if mime_cache.exists():
        assert APP_ID not in mime_cache.read_text()
    leftover = sorted(
        str(p.relative_to(home)) for p in home.rglob("*") if not p.is_dir() and p != mime_cache
    )
    assert leftover == []
