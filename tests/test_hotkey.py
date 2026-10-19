"""Hotkey strings recorded in Settings must resolve to real X11 keysyms."""

import pytest

pytest.importorskip("gi")  # hotkey.py schedules its callback through GLib
pytest.importorskip("Xlib")

from triggers import hotkey  # noqa: E402


@pytest.mark.parametrize(
    "combo,mods,key",
    [
        ("super+shift+y", hotkey.X.Mod4Mask | hotkey.X.ShiftMask, "y"),
        ("ctrl+alt+space", hotkey.X.ControlMask | hotkey.X.Mod1Mask, "space"),
        ("super+f5", hotkey.X.Mod4Mask, "F5"),
        ("ctrl+Page_Up", hotkey.X.ControlMask, "page_up"),
    ],
)
def test_parse(combo, mods, key):
    parsed_mods, parsed_key = hotkey._parse(combo)
    assert parsed_mods == mods
    assert parsed_key.lower() == key.lower()


def test_parse_rejects_unknown_modifiers_and_empty_strings():
    with pytest.raises(ValueError):
        hotkey._parse("hyperdrive+x")
    with pytest.raises(ValueError):
        hotkey._parse("  ")


@pytest.mark.parametrize(
    "name",
    [
        # Gdk.keyval_name() lower-cased, which is what the Settings recorder stores
        "page_up",
        "page_down",
        "kp_add",
        "scroll_lock",
        "xf86audioplay",
        "exclam",
        "bracketleft",
        "return",
        "space",
        "f12",
        "a",
        "1",
    ],
)
def test_recorded_key_names_resolve(name):
    _mods, key_name = hotkey._parse(f"ctrl+{name}")
    assert hotkey._resolve_keysym(key_name) != 0, name


def test_shift_tab_records_as_plain_tab():
    # Shift+Tab reports ISO_Left_Tab; shift is already in the modifier mask
    _mods, key_name = hotkey._parse("shift+iso_left_tab")
    assert hotkey._resolve_keysym(key_name) == hotkey._resolve_keysym("Tab")


def test_unknown_key_names_still_fail_cleanly():
    assert hotkey._resolve_keysym("definitely_not_a_key") == 0


# ---- KDE Wayland: Qt key codes for KGlobalAccel ------------------------------


@pytest.fixture
def qt_keycode():
    try:
        from platform_backend import wayland_kde
    except (ImportError, ValueError) as exc:
        pytest.skip(f"wayland_kde backend unavailable: {exc}")
    return wayland_kde._qt_keycode


CTRL, SUPER = 0x04000000, 0x10000000


@pytest.mark.parametrize(
    "combo,expected",
    [
        ("ctrl+comma", CTRL | 0x2C),
        ("super+period", SUPER | 0x2E),
        ("ctrl+grave", CTRL | 0x60),
        ("ctrl+bracketleft", CTRL | 0x5B),
        ("ctrl+backslash", CTRL | 0x5C),
        ("ctrl+page_up", CTRL | 0x01000016),
        ("ctrl+print", CTRL | 0x01000009),
        ("shift+iso_left_tab", 0x02000000 | 0x01000001),
        ("super+shift+y", SUPER | 0x02000000 | ord("Y")),
        ("ctrl+f5", CTRL | 0x01000034),
    ],
)
def test_qt_keycode_for_recorded_names(qt_keycode, combo, expected):
    assert qt_keycode(combo) == expected


@pytest.mark.parametrize(
    "combo",
    # The last is what GTK recorded for the Copilot key (XF86Assistant, no Qt key).
    ["", "ctrl+", "hyper+x", "ctrl+not_a_key", "shift+super+0x10081247"],
)
def test_qt_keycode_rejects_what_it_cannot_encode(qt_keycode, combo):
    assert qt_keycode(combo) is None


@pytest.mark.parametrize("combo,ok", [("super+shift+y", True), ("shift+super+0x10081247", False)])
def test_x11_can_bind_hotkey(combo, ok):
    try:
        from platform_backend.x11 import X11Backend
    except (ImportError, ValueError) as exc:
        pytest.skip(f"x11 backend unavailable: {exc}")
    assert X11Backend.can_bind_hotkey(None, combo) is ok
