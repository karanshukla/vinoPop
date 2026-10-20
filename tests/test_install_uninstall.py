"""install.sh --uninstall [--purge] in a throwaway HOME. Needs no GTK: the
uninstall path runs before the dependency checks."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parent.parent / "install.sh"

SHORTCUTS = """[kwin]
Walk Through Windows=Alt+Tab,Alt+Tab,Walk Through Windows

[vinopop]
_k_friendly_name=vinoPop
vinopop-popup=Meta+Shift+P,none,Show the popup
vinopop-clipboard=Meta+Shift+V,none,Clipboard history
vinopop-ocr=Meta+Shift+O,none,Screen OCR
"""


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    for d in (
        ".config/vinopop/plugins",
        ".cache/vinopop/ov-cache/llm",
        ".local/share/vinopop/models",
        ".local/state/vinopop",
        "intel",
    ):
        (home / d).mkdir(parents=True)
    (home / ".local/share/vinopop/models/weights.bin").write_bytes(b"x" * 4096)
    (home / ".config/kglobalshortcutsrc").write_text(SHORTCUTS)
    for name in ("openvino_telemetry", "openvino_ga_cid", "stats"):
        (home / "intel" / name).write_text("1")

    # Stubs: nothing may kill a real vinoPop, talk to a real session bus or
    # touch a real KDE config. kwriteconfig6 logs its arguments.
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "kwriteconfig6.log"
    for tool in ("pkill", "update-desktop-database", "gdbus"):
        (stubs / tool).write_text("#!/bin/sh\nexit 0\n")
    (stubs / "kwriteconfig6").write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    for tool in stubs.iterdir():
        tool.chmod(0o755)
    return home, stubs, log


def _run(home, stubs, *args, path=None, stdin=None):
    env = {"HOME": str(home), "PATH": path or f"{stubs}:{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
    )


def test_uninstall_lists_leftovers_and_keeps_them(home):
    home, stubs, log = home
    out = _run(home, stubs, "--uninstall")
    assert out.returncode == 0, out.stderr
    for d in (".config/vinopop", ".cache/vinopop", ".local/share/vinopop", ".local/state/vinopop"):
        assert str(home / d) in out.stdout
        assert (home / d).is_dir()
    assert str(home / "intel") in out.stdout
    assert "[vinopop] shortcuts" in out.stdout
    assert "--purge" in out.stdout
    assert not log.exists()


def test_purge_with_yes_deletes_them(home):
    home, stubs, log = home
    out = _run(home, stubs, "--uninstall", "--purge", "--yes")
    assert out.returncode == 0, out.stderr
    for d in (".config/vinopop", ".cache/vinopop", ".local/share/vinopop", ".local/state/vinopop"):
        assert not (home / d).exists()
    assert not (home / "intel").exists()
    deleted = log.read_text().splitlines()
    assert len(deleted) == 4
    assert all("--group vinopop" in line and line.endswith("--delete") for line in deleted)
    assert any("--key vinopop-popup" in line for line in deleted)


def test_purge_without_a_terminal_or_yes_deletes_nothing(home):
    home, stubs, _ = home
    out = _run(home, stubs, "--uninstall", "--purge", stdin="y\n")
    assert out.returncode == 1
    assert "--yes" in out.stdout
    assert (home / ".local/share/vinopop/models/weights.bin").is_file()


def test_intel_with_other_files_is_kept(home):
    home, stubs, _ = home
    (home / "intel" / "some-other-tool").mkdir()
    out = _run(home, stubs, "--uninstall", "--purge", "--yes")
    assert out.returncode == 0, out.stderr
    assert (home / "intel" / "openvino_telemetry").is_file()
    assert not (home / ".config/vinopop").exists()


@pytest.mark.skipif(shutil.which("kwriteconfig6") is not None, reason="real kwriteconfig6 on PATH")
def test_purge_without_kwriteconfig6_says_what_to_remove(home, tmp_path):
    home, stubs, _ = home
    (stubs / "kwriteconfig6").unlink()
    out = _run(home, stubs, "--uninstall", "--purge", "--yes")
    assert out.returncode == 0, out.stderr
    assert "delete the [vinopop] group" in out.stdout
    assert "[vinopop]" in (home / ".config/kglobalshortcutsrc").read_text()


def test_nothing_left(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "pkill").write_text("#!/bin/sh\nexit 0\n")
    (stubs / "pkill").chmod(0o755)
    out = _run(home, stubs, "--uninstall")
    assert out.returncode == 0, out.stderr
    assert "nothing else" in out.stdout


def test_purge_alone_is_refused(home):
    home, stubs, _ = home
    out = _run(home, stubs, "--purge")
    assert out.returncode == 2
    assert (home / ".config/vinopop").is_dir()
