"""Is the focused widget editable, and where is its selection: the order
the answers are tried in, and the AT-SPI walk against a fake desktop."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from triggers import editable_detect as ed

ACTIVE, FOCUSED, EDITABLE = "active", "focused", "editable"


class _Node:
    def __init__(self, *states, children=(), role="node", name="app"):
        self.states = set(states)
        self.children = list(children)
        self.role = role
        self.name = name

    def get_state_set(self):
        return SimpleNamespace(contains=lambda s: s in self.states)

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, i):
        return self.children[i]

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name


def _fake_atspi(desktop, text=None):
    state = SimpleNamespace(ACTIVE=ACTIVE, FOCUSED=FOCUSED, EDITABLE=EDITABLE)
    return SimpleNamespace(
        StateType=state,
        CoordType=SimpleNamespace(SCREEN="screen"),
        get_desktop=lambda _i: desktop,
        Text=text,
    )


@pytest.fixture
def atspi(monkeypatch):
    """Install a fake Atspi over `desktop`, with the opt-in setting on."""

    def install(desktop, text=None, enabled=True):
        monkeypatch.setattr(ed, "_HAS_ATSPI", True)
        monkeypatch.setattr(ed, "Atspi", _fake_atspi(desktop, text), raising=False)
        from core import settings

        fake = SimpleNamespace(get=lambda key: enabled)
        monkeypatch.setattr(settings, "get_settings", lambda: fake)

    return install


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(ed, "_focus_cache", {"editable": None, "timestamp": 0.0, "role": ""})
    monkeypatch.setattr(ed, "_start_focus_listener", lambda: None)


def _desktop(*apps):
    return _Node(children=apps)


def _shell_that_always_claims_focus():
    return _Node(children=[_Node(children=[_Node(FOCUSED)])], name="cinnamon")


# ---- the AT-SPI walk ----------------------------------------------------------


def test_only_the_active_window_is_searched_for_the_focused_widget(atspi):
    editor = _Node(children=[_Node(ACTIVE, children=[_Node(FOCUSED, EDITABLE)])])
    atspi(_desktop(_shell_that_always_claims_focus(), editor))
    assert ed._atspi_focus_editable() is True


def test_a_focused_read_only_widget_is_not_editable(atspi):
    viewer = _Node(children=[_Node(ACTIVE, children=[_Node(FOCUSED)])])
    atspi(_desktop(viewer))
    assert ed._atspi_focus_editable() is False


def test_an_active_window_with_no_focused_widget_gives_no_answer(atspi):
    electron = _Node(children=[_Node(ACTIVE, EDITABLE, children=[_Node()])])
    atspi(_desktop(electron))
    assert ed._atspi_focus_editable() is None


def test_no_active_window_gives_no_answer(atspi):
    atspi(_desktop(_shell_that_always_claims_focus()))
    assert ed._atspi_focus_editable() is None


def test_the_walk_is_off_unless_the_setting_is_on(atspi):
    editor = _Node(children=[_Node(ACTIVE, children=[_Node(FOCUSED, EDITABLE)])])
    atspi(_desktop(editor), enabled=False)
    assert ed._atspi_focus_editable() is None
    assert ed.focused_selection_rect() is None


def _nested(depth, leaf):
    node = leaf
    for _ in range(depth):
        node = _Node(children=[node])
    return node


def test_the_focused_widget_is_looked_for_ten_levels_deep(atspi):
    atspi(_desktop())
    leaf = _Node(FOCUSED)
    assert ed._find_focused_in(_nested(9, leaf)) is leaf
    assert ed._find_focused_in(_nested(10, leaf)) is None


# ---- the selection's rectangle ------------------------------------------------


def _text(extents, n_selections=1, start=2, end=7):
    return SimpleNamespace(
        get_n_selections=lambda acc: n_selections,
        get_selection=lambda acc, i: SimpleNamespace(start_offset=start, end_offset=end),
        get_range_extents=lambda acc, s, e, coords: SimpleNamespace(
            x=extents[0], y=extents[1], width=extents[2], height=extents[3]
        ),
    )


def test_the_selection_rect_comes_from_the_focused_widget_of_the_active_window(atspi):
    editor = _Node(children=[_Node(ACTIVE, children=[_Node(FOCUSED)])])
    atspi(_desktop(_shell_that_always_claims_focus(), editor), text=_text((100, 200, 80, 18)))
    assert ed.focused_selection_rect() == (100, 200, 80, 18)


def test_a_zero_height_selection_gets_a_line_height(atspi):
    atspi(_desktop(), text=_text((100, 200, 80, 0)))
    assert ed._selection_rect_of(object()) == (100, 200, 80, 24)


def test_a_zero_width_selection_is_no_anchor(atspi):
    atspi(_desktop(), text=_text((100, 200, 0, 18)))
    assert ed._selection_rect_of(object()) is None


@pytest.mark.parametrize(
    ("extents", "kept"),
    [
        ((-50_000, 0, 10, 10), True),
        ((-50_001, 0, 10, 10), False),
        ((0, -50_001, 10, 10), False),
        ((0, 0, 100_000, 10), True),
        ((0, 0, 100_001, 10), False),
        ((0, 0, 10, 100_001), False),
    ],
)
def test_absurd_rectangles_are_no_anchor(atspi, extents, kept):
    atspi(_desktop(), text=_text(extents))
    assert (ed._selection_rect_of(object()) is not None) is kept


def test_nothing_selected_is_no_anchor(atspi):
    atspi(_desktop(), text=_text((1, 1, 10, 10), n_selections=0))
    assert ed._selection_rect_of(object()) is None
    atspi(_desktop(), text=_text((1, 1, 10, 10), start=5, end=5))
    assert ed._selection_rect_of(object()) is None


# ---- focus events -------------------------------------------------------------


def _event(detail1, *states):
    return SimpleNamespace(detail1=detail1, source=_Node(*states, role="entry"))


def test_gaining_focus_is_remembered_and_losing_it_is_not(atspi):
    atspi(_desktop())
    ed._on_focus_event(_event(0, EDITABLE))
    assert ed._cached_focus_editable() is None
    ed._on_focus_event(_event(1, EDITABLE))
    assert ed._cached_focus_editable() is True
    ed._on_focus_event(_event(1))
    assert ed._cached_focus_editable() is False


def test_a_focus_event_is_trusted_for_thirty_seconds(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ed._time, "monotonic", lambda: now[0])
    ed._focus_cache.update(editable=False, timestamp=1000.0)
    now[0] = 1030.0
    assert ed._cached_focus_editable() is False
    now[0] = 1030.1
    assert ed._cached_focus_editable() is None


# ---- the order of answers -----------------------------------------------------


@pytest.fixture
def backend(monkeypatch):
    def use(name):
        monkeypatch.setattr(ed, "get_backend", lambda: SimpleNamespace(name=name))

    use("x11")
    return use


def _xprop(cls):
    return ["some title", f'wm_class(string) = "{cls}", "{cls}"']


def test_a_recent_focus_event_wins_over_everything(monkeypatch, backend):
    ed._focus_cache.update(editable=False, timestamp=ed._time.monotonic())
    monkeypatch.setattr(ed, "_atspi_focus_editable", lambda: True)
    assert ed.is_focus_editable(haystacks=lambda: _xprop("kate")) is False


def test_the_walk_wins_over_the_window_class(monkeypatch, backend):
    monkeypatch.setattr(ed, "_atspi_focus_editable", lambda: True)
    assert ed.is_focus_editable(haystacks=lambda: _xprop("evince")) is True


def test_a_viewers_window_class_is_read_only_when_at_spi_is_silent(backend):
    assert ed.is_focus_editable(haystacks=lambda: _xprop("evince")) is False


def test_an_unknown_app_is_editable(backend):
    assert ed.is_focus_editable(haystacks=lambda: _xprop("kate")) is True


def test_no_answer_at_all_is_editable(backend):
    assert ed.is_focus_editable(haystacks=lambda: []) is True

    def broken():
        raise RuntimeError("no window manager")

    assert ed.is_focus_editable(haystacks=broken) is True


def test_the_users_own_read_only_classes_count_whatever_their_case(backend):
    assert ed.is_focus_editable(("MyViewer",), haystacks=lambda: _xprop("myviewer")) is False


def test_a_title_that_contains_a_viewers_name_is_not_a_viewer(backend):
    # "eog" is the image viewer; on X11 a window without WM_CLASS has only its title
    assert ed.is_focus_editable(haystacks=lambda: ["geography notes"]) is True
    title_and_class = ["geography notes", 'wm_class(string) = "kate", "kate"']
    assert ed.is_focus_editable(haystacks=lambda: title_and_class) is True


def test_on_kde_the_first_haystack_is_the_class(backend):
    backend("wayland_kde")
    assert ed._window_class(["org.gnome.eog", "holiday.png"]) == "org.gnome.eog"
    assert ed.is_focus_editable(haystacks=lambda: ["org.gnome.eog", "holiday.png"]) is False


# ---- is AT-SPI safe to load ---------------------------------------------------


def test_at_spi_is_not_loaded_without_its_bus_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    ok, why = ed._atspi_environment_safe()
    assert ok is False
    assert "bus socket missing" in why
