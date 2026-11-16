"""The X11 hotkey thread against a fake display: what it grabs, what it
reports when it cannot, which key events fire the callback, and how it
follows a keyboard remap."""

from __future__ import annotations

import select
from types import SimpleNamespace

import pytest

pytest.importorskip("gi")
pytest.importorskip("Xlib")

from Xlib import X, error  # noqa: E402

from triggers import hotkey  # noqa: E402

KEYCODE = 29
LOCK_VARIANTS = [0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask]
SUPER_SHIFT = X.Mod4Mask | X.ShiftMask


class FakeRoot:
    def __init__(self, refuse=(), pointer_mask=0):
        self.refuse = set(refuse)
        self.pointer_mask = pointer_mask
        self.grabs: list[tuple[int, int]] = []
        self.ungrabs: list[tuple[int, int]] = []

    def grab_key(self, keycode, mask, owner_events, pointer_mode, keyboard_mode, onerror=None):
        assert (owner_events, pointer_mode, keyboard_mode) == (1, X.GrabModeAsync, X.GrabModeAsync)
        if mask in self.refuse:
            onerror(error.BadAccess.__new__(error.BadAccess), None)
            return
        self.grabs.append((keycode, mask))

    def ungrab_key(self, keycode, mask):
        self.ungrabs.append((keycode, mask))

    def query_pointer(self):
        return SimpleNamespace(mask=self.pointer_mask)


class FakeDisplay:
    def __init__(self, events=(), keycode=KEYCODE, root=None):
        self.events = list(events)
        self.keycode = keycode
        self.root = root or FakeRoot()
        self.syncs = 0

    def keysym_to_keycode(self, _keysym):
        return self.keycode

    def screen(self):
        return SimpleNamespace(root=self.root)

    def sync(self):
        self.syncs += 1

    def pending_events(self):
        return len(self.events)

    def next_event(self):
        return self.events.pop(0)

    def fileno(self):
        return 0


def _key(kind, time, state=SUPER_SHIFT, detail=KEYCODE):
    return SimpleNamespace(type=kind, detail=detail, time=time, state=state)


class _Remap:
    type = X.MappingNotify

    def __init__(self, dpy, new_keycode):
        self._dpy, self._new = dpy, new_keycode
        self.refreshed = False

    def refresh_keyboard_mapping(self):
        self.refreshed = True
        self._dpy.keycode = self._new


