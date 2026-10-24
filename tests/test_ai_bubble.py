"""A superseded AI run must not write into the bubble's next run."""

from types import SimpleNamespace

import pytest

pytest.importorskip("gi")


@pytest.fixture
def ai_bubble():
    try:
        from ui import ai_bubble
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 unavailable: {exc}")
    return ai_bubble


class _FakeBubble:
    """The few members _RunSink touches, without needing a display."""

    def __init__(self):
        self._gen = 0
        self._dead = False
        self.text = ""
        self.done = None
        self.warning = None

    def _set_status(self, msg):
        self.text = msg
        return False

    def _append(self, chunk):
        self.text += chunk
        return False

    def _finish(self, text, error, warning=None):
        self.done = (text, error)
        self.warning = warning
        return False


def _drain():
    from gi.repository import GLib

    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)


def test_sink_feeds_the_bubble_while_current(ai_bubble):
    bubble = _FakeBubble()
    sink = ai_bubble._RunSink(bubble, 0)
    sink.push_status("loading")
    sink.push_token("a")
    sink.push_token("b")
    sink.push_done(text="ab")
    _drain()
    assert bubble.text == "loadingab"
    assert bubble.done == ("ab", None)


def test_stale_run_is_ignored_after_restart(ai_bubble):
    bubble = _FakeBubble()
    old = ai_bubble._RunSink(bubble, bubble._gen)
    old.push_token("old ")
    bubble._gen += 1  # what restart() does
    new = ai_bubble._RunSink(bubble, bubble._gen)
    old.push_token("late old token")  # in flight from the superseded run
    old.push_done(text="old answer")
    new.push_token("new")
    _drain()
    # staleness is judged when the update is applied on the main thread, so even
    # output the old run queued before the restart is dropped
    assert bubble.text == "new"
    assert bubble.done is None


def test_sink_reports_cancelled_once_superseded_or_closed(ai_bubble):
    bubble = _FakeBubble()
    sink = ai_bubble._RunSink(bubble, bubble._gen)
    assert not sink.cancelled()
    bubble._gen += 1  # restart(): a new language was picked
    assert sink.cancelled()

    closed = _FakeBubble()
    sink = ai_bubble._RunSink(closed, closed._gen)
    closed._dead = True  # dismiss()
    assert sink.cancelled()


def test_warning_is_passed_through_the_sink(ai_bubble):
    bubble = _FakeBubble()
    ai_bubble._RunSink(bubble, 0).push_done(text="part", warning="cut short")
    _drain()
    assert bubble.done == ("part", None) and bubble.warning == "cut short"


class _Widget:
    def __init__(self):
        self.sensitive = self.visible = None
        self.text = self.tooltip = None

    def set_sensitive(self, v):
        self.sensitive = v

    def set_visible(self, v):
        self.visible = v

    def set_text(self, v):
        self.text = v

    def get_text(self):
        return self.text

    def set_tooltip_text(self, v):
        self.tooltip = v

    def set_spinning(self, v):
        self.spinning = v


def _headless_bubble(ai_bubble, monkeypatch):
    """A ResultBubble with stand-in widgets, so _finish and _sync_buttons run
    without a display."""
    b = ai_bubble.ResultBubble.__new__(ai_bubble.ResultBubble)
    b._dead, b._busy, b._text, b._incomplete = False, True, "", False
    b._dismiss_id = None
    b._gen, b._last_activity = 0, 0.0
    b._text_lbl, b._warn_lbl = _Widget(), _Widget()
    b._copy_btn, b._replace_btn = _Widget(), _Widget()
    b._spinner = _Widget()
    b._kbd_grab = None  # opened with the pointer
    monkeypatch.setattr(b, "_arm_timeout", lambda: None, raising=False)
    monkeypatch.setattr(b, "_queue_replace", lambda: None, raising=False)  # no window to measure
    b.announced = []
    monkeypatch.setattr(b, "_announce", b.announced.append, raising=False)
    return b


