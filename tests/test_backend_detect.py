"""platform_backend.detect() (#85): the KWin backend only on KDE Wayland,
the X11 backend (XWayland on Wayland) everywhere else. No GTK needed."""

import pytest

from platform_backend import detect, is_kde_desktop, is_wayland_session
from vino_ai import doctor

_VARS = ("VINOPOP_BACKEND", "WAYLAND_DISPLAY", "DISPLAY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP")


@pytest.fixture
def env(monkeypatch):
    for var in _VARS:
        monkeypatch.delenv(var, raising=False)

    def set_env(**values):
        for key, value in values.items():
            monkeypatch.setenv(key, value)

    return set_env


WAYLAND = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0", "XDG_SESSION_TYPE": "wayland"}


@pytest.mark.parametrize("desktop", ["KDE", "kde", "KDE:Plasma", "Plasma:KDE", " kde "])
def test_kde_wayland_gets_kde_backend(env, desktop):
    env(**WAYLAND, XDG_CURRENT_DESKTOP=desktop)
    assert detect() == "wayland_kde"


def test_kde_pure_wayland_without_xwayland(env):
    env(WAYLAND_DISPLAY="wayland-0", XDG_CURRENT_DESKTOP="KDE")
    assert detect() == "wayland_kde"


@pytest.mark.parametrize(
    "desktop", ["sway", "Hyprland", "GNOME", "ubuntu:GNOME", "niri", "KDEish", "LXQt", ""]
)
def test_other_wayland_desktops_get_x11(env, desktop):
    env(**WAYLAND, XDG_CURRENT_DESKTOP=desktop)
    assert detect() == "x11"


def test_other_pure_wayland_still_x11(env):
    env(WAYLAND_DISPLAY="wayland-0", XDG_SESSION_TYPE="wayland", XDG_CURRENT_DESKTOP="sway")
    assert detect() == "x11"


def test_kde_x11_session_gets_x11(env):
    env(DISPLAY=":0", XDG_SESSION_TYPE="x11", XDG_CURRENT_DESKTOP="KDE")
    assert detect() == "x11"


def test_kde_with_wayland_and_x11_but_x11_session(env):
    env(
        WAYLAND_DISPLAY="wayland-0", DISPLAY=":0", XDG_SESSION_TYPE="x11", XDG_CURRENT_DESKTOP="KDE"
    )
    assert not is_wayland_session()
    assert detect() == "x11"


@pytest.mark.parametrize("forced", ["x11", "X11", " x11 "])
def test_override_x11_on_kde_wayland(env, forced):
    env(**WAYLAND, XDG_CURRENT_DESKTOP="KDE", VINOPOP_BACKEND=forced)
    assert detect() == "x11"


def test_override_wayland_kde_elsewhere(env):
    env(**WAYLAND, XDG_CURRENT_DESKTOP="sway", VINOPOP_BACKEND="wayland_kde")
    assert detect() == "wayland_kde"


def test_unknown_override_ignored(env):
    env(**WAYLAND, XDG_CURRENT_DESKTOP="sway", VINOPOP_BACKEND="bogus")
    assert detect() == "x11"


def test_is_kde_desktop_unset(env):
    assert not is_kde_desktop()


def test_doctor_skips_wayland_input_off_kde(env):
    env(**WAYLAND, XDG_CURRENT_DESKTOP="sway")
    assert doctor._wayland_input() == []
