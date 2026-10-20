"""scripts/release_notes.py: one CHANGELOG.md section, unwrapped for GitHub."""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("release_notes", REPO / "scripts/release_notes.py")
assert _spec and _spec.loader
release_notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_notes)

CHANGELOG = """# Changelog

## [Unreleased]

## [1.1.0] - 2026-10-20

Intro paragraph that
wraps onto a second line.

### Features

- **Bold lead.** A bullet that
  wraps twice and ends with
  a link ([#1](https://x/1)).
- A short bullet.

```sh
git clone x
cd x
```

| a | b |
|---|---|

## [1.0.0] - 2026-10-19

- Old.

[1.1.0]: https://x/compare
"""


def test_wrapped_lines_join_and_blocks_stay_apart():
    assert release_notes.notes(CHANGELOG, "1.1.0") == (
        "Intro paragraph that wraps onto a second line.\n"
        "\n"
        "### Features\n"
        "\n"
        "- **Bold lead.** A bullet that wraps twice and ends with a link ([#1](https://x/1)).\n"
        "- A short bullet.\n"
        "\n"
        "```sh\n"
        "git clone x\n"
        "cd x\n"
        "```\n"
        "\n"
        "| a | b |\n"
        "|---|---|\n"
    )


def test_last_section_stops_at_the_link_definitions():
    assert release_notes.notes(CHANGELOG, "1.0.0") == "- Old.\n"


def test_missing_or_empty_section_fails(tmp_path, capsys):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG)
    assert release_notes.main(["9.9.9", str(path)]) == 1
    assert release_notes.main(["Unreleased", str(path)]) == 1
    assert "no '## [9.9.9]' section" in capsys.readouterr().err


def test_real_changelog_has_no_line_left_wrapped():
    text = release_notes.notes((REPO / "CHANGELOG.md").read_text(), "1.1.0")
    assert text
    for line in text.splitlines():
        assert not line.startswith("  "), line
