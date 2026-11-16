"""Paste & Enter submits with the key the focused app treats as "send"."""

import editing_actions
import pytest

import platform_backend


class _Backend:
    def __init__(self, haystacks):
        self.haystacks = haystacks
        self.keys = []

    def active_window_haystacks(self):
        return self.haystacks

    def send_key(self, combo):
        self.keys.append(combo)


@pytest.fixture
def focus(monkeypatch):
    def use(*haystacks):
        backend = _Backend(list(haystacks))
        monkeypatch.setattr(platform_backend, "get_backend", lambda: backend)
        monkeypatch.setattr(editing_actions, "_PASTE_SETTLE_S", 0)
        return backend

    return use


def test_paste_and_enter_submits_with_ctrl_return_in_slack(focus):
    backend = focus("slack", "Slack | general")
    editing_actions._paste_and_enter("")
    assert backend.keys == ["ctrl+v", "ctrl+Return"]


def test_paste_and_enter_submits_with_return_elsewhere(focus):
    backend = focus("gnome-terminal-server", "Terminal")
    editing_actions._paste_and_enter("")
    assert backend.keys == ["ctrl+v", "Return"]
