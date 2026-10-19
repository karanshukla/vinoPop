"""Popup bar behaviour that needs no compositor (#34): the hit-test rect
after 'expand' reveals row 2, and touchpad taps in the Wayland click
watcher. All with fakes; GTK is imported but nothing is shown."""

from types import SimpleNamespace

import pytest

pytest.importorskip("gi")


@pytest.fixture
def popup():
    try:
        from ui import popup
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 unavailable: {exc}")
    return popup


class _Mon:
    def __init__(self, x, y, w, h):
        self._g = SimpleNamespace(x=x, y=y, width=w, height=h)

    def get_geometry(self):
        return self._g


class _FakeWin:
    def __init__(self, w, h):
        self.size = (w, h)

    def get_preferred_size(self):
        w, h = self.size
        return None, SimpleNamespace(width=w, height=h)

    def get_display(self):
        return None

    def queue_resize(self):
        pass


@pytest.fixture
def bar(popup, monkeypatch):
    """A PopupWindow placed at (800, 300) as one 220x40 row on a 1920x1080
    monitor, with a recording backend."""
    moves = []
    monkeypatch.setattr(
        popup,
        "get_backend",
        lambda: SimpleNamespace(move_popup_window=lambda _w, x, y: moves.append((x, y))),
    )
    monkeypatch.setattr(popup, "_monitor_for_point", lambda *_a: _Mon(0, 0, 1920, 1080))
    p = popup.PopupWindow.__new__(popup.PopupWindow)
    p.win = _FakeWin(220, 40)
    p._popup_rect = (800, 300, 220, 40)
    return p, moves


def test_refit_after_expand_covers_row_two(bar):
    p, moves = bar
    row2 = (900, 360)  # a button on the revealed second row
    assert not p._point_in_popup(*row2)  # the stale one-row rect: the bug
    p.win.size = (220, 72)  # what get_preferred_size reports after _expand
    p._refit()
    assert p._popup_rect == (800, 300, 220, 72)
    assert p._point_in_popup(*row2)
    assert moves == []  # still fits: no move


def test_refit_pulls_a_grown_bar_back_on_screen(bar):
    p, moves = bar
    p._popup_rect = (1800, 1030, 220, 40)
    p.win.size = (220, 72)
    p._refit()
    x, y, w, h = p._popup_rect
    assert (w, h) == (220, 72)
    assert x + w <= 1920 - 4 and y + h <= 1080 - 4
    assert moves == [(x, y)]


# ---- Wayland click watcher: touchpad taps ---------------------------------
BTN_LEFT, BTN_TOUCH = 0x110, 0x14A


def test_mouse_press_is_a_click(popup):
    w = popup._ClickWatcher(lambda: None)
    assert w._is_click(BTN_LEFT, 1, 0.0)
    assert not w._is_click(BTN_LEFT, 0, 0.05)


def test_short_touch_is_a_tap(popup):
    w = popup._ClickWatcher(lambda: None)
    assert not w._is_click(BTN_TOUCH, 1, 10.00)
    assert w._is_click(BTN_TOUCH, 0, 10.12)


def test_finger_moving_the_pointer_is_not_a_tap(popup):
    w = popup._ClickWatcher(lambda: None)
    assert not w._is_click(BTN_TOUCH, 1, 10.0)
    assert not w._is_click(BTN_TOUCH, 0, 11.5)
    # a release with no press seen (watcher started mid-touch) is ignored
    assert not w._is_click(BTN_TOUCH, 0, 12.0)


def test_key_bitmap_parsing(popup):
    # sysfs words are most significant first; BTN_TOUCH 0x14a is bit 10 of
    # word 5, BTN_TOOL_FINGER 0x145 bit 5 of the same word
    words = ["0"] * 6
    words[0] = format((1 << 10) | (1 << 5), "x")
    bitmap = " ".join(words)
    assert popup._has_key_bit(bitmap, BTN_TOUCH)
    assert popup._has_key_bit(bitmap, 0x145)
    assert not popup._has_key_bit(bitmap, BTN_LEFT)
    assert not popup._has_key_bit("0", BTN_TOUCH)


# ---- the shared X11 poll (used by the AI bubble) ---------------------------
def test_x_poll_fires_on_new_presses_only(popup, monkeypatch):
    clicks, escs = [], []
    poll = popup._XInputPoll(lambda: clicks.append(1), lambda: escs.append(1))
    poll._xdpy = object()
    poll._id = 1
    poll._prev = (True, False)  # button held when the bubble opened
    states = iter(
        [(True, 0, 0, False), (False, 0, 0, False), (True, 0, 0, False), (True, 0, 0, True)]
    )
    monkeypatch.setattr(popup, "_x11_input_state", lambda *_a: next(states))
    for _ in range(4):
        poll._tick()
    assert clicks == [1] and escs == [1]
