"""Snippet/history persistence and the reload lifecycle of the clipboard plugin."""

import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("gi")  # clipboard_history is a GTK plugin


@pytest.fixture
def ch(monkeypatch):
    try:
        import clipboard_history
    except (ImportError, ValueError) as exc:  # no GTK 4 typelibs on this machine
        pytest.skip(f"GTK 4 unavailable: {exc}")
    return clipboard_history


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


# ----- issue #21: secrets, file modes, delete, size caps ----------------------


@pytest.fixture
def store(ch, monkeypatch, tmp_path):
    """The plugin's stores pointed at a fresh tmp dir, with a 022 umask so a
    0644 file would show up as one."""
    cache = tmp_path / "cache" / "vinopop"
    config = tmp_path / "config" / "vinopop"
    clip = cache / "clipboard"
    monkeypatch.setattr(ch, "_LP_CACHE", cache)
    monkeypatch.setattr(ch, "_LP_CONFIG", config)
    monkeypatch.setattr(ch, "CACHE_DIR", clip)
    monkeypatch.setattr(ch, "HISTORY_FILE", clip / "history.json")
    monkeypatch.setattr(ch, "IMAGES_DIR", clip / "images")
    monkeypatch.setattr(ch, "SNIPPETS_FILE", config / "snippets.json")
    monkeypatch.setattr(ch, "_DEDUP_STATE_FILE", clip / "last_image.sha1")
    monkeypatch.setattr(ch, "_history", [])
    monkeypatch.setattr(ch, "_snippets", [])
    monkeypatch.setitem(ch._state, "last_text", "")
    monkeypatch.setitem(ch._state, "last_image_hash", b"")
    settings: dict = {}
    monkeypatch.setattr(ch, "_cfg", lambda k, d: settings.get(k, d))
    ch.settings = settings  # tests set keys here
    ch._watcher_stop.clear()
    old = os.umask(0o022)
    yield ch
    os.umask(old)
    del ch.settings


def _clipboard(ch, monkeypatch, targets, text="", image=None, hint=b"secret"):
    """Fake the external clipboard readers."""
    monkeypatch.setattr(ch, "_read_clipboard_targets", lambda: list(targets))
    monkeypatch.setattr(ch, "_read_clipboard_text", lambda: text)
    monkeypatch.setattr(ch, "_read_clipboard_image", lambda: image)
    monkeypatch.setattr(ch, "_read_clipboard_target", lambda t: hint)


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_plain_copy_is_captured(store, monkeypatch):
    _clipboard(store, monkeypatch, ["UTF8_STRING", "text/plain"], text="hello")
    store._capture_current_clipboard()
    assert [e.text for e in store._history] == ["hello"]


@pytest.mark.parametrize(
    ("targets", "hint"),
    [
        (["text/plain", "x-kde-passwordManagerHint"], b"secret"),
        (["UTF8_STRING", "x-kde-passwordManagerHint"], b"secret\n"),
        (["text/plain", "x-kde-passwordManagerHint"], None),  # unreadable: assume secret
        (["text/plain", "application/x-nspasteboard-concealed-type"], None),
    ],
)
def test_password_manager_copies_are_not_captured(store, monkeypatch, targets, hint):
    _clipboard(store, monkeypatch, targets, text="hunter2", hint=hint)
    store._capture_current_clipboard()
    assert store._history == []
    assert not store.HISTORY_FILE.exists()


def test_a_non_secret_hint_value_is_captured(store, monkeypatch):
    _clipboard(
        store, monkeypatch, ["text/plain", "x-kde-passwordManagerHint"], text="hi", hint=b"other"
    )
    store._capture_current_clipboard()
    assert [e.text for e in store._history] == ["hi"]


def test_copies_from_a_blocklisted_window_are_not_captured(store, monkeypatch):
    import platform_backend

    backend = types.SimpleNamespace(active_window_haystacks=lambda: ["keepassxc", "vault"])
    monkeypatch.setattr(platform_backend, "get_backend", lambda: backend)
    store.settings["blocklist_patterns"] = ["KeePassXC"]
    _clipboard(store, monkeypatch, ["text/plain"], text="hunter2")
    store._capture_current_clipboard()
    assert store._history == []

    store.settings["blocklist_patterns"] = ["1Password"]
    _clipboard(store, monkeypatch, ["text/plain"], text="not secret")
    store._capture_current_clipboard()
    assert [e.text for e in store._history] == ["not secret"]


