"""Which PRIMARY changes the Wayland watcher treats as real selections."""

import subprocess
import sys
import threading
import time

import pytest

pytest.importorskip("gi")

from platform_backend import evdev_input as ev  # noqa: E402
from platform_backend import wayland_kde  # noqa: E402
from platform_backend.wayland_kde import WaylandSelectionWatcher  # noqa: E402


class _Backend:
    def __init__(self, clipboard):
        self.clipboard = clipboard

    def read_selection(self, source):
        assert source == "clipboard"
        return self.clipboard


class _Gestures:
    def __init__(self, selected=True):
        self.selected = selected

    def selected_since(self, t):
        return self.selected


def _watcher(clipboard, selected=True):
    backend = _Backend(clipboard)
    w = WaylandSelectionWatcher(backend, lambda *_: None, gestures=_Gestures(selected))
    return w, backend


def test_copy_button_is_ignored():
    w, backend = _watcher("old")
    backend.clipboard = "curl … | bash"  # the button set both at once
    w._clip_changed_at = 100.1
    assert not w._should_show("curl … | bash", burst_t0=100.0)


def test_plain_selection_is_shown():
    w, _ = _watcher("old")
    w._clip_changed_at = 100.1
    assert w._should_show("some highlighted text", burst_t0=100.0)


def test_reselecting_what_was_copied_is_shown():
    # select "foo", Ctrl+C, later select "foo" again
    w, _ = _watcher("foo")
    w._clip_changed_at = 50.0
    assert w._should_show("foo", burst_t0=100.0)


def test_primary_change_without_gesture_is_ignored():
    # Alt+Tab into a terminal that re-claims PRIMARY with an old selection
    w, _ = _watcher("old", selected=False)
    assert not w._should_show("ls -la ~/old", burst_t0=100.0)


def test_ocr_text_is_shown_though_clipboard_matches_and_no_gesture():
    w, _ = _watcher("recognised text", selected=False)
    w._clip_changed_at = time.monotonic()
    wayland_kde.expect_selection("recognised text")
    assert w._should_show("recognised text", burst_t0=time.monotonic())
    # only once
    assert not w._should_show("recognised text", burst_t0=time.monotonic())


class _DevHub:
    def __init__(self, devices=("kbd",)):
        self._devices = list(devices)
        self.subs = {}

    def devices(self):
        return self._devices

    def subscribe(self, cb):
        self.subs[1] = cb
        return 1

    def unsubscribe(self, handle):
        self.subs.pop(handle)


def _gestures(devices=("kbd",)):
    hub = _DevHub(devices)
    g = wayland_kde._SelectionGestures(hub)
    g.start()
    return g, hub.subs[1]


_KBD, _PTR = frozenset({ev.KEYBOARD}), frozenset({ev.POINTER})


def test_drag_is_a_selecting_gesture():
    g, send = _gestures()
    t = time.monotonic()
    send(ev.InputEvent(ev.BTN_LEFT, 1, _PTR, 10.0))
    send(ev.InputEvent(ev.BTN_LEFT, 0, _PTR, 10.6))
    assert g.selected_since(t)


def test_click_to_focus_is_not():
    g, send = _gestures()
    t = time.monotonic()
    send(ev.InputEvent(ev.BTN_LEFT, 1, _PTR, 10.0))
    send(ev.InputEvent(ev.BTN_LEFT, 0, _PTR, 10.08))
    assert not g.selected_since(t)


def test_double_click_is():
    g, send = _gestures()
    t = time.monotonic()
    for down, up in ((10.0, 10.05), (10.2, 10.25)):
        send(ev.InputEvent(ev.BTN_LEFT, 1, _PTR, down))
        send(ev.InputEvent(ev.BTN_LEFT, 0, _PTR, up))
    assert g.selected_since(t)


@pytest.mark.parametrize(
    "mod,key", [(ev.KEY_LEFTSHIFT, 106), (ev.KEY_RIGHTSHIFT, 102), (ev.KEY_LEFTCTRL, 30)]
)
def test_keyboard_selection_is(mod, key):
    g, send = _gestures()
    t = time.monotonic()
    send(ev.InputEvent(mod, 1, _KBD, 10.0))
    send(ev.InputEvent(key, 1, _KBD, 10.1))
    assert g.selected_since(t)


def test_alt_tab_is_not():
    g, send = _gestures()
    t = time.monotonic()
    send(ev.InputEvent(ev.KEY_LEFTALT, 1, _KBD, 10.0))
    send(ev.InputEvent(15, 1, _KBD, 10.1))  # KEY_TAB
    assert not g.selected_since(t)


def test_without_input_devices_every_change_counts():
    g, _ = _gestures(devices=())
    assert g.selected_since(time.monotonic())


def test_clipboard_read_asks_for_text(monkeypatch):
    seen = []

    def run(args, **kw):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, b"hi", b"")

    monkeypatch.setattr(wayland_kde.subprocess, "run", run)
    assert wayland_kde.WaylandKdeBackend.read_selection(object(), "clipboard") == "hi"
    assert seen[0][-2:] == ["--type", "text"]


# ---- modifier+double-click over the shared evdev hub (#27) --------------------


class _Hub:
    def __init__(self):
        self.subs = {}

    def subscribe(self, cb):
        self.subs[1] = cb
        return 1

    def unsubscribe(self, handle):
        self.subs.pop(handle)


class _NoXkb:
    def update(self, code, down):
        pass

    def is_active(self, mod):
        return None  # raw-keycode fallback


