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


# ---- the shared key grab (popup and AI bubble) ------------------------------
class _FakeXRoot:
    def __init__(self):
        self.grabbed, self.ungrabbed = [], []

    def grab_key(self, keycode, mask, *_a, **_kw):
        self.grabbed.append((keycode, mask))

    def ungrab_key(self, keycode, mask):
        self.ungrabbed.append((keycode, mask))


class _FakeXDisplay:
    """Keycode = 10 + position in KEYS; queued events come back in order."""

    KEYS = ["Left", "Tab"]

    def __init__(self, X, XK):
        self.root = _FakeXRoot()
        self.events: list = []
        self._X, self._XK = X, XK

    def keysym_to_keycode(self, keysym):
        names = [self._XK.string_to_keysym(n) for n in self.KEYS]
        return 10 + names.index(keysym) if keysym in names else 0

    def screen(self):
        return SimpleNamespace(root=self.root)

    def sync(self):
        pass

    def fileno(self):
        return 0

    def pending_events(self):
        return len(self.events)

    def next_event(self):
        return self.events.pop(0)

    def press(self, keycode, shift=False):
        state = self._X.ShiftMask if shift else 0
        self.events.append(SimpleNamespace(type=self._X.KeyPress, detail=keycode, state=state))


def test_key_grab_routes_presses_and_ungrabs(popup, monkeypatch):
    if popup.X is None:
        pytest.skip("python-xlib unavailable")
    monkeypatch.setattr(popup.GLib, "io_add_watch", lambda *_a: 7)
    monkeypatch.setattr(popup.GLib, "source_remove", lambda _id: None)
    dpy = _FakeXDisplay(popup.X, popup.XK)
    keys = []
    grab = popup._KeyGrab(
        dpy, {"Left": False, "Tab": True, "Nope": False}, lambda n, s: keys.append((n, s))
    )
    grab.start()
    # Each key under every Caps/Num Lock combination; Tab with Shift too
    assert len(dpy.root.grabbed) == 4 + 8
    dpy.press(11, shift=True)
    dpy.press(10)
    dpy.press(99)  # not ours
    grab.drain()
    assert keys == [("Tab", True), ("Left", False)]
    dpy.press(10)  # raced the ungrab: dropped
    grab.stop()
    assert keys == [("Tab", True), ("Left", False)]
    assert sorted(dpy.root.ungrabbed) == sorted(dpy.root.grabbed)
    assert not grab.active


def test_only_keyboard_activation_counts(popup, monkeypatch):
    monkeypatch.setattr(popup, "_kbd_activated_at", 0.0)
    assert not popup.activated_by_keyboard()
    monkeypatch.setattr(popup, "_kbd_activated_at", popup.time.monotonic())
    assert popup.activated_by_keyboard()


# ---- keyboard navigation (#44) ---------------------------------------------
@pytest.mark.parametrize(
    "rows,index,key,shift,expected",
    [
        ([3], 0, "Right", False, 1),
        ([3], 2, "Right", False, 0),  # wraps
        ([3], 0, "Left", False, 2),
        ([3], 1, "Tab", False, 2),
        ([3], 1, "Tab", True, 0),  # Shift+Tab
        ([4, 2], 3, "Right", False, 4),  # across into row 2
        ([4, 2], 1, "Down", False, 5),
        ([4, 2], 3, "Down", False, 5),  # clamps to row 2's last button
        ([4, 2], 5, "Up", False, 1),
        ([4, 2], 1, "Up", False, 1),  # already on the top row
        ([4, 0], 1, "Down", False, 1),  # row 2 empty
        ([4, 2], 4, "Home", False, 0),
        ([4, 2], 0, "End", False, 5),
        ([3], 9, "Left", False, 1),  # a stale index is clamped first
        ([], 0, "Right", False, 0),
    ],
)
def test_nav_target(popup, rows, index, key, shift, expected):
    assert popup._nav_target(rows, index, key, shift) == expected


