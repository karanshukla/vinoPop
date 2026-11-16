"""Popup rules that need no window: the button size clamp, touchpad taps,
the force-all modifier, glyph recolouring and the overflow mode."""

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


@pytest.fixture
def settings(monkeypatch):
    import core.settings

    values: dict = {}
    fake = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(core.settings, "get_settings", lambda: fake)
    return values


# ── button size ──────────────────────────────────────────────────────────────


def test_a_button_size_of_sixteen_is_kept(popup, settings):
    settings["popup_button_size"] = 16
    assert popup._resolve_button_size() == 16


def test_a_button_size_under_sixteen_is_raised_to_it(popup, settings):
    settings["popup_button_size"] = 15
    assert popup._resolve_button_size() == 16


def test_a_button_size_of_thirty_two_is_kept(popup, settings):
    settings["popup_button_size"] = 32
    assert popup._resolve_button_size() == 32


def test_a_button_size_over_thirty_two_is_lowered_to_it(popup, settings):
    settings["popup_button_size"] = 33
    assert popup._resolve_button_size() == 32


def test_a_nonsense_button_size_is_the_default(popup, settings):
    settings["popup_button_size"] = "huge"
    assert popup._resolve_button_size() == 22


# ── touchpad taps ────────────────────────────────────────────────────────────

BTN_TOUCH = 0x14A


def test_a_touch_lifted_within_the_tap_time_is_a_click(popup):
    w = popup._ClickWatcher(lambda: None)
    w._is_click(BTN_TOUCH, 1, 10.0)
    assert w._is_click(BTN_TOUCH, 0, 10.0 + popup._TAP_MAX_S)


def test_a_touch_held_past_the_tap_time_is_not(popup):
    w = popup._ClickWatcher(lambda: None)
    w._is_click(BTN_TOUCH, 1, 10.0)
    assert not w._is_click(BTN_TOUCH, 0, 10.0 + popup._TAP_MAX_S + 0.01)


# ── force-all modifier ───────────────────────────────────────────────────────


def _popup_with_mask(popup, mask):
    p = popup.PopupWindow.__new__(popup.PopupWindow)
    root = SimpleNamespace(query_pointer=lambda: SimpleNamespace(mask=mask))
    p._xdpy = SimpleNamespace(screen=lambda: SimpleNamespace(root=root))
    return p


def test_holding_the_force_all_modifier_counts(popup, settings):
    settings["popup_force_all_modifier"] = "Shift"
    assert _popup_with_mask(popup, 1 << 0)._force_all_held()


def test_holding_another_modifier_does_not(popup, settings):
    settings["popup_force_all_modifier"] = "alt"
    assert not _popup_with_mask(popup, 1 << 0)._force_all_held()


# ── glyph recolouring ────────────────────────────────────────────────────────


def test_a_glyph_body_and_accent_take_the_foreground(popup):
    svg = b'<path stroke="#f0f0f0"/><path fill="#F0F0F0"/><circle fill="#1d7a3a"/>'
    assert popup._recolour_glyph(svg, "#111111", "#eeeeee") == (
        b'<path stroke="#111111"/><path fill="#111111"/><circle fill="#111111"/>'
    )


def test_a_glyph_hole_takes_the_background(popup):
    svg = b'<path fill="#2b2b2b"/><path fill="#2B2B2B"/>'
    assert popup._recolour_glyph(svg, "#111111", "#eeeeee") == (
        b'<path fill="#eeeeee"/><path fill="#eeeeee"/>'
    )


# ── overflow mode ────────────────────────────────────────────────────────────


def _overflow_mode(popup):
    return popup.PopupWindow.__new__(popup.PopupWindow)._overflow_mode()


def test_a_known_overflow_mode_is_used(popup, settings):
    settings["popup_overflow_mode"] = " Expand "
    assert _overflow_mode(popup) == "expand"


def test_an_unknown_overflow_mode_wraps(popup, settings):
    settings["popup_overflow_mode"] = "scroll"
    assert _overflow_mode(popup) == "wrap"


# ── light remap ──────────────────────────────────────────────────────────────


@pytest.fixture
def theme():
    try:
        from ui import theme
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 unavailable: {exc}")
    return theme


def test_a_remapped_colour_is_not_remapped_again(theme):
    remap = {"#111111": "#222222", "#222222": "#333333"}
    assert theme._apply_remap(b"a #111111 b #222222", remap) == b"a #222222 b #333333"


def test_a_longer_colour_wins_over_its_prefix(theme):
    remap = {"#abc": "#000", "#abcdef": "#fff"}
    assert theme._apply_remap(b"#abcdef #abc;", remap) == b"#fff #000;"
