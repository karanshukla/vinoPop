"""Whether this is a release or a source build.

Release CI adds a VERSION file to the tarball get.sh unpacks; a git checkout
has none. So a VERSION file means a release and its absence means `dev`.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent


def version_of(stamp: str | None) -> str:
    """The release version (no leading `v`) from the VERSION file's text, or
    `dev` without one (pinned by `tests/test_update.py::test_a_release_build_reports_its_version`)."""
    text = (stamp or "").strip().removeprefix("v")
    return text or "dev"


def get_version() -> str:
    try:
        stamp = (_HERE / "VERSION").read_text(encoding="utf-8")
    except OSError:
        stamp = None
    return version_of(stamp)


__version__ = get_version()