def test_history_and_images_are_private(store, monkeypatch):
    _clipboard(store, monkeypatch, ["text/plain"], text="hello")
    store._capture_current_clipboard()
    _clipboard(store, monkeypatch, ["image/png"], image=b"\x89PNG fake")
    store._capture_current_clipboard()

    image = Path(store._history[0].image_path)
    for f in (store.HISTORY_FILE, image, store._DEDUP_STATE_FILE):
        assert _mode(f) == 0o600, f
    for d in (store.CACHE_DIR, store.IMAGES_DIR):
        assert _mode(d) == 0o700, d


def test_snippets_file_is_private(store):
    store._create_snippet("sig", "Regards")
    assert _mode(store.SNIPPETS_FILE) == 0o600
    assert _mode(store.SNIPPETS_FILE.parent) == 0o700


def test_files_from_older_versions_are_made_private(store):
    for d in (store._LP_CACHE, store.CACHE_DIR, store.IMAGES_DIR, store._LP_CONFIG):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o755)
    old = [store.HISTORY_FILE, store.SNIPPETS_FILE, store.IMAGES_DIR / "abc.png"]
    for f in old:
        f.write_text("[]")
        f.chmod(0o644)
    store._tighten_permissions()
    for f in old:
        assert _mode(f) == 0o600, f
    for d in (store._LP_CACHE, store.CACHE_DIR, store.IMAGES_DIR, store._LP_CONFIG):
        assert _mode(d) == 0o700, d


def _image_entry(store, eid):
    store.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    path = store.IMAGES_DIR / f"{eid}.png"
    path.write_bytes(b"png")
    return store.Entry(id=eid, timestamp=1.0, kind="image", image_path=str(path))


def test_delete_removes_the_entry_and_its_image(store):
    text = store.Entry(id="t1", timestamp=2.0, kind="text", text="oops my password")
    img = _image_entry(store, "i1")
    store._history[:] = [text, img]

    assert store._remove_history_entry("t1") is True
    assert store._remove_history_entry("i1") is True
    assert store._history == []
    assert not Path(img.image_path).exists()
    # persisted straight away
    assert json.loads(store.HISTORY_FILE.read_text()) == []
    assert store._remove_history_entry("t1") is False


def test_delete_keeps_an_image_a_snippet_uses(store):
    img = _image_entry(store, "i2")
    store._history[:] = [img]
    store._pin_entry(img, "logo")

    store._remove_history_entry("i2")
    assert store._history == []
    assert Path(img.image_path).exists()


def test_oversized_text_is_not_kept(store, monkeypatch):
    store.settings["clipboard_history_max_text_kb"] = 1
    _clipboard(store, monkeypatch, ["text/plain"], text="x" * 2048)
    store._capture_current_clipboard()
    assert store._history == []

    _clipboard(store, monkeypatch, ["text/plain"], text="x" * 1000)
    store._capture_current_clipboard()
    assert len(store._history) == 1


def test_oversized_image_is_not_kept(store, monkeypatch):
    store.settings["clipboard_history_max_image_mb"] = 1
    _clipboard(store, monkeypatch, ["image/png"], image=b"\0" * (1024 * 1024 + 1))
    store._capture_current_clipboard()
    assert store._history == []
    assert not store.IMAGES_DIR.exists() or not any(store.IMAGES_DIR.iterdir())


def test_image_path_is_not_searchable(store):
    img = store.Entry(
        id="i3", timestamp=1.0, kind="image", image_path="/home/karan/.cache/vinopop/a1b2.png"
    )
    hay = img.search_haystack()
    assert "image" in hay
    for needle in ("home", "karan", "a1", ".png"):
        assert needle not in hay


def test_search_haystack_follows_edits(store):
    e = store.Entry(id="s1", timestamp=1.0, kind="text", text="Hello", name="Greeting")
    assert "greeting" in e.search_haystack()
    e.name = "Salutation"
    e.text = "Hi"
    hay = e.search_haystack()
    assert "salutation" in hay and "greeting" not in hay and "hello" not in hay
    assert "_haystack" not in store.asdict(e)  # the cache isn't saved


def test_long_preview_matches_the_short_one(store):
    e = store.Entry(id="p", timestamp=1.0, kind="text", text="a\tb\n" * 1000)
    assert e.preview(10) == ("a  b↵" * 3)[:10] + "…"
    assert store.Entry(id="q", timestamp=1.0, kind="text", text="short").preview() == "short"
