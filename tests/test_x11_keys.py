"""Keystrokes over XTEST in the X11 backend (what xdotool key did)."""

import pytest

pytest.importorskip("Xlib")

from Xlib import XK, X  # noqa: E402

from platform_backend import x11  # noqa: E402

# A small US-layout keymap: keycode -> (level 1, level 2) keysyms
KEYMAP = {
    37: ("Control_L", None),
    50: ("Shift_L", None),
    64: ("Alt_L", None),
    133: ("Super_L", None),
    55: ("v", "V"),
    38: ("a", "A"),
    21: ("equal", "plus"),
    22: ("BackSpace", None),
    36: ("Return", None),
}
MODIFIERS = [[50], [66], [37], [64], [77], [], [133], []]  # Shift, Lock, Control, Mod1..


class FakeDisplay:
    def __init__(self, held=(), xtest=True):
        self.held = set(held)
        self.xtest = xtest
        self.sent = []
        self.synced = False

    def has_extension(self, name):
        return self.xtest and name == "XTEST"

    def keysym_to_keycode(self, ks):
        for kc, syms in KEYMAP.items():
            if any(s and XK.string_to_keysym(s) == ks for s in syms):
                return kc
        return 0

    def keycode_to_keysym(self, kc, index):
        sym = KEYMAP.get(kc, (None, None))[index]
        return XK.string_to_keysym(sym) if sym else 0

    def get_modifier_mapping(self):
        return MODIFIERS

    def query_keymap(self):
        keys = [0] * 32
        for kc in self.held:
            keys[kc // 8] |= 1 << (kc % 8)
        return keys

    def sync(self):
        self.synced = True


@pytest.fixture
def dpy(monkeypatch):
    d = FakeDisplay()

    def fake_input(display, event_type, detail=0, **_):
        display.sent.append(("down" if event_type == X.KeyPress else "up", detail))

    monkeypatch.setattr("Xlib.ext.xtest.fake_input", fake_input)
    return d


def test_chord_is_pressed_then_released_in_reverse(dpy):
    assert x11.xtest_send_key(dpy, "ctrl+v")
    assert dpy.sent == [("down", 37), ("down", 55), ("up", 55), ("up", 37)]
    assert dpy.synced


def test_named_keys_and_capitals(dpy):
    assert x11.xtest_send_key(dpy, "BackSpace")
    assert x11.xtest_send_key(dpy, "ctrl+A")  # no implicit Shift, as xdotool
    assert dpy.sent == [
        ("down", 22),
        ("up", 22),
        ("down", 37),
        ("down", 38),
        ("up", 38),
        ("up", 37),
    ]


def test_a_shift_level_key_gets_shift(dpy):
    assert x11.xtest_send_key(dpy, "ctrl+plus")
    assert dpy.sent == [
        ("down", 50),
        ("down", 37),
        ("down", 21),
        ("up", 21),
        ("up", 37),
        ("up", 50),
    ]


def test_held_modifiers_are_lifted_and_put_back(dpy):
    # Still holding Super+Shift from the popup hotkey, and Caps Lock is on
    dpy.held = {133, 50, 66}
    assert x11.xtest_send_key(dpy, "ctrl+v")
    released = [kc for kind, kc in dpy.sent[:2]]
    assert sorted(released) == [50, 133]
    assert dpy.sent[2:6] == [("down", 37), ("down", 55), ("up", 55), ("up", 37)]
    assert sorted(kc for _kind, kc in dpy.sent[6:]) == [50, 133]
    assert all(kind == "down" for kind, _kc in dpy.sent[6:])
    assert 66 not in [kc for _kind, kc in dpy.sent]  # Caps Lock left alone


def test_held_modifier_that_is_part_of_the_chord_is_not_doubled(dpy):
    dpy.held = {37}
    assert x11.xtest_send_key(dpy, "ctrl+v")
    assert dpy.sent == [("down", 37), ("down", 55), ("up", 55), ("up", 37)]


@pytest.mark.parametrize("combo", ["ctrl+eacute", "", "+"])
def test_unmapped_key_sends_nothing_so_xdotool_takes_over(dpy, combo):
    assert not x11.xtest_send_key(dpy, combo)
    assert dpy.sent == []


def test_repeat_without_clearing_modifiers(dpy, monkeypatch):
    # The X11 snippet trigger: n BackSpaces 16 ms apart, no modifier dance
    sleeps = []
    monkeypatch.setattr(x11.time, "sleep", sleeps.append)
    dpy.held = {37}
    assert x11.xtest_send_key(dpy, "BackSpace", repeat=3, delay_s=0.016, clear_modifiers=False)
    assert dpy.sent == [("down", 22), ("up", 22)] * 3
    assert sleeps == [0.016, 0.016]


def test_no_xtest_sends_nothing(dpy):
    dpy.xtest = False
    assert not x11.xtest_send_key(dpy, "ctrl+v")
    assert dpy.sent == []


def test_send_key_falls_back_to_xdotool(monkeypatch):
    calls = []
    monkeypatch.setattr(x11, "send_keys", lambda combo: False)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(x11.subprocess, "run", lambda argv, **_: calls.append(argv))
    x11.X11Backend().send_key("ctrl+v")
    assert calls == [["xdotool", "key", "--clearmodifiers", "ctrl+v"]]


def test_send_key_skips_xdotool_when_xtest_worked(monkeypatch):
    calls = []
    monkeypatch.setattr(x11, "send_keys", lambda combo: True)
    monkeypatch.setattr(x11.subprocess, "run", lambda argv, **_: calls.append(argv))
    x11.X11Backend().send_key("ctrl+v")
    assert calls == []
