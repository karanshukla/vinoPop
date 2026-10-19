"""Only web links reach xdg-open as links: selected text is whatever a page
put there, and xdg-open hands file: and custom schemes to whatever claims them."""

import pytest

import platform_backend
from core import actions
from plugin_system import recipe_loader


class _Backend:
    def __init__(self):
        self.opened = []

    def open_url(self, url):
        self.opened.append(url)


@pytest.fixture
def opened(monkeypatch):
    backend = _Backend()
    monkeypatch.setattr(platform_backend, "get_backend", lambda: backend)
    monkeypatch.setattr(actions, "notify", lambda *a, **k: None)
    return backend.opened


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(actions.subprocess, "Popen", lambda argv, **k: calls.append(argv))
    return calls


@pytest.mark.parametrize(
    "url", ["https://example.com/a", "http://x.org", "ftp://ftp.x.org/f", "mailto:a@b.c"]
)
def test_web_links_open(opened, url):
    assert actions.open_web_url(url)
    assert opened == [url]


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "steam://run/1", "vscode://x", "javascript:alert(1)", "/tmp/x"]
)
def test_other_schemes_are_refused(opened, url):
    assert not actions.open_web_url(url)
    assert opened == []


def test_open_in_browser_on_a_file_url_opens_its_folder(opened, spawned, tmp_path):
    exe = tmp_path / "evil.exe"
    exe.write_bytes(b"MZ")
    actions.open_url(f"file://{exe}")
    assert opened == []
    assert spawned == [["xdg-open", str(tmp_path)]]


def test_a_file_url_to_a_folder_opens_that_folder(opened, spawned, tmp_path):
    (tmp_path / "my dir").mkdir()
    actions.open_url(f"file://{tmp_path}/my%20dir")
    assert spawned == [["xdg-open", str(tmp_path / "my dir")]]


def test_naked_urls_still_get_https(opened):
    actions.open_url("example.com/page")
    assert opened == ["https://example.com/page"]


def test_an_open_url_recipe_cannot_open_other_schemes(opened):
    recipe = {"name": "x", "action": {"type": "open_url", "template": "{text}"}}
    handler = recipe_loader._build_handler(recipe)
    handler("vscode://evil/run")
    handler("https://example.com/ok")
    assert opened == ["https://example.com/ok"]


def test_a_chain_cannot_open_a_decoded_file_url(opened):
    import base64
    import json
    from pathlib import Path

    path = (
        Path(recipe_loader.__file__).parent.parent
        / "plugins_repo/recipes/chain-base64-decode-open.json"
    )
    handler = recipe_loader._build_handler(json.loads(path.read_text()))
    handler(base64.b64encode(b"file:///home/u/Downloads/evil.exe").decode())
    assert opened == []
    handler(base64.b64encode(b"https://example.com/").decode())
    assert opened == ["https://example.com/"]
