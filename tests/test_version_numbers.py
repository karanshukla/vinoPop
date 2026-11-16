"""The three places a dotted version is read. core.update is strict,
npu_preflight sorts driver libraries by their numeric start, and ovfetch wants a major and a
minor through int(); the cases at the end are where they part ways."""

from __future__ import annotations

import pytest

from core import update
from core.version_numbers import leading_numbers
from vino_ai import ovfetch


@pytest.mark.parametrize(
    ("text", "parts"),
    [
        ("1.3.0", (1, 3, 0)),
        ("v1.3.0", (1, 3, 0)),
        ("7", (7,)),
        ("01.002", (1, 2)),
        ("", None),
        ("v", None),
        ("vv1.2", None),
        ("V1.2", None),
        ("1..2", None),
        ("1.2.", None),
        (".1", None),
        ("1.3.0-dirty", None),
        ("1.3.0-2-gabc123", None),
        ("1.3rc1", None),
        (" 1.3", None),
        ("1.3\n", None),
        ("+1.3", None),
        ("1_0.3", None),
    ],
)
def test_an_update_version_is_digits_and_dots_only(text, parts):
    assert update.version_parts(text) == parts


@pytest.mark.parametrize(
    ("text", "parts"),
    [
        ("1.35.0", (1, 35, 0)),
        ("1", (1,)),
        ("1.35.0-rc1", (1, 35)),
        ("1.35rc.2", (1,)),
        ("1..2", (1,)),
        ("1.2.", (1, 2)),
        ("", ()),
        ("v1.2", ()),
        ("libze_intel_npu.so", ()),
        (" 1.2", ()),
        ("1.2\n", (1,)),
    ],
)
def test_a_library_version_is_its_numeric_start(text, parts):
    assert leading_numbers(text) == parts


def test_library_versions_sort_by_number_not_by_text():
    names = ["1.9.0", "1.35.0", "1.10.2"]
    assert sorted(names, key=leading_numbers) == ["1.9.0", "1.10.2", "1.35.0"]


@pytest.mark.parametrize(
    ("text", "minor"),
    [
        ("2026.4", (2026, 4)),
        ("2026.4.1.0", (2026, 4)),
        ("2026.4.dev20260805", (2026, 4)),
        ("2026.4.1-12345-abc", (2026, 4)),
        ("2026", None),
        ("2026.", None),
        (".4", None),
        ("2026.4rc1", None),
        ("v2026.4", None),
        ("", None),
        (None, None),
    ],
)
def test_an_openvino_version_is_its_major_and_minor(text, minor):
    assert ovfetch._minor(text) == minor


@pytest.mark.parametrize(
    ("text", "minor"),
    [
        (" 2026.4", (2026, 4)),
        ("2026.4\n", (2026, 4)),
        ("+2026.4", (2026, 4)),
        ("2_026.4", (2026, 4)),
    ],
)
def test_ovfetch_reads_what_int_reads_which_the_other_two_refuse(text, minor):
    assert ovfetch._minor(text) == minor
    assert len(leading_numbers(text)) < 2
    assert update.version_parts(text) is None


def test_ovfetch_reads_a_negative_number_as_a_version():
    assert ovfetch._minor("-1.2") == (-1, 2)
    assert leading_numbers("-1.2") == ()
