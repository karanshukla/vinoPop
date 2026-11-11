"""get.sh against a file:// release built from this tree: install, upgrade, refusal."""

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
pytest.importorskip("Xlib")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk  # noqa: F401
except (ImportError, ValueError):
    pytest.skip("needs GTK 4 + libadwaita", allow_module_level=True)

REPO = Path(__file__).resolve().parent.parent


def _release(tmp_path: Path, tag: str) -> Path:
    """A release directory as release.yml makes one: tarball, get.sh, SHA256SUMS."""
    tree = tmp_path / f"tree-{tag}" / f"vinopop-{tag}"
    skip = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".*_cache", "tests", "docs")
    shutil.copytree(REPO, tree, ignore=skip)
    (tree / "VERSION").write_text(tag)
    rel = tmp_path / f"release-{tag}"
    rel.mkdir()
    with tarfile.open(rel / f"vinopop-{tag}.tar.gz", "w:gz") as tar:
        tar.add(tree, arcname=tree.name)
    shutil.copy(REPO / "get.sh", rel / "get.sh")
    sums = [
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in sorted(rel.iterdir())
    ]
    (rel / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    return rel


@pytest.fixture
def env(tmp_path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for tool in ("xclip", "xdg-open", "wl-copy", "wl-paste"):
        (stubs / tool).write_text("#!/bin/sh\n")
        (stubs / tool).chmod(0o755)
    (stubs / "vinopop-test-python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (stubs / "vinopop-test-python").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    return {
        "HOME": str(home),
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "VINOPOP_PYTHON": "vinopop-test-python",
    }


def _get(rel: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(REPO / "get.sh"), *args],
        env={**env, "VINOPOP_RELEASE_BASE": f"file://{rel}"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


def test_installs_behind_a_link_and_upgrades_by_moving_it(tmp_path, env):
    home = Path(env["HOME"])
    root = home / ".local/share/vinopop"
    first = _get(_release(tmp_path, "v9.9.0"), env, "v9.9.0")
    assert first.returncode == 0, first.stderr
    assert os.readlink(root / "app") == "v9.9.0"
    assert (root / "app/VERSION").read_text() == "v9.9.0"
    # The launchers run the link, not the versioned directory.
    launcher = (
        home / ".local/share/applications/io.github.karanshukla.vinoPop.desktop"
    ).read_text()
    assert str(root / "app") in launcher and "v9.9.0" not in launcher
    upgrade = home / ".local/bin/vinopop-upgrade"
    assert upgrade.read_bytes() == (REPO / "get.sh").read_bytes()

    second = _get(_release(tmp_path, "v9.10.0"), env, "v9.10.0")
    assert second.returncode == 0, second.stderr
    assert os.readlink(root / "app") == "v9.10.0"
    # The release a running vinoPop still has open stays until the next one.
    assert (root / "v9.9.0").is_dir()
    third = _get(_release(tmp_path, "v9.11.0"), env, "v9.11.0")
    assert third.returncode == 0, third.stderr
    assert not (root / "v9.9.0").exists() and (root / "v9.10.0").is_dir()


def test_a_tampered_tarball_changes_nothing(tmp_path, env):
    rel = _release(tmp_path, "v9.9.0")
    with open(rel / "vinopop-v9.9.0.tar.gz", "ab") as f:
        f.write(b"x")
    result = _get(rel, env, "v9.9.0")
    assert result.returncode == 1
    assert "doesn't match the release's SHA256SUMS" in result.stderr
    assert not (Path(env["HOME"]) / ".local/share/vinopop").exists()


def test_a_malformed_tag_is_refused(tmp_path, env):
    result = _get(tmp_path, env, "v1;rm")
    assert result.returncode == 1
    assert "not a release tag" in result.stderr


def test_uninstall_deletes_the_model_even_from_an_older_release(tmp_path, env):
    home = Path(env["HOME"])
    root = home / ".local/share/vinopop"
    assert _get(_release(tmp_path, "v9.9.0"), env, "v9.9.0").returncode == 0
    # An older release's install.sh --uninstall kept the model (and here does
    # nothing at all, so no real vinoPop gets stopped).
    (root / "app/install.sh").write_text("#!/bin/sh\nexit 0\n")
    (root / "models/gemma-4-E2B-it-int4-cw-ov").mkdir(parents=True)
    (home / ".cache/vinopop/ov-cache/llm").mkdir(parents=True)
    (home / ".cache/vinopop/clipboard").mkdir(parents=True)
    result = _get(tmp_path, env, "--uninstall")
    assert result.returncode == 0, result.stderr
    assert not (root / "models").exists() and not (root / "app").exists()
    assert not (home / ".cache/vinopop/ov-cache").exists()
    assert (home / ".cache/vinopop/clipboard").is_dir()
