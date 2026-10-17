"""Wayland input (#27): which /dev/input devices we read, how key injection
falls back, layout-aware keycodes, and KGlobalAccel hygiene. No GTK needed."""

import os
import struct
import subprocess

import pytest

from platform_backend import evdev_input as ev
from platform_backend import kde_hotkey, keyinject, xkb

# ---- device filter by capability bits ----------------------------------------


def _bits(*nums, size):
    buf = bytearray(size)
    for n in nums:
        buf[n // 8] |= 1 << (n % 8)
    return bytes(buf)


EV_SIZE = ev.EV_MAX // 8 + 1
KEY_SIZE = ev.KEY_MAX // 8 + 1
KEY_A, KEY_POWER, BTN_RIGHT = 30, 116, 0x111


def _fake_ioctl(devices):
    """devices: fd -> (name, ev bits, key bits). Answers EVIOCGNAME/EVIOCGBIT."""

    def ioctl(fd, request, size):
        name, ev_bits, key_bits = devices[fd]
        if request == ev.eviocgname(size):
            return name.encode() + b"\0" * (size - len(name))
        if request == ev.eviocgbit(0, size):
            return ev_bits
        if request == ev.eviocgbit(ev.EV_KEY, size):
            return key_bits
        raise OSError(25, "unexpected ioctl")

    return ioctl


KEYBOARD_CAPS = (
    _bits(ev.EV_KEY, size=EV_SIZE),
    _bits(KEY_A, ev.KEY_LEFTCTRL, ev.KEY_LEFTSHIFT, size=KEY_SIZE),
)
POINTER_CAPS = (_bits(ev.EV_KEY, size=EV_SIZE), _bits(ev.BTN_LEFT, BTN_RIGHT, size=KEY_SIZE))
POWER_CAPS = (_bits(ev.EV_KEY, size=EV_SIZE), _bits(KEY_POWER, size=KEY_SIZE))


def test_classify_by_capability_bits():
    assert ev.classify(*KEYBOARD_CAPS) == {ev.KEYBOARD}
    assert ev.classify(*POINTER_CAPS) == {ev.POINTER}
    # a power button has EV_KEY but neither modifiers nor BTN_LEFT
    assert ev.classify(*POWER_CAPS) == frozenset()
    # modifier bits without EV_KEY in the event-type mask don't count
    assert ev.classify(bytes(EV_SIZE), KEYBOARD_CAPS[1]) == frozenset()
    combo = ev.classify(
        KEYBOARD_CAPS[0], _bits(ev.KEY_RIGHTALT, ev.BTN_LEFT, size=KEY_SIZE)
    )  # a keyboard with a touchpad
    assert combo == {ev.KEYBOARD, ev.POINTER}


def test_probe_reads_name_and_kinds_through_ioctl():
    ioctl = _fake_ioctl({7: ("keyd virtual keyboard", *KEYBOARD_CAPS)})
    info = ev.probe(7, "/dev/input/event10", ioctl)
    assert info.name == "keyd virtual keyboard"
    assert info.kinds == {ev.KEYBOARD}


@pytest.fixture
def fake_dev(tmp_path):
    """A /dev/input stand-in: plain files, a fake open() and fake ioctls."""
    table = {}  # path -> (name, ev bits, key bits)
    by_fd = {}
    opened = []

    def add(node, name, caps):
        path = tmp_path / node
        path.write_bytes(b"")
        table[str(path)] = (name, *caps)

    def opener(path):
        fd = os.open(path, os.O_RDONLY)
        by_fd[fd] = table[path]
        opened.append(path)
        return fd

    hub = ev.InputEventHub(str(tmp_path), ioctl=_fake_ioctl(by_fd), opener=opener)
    yield hub, add, opened
    hub._close_all()


def test_scan_keeps_keyboards_and_pointers_and_skips_injectors(fake_dev):
    hub, add, _ = fake_dev
    add("event3", "AT Translated Set 2 keyboard", KEYBOARD_CAPS)  # grabbed by keyd
    add("event10", "keyd virtual keyboard", KEYBOARD_CAPS)  # no by-path link
    add("event4", "Logitech MX Master", POINTER_CAPS)
    add("event0", "Power Button", POWER_CAPS)
    add("event12", "ydotoold virtual device", KEYBOARD_CAPS)
    names = sorted(d.name for d in hub.scan())
    assert names == ["AT Translated Set 2 keyboard", "Logitech MX Master", "keyd virtual keyboard"]


def test_rescan_picks_up_hotplugged_devices_once(fake_dev):
    hub, add, opened = fake_dev
    add("event3", "AT Translated Set 2 keyboard", KEYBOARD_CAPS)
    add("event0", "Power Button", POWER_CAPS)
    assert len(hub.scan()) == 1
    add("event21", "Keychron K2 (Bluetooth)", KEYBOARD_CAPS)
    added = hub.scan()
    assert [d.name for d in added] == ["Keychron K2 (Bluetooth)"]
    assert hub.scan() == []
    # open devices and rejected ones are not reopened on every rescan
    assert opened.count(str(fake_dev[0]._dev_dir) + "/event0") == 1
    assert len(opened) == 3


def _event(code, value, sec=100, usec=0, etype=ev.EV_KEY):
    return struct.pack("llHHi", sec, usec, etype, code, value)


def test_reader_parses_events_for_every_subscriber(fake_dev):
    hub, _, _ = fake_dev
    r, w = os.pipe()
    os.set_blocking(r, False)
    hub._fds[r] = ev.DeviceInfo(
        "/dev/input/event10", "keyd virtual keyboard", frozenset({ev.KEYBOARD})
    )
    got_a, got_b = [], []
    hub._subs = {
        1: got_a.append,
        2: lambda e: (_ for _ in ()).throw(RuntimeError("bad")),
        3: got_b.append,
    }
    os.write(
        w, _event(ev.KEY_LEFTCTRL, 1) + _event(0, 0, etype=0) + _event(ev.BTN_LEFT, 1, usec=500000)
    )
    hub._read(r)
    os.close(w)
    assert [(e.code, e.value) for e in got_a] == [(ev.KEY_LEFTCTRL, 1), (ev.BTN_LEFT, 1)]
    assert got_a == got_b  # a failing subscriber doesn't starve the others
    assert got_a[1].time == pytest.approx(100.5)
    assert got_a[0].kinds == {ev.KEYBOARD}


def test_unplugged_device_is_dropped(fake_dev):
    hub, add, _ = fake_dev
    add("event21", "Keychron K2 (Bluetooth)", KEYBOARD_CAPS)
    hub.scan()
    r, w = os.pipe()
    os.close(w)  # EOF, like a vanished device
    hub._fds[r] = ev.DeviceInfo(fake_dev[0]._dev_dir + "/event99", "gone", frozenset({ev.KEYBOARD}))
    hub._paths.add(fake_dev[0]._dev_dir + "/event99")
    hub._read(r)
    assert r not in hub._fds and fake_dev[0]._dev_dir + "/event99" not in hub._paths


def test_subscribe_starts_and_last_unsubscribe_stops_the_reader(tmp_path):
    hub = ev.InputEventHub(str(tmp_path))
    a = hub.subscribe(lambda e: None)
    b = hub.subscribe(lambda e: None)
    assert hub._thread.is_alive()
    hub.unsubscribe(a)
    assert hub._thread.is_alive()
    hub.unsubscribe(b)
    assert not hub._thread.is_alive()


# ---- ydotool: failures are failures -------------------------------------------


class _Done:
    def __init__(self, rc, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def test_ydotool_run_reports_daemon_failure():
    calls = []

    def runner(argv, **kw):
        calls.append(kw)
        return _Done(2, "failed to connect socket `/run/user/1000/.ydotool_socket': No such file")

    assert keyinject._ydotool_run(["key", "29:1"], runner=runner) is False
    assert calls[0]["timeout"] == keyinject.YDOTOOL_TIMEOUT_S


def test_ydotool_run_times_out_instead_of_hanging():
    def runner(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    assert keyinject._ydotool_run(["key", "29:1"], runner=runner) is False


def test_ydotool_run_success():
    assert keyinject._ydotool_run(["key", "29:1"], runner=lambda a, **k: _Done(0)) is True


def test_ydotool_socket_lookup(tmp_path, monkeypatch):
    monkeypatch.delenv("YDOTOOL_SOCKET", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    if os.path.exists("/tmp/.ydotool_socket"):  # nosec B108 - a real daemon on this machine
        pytest.skip("a real ydotoold socket exists in /tmp")
    assert keyinject.ydotool_socket() is None
    (tmp_path / ".ydotool_socket").write_bytes(b"")
    assert keyinject.ydotool_socket() == str(tmp_path / ".ydotool_socket")
    monkeypatch.setenv("YDOTOOL_SOCKET", str(tmp_path / "elsewhere"))
    assert keyinject.ydotool_socket() is None  # explicit and missing: don't guess


class _Portal:
    def __init__(self, ok):
        self.ok, self.sent, self.denied = ok, [], False

    def send(self, seq):
        self.sent.append(seq)
        return self.ok

    def close_if_idle(self):
        pass


@pytest.fixture
def no_wtype(monkeypatch):
    monkeypatch.setattr(keyinject, "wtype_usable", lambda: False)


def test_portal_failure_falls_back_to_ydotool(monkeypatch, no_wtype):
    ran = []
    monkeypatch.setattr(keyinject.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(keyinject, "_ydotool_run", lambda argv: ran.append(argv) or True)
    monkeypatch.setattr(xkb, "detect_kbd_rmlvo", lambda: ("pc105", "us", "", ""))
    portal = _Portal(ok=False)
    notes = []
    inj = keyinject.KeyInjector(notify=notes.append, portal=portal, portal_available=lambda: True)
    assert inj.deliver("key", "ctrl+v") is True
    assert portal.sent == [[(0xFFE3, 1), (0x76, 1), (0x76, 0), (0xFFE3, 0)]]
    assert ran == [["key", "29:1", "47:1", "47:0", "29:0"]]
    assert notes == []


def test_everything_failing_returns_false_and_notifies(monkeypatch, no_wtype):
    monkeypatch.setattr(keyinject.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(keyinject, "_ydotool_run", lambda argv: False)
    monkeypatch.setattr(xkb, "detect_kbd_rmlvo", lambda: ("pc105", "us", "", ""))
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    notes = []
    inj = keyinject.KeyInjector(notify=notes.append, portal_available=lambda: False)
    assert inj.deliver("key", "ctrl+x") is False
    assert len(notes) == 1 and "ydotoold" in notes[0] and "Remote" in notes[0]


def test_can_inject_needs_a_live_ydotoold(monkeypatch, no_wtype):
    monkeypatch.setattr(keyinject.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(keyinject, "ydotool_socket", lambda: None)
    inj = keyinject.KeyInjector(portal_available=lambda: False)
    assert inj.can_inject() is False  # installed, daemon down: Paste can't work
    monkeypatch.setattr(keyinject, "ydotool_socket", lambda: "/run/user/1000/.ydotool_socket")
    assert inj.can_inject() is True


def test_declined_portal_is_not_asked_again(monkeypatch, no_wtype):
    monkeypatch.setattr(keyinject.shutil, "which", lambda name: None)
    portal = _Portal(ok=False)
    portal.denied = True
    notes = []
    inj = keyinject.KeyInjector(notify=notes.append, portal=portal, portal_available=lambda: True)
    assert inj.deliver("key", "ctrl+a") is False
    assert portal.sent == []
    assert "declined" in notes[0]


def test_wtype_is_never_used_on_kwin(monkeypatch):
    monkeypatch.setattr(keyinject.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    assert keyinject.wtype_usable() is False
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "sway")
    assert keyinject.wtype_usable() is True


# ---- keysyms -> keycodes through the user's layout ----------------------------


def _keymap_or_skip(layout, variant=""):
    if xkb.lib() is None:
        pytest.skip("libxkbcommon.so.0 not loadable")
    km = xkb.keymap_for(("pc105", layout, variant, ""))
    if km is None:
        pytest.skip(f"no xkeyboard-config data for {layout}")
    return km


def test_azerty_a_is_the_physical_q_key():
    km = _keymap_or_skip("fr")
    assert km.keycode_for(ord("a")) == (16, False)  # KEY_Q
    assert km.keycode_for(ord("q")) == (30, False)  # KEY_A
    events = keyinject.ydotool_key_events(keyinject.parse_chord("ctrl+a"), ("pc105", "fr", "", ""))
    assert events == ["29:1", "16:1", "16:0", "29:0"]  # Ctrl+A, not Ctrl+Q


def test_dvorak_paste_is_ctrl_v_not_ctrl_k():
    _keymap_or_skip("us", "dvorak")
    events = keyinject.ydotool_key_events(
        keyinject.parse_chord("ctrl+v"), ("pc105", "us", "dvorak", "")
    )
    assert events == ["29:1", "52:1", "52:0", "29:0"]  # KEY_DOT carries 'v' on Dvorak


def test_us_layout_uses_qwerty_codes_without_xkb(monkeypatch):
    monkeypatch.setattr(xkb, "keymap_for", lambda rmlvo: pytest.fail("no keymap needed for us"))
    events = keyinject.ydotool_key_events(
        keyinject.parse_chord("BackSpace"), ("pc105", "us", "", "")
    )
    assert events == ["14:1", "14:0"]


def test_keysym_names():
    assert xkb.keysym_from_name("v") == 0x76
    assert xkb.keysym_from_name("V") == 0x76  # never an implicit Shift
    assert xkb.keysym_from_name("Return") == 0xFF0D
    assert xkb.keysym_from_name("BackSpace") == 0xFF08
    assert keyinject.parse_chord("hyper+v") is None


def test_kxkbrc_layout_wins_over_localectl(tmp_path, monkeypatch):
    cfg = tmp_path / ".config"
    cfg.mkdir()
    (cfg / "kxkbrc").write_text(
        "[Layout]\nLayoutList=fr,us\nVariantList=azerty,\nOptions=ctrl:swap_lalt_lctl\nUse=true\n"
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        xkb.subprocess, "run", lambda *a, **k: _Done(0, "X11 Layout: us\nX11 Model: pc105\n")
    )
    assert xkb.detect_kbd_rmlvo() == ("pc105", "fr", "azerty", "ctrl:swap_lalt_lctl")


# ---- KGlobalAccel ------------------------------------------------------------

CTRL, SHIFT, KEYPAD = 0x04000000, 0x02000000, 0x20000000


@pytest.mark.parametrize(
    "combo,expected",
    [
        ("KP_Add", KEYPAD | 0x2B),
        ("ctrl+kp_1", CTRL | KEYPAD | ord("1")),
        ("super+kp_enter", 0x10000000 | KEYPAD | 0x01000005),
        ("ctrl+shift+exclam", CTRL | 0x21),  # KWin consumes the Shift that made '!'
        ("ctrl+shift+y", CTRL | SHIFT | ord("Y")),  # but keeps it for letters
        ("ctrl+shift+f5", CTRL | SHIFT | 0x01000034),
        ("ctrl+question", CTRL | ord("?")),
        ("xf86audioplay", 0x01000080),
    ],
)
def test_qt_keycode_keypad_and_shifted(combo, expected):
    assert kde_hotkey._qt_keycode(combo) == expected


def test_action_ids_are_stable_per_role():
    def show_popup_now():
        pass

    def _on_ocr_hotkey():
        pass

    first = kde_hotkey.WaylandKdeHotkey("super+y", show_popup_now)
    again = kde_hotkey.WaylandKdeHotkey("super+u", show_popup_now)
    ocr = kde_hotkey.WaylandKdeHotkey("super+o", _on_ocr_hotkey)
    clip = kde_hotkey.WaylandKdeHotkey("super+v", lambda: None, role="clipboard")
    assert first._action == again._action == "vinopop-popup"
    assert ocr._action == "vinopop-ocr"
    assert clip._action == "vinopop-clipboard"


class _Kga:
    def __init__(self, actions):
        self.actions, self.removed = actions, []

    def allActionsForComponent(self, cid):
        return self.actions

    def unRegister(self, aid):
        self.removed.append(aid[1])


def test_legacy_numbered_actions_are_cleaned_up_once(monkeypatch):
    monkeypatch.setattr(kde_hotkey, "_legacy_cleaned", False)
    kga = _Kga(
        [
            ["vinopop", "vinopop-action-1", "vinoPop", "super+y"],
            ["vinopop", "vinopop-action-3", "vinoPop", "super+o"],
            ["vinopop", "vinopop-popup", "vinoPop", "Show the popup"],
        ]
    )
    assert kde_hotkey.cleanup_legacy_actions(kga) == ["vinopop-action-1", "vinopop-action-3"]
    assert kde_hotkey.cleanup_legacy_actions(kga) == []


def test_stop_removes_the_signal_receiver():
    class Match:
        removed = False

        def remove(self):
            Match.removed = True

    hk = kde_hotkey.WaylandKdeHotkey("super+y", lambda: None, role="popup")
    hk._match = Match()
    hk.stop()
    assert Match.removed and hk._match is None


def test_other_global_shortcuts_are_ignored(capsys):
    hk = kde_hotkey.WaylandKdeHotkey("super+y", lambda: pytest.fail("not ours"), role="popup")
    hk._on_pressed("kwin", "Window Close", 12345)
    assert capsys.readouterr().out == ""
