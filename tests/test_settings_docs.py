"""docs/how-it-works.md#settings is where each setting is explained (the
comments that used to sit on core.settings.DEFAULTS moved there), so the
table and DEFAULTS have to list the same keys."""

import re
from pathlib import Path

from core.settings import DEFAULTS

DOC = Path(__file__).resolve().parent.parent / "docs" / "how-it-works.md"


def _documented_keys() -> set[str]:
    settings = DOC.read_text(encoding="utf-8").split("## Settings", 1)[1].split("\n### ", 1)[0]
    return set(re.findall(r"^\| `([a-z_]+)` \|", settings, re.MULTILINE))


def test_every_setting_has_a_row_in_the_docs():
    assert set(DEFAULTS) - _documented_keys() == set()


def test_the_docs_list_no_setting_that_does_not_exist():
    assert _documented_keys() - set(DEFAULTS) == set()
