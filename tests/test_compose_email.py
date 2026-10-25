"""Compose email: the selection becomes one mailto: argument, quoted, so
it can't add headers or turn into another scheme."""

import pytest

from core import actions


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(actions.subprocess, "Popen", lambda argv, **k: calls.append(argv))
    return calls


@pytest.mark.parametrize(
    ("selected", "url"),
    [
        ("a@b.c", "mailto:a@b.c"),
        ("first+tag@x.org", "mailto:first+tag@x.org"),
        ("a@b.c,d@e.f", "mailto:a@b.c,d@e.f"),
        ("a b@c", "mailto:a%20b@c"),
        ("a@b.c?bcc=evil@x.org", "mailto:a@b.c%3Fbcc%3Devil@x.org"),
        ("a@b.c&body=hi", "mailto:a@b.c%26body%3Dhi"),
        ("​a@b.c​", "mailto:a@b.c"),  # invisible characters are stripped
    ],
)
def test_the_address_is_quoted_into_one_mailto(spawned, selected, url):
    actions.compose_email(selected)
    assert spawned == [["xdg-open", url]]


def test_no_xdg_open_is_not_a_crash(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("xdg-open")

    monkeypatch.setattr(actions.subprocess, "Popen", missing)
    actions.compose_email("a@b.c")