def test_ctrl_double_click_from_a_virtual_keyboard_fires(monkeypatch):
    from platform_backend import evdev_input as ev
    from platform_backend import wayland_kde

    fired = []
    monkeypatch.setattr(wayland_kde, "_XkbModifierState", _NoXkb)
    monkeypatch.setattr(wayland_kde.GLib, "timeout_add", lambda ms, fn, *a: fired.append(a))

    class Backend:
        def pointer_position(self):
            return (500, 300)

    hub = _Hub()
    w = wayland_kde.WaylandDoubleClickWatcher(Backend(), lambda x, y: None, hub=hub)
    w.start()
    send = hub.subs[1]
    kbd, ptr = frozenset({ev.KEYBOARD}), frozenset({ev.POINTER})
    # keyd's virtual keyboard carries the Ctrl press; the mouse the clicks
    send(ev.InputEvent(ev.KEY_LEFTCTRL, 1, kbd, 10.0))
    send(ev.InputEvent(ev.BTN_LEFT, 1, ptr, 10.10))
    send(ev.InputEvent(ev.BTN_LEFT, 0, ptr, 10.15))
    send(ev.InputEvent(ev.BTN_LEFT, 1, ptr, 10.30))
    assert fired == [(500, 300)]
    # without the modifier, a double-click is just a double-click
    send(ev.InputEvent(ev.KEY_LEFTCTRL, 0, kbd, 11.0))
    send(ev.InputEvent(ev.BTN_LEFT, 1, ptr, 11.1))
    send(ev.InputEvent(ev.BTN_LEFT, 1, ptr, 11.2))
    assert len(fired) == 1
    w.stop()
    assert hub.subs == {}


# ---- settle: a short pause once the selecting gesture has ended -----------------


def test_settle_is_short_after_release():
    g, send = _gestures()
    t0 = time.monotonic()
    send(ev.InputEvent(ev.BTN_LEFT, 1, _PTR, 10.0))
    assert g.settle_s(t0, 0.2) == 0.2  # still dragging
    send(ev.InputEvent(ev.BTN_LEFT, 0, _PTR, 10.6))
    assert g.settle_s(t0, 0.2) <= wayland_kde._AFTER_GESTURE_S


def test_settle_is_full_without_a_gesture():
    g, send = _gestures()
    send(ev.InputEvent(ev.BTN_LEFT, 1, _PTR, 10.0))
    send(ev.InputEvent(ev.BTN_LEFT, 0, _PTR, 10.08))  # click to focus
    assert g.settle_s(time.monotonic(), 0.2) == 0.2


# ---- KWin helper client ------------------------------------------------------


class _Pipe:
    def __init__(self, lines):
        self.lines = list(lines)
        self.written = []

    def write(self, s):
        self.written.append(s)

    def flush(self):
        pass

    def readline(self):
        return self.lines.pop(0)


class _Proc:
    def __init__(self, lines):
        self.stdin, self.stdout = _Pipe([]), _Pipe(lines)

    def poll(self):
        return None


def _helper(monkeypatch, *procs):
    procs = list(procs)
    monkeypatch.setattr(wayland_kde.subprocess, "Popen", lambda *a, **kw: procs.pop(0))
    monkeypatch.setattr(wayland_kde._select, "select", lambda r, *_: (r, [], []))
    monkeypatch.setattr(wayland_kde, "_reap", lambda p: None)
    return wayland_kde._KWinHelper()


def test_kwin_helper_reuses_one_process(monkeypatch):
    proc = _Proc([b"10 20\n", b"org.kde.dolphin\tHome\n"])
    h = _helper(monkeypatch, proc)
    assert h.query("cursor") == "10 20"
    assert h.query("active") == "org.kde.dolphin\tHome"
    assert proc.stdin.written == [b"cursor\n", b"active\n"]


def test_kwin_helper_restarts_after_no_answer(monkeypatch):
    dead, fresh = _Proc([b""]), _Proc([b"1 2\n"])
    h = _helper(monkeypatch, dead, fresh)
    assert h.query("cursor") == ""
    assert h.query("cursor") == "1 2"


def test_kwin_helper_runs_a_real_process(monkeypatch):
    # A stand-in helper that answers with a "\r" inside the line: it must
    # come back whole, and Popen must accept the helper's arguments.
    echo = "import sys\nfor _ in sys.stdin:\n    print('a\\rb', flush=True)"
    monkeypatch.setattr(wayland_kde._KWinHelper, "_ARGV", [sys.executable, "-c", echo])
    h = wayland_kde._KWinHelper()
    assert h.query("cursor") == "a\rb"
    assert h.query("active") == "a\rb"
    h._proc.kill()
    h._proc.wait()


def test_active_window_prefetch_is_used_once(monkeypatch):
    backend = wayland_kde.WaylandKdeBackend.__new__(wayland_kde.WaylandKdeBackend)
    backend._active_lock = threading.Lock()
    backend._active_pending = None
    answers = iter(["org.kde.kate\tnotes.txt", "org.kde.konsole\tbash"])
    backend._kwin = type("K", (), {"query": lambda self, kind: next(answers)})()
    backend.prefetch_active_window()
    assert backend.active_window_haystacks() == ["org.kde.kate", "notes.txt"]
    assert backend.active_window_haystacks() == ["org.kde.konsole", "bash"]


def test_kwin_answer_cannot_break_framing():
    pytest.importorskip("dbus")
    from platform_backend._kwin_query import _one_line

    title = "evil\rpage\ntitle\u2028x\x85y\tz"
    assert _one_line(title) == "evil page title x y z"
