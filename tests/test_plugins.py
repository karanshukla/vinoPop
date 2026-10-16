"""Every GTK-free bundled plugin imports and registers at least one action."""

import importlib
from pathlib import Path

import pytest

from plugin_base import Plugin

PLUGINS = Path(__file__).resolve().parent.parent / "plugins_repo"
GTK_FREE = sorted(
    p.stem
    for p in PLUGINS.glob("*.py")
    if "gi.repository" not in p.read_text() and "import gi" not in p.read_text()
)


@pytest.mark.parametrize("name", GTK_FREE)
def test_plugin_registers(name):
    registered: list[Plugin] = []
    importlib.import_module(name).register(registered.append)
    assert registered
    assert all(isinstance(p, Plugin) and p.name and callable(p.handler) for p in registered)


def test_calculator_predicate():
    import calculator

    assert calculator._looks_like_math("2 * (3 + 4)")
    assert not calculator._looks_like_math("hello world")


def test_url_decode_predicate():
    import url_encode

    assert url_encode._looks_percent_encoded("a%20b%2Fc")
    assert not url_encode._looks_percent_encoded("plain text")


def test_base64_predicate():
    import base64_codec

    assert base64_codec._looks_like_base64("aGVsbG8gdmlub1BvcCBmcm9tIGEgdGVzdA==")
    assert not base64_codec._looks_like_base64("not base64 at all!")
