"""core.actions rules that its docstrings cite."""

import pytest

import core.settings
import platform_backend
from core import actions


class _Backend:
    def __init__(self, can_paste=True):
        self.clipboard = None
        self.pastes = 0
        self._can_paste = can_paste

    def set_clipboard(self, text):
        self.clipboard = text

    def paste(self):
        self.pastes += 1

    def can_paste(self):
        return self._can_paste


@pytest.fixture
def notes(monkeypatch):
    shown = []
    monkeypatch.setattr(actions, "notify", lambda *a, **k: shown.append(a))
    monkeypatch.setattr(actions, "SELECTION_OWNER_SETTLE_S", 0)
    return shown


def _use(monkeypatch, backend):
    monkeypatch.setattr(platform_backend, "get_backend", lambda: backend)
    return backend


def test_replace_selection_pastes_over_the_selection(monkeypatch, notes):
    backend = _use(monkeypatch, _Backend())
    actions.replace_selection("new")
    assert (backend.clipboard, backend.pastes) == ("new", 1)
    assert notes == []


def test_force_copy_mode_leaves_the_result_on_the_clipboard_only(monkeypatch, notes):
    backend = _use(monkeypatch, _Backend())
    with actions.force_copy_mode():
        actions.replace_selection("new")
    assert (backend.clipboard, backend.pastes) == ("new", 0)
    assert not actions.force_copy_active()


def test_without_auto_paste_the_user_is_told_to_paste(monkeypatch, notes):
    backend = _use(monkeypatch, _Backend(can_paste=False))
    actions.replace_selection("new")
    assert backend.clipboard == "new"
    assert [n[0] for n in notes] == ["Copied to clipboard"]


def test_desktop_env_fills_in_the_session_bus(monkeypatch):
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    env = actions._desktop_env()
    runtime = f"/run/user/{actions.os.getuid()}"
    assert env["XDG_RUNTIME_DIR"] == runtime
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime}/bus"


def test_desktop_env_keeps_a_bus_already_set(monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/elsewhere")
    assert actions._desktop_env()["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/elsewhere"


def _search_settings(monkeypatch, **values):
    monkeypatch.setattr(core.settings, "get_settings", lambda: values)


def test_a_custom_search_url_with_the_query_is_used(monkeypatch):
    _search_settings(
        monkeypatch, search_engine="custom", search_engine_custom_url="https://s.example/?x={q}"
    )
    assert actions._search_template() == "https://s.example/?x={q}"


def test_a_custom_search_url_without_the_query_falls_back_to_google(monkeypatch):
    _search_settings(
        monkeypatch, search_engine="custom", search_engine_custom_url="https://s.example/"
    )
    assert actions._search_template() == actions.SEARCH_ENGINES["google"][1]


def test_an_unknown_search_engine_falls_back_to_google(monkeypatch):
    _search_settings(monkeypatch, search_engine="altavista")
    assert actions._search_template() == actions.SEARCH_ENGINES["google"][1]
