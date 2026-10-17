"""Snippet/history persistence and the reload lifecycle of the clipboard plugin."""

import json
import sys
import types

import pytest

pytest.importorskip("gi")  # clipboard_history is a GTK plugin


@pytest.fixture
def ch(monkeypatch):
    try:
        import clipboard_history
    except (ImportError, ValueError) as exc:  # no GTK 4 typelibs on this machine
        pytest.skip(f"GTK 4 unavailable: {exc}")
    return clipboard_history


def test_non_numeric_history_size_falls_back_to_the_default(ch, monkeypatch):
    # issue #24: a hand-edited "clipboard_history_size": "lots" raised in
    # the watcher instead of using 25
    stored = {"clipboard_history_size": "lots", "clipboard_poll_interval": None}
    monkeypatch.setattr(ch, "_cfg", lambda k, d: stored.get(k, d))
    assert ch._history_size() == 25
    assert ch._poll_interval() == 0.5


def test_unreadable_snippets_are_backed_up_not_overwritten(ch, tmp_path):
    path = tmp_path / "snippets.json"
    path.write_text('[{"id": "a", "timestamp": 1, "kind": "text", "text": "keep me"')  # truncated
    target: list = []
    assert ch._load_list(path, target) is False
    assert target == []
    assert "keep me" in (tmp_path / "snippets.json.unreadable").read_text()


def test_wrong_shape_counts_as_unreadable(ch, tmp_path):
    path = tmp_path / "snippets.json"
    path.write_text(json.dumps({"snippets": []}))
    assert ch._load_list(path, []) is False
    assert (tmp_path / "snippets.json.unreadable").exists()


def test_entries_with_unknown_fields_survive_a_load(ch, tmp_path):
    path = tmp_path / "snippets.json"
    path.write_text(
        json.dumps(
            [
                {"id": "a", "timestamp": 1.0, "kind": "text", "text": "hi", "from_the_future": 1},
                "not an object",
                {"id": "b", "timestamp": 2.0, "kind": "text", "text": "there"},
            ]
        )
    )
    target: list = []
    assert ch._load_list(path, target) is True
    assert [e.text for e in target] == ["hi", "there"]


def test_shutdown_stops_the_watchers(ch, monkeypatch):
    stopped = []
    monkeypatch.setattr(
        ch, "_trigger_watcher", types.SimpleNamespace(stop=lambda: stopped.append(1))
    )
    ch._watcher_stop.clear()
    ch.shutdown()
    assert ch._watcher_stop.is_set()
    assert stopped == [1]
    assert ch._trigger_watcher is None
    ch._watcher_stop.clear()


def test_a_stopped_watcher_captures_nothing(ch, monkeypatch):
    called = []
    monkeypatch.setattr(ch, "_read_clipboard_targets", lambda: called.append(1) or [])
    ch._watcher_stop.set()
    try:
        ch._capture_current_clipboard()
    finally:
        ch._watcher_stop.clear()
    assert called == []


def test_plugin_loader_shuts_down_the_previous_copy_before_reloading(monkeypatch):
    import plugin_loader

    calls = []
    good = types.ModuleType("vinopop_user_good")
    good.shutdown = lambda: calls.append("good")
    bad = types.ModuleType("vinopop_user_bad")

    def boom():
        raise RuntimeError("nope")

    bad.shutdown = boom
    plain = types.ModuleType("vinopop_user_plain")  # no hook: fine
    other = types.ModuleType("somebody_else")
    other.shutdown = lambda: calls.append("other")
    for mod in (good, bad, plain, other):
        monkeypatch.setitem(sys.modules, mod.__name__, mod)

    plugin_loader._shutdown_user_plugins()
    assert calls == ["good"]  # a failing hook doesn't stop the others; non-plugins are left alone
    # stale copies are gone, so a removed plugin can't be found via sys.modules
    assert not any(n.startswith("vinopop_user_") for n in sys.modules)
    assert "somebody_else" in sys.modules
