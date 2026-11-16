"""The X11 modifier+double-click watcher (triggers/mouse_watcher.py)."""

import pytest

pytest.importorskip("gi")

from triggers import mouse_watcher as mw  # noqa: E402


@pytest.fixture
def clicks(monkeypatch):
    now = [10.0]
    fired = []
    monkeypatch.setattr(mw.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(mw.GLib, "timeout_add", lambda _ms, fn, *a: fn(*a))
    watcher = mw.DoubleClickWatcher(lambda x, y: fired.append((x, y)))

    def click(after_ms=0, x=100, y=100):
        now[0] += after_ms / 1000
        watcher._on_left_click(x, y)
        return fired

    return click


def test_a_second_click_just_inside_the_window_fires(clicks):
    clicks()
    assert clicks(after_ms=mw._DOUBLE_CLICK_MS - 5) == [(100, 100)]


def test_a_second_click_just_after_the_window_does_not(clicks):
    clicks()
    assert clicks(after_ms=mw._DOUBLE_CLICK_MS + 5) == []


def test_a_wiggle_just_inside_the_tolerance_still_fires(clicks):
    clicks()
    assert clicks(after_ms=50, x=100 + mw._POSITION_TOLERANCE_PX - 1) != []


def test_a_move_of_the_tolerance_does_not(clicks):
    clicks()
    assert clicks(after_ms=50, y=100 + mw._POSITION_TOLERANCE_PX) == []


def test_a_third_click_does_not_fire_again(clicks):
    clicks()
    clicks(after_ms=50)
    assert len(clicks(after_ms=50)) == 1


@pytest.mark.parametrize(
    "setting,mask", [("ctrl", 4), ("shift", 1), ("alt", 8), ("super", 64), ("hyper", 4)]
)
def test_the_modifier_setting_picks_the_x_mask(monkeypatch, setting, mask):
    monkeypatch.setattr("core.settings.get_settings", lambda: {"double_click_modifier": setting})
    assert mw._required_modifier_mask() == mask