def test_incomplete_answer_disables_replace_but_not_copy(ai_bubble, monkeypatch):
    b = _headless_bubble(ai_bubble, monkeypatch)
    b._finish("a third of the rewrite", None, "The answer stopped at its length limit")
    assert b._copy_btn.sensitive is True
    assert b._replace_btn.sensitive is False and b._replace_btn.tooltip
    assert b._warn_lbl.visible and "length limit" in b._warn_lbl.text


def test_finished_answer_is_announced(ai_bubble, monkeypatch):
    # The bubble never has focus, so a screen reader only hears the answer
    # through an announcement; an incomplete one says why.
    b = _headless_bubble(ai_bubble, monkeypatch)
    b._finish("a short summary", None)
    assert b.announced == ["a short summary"]
    b = _headless_bubble(ai_bubble, monkeypatch)
    b._finish("a third", None, "The answer stopped at its length limit")
    assert b.announced == ["a third The answer stopped at its length limit"]


def test_complete_answer_keeps_replace(ai_bubble, monkeypatch):
    b = _headless_bubble(ai_bubble, monkeypatch)
    b._finish("the whole rewrite", None)
    assert b._replace_btn.sensitive is True and b._replace_btn.tooltip is None
    assert b._warn_lbl.visible is False


def test_closed_bubble_reports_cancelled(ai_bubble, monkeypatch):
    b = _headless_bubble(ai_bubble, monkeypatch)
    assert not b.cancelled()
    b._dead = True
    assert b.cancelled()


def test_spinner_runs_until_the_first_word(ai_bubble, monkeypatch):
    b = _headless_bubble(ai_bubble, monkeypatch)
    b._set_status("Looking at the image…")
    assert b._spinner.spinning and b._spinner.visible
    b._append("A cartoon")
    assert not b._spinner.spinning and not b._spinner.visible


def test_spinner_stops_on_an_error(ai_bubble, monkeypatch):
    b = _headless_bubble(ai_bubble, monkeypatch)
    b._set_status("Loading model on NPU…")
    b._finish(None, "Couldn't run the local model: boom")
    assert not b._spinner.spinning


