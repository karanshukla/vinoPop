"""The selection-watcher rules both backends share (platform_backend/base.py)."""

import pytest

from platform_backend import base

BURST_T0 = 100.0


def _copy_button(clip_changed_at, clipboard="same"):
    return base.is_copy_button("same", BURST_T0, clip_changed_at, lambda: clipboard)


def test_a_clipboard_change_just_inside_the_burst_is_a_copy_button():
    assert _copy_button(BURST_T0 - base.COPY_BURST_S + 0.01)


def test_a_clipboard_change_just_before_the_burst_is_not():
    assert not _copy_button(BURST_T0 - base.COPY_BURST_S - 0.01)


def test_a_clipboard_change_with_other_text_is_not_a_copy_button():
    assert not _copy_button(BURST_T0, clipboard="other")


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(base.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(base, "_expected", None)
    return now


def test_an_expected_selection_is_taken_just_inside_its_ttl(clock):
    base.expect_selection("ocr text")
    clock[0] += base._EXPECT_TTL_S - 0.01
    assert base.take_expected("ocr text")


def test_an_expected_selection_is_stale_just_after_its_ttl(clock):
    base.expect_selection("ocr text")
    clock[0] += base._EXPECT_TTL_S + 0.01
    assert not base.take_expected("ocr text")


def test_an_expected_selection_is_taken_only_once(clock):
    base.expect_selection("ocr text")
    assert base.take_expected("ocr text")
    assert not base.take_expected("ocr text")


def test_a_missing_injector_is_reported_once_per_key(monkeypatch):
    shown = []
    monkeypatch.setattr(base, "_missing_injector_warned", set())
    monkeypatch.setattr("core.actions.notify", lambda *a, **k: shown.append(a[1]))
    base._warn_missing_injector("x11", "first")
    base._warn_missing_injector("x11", "again")
    base._warn_missing_injector("other", "second")
    assert shown == ["first", "second"]
