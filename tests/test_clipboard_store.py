"""Snippet/history persistence and the reload lifecycle of the clipboard plugin."""

import json
import os
import stat
import sys
import threading
import time
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
    monkeypatch.setattr(ch, "SNIPPET_IMAGES_DIR", tmp_path / "data" / "vinopop" / "snippet_images")
    monkeypatch.setattr(ch, "_history", [])
    monkeypatch.setattr(ch, "_snippets", [])
    monkeypatch.setitem(ch._state, "last_text", "")
    monkeypatch.setitem(ch._state, "last_image_hash", b"")
    monkeypatch.setattr(ch, "_own_copies", {})
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


def _image_snippet(store, image_path):
    return store.Entry(id="s", timestamp=1.0, kind="image", image_path=image_path)


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
    # snippets pinned before #39 share the history entry's file
    img = _image_entry(store, "i2")
    store._history[:] = [img]
    store._snippets[:] = [_image_snippet(store, img.image_path)]

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


# ----- issue #39 ---------------------------------------------------------------


def test_a_pinned_image_survives_its_history_entry_ageing_out(store, monkeypatch):
    store.settings["clipboard_history_size"] = 3
    _clipboard(store, monkeypatch, ["image/png"], image=b"\x89PNG screenshot")
    store._capture_current_clipboard()
    shot = store._history[0]
    store._pin_entry(shot, "screenshot")
    snippet = store._snippets[0]
    assert snippet.image_path != shot.image_path
    assert Path(snippet.image_path).parent == store.SNIPPET_IMAGES_DIR
    assert _mode(Path(snippet.image_path)) == 0o600

    for i in range(5):
        _clipboard(store, monkeypatch, ["text/plain"], text=f"copy {i}")
        store._capture_current_clipboard()
    assert shot not in store._history
    assert not Path(shot.image_path).exists()  # history's own file is still cleaned up
    assert Path(snippet.image_path).read_bytes() == b"\x89PNG screenshot"

    store._unpin_snippet(snippet.id)  # and the snippet's copy goes with the snippet
    assert not Path(snippet.image_path).exists()


def test_eviction_keeps_an_image_an_older_pin_shares(store, monkeypatch):
    store.settings["clipboard_history_size"] = 1
    img = _image_entry(store, "old")
    store._history[:] = [img]
    store._snippets[:] = [_image_snippet(store, img.image_path)]
    _clipboard(store, monkeypatch, ["text/plain"], text="push it out")
    store._capture_current_clipboard()
    assert [e.text for e in store._history] == ["push it out"]
    assert Path(img.image_path).exists()


def _kinds(store):
    return [(e.kind, e.text) for e in store._history]


def test_text_copied_again_after_an_image_comes_back_to_the_top(store, monkeypatch):
    _clipboard(store, monkeypatch, ["text/plain"], text="A")
    store._capture_current_clipboard()
    _clipboard(store, monkeypatch, ["image/png"], image=b"\x89PNG one")
    store._capture_current_clipboard()
    _clipboard(store, monkeypatch, ["text/plain"], text="A")
    store._capture_current_clipboard()
    assert _kinds(store) == [("text", "A"), ("image", "")]


def test_image_copied_again_after_text_comes_back_to_the_top(store, monkeypatch):
    _clipboard(store, monkeypatch, ["image/png"], image=b"\x89PNG one")
    store._capture_current_clipboard()
    _clipboard(store, monkeypatch, ["text/plain"], text="A")
    store._capture_current_clipboard()
    _clipboard(store, monkeypatch, ["image/png"], image=b"\x89PNG one")
    store._capture_current_clipboard()
    assert _kinds(store) == [("image", ""), ("text", "A")]
    assert len(list(store.IMAGES_DIR.iterdir())) == 1


def test_text_wins_when_an_image_is_offered_too(store, monkeypatch):
    # LibreOffice Calc: the cell's text plus a bitmap of it
    _clipboard(store, monkeypatch, ["image/png", "text/plain"], text="42", image=b"\x89PNG cell")
    store._capture_current_clipboard()
    assert _kinds(store) == [("text", "42")]


def test_an_svg_only_copy_is_kept_as_text(store, monkeypatch):
    # Inkscape offers image/svg+xml and no PNG
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    _clipboard(store, monkeypatch, ["image/svg+xml"], image=None, hint=svg)
    store._capture_current_clipboard()
    assert _kinds(store) == [("text", svg.decode())]


def test_pasting_an_older_text_entry_moves_it_up(store, monkeypatch):
    for t in ("A", "B", "C"):
        _clipboard(store, monkeypatch, ["text/plain"], text=t)
        store._capture_current_clipboard()
    first_id = store._history[-1].id
    # the picker puts "A" back on the clipboard and the watcher sees it
    _clipboard(store, monkeypatch, ["text/plain"], text="A")
    store._capture_current_clipboard()
    assert [e.text for e in store._history] == ["A", "C", "B"]
    assert store._history[0].id == first_id
    assert [e["text"] for e in json.loads(store.HISTORY_FILE.read_text())] == ["A", "C", "B"]


