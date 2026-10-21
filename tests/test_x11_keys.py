"""Keystrokes and typing over XTEST in the X11 backend (what xdotool key
and xdotool type did)."""

import types

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
MIN_KEYCODE, MAX_KEYCODE = 8, 140


class FakeDisplay:
    def __init__(self, held=(), xtest=True, spare=True):
        self.held = set(held)
        self.xtest = xtest
        self.sent = []
        self.synced = False
        self.remaps = []  # (keycode, keysym) as change_keyboard_mapping saw them
        self.keymap = dict(KEYMAP)
        if not spare:
            # Every keycode taken, like a keyboard with no room to borrow
            for kc in range(MIN_KEYCODE, MAX_KEYCODE + 1):
                self.keymap.setdefault(kc, ("F35", None))
        self.display = types.SimpleNamespace(
            info=types.SimpleNamespace(min_keycode=MIN_KEYCODE, max_keycode=MAX_KEYCODE)
        )

    def has_extension(self, name):
        return self.xtest and name == "XTEST"

    def keysym_to_keycode(self, ks):
        for kc, syms in self.keymap.items():
            if any(s and XK.string_to_keysym(s) == ks for s in syms):
                return kc
        return 0

    def keycode_to_keysym(self, kc, index):
        sym = self.keymap.get(kc, (None, None))[index]
        return XK.string_to_keysym(sym) if sym else 0

    def get_modifier_mapping(self):
        return MODIFIERS

    def query_keymap(self):
        keys = [0] * 32
        for kc in self.held:
            keys[kc // 8] |= 1 << (kc % 8)
        return keys

    def get_keyboard_mapping(self, first, count):
        return [
            tuple(XK.string_to_keysym(s) if s else 0 for s in self.keymap.get(kc, (None, None)))
            for kc in range(first, first + count)
        ]

    def change_keyboard_mapping(self, first, keysyms):
        self.remaps.append((first, keysyms[0][0]))

    def sync(self):
        self.synced = True


@pytest.fixture
def dpy(monkeypatch):
    d = FakeDisplay()

    def fake_input(display, event_type, detail=0, **_):
        display.sent.append(("down" if event_type == X.KeyPress else "up", detail))

    monkeypatch.setattr("Xlib.ext.xtest.fake_input", fake_input)
    monkeypatch.setattr(x11.time, "sleep", lambda _s: None)
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
    dpy.held = {133, 50, 66}  # Super, Shift, Caps Lock
    assert x11.xtest_send_key(dpy, "ctrl+v")
    assert dpy.sent[:2] == [("up", 50), ("up", 133)] or dpy.sent[:2] == [("up", 133), ("up", 50)]
    assert dpy.sent[2:6] == [("down", 37), ("down", 55), ("up", 55), ("up", 37)]
    assert sorted(dpy.sent[6:]) == [("down", 50), ("down", 133)]
    assert 66 not in [kc for _kind, kc in dpy.sent]  # Caps Lock left alone


def test_held_modifier_that_is_part_of_the_chord_is_not_doubled(dpy):
    dpy.held = {37}
    assert x11.xtest_send_key(dpy, "ctrl+v")
    assert dpy.sent == [("down", 37), ("down", 55), ("up", 55), ("up", 37)]


def test_a_key_the_layout_lacks_borrows_a_spare_keycode(dpy):
    # The highest free keycode gets the keysym, then goes back to NoSymbol
    assert x11.xtest_send_key(dpy, "ctrl+eacute")
    eacute = XK.string_to_keysym("eacute")
    assert dpy.remaps == [(MAX_KEYCODE, eacute), (MAX_KEYCODE, 0)]
    assert dpy.sent == [("down", 37), ("down", MAX_KEYCODE), ("up", MAX_KEYCODE), ("up", 37)]


@pytest.mark.parametrize("combo", ["", "+", "ctrl+nosuchkeysym"])
def test_nothing_to_send_sends_nothing(dpy, combo):
    assert not x11.xtest_send_key(dpy, combo)
    assert dpy.sent == [] and dpy.remaps == []


def test_no_spare_keycode_means_the_key_cannot_be_sent(monkeypatch):
    dpy = FakeDisplay(spare=False)
    monkeypatch.setattr("Xlib.ext.xtest.fake_input", lambda *a, **k: dpy.sent.append(a))
    assert not x11.xtest_send_key(dpy, "ctrl+eacute")
    assert dpy.sent == [] and dpy.remaps == []


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
    assert not x11.xtest_type_text(dpy, "av")
    assert dpy.sent == []


# ---- typing (what xdotool type did) -----------------------------------------


def test_typing_presses_each_character_with_shift_where_the_layout_needs_it(dpy):
    assert x11.xtest_type_text(dpy, "aV+")
    assert dpy.sent == [
        ("down", 38),
        ("up", 38),
        ("down", 50),
        ("down", 55),
        ("up", 55),
        ("up", 50),
        ("down", 50),
        ("down", 21),
        ("up", 21),
        ("up", 50),
    ]
    assert dpy.remaps == []


def test_typing_lifts_held_modifiers_and_puts_them_back(dpy):
    dpy.held = {37}
    assert x11.xtest_type_text(dpy, "a")
    assert dpy.sent == [("up", 37), ("down", 38), ("up", 38), ("down", 37)]


def test_typing_binds_missing_characters_to_one_spare_keycode(dpy):
    assert x11.xtest_type_text(dpy, "éa€")
    euro, eacute = 0x20AC, XK.string_to_keysym("eacute")  # EuroSign isn't in Xlib.XK
    assert dpy.remaps == [
        (MAX_KEYCODE, eacute),
        (MAX_KEYCODE, euro),
        (MAX_KEYCODE, 0),
    ]
    assert dpy.sent == [
        ("down", MAX_KEYCODE),
        ("up", MAX_KEYCODE),
        ("down", 38),
        ("up", 38),
        ("down", MAX_KEYCODE),
        ("up", MAX_KEYCODE),
    ]


def test_typing_paces_characters_with_the_delay(dpy, monkeypatch):
    sleeps = []
    monkeypatch.setattr(x11.time, "sleep", sleeps.append)
    assert x11.xtest_type_text(dpy, "aaa", delay_s=0.001)
    assert sleeps == [0.001, 0.001]


def test_typing_skips_what_it_cannot_bind_and_types_the_rest(monkeypatch, capsys):
    dpy = FakeDisplay(spare=False)
    monkeypatch.setattr(
        "Xlib.ext.xtest.fake_input", lambda d, t, detail=0, **_: d.sent.append(detail)
    )
    assert x11.xtest_type_text(dpy, "aé")
    assert dpy.sent == [38, 38] and dpy.remaps == []
    assert "typed 1 of 2" in capsys.readouterr().out


# ---- the backend methods: XTEST or a warning, never a subprocess -------------


def test_send_key_warns_when_xtest_cannot(monkeypatch):
    warned = []
    monkeypatch.setattr(x11, "send_keys", lambda combo: False)
    monkeypatch.setattr("platform_backend.base._warn_missing_injector", lambda *a: warned.append(a))
    monkeypatch.setattr(x11.subprocess, "run", lambda *a, **k: pytest.fail("spawned a process"))
    x11.X11Backend().send_key("ctrl+v")
    assert warned and "XTEST" in warned[0][1]


def test_type_text_goes_over_xtest(monkeypatch):
    typed = []
    monkeypatch.setattr(x11, "type_text", lambda text, delay_s=0.0: typed.append(text) or True)
    monkeypatch.setattr(x11.subprocess, "run", lambda *a, **k: pytest.fail("spawned a process"))
    x11.X11Backend().type_text("hello")
    assert typed == ["hello"]


def test_can_paste_is_xtest(monkeypatch):
    monkeypatch.setattr(x11, "xtest_available", lambda: False)
    assert not x11.X11Backend().can_paste()
    monkeypatch.setattr(x11, "xtest_available", lambda: True)
    assert x11.X11Backend().can_paste()
