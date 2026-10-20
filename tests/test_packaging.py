"""pyproject.toml stays runnable with `uv run` and in step with requirements-ai.txt."""

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text())


def test_uv_does_not_try_to_build_the_app():
    # A flat layout of several packages: setuptools refuses to build it, so
    # `uv run poe check` broke on a fresh clone
    assert PYPROJECT["tool"]["uv"]["package"] is False
    assert "version" not in PYPROJECT["project"].get("dynamic", [])


def test_ai_extra_matches_requirements_ai_txt():
    lines = (REPO / "requirements-ai.txt").read_text().splitlines()
    pinned = [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]
    assert pinned == PYPROJECT["project"]["optional-dependencies"]["ai"]
