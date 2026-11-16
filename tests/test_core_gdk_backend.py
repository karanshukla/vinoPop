"""Where main.py runs GDK: native Wayland only for KDE with gtk4-layer-shell,
XWayland everywhere else."""

import os

import pytest

pytest.importorskip("gi")
pytest.importorskip("Xlib")


@pytest.fixture(scope="module")
def main_mod():
    mp = pytest.MonkeyPatch()
    mp.setenv("GDK_BACKEND", "x11")
    try:
        import main
    finally:
        mp.undo()
    return main


@pytest.fixture
def choose(main_mod, monkeypatch):
    """Run _choose_gdk_backend for a session; returns (log line, GDK_BACKEND)."""

    def run(*, wayland=True, backend="x11", layer_shell=True, display=":0"):
        monkeypatch.delenv("GDK_BACKEND", raising=False)
        monkeypatch.delenv("VINOPOP_BACKEND", raising=False)
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "sway")
        if display:
            monkeypatch.setenv("DISPLAY", display)
        else:
            monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setattr(main_mod, "is_wayland_session", lambda: wayland)
        monkeypatch.setattr(main_mod, "detect_backend", lambda: backend)
        monkeypatch.setattr(main_mod, "_load_layer_shell", lambda: layer_shell)
        line = main_mod._choose_gdk_backend()
        return line, os.environ.get("GDK_BACKEND")

    return run


def test_a_preset_gdk_backend_is_left_alone(main_mod, monkeypatch):
    monkeypatch.setenv("GDK_BACKEND", "wayland")
    monkeypatch.setattr(main_mod, "is_wayland_session", lambda: True)
    monkeypatch.setattr(main_mod, "detect_backend", lambda: "x11")
    assert "left alone" in main_mod._choose_gdk_backend()
    assert os.environ["GDK_BACKEND"] == "wayland"


def test_an_x11_session_stays_on_x11(choose):
    line, gdk = choose(wayland=False)
    assert gdk is None
    assert "X11 session" in line


def test_a_non_kde_wayland_desktop_runs_on_xwayland(choose):
    line, gdk = choose(backend="x11")
    assert gdk == "x11"
    assert "sway is not KDE Plasma" in line


def test_kde_without_layer_shell_runs_on_xwayland(choose):
    line, gdk = choose(backend="wayland_kde", layer_shell=False)
    assert gdk == "x11"
    assert "gtk4-layer-shell" in line


def test_kde_with_layer_shell_runs_native_wayland(choose):
    line, gdk = choose(backend="wayland_kde", layer_shell=True)
    assert gdk is None
    assert "native Wayland" in line


def test_without_xwayland_the_backend_is_not_forced(choose):
    line, gdk = choose(backend="x11", display="")
    assert gdk is None
    assert "popup placement unavailable" in line
