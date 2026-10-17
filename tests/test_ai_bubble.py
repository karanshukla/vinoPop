"""A superseded AI run must not write into the bubble's next run."""

import pytest

pytest.importorskip("gi")


@pytest.fixture
def ai_bubble():
    try:
        import ai_bubble
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

    def set_tooltip_text(self, v):
        self.tooltip = v


def _headless_bubble(ai_bubble, monkeypatch):
    """A ResultBubble with stand-in widgets, so _finish and _sync_buttons run
    without a display."""
    b = ai_bubble.ResultBubble.__new__(ai_bubble.ResultBubble)
    b._dead, b._busy, b._text, b._incomplete = False, True, "", False
    b._dismiss_id = None
    b._text_lbl, b._warn_lbl = _Widget(), _Widget()
    b._copy_btn, b._replace_btn = _Widget(), _Widget()
    monkeypatch.setattr(b, "_arm_timeout", lambda: None, raising=False)
    return b


def test_incomplete_answer_disables_replace_but_not_copy(ai_bubble, monkeypatch):
    b = _headless_bubble(ai_bubble, monkeypatch)
    b._finish("a third of the rewrite", None, "The answer stopped at its length limit")
    assert b._copy_btn.sensitive is True
    assert b._replace_btn.sensitive is False and b._replace_btn.tooltip
    assert b._warn_lbl.visible and "length limit" in b._warn_lbl.text


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
