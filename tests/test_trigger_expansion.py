"""Snippet text expansion: typing a trigger then space deletes the trigger
and pastes the snippet into the focused window. These check which typed
text counts as a trigger, how its case carries over, and what the X11
worker sends (BackSpaces, paste, clipboard put back), with the keyboard
and clipboard faked."""

from types import SimpleNamespace

import pytest

pytest.importorskip("gi")  # clipboard_history is a GTK plugin


@pytest.fixture
def ch():
    try:
        import clipboard_history
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 unavailable: {exc}")
    return clipboard_history


# ── word boundary ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("buf", "trig", "ok"),
    [
        ("brb", "brb", True),  # whole buffer
        ("ok brb", "brb", True),  # after a space
        ("(brb", "brb", True),  # after punctuation
        ("superb brb", "brb", True),
        ("herbrb", "brb", False),  # inside a word
        ("x_brb", "brb", False),  # underscore is a word char
        ("1brb", "brb", False),  # so is a digit
        ("word;sig", ";sig", True),  # a punctuation trigger is its own boundary
        ("é brb", "brb", True),
        ("ébrb", "brb", False),  # non-ASCII letters are word chars too
    ],
)
def test_word_boundary(ch, buf, trig, ok):
    assert ch._trigger_word_boundary_ok(buf, trig) is ok


# ── case ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("typed", "style"),
    [
        ("brb", "lower"),
        ("Brb", "title"),
        ("BRB", "upper"),
        ("B", "title"),  # one capital is ambiguous; treat as title
        (";Sig", "title"),
        ("bRb", "mixed"),
        (";;1", "lower"),  # no letters: no change
    ],
)
def test_detect_case_style(ch, typed, style):
    assert ch._detect_case_style(typed) == style


@pytest.mark.parametrize(
    ("typed", "out", "want"),
    [
        ("brb", "be right back", "be right back"),
        ("Brb", "be right back", "Be right back"),
        ("BRB", "be right back", "BE RIGHT BACK"),
        ("bRb", "be right back", "be right back"),
        ("Sig", "-- \nKaran", "-- \nKaran"),  # first alpha char only
        ("Sig", "123 main st", "123 Main st"),
        ("Sig", "", ""),
    ],
)
def test_propagate_case(ch, typed, out, want):
    assert ch._propagate_case(typed, out) == want


# ── the X11 worker ───────────────────────────────────────────────────────────


class _SyncThread:
    def __init__(self, target=None, **_kw):
        self.target = target

    def start(self):
        self.target()


@pytest.fixture
def x11(ch, monkeypatch):
    """Everything _fire_trigger_expansion's X11 worker touches, faked and
    recorded. `clip` is the clipboard's text; restore happens only if it
    still holds the expansion."""
    rec = SimpleNamespace(keys=[], keyups=[], writes=[], blocked=False, clip="old")
    monkeypatch.setattr(ch.threading, "Thread", _SyncThread)
    monkeypatch.setattr(ch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ch, "_is_wayland", lambda: False)
    monkeypatch.setattr(ch, "_x_can_inject", lambda: True)
    monkeypatch.setattr(ch.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(ch, "_trigger_blocked", lambda: rec.blocked)
    monkeypatch.setattr(ch, "_bump_usage", lambda _id: None)
    monkeypatch.setattr(ch, "_mark_own_copy", lambda _c: None)
    monkeypatch.setattr(
        ch, "_x_key", lambda combo, repeat=1, **_kw: rec.keys.append((combo, repeat))
    )
    monkeypatch.setattr(ch, "_x_keyup", lambda *names: rec.keyups.extend(names))
    monkeypatch.setattr(ch, "_snapshot_clipboard", lambda: ("text", rec.clip.encode()))
    monkeypatch.setattr(ch, "_read_clipboard_text", lambda: rec.clip)

    def write(kind, data, primary=False):
        rec.writes.append((kind, data.decode(), primary))
        rec.clip = data.decode()

    monkeypatch.setattr(ch, "_write_clipboard", write)
    return rec


def _snippet(ch, text, literal=True):
    return ch.Entry(id="s", timestamp=0, kind="text", text=text, literal=literal)


def test_deletes_the_trigger_and_space_then_pastes_and_restores(ch, x11):
    assert ch._fire_trigger_expansion("brb", "Brb", " ", _snippet(ch, "be right back")) is False
    assert x11.keys == [("BackSpace", 4), ("ctrl+v", 1)]
    assert x11.writes == [("text", "Be right back", False), ("text", "old", False)]
    assert x11.clip == "old"
    assert "ctrl" in x11.keyups and "shift" in x11.keyups


def test_a_newer_copy_is_not_overwritten_by_the_restore(ch, x11, monkeypatch):
    monkeypatch.setattr(ch, "_read_clipboard_text", lambda: "copied meanwhile")
    ch._fire_trigger_expansion("brb", "brb", " ", _snippet(ch, "be right back"))
    assert x11.writes == [("text", "be right back", False)]


def test_cursor_placeholder_moves_left_after_the_paste(ch, x11):
    ch._fire_trigger_expansion(";p", ";p", " ", _snippet(ch, "(" + "{cursor}" + ")", False))
    assert x11.keys == [("BackSpace", 3), ("ctrl+v", 1), ("Left", 1)]
    assert x11.writes[0] == ("text", "()", False)


def test_a_blocklisted_window_gets_nothing(ch, x11):
    x11.blocked = True
    ch._fire_trigger_expansion("brb", "brb", " ", _snippet(ch, "be right back"))
    assert x11.keys == [] and x11.writes == []


def test_an_ask_snippet_is_left_to_the_picker(ch, x11):
    ch._fire_trigger_expansion(";a", ";a", " ", _snippet(ch, "Hi {ask:Name}", literal=False))
    assert x11.keys == [] and x11.writes == []


def test_an_image_entry_is_never_typed(ch, x11):
    entry = ch.Entry(id="i", timestamp=0, kind="image", image_path="/x.png")
    ch._fire_trigger_expansion(";i", ";i", " ", entry)
    assert x11.keys == []


def test_without_xtest_nothing_is_deleted(ch, x11, monkeypatch):
    # Bailing after the BackSpaces would lose both the trigger and the text
    monkeypatch.setattr(ch, "_x_can_inject", lambda: False)
    ch._fire_trigger_expansion("brb", "brb", " ", _snippet(ch, "be right back"))
    assert x11.keys == [] and x11.writes == []


def test_without_xclip_nothing_is_deleted(ch, x11, monkeypatch):
    monkeypatch.setattr(ch.shutil, "which", lambda tool: None)
    ch._fire_trigger_expansion("brb", "brb", " ", _snippet(ch, "be right back"))
    assert x11.keys == [] and x11.writes == []
