"""Resolve vinoPop's version: its git tag in a checkout, else the VERSION file a
release tarball carries (get.sh installs those)."""

from __future__ import annotations

import subprocess
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent


def _from_git() -> str | None:
    if not (_HERE / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=_HERE,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    tag = out.stdout.strip()
    if not tag:
        return None
    return tag[1:] if tag.startswith("v") else tag


def _from_file() -> str | None:
    try:
        text = (_HERE / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    return text[1:] if text.startswith("v") else text


def get_version() -> str:
    return _from_git() or _from_file() or "0.0.0"


__version__ = get_version()
