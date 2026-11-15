"""Which buttons a popup gets and where it is placed, with the plugins, the
settings, the monitor and the platform backend faked. Nothing is shown."""

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


def _plugin(name, priority=50, requires_editable=False, matches=True):
    return SimpleNamespace(
        name=name,
        icon=f"{name}-symbolic",
        tooltip=name.title(),
        priority=priority,
        requires_editable=requires_editable,
        matches=lambda _text: matches,
    )


@pytest.fixture
def shown(popup, monkeypatch):
    """A PopupWindow whose show_for() records the buttons it would draw.
    `plugins` is what matched the content type, `everything` all plugins."""
    rec = SimpleNamespace(specs=None, hidden=None, settings={}, plugins=[], everything=[])
    p = popup.PopupWindow.__new__(popup.PopupWindow)
    p.win = SimpleNamespace(get_visible=lambda: False)
    p._pointer_in_popup = False
    p._clear_buttons = lambda: None
    p._force_all_held = lambda: False
    p._present_near = lambda *_a, **_k: None

    def present_buttons(specs, hidden, cap):
        rec.specs, rec.hidden = [tooltip for _icon, tooltip, _run in specs], hidden

    p._present_buttons = present_buttons
    monkeypatch.setattr(popup.plugin_loader, "for_content_type", lambda *_a: list(rec.plugins))
    monkeypatch.setattr(popup.plugin_loader, "all_plugins", lambda: list(rec.everything))
    import core.settings

    settings = SimpleNamespace(
        get=lambda key, default=None: rec.settings.get(key, default),
        get_int=lambda key: rec.settings.get(key, 0),
    )
    monkeypatch.setattr(core.settings, "get_settings", lambda: settings)
    rec.popup = p
    return rec


def _show(rec, editable=True):
    from core.classifier import ContentType

    rec.popup.show_for("hello", 10, 10, ContentType.PLAIN_TEXT, editable=editable)


def test_a_read_only_selection_gets_no_editing_buttons(shown):
    shown.plugins = [_plugin("copy"), _plugin("paste", requires_editable=True)]
    _show(shown, editable=False)
    assert shown.specs == ["Copy"]


def test_buttons_up_to_the_cap_are_all_drawn(shown):
    shown.settings["max_popup_buttons"] = 2
    shown.plugins = [_plugin("a"), _plugin("b")]
    _show(shown)
    assert (shown.specs, shown.hidden) == (["A", "B"], 0)


def test_buttons_past_the_cap_are_counted_as_hidden(shown):
    shown.settings["max_popup_buttons"] = 2
    shown.plugins = [_plugin("a"), _plugin("b"), _plugin("c")]
    _show(shown)
    assert (shown.specs, shown.hidden) == (["A", "B"], 1)


def test_an_unset_cap_is_ten(shown):
    shown.plugins = [_plugin(f"p{i}") for i in range(12)]
    _show(shown)
    assert (len(shown.specs), shown.hidden) == (10, 2)


def test_no_plugins_means_no_popup(shown):
    _show(shown)
    assert shown.specs is None


def test_the_force_all_key_shows_every_plugin_that_matches_the_text(shown):
    shown.popup._force_all_held = lambda: True
    shown.settings["plugin_order"] = ["late", "early"]
    shown.plugins = [_plugin("early")]
    shown.everything = [
        _plugin("unlisted-b", priority=20),
        _plugin("early"),
        _plugin("wrong-text", matches=False),
        _plugin("late"),
        _plugin("unlisted-a", priority=10),
    ]
    _show(shown)
    assert shown.specs == ["Late", "Early", "Unlisted-A", "Unlisted-B"]


def test_a_plugin_without_a_tooltip_is_named_after_itself(shown):
    nameless = _plugin("shout-it")
    nameless.tooltip = ""
    shown.plugins = [nameless]
    _show(shown)
    assert shown.specs == ["shout-it"]


def test_a_selection_change_under_the_pointer_keeps_the_buttons(shown):
    shown.popup.win = SimpleNamespace(get_visible=lambda: True)
    shown.popup._pointer_in_popup = True
    shown.plugins = [_plugin("copy")]
    _show(shown)
    assert shown.specs is None


# ── placement ────────────────────────────────────────────────────────────────