# ---- #34: stalls ----------------------------------------------------------
def test_a_silent_run_is_given_up(ai_bubble, monkeypatch):
    b = _headless_bubble(ai_bubble, monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(ai_bubble.time, "monotonic", lambda: now[0])
    b._last_activity = now[0]
    assert b._check_stall() is True and b._busy  # quiet for 0 s: fine
    now[0] += ai_bubble._STALL_TIMEOUT_S + 1
    assert b._check_stall() is True
    assert not b._busy and b._gen == 1  # late output from the hung run is dropped
    assert "No answer" in b._text_lbl.text


def test_a_streaming_run_is_not_given_up(ai_bubble, monkeypatch):
    b = _headless_bubble(ai_bubble, monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(ai_bubble.time, "monotonic", lambda: now[0])
    b._last_activity = now[0]
    now[0] += ai_bubble._STALL_TIMEOUT_S - 10
    b._append("still going")
    now[0] += 20
    b._check_stall()
    assert b._busy and b._gen == 0


def test_dead_bubble_stops_the_stall_check(ai_bubble, monkeypatch):
    b = _headless_bubble(ai_bubble, monkeypatch)
    b._dead = True
    b._stall_id = 7
    assert b._check_stall() is False and b._stall_id is None


# ---- keyboard (#124) ---------------------------------------------------------


@pytest.fixture
def kbd_bubble(ai_bubble, monkeypatch):
    """A real bubble (not shown) in keyboard mode, with the X grab faked:
    Copy, Replace and Close, and a log of what ran and what was spoken."""
    from gi.repository import Gdk

    from ui import gtk4_app

    try:
        gtk4_app.init()
    except Exception as exc:  # Gtk.init() raises without a display
        pytest.skip(f"no display: {exc}")
    if Gdk.Display.get_default() is None:
        pytest.skip("no display")
    log: list = []
    import platform_backend

    backend = SimpleNamespace(
        name="x11",
        popup_uses_xlib=False,
        pointer_is_logical=True,
        set_clipboard=lambda t: log.append(("copy", t)),
    )
    monkeypatch.setattr(platform_backend, "get_backend", lambda: backend)

    class _Grab:
        def stop(self):
            log.append(("ungrab",))

    b = ai_bubble.ResultBubble(
        "Rewrite", 0, 0, on_replace=lambda t: log.append(("replace", t)), keyboard=True
    )
    b._kbd_grab = _Grab()
    b._say = lambda m: log.append(("say", m))
    b._announce = lambda m: log.append(("announce", m))
    b._kbd_select(0)
    yield b, log
    b.dismiss()


def _selected(b):
    return [b._chip_name(c) for c in b._chips if c.has_css_class("vinopop-selected")]


def test_keyboard_picks_a_chip_and_announces_it(kbd_bubble):
    b, log = kbd_bubble
    assert _selected(b) == ["Copy"]
    # Still streaming: the chips are off, and say so
    assert log[-1] == ("say", "Copy, not available yet")
    assert b._kbd_key("Right")
    assert _selected(b) == ["Replace"]
    assert b._kbd_key("Tab")
    assert _selected(b) == ["Close"]
    assert b._kbd_key("Right")  # wraps
    assert _selected(b) == ["Copy"]
    assert b._kbd_key("Tab", shift=True)
    assert b._kbd_key("Left")
    assert _selected(b) == ["Replace"]
    assert not b._kbd_key("a")


def test_enter_waits_for_the_answer(kbd_bubble):
    b, log = kbd_bubble
    assert b._kbd_key("Return")
    assert ("say", "The answer isn't ready yet") in log
    assert not any(e[0] in ("copy", "ungrab") for e in log)


def test_enter_copies_then_hands_the_keys_back(kbd_bubble):
    b, log = kbd_bubble
    b._finish("new text", None)
    # The answer is read out with the way to act on it
    assert log[-1] == ("announce", "new text Copy is selected; press Enter.")
    assert b._kbd_key("Return")
    assert ("copy", "new text") in log and ("say", "Copied") in log
    assert ("ungrab",) in log and b._kbd_grab is None
    assert _selected(b) == []
    # Keys are the app's again: nothing more is handled
    assert not b._kbd_key("Return")


def test_enter_on_replace_ungrabs_before_pasting(kbd_bubble):
    b, log = kbd_bubble
    b._finish("new text", None)
    b._kbd_key("Right")
    b._kbd_key("KP_Enter")
    # The paste goes to the source app, so its keys are released first
    assert log.index(("ungrab",)) < log.index(("replace", "new text"))


def test_replace_says_why_when_the_answer_is_incomplete(kbd_bubble):
    b, log = kbd_bubble
    b._finish("part", None, warning="Cut short.")
    b._kbd_key("Right")
    b._kbd_key("Return")
    assert not any(e[0] == "replace" for e in log)
    assert log[-1][1].startswith("The answer is incomplete")


def test_escape_closes(kbd_bubble):
    b, log = kbd_bubble
    assert b._kbd_key("Escape")
    assert b._dead and ("ungrab",) in log


def test_only_a_keyboard_run_action_opens_a_keyboard_bubble(ai_bubble, monkeypatch):
    from gi.repository import GLib

    from ui import popup

    built = []

    class _Bubble:
        def __init__(self, *_a, keyboard=False, **_kw):
            built.append(keyboard)

        def present(self):
            pass

    monkeypatch.setattr(ai_bubble, "ResultBubble", _Bubble)
    monkeypatch.setattr(ai_bubble, "pointer_position", lambda: (0, 0))
    monkeypatch.setattr(GLib, "idle_add", lambda fn, *a: fn(*a))
    monkeypatch.setattr(popup, "_kbd_activated_at", 0.0)
    ai_bubble.show("Summary", lambda _b: None)
    # Popup key grabs steal those keys from the app, so a clicked action
    # mustn't get them
    monkeypatch.setattr(popup, "_kbd_activated_at", popup.time.monotonic())
    ai_bubble.show("Summary", lambda _b: None)
    assert built == [False, True]
