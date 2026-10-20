#!/usr/bin/env python3
"""Print the CHANGELOG.md section for one version, as GitHub release notes.

    scripts/release_notes.py 1.1.0 [CHANGELOG.md]

CHANGELOG.md is wrapped at ~80 columns for reading in a terminal, but a
GitHub release turns every newline into a line break, so wrapped lines are
joined back into one line per bullet or paragraph. Exits 1 when the version
has no section. Used by .github/workflows/release.yml.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# A line that starts a block of its own rather than continuing the last one
_BLOCK_START = re.compile(r"\s*([-*+] |\d+\. |#|```|\||>|<!--)")
_LINK_DEF = re.compile(r"\[[^]]+\]: ")


def section(changelog: str, version: str) -> list[str] | None:
    header = f"## [{version}]"
    lines, found = [], False
    for line in changelog.splitlines():
        if line.startswith(header):
            found = True
            continue
        if found and (line.startswith("## [") or _LINK_DEF.match(line)):
            break
        if found:
            lines.append(line)
    return lines if found else None


def unwrap(lines: list[str]) -> list[str]:
    out: list[str] = []
    fenced = False
    for line in lines:
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append(line)
            continue
        joinable = (
            not fenced
            and out
            and out[-1].strip()
            and not out[-1].lstrip().startswith(("```", "#", "|"))
            and line.strip()
            and not _BLOCK_START.match(line)
        )
        if joinable:
            out[-1] = f"{out[-1].rstrip()} {line.strip()}"
        else:
            out.append(line)
    return out


def notes(changelog: str, version: str) -> str | None:
    lines = section(changelog, version)
    if lines is None:
        return None
    return "\n".join(unwrap(lines)).strip() + "\n"


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    path = Path(argv[1] if len(argv) == 2 else "CHANGELOG.md")
    text = notes(path.read_text(encoding="utf-8"), argv[0])
    if not text or not text.strip():
        print(f"{path} has no '## [{argv[0]}]' section", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
