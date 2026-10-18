"""Popup placement (#25): shared KWin pointer queries, per-output
layer-shell margins, HiDPI conversion for the AI bubble, per-monitor OCR
capture. All with fake monitors; no compositor needed."""

import threading
import time
from types import SimpleNamespace

import pytest

from platform_backend.monitors import layer_margins, monitor_at, to_logical


class _Mon:
    def __init__(self, x, y, w, h, scale=1, connector=None):
        self._g = SimpleNamespace(x=x, y=y, width=w, height=h)
        self._scale = scale
        self._connector = connector

    def get_geometry(self):
        return self._g

    def get_scale_factor(self):
        return self._scale

    def get_connector(self):
        return self._connector


LEFT = _Mon(0, 0, 1920, 1080, connector="eDP-1")
RIGHT = _Mon(1920, 0, 2560, 1440, connector="DP-2")


# ---- monitor maths --------------------------------------------------------
def test_margins_are_relative_to_the_second_monitor():
    mon = monitor_at([LEFT, RIGHT], 2000, 300)
    assert mon is RIGHT
    assert layer_margins(2000, 300, mon.get_geometry()) == (80, 300)


def test_margins_on_the_primary_monitor_are_unchanged():
    assert layer_margins(500, 400, monitor_at([LEFT, RIGHT], 500, 400).get_geometry()) == (
        500,
        400,
    )


def test_point_on_no_monitor_falls_back_only_when_asked():
    assert monitor_at([LEFT, RIGHT], -10000, -10000) is LEFT
    assert monitor_at([LEFT, RIGHT], -10000, -10000, fallback=False) is None


def test_physical_coords_are_scaled_per_monitor():
    hidpi = _Mon(0, 0, 1920, 1080, scale=2)
    # X11 root coords (3000, 1600) on a scale-2 screen = logical (1500, 800)
    mon = monitor_at([hidpi], 3000, 1600, is_logical=False)
    assert mon is hidpi
    assert to_logical(3000, 1600, mon, is_logical=False) == (1500, 800)
    assert to_logical(3000, 1600, mon, is_logical=True) == (3000, 1600)


# ---- shared KWin pointer query --------------------------------------------
@pytest.fixture
def wayland_backend(monkeypatch):
    pytest.importorskip("gi")
    from platform_backend import wayland_kde

    calls = []

    class FakeKWin:
        answers: list[str] = []

        def query(self, kind):
            calls.append(kind)
            time.sleep(0.05)  # a real round-trip takes a few ms
            return self.answers.pop(0) if self.answers else "2100 640"

    backend = wayland_kde.WaylandKdeBackend()
    backend._kwin = FakeKWin()
    return backend, calls


def test_concurrent_pointer_queries_share_one_result(wayland_backend):
    backend, calls = wayland_backend
    n = 8
    barrier = threading.Barrier(n)
    results = []

    def ask():
        barrier.wait()
        results.append(backend.pointer_position())

    threads = [threading.Thread(target=ask) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert results == [(2100, 640)] * n
    assert len(calls) == 1


def test_pointer_cache_expires(wayland_backend):
    backend, calls = wayland_backend
    backend.pointer_position()
    time.sleep(backend._POINTER_CACHE_S + 0.05)
    backend.pointer_position()
    assert len(calls) == 2


def test_failed_pointer_query_is_not_cached(wayland_backend):
    backend, _ = wayland_backend
    backend._kwin.answers = ["", "5 6"]
    assert backend.pointer_position() == (0, 0)
    assert backend.pointer_position() == (5, 6)


# ---- layer-shell placement -------------------------------------------------
class _FakeLayer:
    class Edge:
        LEFT, TOP = "left", "top"

    def __init__(self):
        self.monitor = None
        self.set_monitor_calls = 0
        self.margins = {}

    def set_anchor(self, *_a):
        pass

    def set_monitor(self, _win, mon):
        self.monitor = mon
        self.set_monitor_calls += 1

    def set_margin(self, _win, edge, v):
        self.margins[edge] = v


class _FakeWin:
    def get_display(self):
        return SimpleNamespace(get_monitors=lambda: [LEFT, RIGHT])


@pytest.fixture
def layer(monkeypatch):
    pytest.importorskip("gi")
    from platform_backend import wayland_kde, x11

    fake = _FakeLayer()
    monkeypatch.setattr(wayland_kde, "_layer_shell", lambda: fake)
    monkeypatch.setattr(x11, "gdk_display_is_x11", lambda _d=None: False)
    return wayland_kde.WaylandKdeBackend(), fake


def test_layer_popup_on_second_monitor(layer):
    backend, fake = layer
    win = _FakeWin()
    backend.move_popup_window(win, 1920 + 300, 200)
    assert fake.monitor is RIGHT
    assert fake.margins == {"left": 300, "top": 200}
    # Same output again: no needless remap.
    backend.move_popup_window(win, 1920 + 50, 60)
    assert fake.set_monitor_calls == 1
    backend.move_popup_window(win, 100, 60)
    assert fake.monitor is LEFT
    assert fake.margins == {"left": 100, "top": 60}


def test_offscreen_parking_keeps_the_output(layer):
    backend, fake = layer
    backend.move_popup_window(_FakeWin(), -10000, -10000)
    assert fake.monitor is None
    assert fake.margins == {"left": -10000, "top": -10000}


# ---- AI bubble on X11 HiDPI ------------------------------------------------
def test_ai_bubble_uses_logical_coords():
    pytest.importorskip("gi")
    try:
        import ai_bubble
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 unavailable: {exc}")
    hidpi = _Mon(0, 0, 1920, 1080, scale=2)
    lx, ly = to_logical(1000, 600, hidpi, is_logical=False)
    tx, ty = ai_bubble._bubble_target(lx, ly, 300, 100, hidpi.get_geometry())
    # Centred under logical (500, 300), not clamped into the bottom-right
    # corner as the physical (1000, 600) used to be after GTK doubled it.
    assert (tx, ty) == (350, 324)


# ---- KWin helper bus names -------------------------------------------------
@pytest.mark.parametrize("mod", ["_kwin_cursor", "_kwin_active"])
def test_kwin_helper_bus_names_are_unique(mod):
    pytest.importorskip("dbus")
    import importlib

    helper = importlib.import_module(f"platform_backend.{mod}")
    a, b = helper.bus_name(), helper.bus_name()
    assert a != b
    assert a.startswith(helper.IFACE + ".p")
    # D-Bus: name elements may not start with a digit.
    assert all(part and not part[0].isdigit() for part in a.split("."))
    js = helper.script_js(a)
    assert f'callDBus("{a}"' in js
    assert f'"{helper.IFACE}", "Report"' in js


# ---- OCR overlay: grab only the pointer's monitor --------------------------
def test_ocr_capture_argv_per_monitor():
    pytest.importorskip("gi")
    try:
        import ocr_selector
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 unavailable: {exc}")
    argv = ocr_selector._capture_argv
    assert argv("spectacle", "/t.png") == ["spectacle", "-f", "-b", "-n", "-o", "/t.png"]
    assert argv("spectacle", "/t.png", RIGHT)[:2] == ["spectacle", "-m"]
    assert argv("grim", "/t.png", RIGHT) == ["grim", "-o", "DP-2", "/t.png"]
    assert argv("grim", "/t.png", _Mon(1920, 0, 800, 600)) == [
        "grim",
        "-g",
        "1920,0 800x600",
        "/t.png",
    ]
    assert argv("maim", "/t.png", _Mon(1920, 0, 1280, 720, scale=2)) == [
        "maim",
        "-g",
        "2560x1440+3840+0",
        "/t.png",
    ]