@pytest.fixture
def fired(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(hotkey.GLib, "idle_add", lambda fn, *a: calls.append(fn))
    return calls


@pytest.fixture
def listen(monkeypatch, fired):
    """Run _listen until the fake display has no more events."""

    def run(dpy, combo="super+shift+y", **kwargs):
        hk = hotkey.Hotkey(combo, lambda: None, **kwargs)
        waits = []

        def no_more_events(rlist, _w, _x, timeout):
            waits.append((rlist, timeout))
            hk._stop.set()
            return [], [], []

        monkeypatch.setattr(select, "select", no_more_events)
        hk._dpy = dpy
        hk._listen(dpy)
        hk.waits = waits
        return hk

    return run


def test_grabs_the_key_with_every_lock_modifier_variant(listen):
    dpy = FakeDisplay()
    hk = listen(dpy)
    assert dpy.root.grabs == [(KEYCODE, SUPER_SHIFT | extra) for extra in LOCK_VARIANTS]
    assert hk._grabbed_combos == dpy.root.grabs
    assert dpy.syncs == 4


def test_waits_for_events_a_second_at_a_time(listen):
    dpy = FakeDisplay()
    hk = listen(dpy)
    assert hk.waits == [([0], 1.0)]


def test_a_broken_connection_ends_the_loop(monkeypatch, fired):
    hk = hotkey.Hotkey("super+shift+y", lambda: None)

    def gone(*_a):
        raise OSError("bad fd")

    monkeypatch.setattr(select, "select", gone)
    hk._listen(FakeDisplay())
    assert not hk._stop.is_set()


@pytest.mark.parametrize(
    "combo,keycode,message",
    [
        ("hyperdrive+y", KEYCODE, "unknown modifier"),
        ("super+definitely_not_a_key", KEYCODE, "unknown key name"),
        ("super+y", 0, "no keycode for"),
    ],
)
def test_a_hotkey_that_cannot_be_resolved_grabs_nothing(listen, capsys, combo, keycode, message):
    dpy = FakeDisplay(keycode=keycode)
    hk = listen(dpy, combo)
    assert dpy.root.grabs == []
    assert hk.waits == []
    assert message in capsys.readouterr().out


def test_polling_mode_grabs_nothing_and_polls(listen, monkeypatch):
    polled = []
    monkeypatch.setattr(hotkey.Hotkey, "_poll_loop", lambda self: polled.append(self._keycode))
    dpy = FakeDisplay()
    hk = listen(dpy, use_polling=True)
    assert polled == [KEYCODE]
    assert dpy.root.grabs == []
    assert hk._mods == SUPER_SHIFT
    assert hk._root is dpy.root


def test_every_variant_refused_tells_the_user_and_stops(listen, monkeypatch):
    notices = []
    from core import actions

    monkeypatch.setattr(actions, "notify", lambda *a, **kw: notices.append((a, kw)))
    dpy = FakeDisplay(
        events=[_key(X.KeyPress, 1)],
        root=FakeRoot(refuse=[SUPER_SHIFT | extra for extra in LOCK_VARIANTS]),
    )
    hk = listen(dpy)
    assert len(notices) == 1
    (title, body, icon), kwargs = notices[0]
    assert title == "Hotkey conflict"
    assert "'super+shift+y' is already bound by another app" in body
    assert icon == "dialog-warning"
    assert kwargs == {"urgency": "critical"}
    assert hk.waits == []
    assert len(dpy.events) == 1


def test_one_variant_refused_still_listens(listen, monkeypatch, capsys, fired):
    from core import actions

    monkeypatch.setattr(actions, "notify", lambda *a, **kw: pytest.fail("no conflict notice"))
    dpy = FakeDisplay(events=[_key(X.KeyPress, 1)], root=FakeRoot(refuse=[SUPER_SHIFT]))
    hk = listen(dpy)
    assert len(hk._grabbed_combos) == 3
    assert "1 lock-variant grab(s) refused (non-fatal)" in capsys.readouterr().out
    assert len(fired) == 1


def test_a_grab_that_raises_counts_as_refused(listen, monkeypatch, capsys):
    root = FakeRoot()
    real = root.grab_key

    def grab(keycode, mask, *a, **kw):
        if mask == SUPER_SHIFT | X.LockMask:
            raise RuntimeError("boom")
        real(keycode, mask, *a, **kw)

    root.grab_key = grab
    hk = listen(FakeDisplay(root=root))
    out = capsys.readouterr().out
    assert len(hk._grabbed_combos) == 3
    assert f"grab failed for mask 0x{SUPER_SHIFT | X.LockMask:x}: boom" in out
    assert "1 lock-variant grab(s) refused" in out


def test_a_press_with_the_hotkeys_modifiers_fires(listen, fired):
    hk = listen(FakeDisplay(events=[_key(X.KeyPress, 1)]))
    assert fired == [hk._safe_trigger]


def test_caps_lock_and_num_lock_do_not_stop_it_firing(listen, fired):
    state = SUPER_SHIFT | X.LockMask | X.Mod2Mask
    listen(FakeDisplay(events=[_key(X.KeyPress, 1, state=state)]))
    assert len(fired) == 1


def test_a_press_with_an_extra_modifier_is_ignored(listen, fired, capsys):
    listen(FakeDisplay(events=[_key(X.KeyPress, 1, state=SUPER_SHIFT | X.ControlMask)]))
    assert fired == []
    assert "press ignored" in capsys.readouterr().out


def test_another_keys_press_is_ignored(listen, fired):
    listen(FakeDisplay(events=[_key(X.KeyPress, 1, detail=KEYCODE + 1)]))
    assert fired == []


def test_a_held_key_fires_once(listen, fired):
    events = [
        _key(X.KeyPress, 100),
        _key(X.KeyRelease, 130),
        _key(X.KeyPress, 130),
        _key(X.KeyRelease, 160),
        _key(X.KeyPress, 160),
    ]
    listen(FakeDisplay(events=events))
    assert len(fired) == 1


def test_a_second_press_after_a_release_fires_again(listen, fired):
    events = [_key(X.KeyPress, 100), _key(X.KeyRelease, 130), _key(X.KeyPress, 131)]
    listen(FakeDisplay(events=events))
    assert len(fired) == 2


def test_a_remap_that_moves_the_key_regrabs_it_on_the_new_keycode(listen, fired):
    dpy = FakeDisplay()
    remap = _Remap(dpy, 61)
    dpy.events = [remap, _key(X.KeyPress, 1), _key(X.KeyPress, 2, detail=61)]
    hk = listen(dpy)
    assert remap.refreshed
    assert dpy.root.ungrabs == [(KEYCODE, SUPER_SHIFT | extra) for extra in LOCK_VARIANTS]
    assert hk._grabbed_combos == [(61, SUPER_SHIFT | extra) for extra in LOCK_VARIANTS]
    assert len(fired) == 1


def test_a_remap_that_leaves_the_key_in_place_regrabs_nothing(listen, fired):
    dpy = FakeDisplay()
    dpy.events = [_Remap(dpy, KEYCODE), _key(X.KeyPress, 1)]
    hk = listen(dpy)
    assert dpy.root.ungrabs == []
    assert hk._grabbed_combos == [(KEYCODE, SUPER_SHIFT | extra) for extra in LOCK_VARIANTS]
    assert len(fired) == 1


def test_a_remap_that_drops_the_key_keeps_the_old_grab(listen):
    dpy = FakeDisplay()
    dpy.events = [_Remap(dpy, 0)]
    listen(dpy)
    assert dpy.root.ungrabs == []


def test_a_refused_regrab_is_not_recorded(listen):
    dpy = FakeDisplay()
    dpy.events = [_Remap(dpy, 61)]
    real = dpy.root.grab_key

    def grab(keycode, mask, *a, **kw):
        if keycode == 61 and mask == SUPER_SHIFT:
            dpy.root.refuse.add(mask)
        real(keycode, mask, *a, **kw)

    dpy.root.grab_key = grab
    hk = listen(dpy)
    assert hk._grabbed_combos == [(61, SUPER_SHIFT | extra) for extra in LOCK_VARIANTS[1:]]