def test_pasting_an_older_image_moves_it_up_without_a_second_file(store, monkeypatch):
    _clipboard(store, monkeypatch, ["image/png"], image=b"\x89PNG one")
    store._capture_current_clipboard()
    first = store._history[0]
    _clipboard(store, monkeypatch, ["image/png"], image=b"\x89PNG two")
    store._capture_current_clipboard()
    _clipboard(store, monkeypatch, ["image/png"], image=b"\x89PNG one")
    store._capture_current_clipboard()
    assert [e.id for e in store._history][0] == first.id
    assert len(store._history) == 2
    assert len(list(store.IMAGES_DIR.iterdir())) == 2


@pytest.fixture
def picker(store, monkeypatch):
    p = store._PickerDialog()
    monkeypatch.setattr(p, "_populate_snippets", lambda: None)
    return p


def test_pin_keeps_the_edited_body_trigger_and_category(store, picker, monkeypatch):
    entry = store.Entry(id="h", timestamp=1.0, kind="text", text="draft")
    monkeypatch.setattr(
        picker, "_ask_edit_snippet_meta", lambda **_: ("Sig", ";sig", "Email", "Regards, K")
    )
    picker._on_pin_clicked(None, entry)
    [s] = store._snippets
    assert (s.name, s.trigger, s.category, s.text) == ("Sig", ";sig", "Email", "Regards, K")
    assert store._trigger_index[";sig"] is s
    assert entry.text == "draft"  # the history entry is left alone


@pytest.mark.parametrize("confirm", [False, True])
def test_pin_checks_for_trigger_conflicts(store, picker, monkeypatch, confirm):
    store._create_snippet("Old", "old body", trigger=";x")
    asked = []
    monkeypatch.setattr(picker, "_confirm_trigger_conflicts", lambda c: asked.append(c) or confirm)
    monkeypatch.setattr(picker, "_ask_edit_snippet_meta", lambda **_: ("New", ";x", "", "new"))
    picker._on_pin_clicked(None, store.Entry(id="h", timestamp=1.0, kind="text", text="new"))
    assert asked == [[(";x", "Old")]]
    assert [s.name for s in store._snippets] == (["New", "Old"] if confirm else ["Old"])


def test_expansion_text_and_the_restored_copy_are_not_recorded(store, monkeypatch):
    _clipboard(store, monkeypatch, ["text/plain"], text="mine")
    store._capture_current_clipboard()
    store._mark_own_copy("Kind regards, Karan")  # what the expansion stages
    _clipboard(store, monkeypatch, ["text/plain"], text="Kind regards, Karan")
    store._capture_current_clipboard()
    assert [e.text for e in store._history] == ["mine"]

    # put back after the paste: a copy from a blocklisted app must stay unrecorded
    store._mark_own_copy("from keepassxc")
    _clipboard(store, monkeypatch, ["text/plain"], text="from keepassxc")
    store._capture_current_clipboard()
    assert [e.text for e in store._history] == ["mine"]


def test_expansion_restores_the_clipboard(store, monkeypatch):
    writes = []
    monkeypatch.setattr(
        store, "_write_clipboard", lambda kind, data, primary=False: writes.append((kind, data))
    )
    _clipboard(store, monkeypatch, ["text/plain"], text="mine")
    saved = store._snapshot_clipboard()
    assert saved == ("text", b"mine")

    # something else was copied in the meantime: leave it
    store._restore_clipboard(saved, staged="snippet")
    assert writes == []

    _clipboard(store, monkeypatch, ["text/plain"], text="snippet")
    store._restore_clipboard(saved, staged="snippet")
    assert writes == [("text", b"mine")]
    assert store._is_own_copy("mine")


def test_a_password_copy_is_not_snapshotted(store, monkeypatch):
    _clipboard(store, monkeypatch, ["text/plain", "x-kde-passwordManagerHint"], text="hunter2")
    assert store._snapshot_clipboard() is None


def test_concurrent_saves_are_not_lost(store):
    errors = []

    def writer(n):
        for i in range(200):
            try:
                store._atomic_write_json(store.HISTORY_FILE, [n, i])
            except OSError as exc:
                errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert json.loads(store.HISTORY_FILE.read_text())[1] == 199
    assert _mode(store.HISTORY_FILE) == 0o600
    assert [p.name for p in store.HISTORY_FILE.parent.iterdir()] == ["history.json"]


def test_shutdown_waits_for_the_watcher(ch, monkeypatch):
    ch._watcher_stop.clear()
    finished = []

    def loop():
        ch._watcher_stop.wait()
        time.sleep(0.2)  # a capture still saving
        finished.append(1)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    monkeypatch.setattr(ch, "_watcher_thread", t)
    monkeypatch.setattr(ch, "_trigger_watcher", None)
    ch.shutdown()
    ch._watcher_stop.clear()
    assert finished == [1]


def test_capture_still_works_with_a_bad_history_size(store, monkeypatch):
    # a hand-edited "clipboard_history_size": "lots" used to raise on every copy
    store.settings["clipboard_history_size"] = "lots"
    _clipboard(store, monkeypatch, ["text/plain"], text="still here")
    store._capture_current_clipboard()
    assert [e.text for e in store._history] == ["still here"]
