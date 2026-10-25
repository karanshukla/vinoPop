"""The {shell:...} snippet placeholder runs bash, so it is off unless the
user turns it on: with the setting off the token comes back as written
and nothing runs."""

import subprocess

import pytest

from core import settings

pytest.importorskip("gi")  # clipboard_history is a GTK plugin


@pytest.fixture
def ch(monkeypatch):
    try:
        import clipboard_history
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 unavailable: {exc}")
    return clipboard_history


@pytest.fixture
def runs(ch, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "out\n\n", "")

    monkeypatch.setattr(ch.subprocess, "run", fake_run)
    return calls


def _enable(ch, monkeypatch, value):
    cfg = {"snippet_shell_enabled": value}
    monkeypatch.setattr(ch, "_cfg", lambda k, d: cfg.get(k, d))


def test_the_default_is_off():
    assert settings.DEFAULTS["snippet_shell_enabled"] is False


@pytest.mark.parametrize("value", [None, False, 0, ""])
def test_off_returns_the_token_and_runs_nothing(ch, monkeypatch, runs, value):
    _enable(ch, monkeypatch, value)
    assert ch._render_shell_token("touch /tmp/pwned") == "{shell:touch /tmp/pwned}"
    assert runs == []


def test_off_by_default_through_the_renderer(ch, monkeypatch, runs):
    monkeypatch.setattr(ch, "_settings", None)
    rendered, _cursor, _cancelled = ch.render_placeholders("a {shell:id} b", lambda _l: None)
    assert rendered == "a {shell:id} b"
    assert runs == []


def test_on_runs_bash_with_a_timeout(ch, monkeypatch, runs):
    _enable(ch, monkeypatch, True)
    assert ch._render_shell_token("  date +%F ") == "out"
    argv, kwargs = runs[0]
    assert argv == ["bash", "-c", "date +%F"]
    assert kwargs["timeout"] == 5.0
    assert kwargs.get("shell") is not True


def test_on_whitespace_only_runs_nothing(ch, monkeypatch, runs):
    _enable(ch, monkeypatch, True)
    assert ch._render_shell_token("   ") == ""
    assert runs == []


def test_a_timeout_is_reported_inline(ch, monkeypatch):
    _enable(ch, monkeypatch, True)

    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(ch.subprocess, "run", slow)
    assert ch._render_shell_token("sleep 60") == "[shell timeout: sleep 60]"


def test_a_missing_bash_is_reported_inline(ch, monkeypatch):
    _enable(ch, monkeypatch, True)

    def missing(argv, **kwargs):
        raise FileNotFoundError("bash")

    monkeypatch.setattr(ch.subprocess, "run", missing)
    assert ch._render_shell_token("id").startswith("[shell error:")


def test_a_literal_snippet_never_reaches_the_shell(ch, monkeypatch, runs):
    # Pinned-from-selection text is someone else's (#86)
    _enable(ch, monkeypatch, True)
    entry = ch.Entry(id="s", timestamp=0, kind="text", text="{shell:id}", literal=True)
    monkeypatch.setattr(ch, "_trigger_blocked", lambda: False)
    monkeypatch.setattr(ch, "_bump_usage", lambda _id: None)
    monkeypatch.setattr(ch, "_is_wayland", lambda: False)
    monkeypatch.setattr(ch.threading, "Thread", _NoThread)
    ch._fire_trigger_expansion(";x", ";x", " ", entry)
    assert runs == []


class _NoThread:
    def __init__(self, target=None, **_kw):
        self.target = target

    def start(self):
        pass
