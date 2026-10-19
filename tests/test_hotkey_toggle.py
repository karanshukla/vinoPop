"""The popup hotkey toggles the popup (#44), without a held key closing the
popup it just opened."""

from triggers.toggle import CLOSE, IGNORE, OPEN, HotkeyToggle


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_press_opens_then_a_later_press_closes():
    clock = _Clock()
    t = HotkeyToggle(clock)
    assert t.press(False, None) == OPEN
    shown = clock.now + 0.1  # the lookup took 100 ms
    clock.now += 2.0
    assert t.press(True, shown) == CLOSE


def test_holding_the_key_neither_closes_nor_reopens():
    clock = _Clock()
    t = HotkeyToggle(clock)
    assert t.press(False, None) == OPEN
    shown = clock.now + 0.1
    # X11's default autorepeat: first repeat after 660 ms, then every 40 ms
    clock.now += 0.66
    assert t.press(True, shown) == IGNORE
    for _ in range(20):
        clock.now += 0.04
        assert t.press(True, shown) == IGNORE
    # released, then pressed again a moment later
    clock.now += 0.5
    assert t.press(True, shown) == CLOSE


def test_repeat_while_the_lookup_is_still_running_is_ignored():
    clock = _Clock()
    t = HotkeyToggle(clock)
    assert t.press(False, None) == OPEN
    clock.now += 0.05
    assert t.press(False, None) == IGNORE


def test_closes_a_popup_a_selection_opened_straight_away():
    clock = _Clock()
    t = HotkeyToggle(clock)
    assert t.press(True, None) == CLOSE


def test_reopens_after_closing():
    clock = _Clock()
    t = HotkeyToggle(clock)
    assert t.press(True, None) == CLOSE
    clock.now += 1.0
    assert t.press(False, None) == OPEN