class _FakeButton:
    def __init__(self, name, log):
        self.name = name
        self.log = log
        self.classes = set()

    def add_css_class(self, c):
        self.classes.add(c)

    def remove_css_class(self, c):
        self.classes.discard(c)

    def emit(self, signal):
        self.log.append((signal, self.name))

    def get_tooltip_text(self):
        return f"Action {self.name}"

    def announce(self, message, _priority):
        _ANNOUNCED.append(message)


_ANNOUNCED: list[str] = []


@pytest.fixture
def kbd(popup):
    """A hotkey-opened PopupWindow with buttons a, b, c on row 1 and d on
    row 2, without GTK widgets or an X connection."""
    log = []
    row1 = [_FakeButton(n, log) for n in "abc"]
    row2 = [_FakeButton("d", log)]
    p = popup.PopupWindow.__new__(popup.PopupWindow)
    p._rows = lambda: [row1, row2]
    p._kbd_nav = True
    p._kbd_index = 0
    p._last_press_state = popup.Gdk.ModifierType.SHIFT_MASK
    p._arm_initial_hide = lambda: log.append(("rearm", None))

    def _hide():
        log.append(("hide", None))
        p._kbd_nav = False

    p.hide = _hide
    p._kbd_select(0)

    def selected():
        return [b.name for b in row1 + row2 if "vinopop-selected" in b.classes]

    return p, log, selected


def test_keyboard_moves_the_highlight_and_enter_runs_it(kbd, popup):
    p, log, selected = kbd
    assert selected() == ["a"]  # opened via the hotkey: first button
    assert p._kbd_key("Right")
    assert p._kbd_key("Down")
    assert selected() == ["d"]
    assert p._kbd_key("Tab", shift=True)
    assert selected() == ["c"]
    assert p._kbd_key("Return")
    assert ("clicked", "c") in log
    # a Shift left over from an earlier mouse press doesn't make it a copy
    assert p._last_press_state == popup.Gdk.ModifierType(0)
    assert ("rearm", None) in log  # keys keep the popup up


def test_space_activates_and_escape_closes(kbd):
    p, log, _selected = kbd
    assert p._kbd_key("space")
    assert ("clicked", "a") in log
    assert p._kbd_key("Escape")
    assert ("hide", None) in log


def test_the_highlight_is_announced(kbd):
    # The popup never has focus, so a screen reader hears the highlighted
    # action only through an announcement
    p, _log, _selected = kbd
    _ANNOUNCED.clear()
    p._kbd_key("Right")
    assert _ANNOUNCED == ["Action b"]


def test_other_keys_are_left_alone(kbd):
    p, log, selected = kbd
    assert not p._kbd_key("a")
    assert selected() == ["a"] and log == []


def test_focused_popup_routes_gtk_keys_the_same_way(kbd, popup):
    p, _log, selected = kbd
    Gdk = popup.Gdk
    assert p._on_key_press(None, Gdk.KEY_Right, 0, Gdk.ModifierType(0))
    assert selected() == ["b"]
    # Shift+Tab arrives as ISO_Left_Tab
    assert p._on_key_press(None, Gdk.KEY_ISO_Left_Tab, 0, Gdk.ModifierType.SHIFT_MASK)
    assert selected() == ["a"]
    p._kbd_nav = False  # a popup a selection opened: only Esc is handled
    assert not p._on_key_press(None, Gdk.KEY_Right, 0, Gdk.ModifierType(0))


def test_enter_marks_the_run_as_from_the_keyboard(kbd, popup, monkeypatch):
    # The AI bubble an action opens reads this to take the keys too (#124)
    p, _log, _selected = kbd
    monkeypatch.setattr(popup, "_kbd_activated_at", 0.0)
    p._kbd_key("Right")
    assert not popup.activated_by_keyboard()
    p._kbd_key("Return")
    assert popup.activated_by_keyboard()
