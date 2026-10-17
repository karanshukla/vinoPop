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
        self.text = ""
        self.done = None

    def _set_status(self, msg):
        self.text = msg
        return False

    def _append(self, chunk):
        self.text += chunk
        return False

    def _finish(self, text, error):
        self.done = (text, error)
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
