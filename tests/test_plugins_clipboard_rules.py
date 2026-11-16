"""Snippet trigger rules in the clipboard plugin: which snippet owns a
trigger, what counts as a clash, and which typed character ends one."""

import pytest

pytest.importorskip("gi")  # clipboard_history is a GTK plugin


@pytest.fixture
def ch(monkeypatch):
    try:
        import clipboard_history
    except (ImportError, ValueError) as exc:  # no GTK 4 typelibs on this machine
        pytest.skip(f"GTK 4 unavailable: {exc}")
    monkeypatch.setattr(clipboard_history, "_snippets", [])
    monkeypatch.setattr(clipboard_history, "_trigger_index", {})
    return clipboard_history


def _snippet(ch, snippet_id, trigger, kind="text", name=""):
    return ch.Entry(
        id=snippet_id, timestamp=0, kind=kind, text=snippet_id, trigger=trigger, name=name
    )


def test_the_newest_snippet_wins_a_shared_trigger(ch):
    # Stored newest first
    ch._snippets[:] = [_snippet(ch, "new", ";sig"), _snippet(ch, "old", ";sig, ;o")]
    ch._rebuild_trigger_index()
    assert ch._trigger_index[";sig"].id == "new"
    assert ch._trigger_index[";o"].id == "old"


def test_an_image_snippet_has_no_trigger(ch):
    ch._snippets[:] = [_snippet(ch, "img", ";pic", kind="image")]
    ch._rebuild_trigger_index()
    assert ch._trigger_index == {}


def test_a_trigger_another_snippet_has_is_a_conflict(ch):
    ch._snippets[:] = [_snippet(ch, "a", ";sig, ;s", name="Signature")]
    assert ch._find_trigger_conflicts([";x", ";s"], own_id="b") == [(";s", "Signature")]


def test_a_snippet_does_not_conflict_with_its_own_trigger(ch):
    ch._snippets[:] = [_snippet(ch, "a", ";sig", name="Signature")]
    assert ch._find_trigger_conflicts([";sig"], own_id="a") == []


@pytest.fixture
def fired(ch, monkeypatch):
    calls: list = []
    monkeypatch.setattr(ch.GLib, "idle_add", lambda _fn, *args: calls.append(args))
    ch._snippets[:] = [_snippet(ch, "mvh", ";mvh")]
    ch._rebuild_trigger_index()
    return calls


@pytest.mark.parametrize("boundary", [" ", "\t", "\n"])
def test_whitespace_ends_a_trigger(ch, fired, boundary):
    watcher = ch._TriggerWatcher()
    for char in ";mvh" + boundary:
        watcher._handle_char(char)
    assert [args[:3] for args in fired] == [(";mvh", ";mvh", boundary)]


def test_punctuation_does_not_end_a_trigger(ch, fired):
    watcher = ch._TriggerWatcher()
    for char in "x;mvh;":
        watcher._handle_char(char)
    assert fired == []
    assert "".join(watcher._buffer) == "x;mvh;"
