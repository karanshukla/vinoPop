"""core/update.py: the version comparison, the tag check and running the upgrade."""

from pathlib import Path

import pytest

from core import update


def test_versions_compare_as_numbers():
    assert update.version_parts("v1.3.0") == (1, 3, 0)
    assert update.version_parts("1.3.0") == (1, 3, 0)
    assert update.version_parts("1.3.0-2-gabc123-dirty") is None
    assert update.is_newer("1.3.0", "v1.4.0")
    assert update.is_newer("1.3.0", "v1.10.0")
    assert not update.is_newer("1.4.0", "v1.4.0")
    assert not update.is_newer("1.10.0", "v1.9.9")
    # A checkout past a tag, or no version at all, is never told to update.
    assert not update.is_newer("1.3.0-2-gabc123", "v9.0.0")
    assert update.is_newer("0.0.0", "v1.0.0")


def test_only_a_plausible_tag_is_taken():
    assert update.latest_tag('{"tag_name": "v1.4.0", "name": "vinoPop 1.4.0"}') == "v1.4.0"
    assert update.latest_tag('{"tag_name": "v1; rm -rf ~"}') is None
    assert update.latest_tag('{"message": "Not Found"}') is None
    assert update.latest_tag("not json") is None
    assert update.latest_tag("[]") is None


def test_upgrade_command_is_the_installed_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_BIN_HOME", str(tmp_path))
    assert update.upgrade_command() is None
    script = tmp_path / "vinopop-upgrade"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    assert update.upgrade_command() == script


def _script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "vinopop-upgrade"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return script


def test_install_passes_the_tag(tmp_path):
    update.install("v1.4.0", _script(tmp_path, 'test "$VINO_UPDATE_TAG" = v1.4.0'))


def test_a_failed_install_says_the_installers_reason(tmp_path):
    script = _script(
        tmp_path, "echo downloading >&2; echo '✗ Checksum mismatch' >&2; echo done >&2; exit 1"
    )
    with pytest.raises(RuntimeError, match="^Checksum mismatch$"):
        update.install("v1.4.0", script)


def test_version_falls_back_to_the_release_tarballs_file(tmp_path, monkeypatch):
    from core import version

    monkeypatch.setattr(version, "_HERE", tmp_path)
    assert version.get_version() == "0.0.0"
    (tmp_path / "VERSION").write_text("v1.4.0\n")
    assert version.get_version() == "1.4.0"
