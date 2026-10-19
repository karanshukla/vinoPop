"""Resolve vinoPop's version from its git tag when running from a checkout."""

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


def get_version() -> str:
    return _from_git() or "0.0.0"


__version__ = get_version()