class _Monitor:
    def __init__(self, scale=1, width=1920, height=1080):
        self._scale = scale
        self._geometry = SimpleNamespace(x=0, y=0, width=width, height=height)

    def get_scale_factor(self):
        return self._scale

    def get_geometry(self):
        return self._geometry


@pytest.fixture
def placed(popup, monkeypatch):
    """A 200x40 popup on a 1920x1080 monitor; place(...) returns where the
    backend was told to put it."""
    rec = SimpleNamespace(moves=[], logical=False, monitor=_Monitor())
    backend = SimpleNamespace(
        move_popup_window=lambda _win, x, y: rec.moves.append((x, y)),
        pointer_is_logical=False,
    )
    monkeypatch.setattr(popup, "get_backend", lambda: backend)
    monkeypatch.setattr(popup, "_monitor_for_point", lambda *_a: rec.monitor)
    p = popup.PopupWindow.__new__(popup.PopupWindow)
    p.win = SimpleNamespace(
        realize=lambda: None,
        get_preferred_size=lambda: (None, SimpleNamespace(width=200, height=40)),
        get_display=lambda: None,
        set_visible=lambda _v: None,
    )
    p._xdpy = None
    p._start_tracking = lambda: None
    p._arm_initial_hide = lambda: None
    p._kbd_stop = lambda: None
    p._kbd_start = lambda: None

    def place(x, y, rect=None, logical=False):
        backend.pointer_is_logical = logical
        p._present_near(x, y, rect=rect)
        return rec.moves[-1]

    rec.place, rec.popup = place, p
    return rec


def test_the_window_is_laid_out_off_screen_before_it_is_placed(placed):
    placed.place(960, 500)
    assert placed.moves[0] == (-10000, -10000)


def test_on_x11_the_popup_sits_a_line_above_the_pointer(placed):
    assert placed.place(960, 500) == (860, 432)  # 500 - 40 high - 28 clearance


def test_physical_pointer_coordinates_are_scaled_down(placed):
    placed.monitor = _Monitor(scale=2)
    assert placed.place(1920, 1000) == (860, 432)


def test_on_wayland_the_popup_is_lifted_further_and_not_scaled(placed):
    placed.monitor = _Monitor(scale=2)
    assert placed.place(960, 500, logical=True) == (860, 396)  # 64 clearance


def test_without_room_above_the_popup_drops_below_the_pointer(placed):
    assert placed.place(960, 71) == (860, 103)  # 71 - 40 - 28 = 3, under the 4 px edge
    assert placed.place(960, 72) == (860, 4)  # exactly 4 stays above


def test_with_a_selection_rect_the_popup_centres_just_above_it(placed):
    assert placed.place(0, 0, rect=(900, 500, 120, 20)) == (860, 450)  # 500 - 40 - 10 gap


def test_a_selection_at_the_top_edge_gets_the_popup_below_it(placed):
    assert placed.place(0, 0, rect=(900, 20, 120, 20)) == (860, 50)  # bottom 40 + 10


def test_the_popup_is_kept_on_the_monitor(placed):
    assert placed.place(10, 500) == (4, 432)
    assert placed.place(1915, 500) == (1716, 432)


def test_the_hide_zones_follow_the_placement(placed):
    placed.place(960, 500)
    assert placed.popup._popup_rect == (860, 432, 200, 40)
    assert placed.popup._origin_logical == (960.0, 500.0)
    assert placed.popup._pointer_in_popup is False


# ── the zone that keeps the popup up ─────────────────────────────────────────


@pytest.fixture
def zone(popup):
    """A 200x40 popup at (500, 100), opened for a selection at (300, 400)."""
    p = popup.PopupWindow.__new__(popup.PopupWindow)
    p._popup_rect = (500, 100, 200, 40)
    p._origin_logical = (300.0, 400.0)
    p._text_zone_radius = 80
    return p._in_safe_zone


def test_the_pointer_may_be_twelve_pixels_outside_the_popup(zone):
    assert zone(712, 152)
    assert not zone(713, 90)


def test_the_pointer_may_be_eighty_pixels_from_the_selection(zone):
    assert zone(220, 400)
    assert not zone(219, 400)


def test_the_path_from_the_selection_to_the_popup_is_safe(zone):
    assert zone(450, 250)  # outside both, on the way
    assert zone(276, 250)  # 24 px beside the path
    assert not zone(275, 250)
    assert not zone(450, 500)  # past the selection, away from the popup
