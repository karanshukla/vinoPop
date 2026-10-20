"""Daemon / hotkey / watcher lifecycle (#28): one OCR capture at a time,
no hotkey rebind churn, clean SIGTERM/SIGHUP, GTK only on the GTK thread,
read-only detection through the backend, no leaked X connections."""

import signal
import sys
import threading
import types

import pytest

pytest.importorskip("gi")
pytest.importorskip("Xlib")


@pytest.fixture(scope="module")
def main_mod():
    # main picks a GDK backend at import and may set GDK_BACKEND; a preset
    # value is left alone, so pin one for the import and put it back.
    mp = pytest.MonkeyPatch()
    mp.setenv("GDK_BACKEND", "x11")
    try:
        import main
    finally:
        mp.undo()
    return main


def _bind(app, main_mod, *names):
    for name in names:
        setattr(app, name, types.MethodType(getattr(main_mod.App, name), app))


# ---- 1. OCR hotkey: one capture at a time ------------------------------------


def test_ocr_hotkey_drops_triggers_while_a_capture_runs(main_mod, monkeypatch):
    release = threading.Event()
    started = []

    def fake_capture():
        started.append(1)
        release.wait(5)

    monkeypatch.setitem(
        sys.modules, "capture.screen_ocr", types.SimpleNamespace(run_ocr_to_clipboard=fake_capture)
    )
    app = types.SimpleNamespace()
    for _ in range(3):  # autorepeat on a held chord
        main_mod.App._on_ocr_hotkey(app)
    assert main_mod._ocr_busy.locked()
    release.set()
    for t in threading.enumerate():
        if t.name == "ocr-capture":
            t.join(5)
    assert started == [1]
    assert not main_mod._ocr_busy.locked()

    # Once the capture is done the next press works again.
    main_mod.App._on_ocr_hotkey(app)
    for t in threading.enumerate():
        if t.name == "ocr-capture":
            t.join(5)
    assert started == [1, 1]


def test_ocr_lock_released_when_the_capture_raises(main_mod, monkeypatch):
    def boom():
        raise RuntimeError("no screenshot portal")

    monkeypatch.setitem(
        sys.modules, "capture.screen_ocr", types.SimpleNamespace(run_ocr_to_clipboard=boom)
    )
    main_mod.App._on_ocr_hotkey(types.SimpleNamespace())
    for t in threading.enumerate():
        if t.name == "ocr-capture":
            t.join(5)
    assert not main_mod._ocr_busy.locked()


# ---- 8. hotkey path: worker thread, presses dropped while busy ---------------


def test_popup_hotkey_runs_off_main_and_drops_presses_while_busy(main_mod):
    release = threading.Event()
    gathers = []

    app = types.SimpleNamespace()
    _bind(app, main_mod, "_off_main")

    def gather(*_args):
        gathers.append(threading.current_thread().name)
        release.wait(5)

    app._gather_hotkey_popup = gather
    for _ in range(3):
        main_mod.App.show_popup_now(app)
    release.set()
    for t in threading.enumerate():
        if t.name == "vinopop-popup-prep":
            t.join(5)
    assert gathers == ["vinopop-popup-prep"]
    assert not main_mod._popup_prep.locked()


def test_prepare_popup_shows_on_the_gtk_thread_with_one_window_lookup(main_mod, monkeypatch):
    lookups = []
    backend = types.SimpleNamespace(
        name="wayland_kde",
        active_window_haystacks=lambda: lookups.append(1) or ["org.kde.okular", "doc.pdf"],
    )
    monkeypatch.setattr(main_mod, "get_backend", lambda: backend)
    from triggers import editable_detect

    monkeypatch.setattr(editable_detect, "get_backend", lambda: backend)
    monkeypatch.setattr(editable_detect, "_start_focus_listener", lambda: None)
    monkeypatch.setattr(editable_detect, "_cached_focus_editable", lambda: None)
    monkeypatch.setattr(editable_detect, "_atspi_focus_editable", lambda: None)
    idle = []
    monkeypatch.setattr(main_mod.GLib, "idle_add", lambda fn, *a: idle.append((fn, a)))
    shown = []
    app = types.SimpleNamespace(
        ignore_ws=True,
        settings={"blocklist_patterns": ["nomatch"]},
        popup=types.SimpleNamespace(show_for=lambda *a, **kw: shown.append(kw)),
    )
    main_mod.App._prepare_popup(app, "hello", 1, 2)
    assert shown == []  # nothing GTK until the idle callback runs
    assert len(idle) == 1
    idle[0][0](*idle[0][1])
    assert shown[0]["editable"] is False  # okular is read-only
    assert lookups == [1]  # blocklist + read-only check shared one KWin lookup


def test_popup_hotkey_closes_a_showing_popup_and_opens_a_hidden_one(main_mod):
    from triggers.toggle import HotkeyToggle

    now = [100.0]
    hidden, opened = [], []
    visible = [True]
    popup = types.SimpleNamespace(
        win=types.SimpleNamespace(get_visible=lambda: visible[0]),
        hotkey_shown_at=None,  # up for a selection
        hide=lambda: hidden.append(1),
    )
    app = types.SimpleNamespace(
        popup=popup,
        _popup_toggle=HotkeyToggle(lambda: now[0]),
        show_popup_now=lambda **kw: opened.append(kw),
    )
    main_mod.App._on_popup_hotkey(app)
    assert hidden == [1] and opened == []
    visible[0] = False
    now[0] += 1.0
    main_mod.App._on_popup_hotkey(app)
    assert opened == [{"via_hotkey": True}]
    # the popup the hotkey just opened survives the key still being held
    visible[0] = True
    popup.hotkey_shown_at = now[0] + 0.1
    now[0] += 0.66
    main_mod.App._on_popup_hotkey(app)
    assert hidden == [1]


# ---- 3. no rebind churn ------------------------------------------------------


class FakeHotkey:
    def __init__(self):
        self.stops = 0

    def stop(self):
        self.stops += 1


def _hotkey_app(main_mod, monkeypatch, backend_name, settings, bound_polling):
    monkeypatch.setattr(main_mod, "get_backend", lambda: types.SimpleNamespace(name=backend_name))
    app = types.SimpleNamespace(
        settings=settings,
        hotkey=None,
        clipboard_hotkey=FakeHotkey(),
        ocr_hotkey=FakeHotkey(),
        _bound_hotkey="",
        _bound_clipboard_hotkey=settings.get("clipboard_hotkey", ""),
        _bound_ocr_hotkey=settings.get("ocr_hotkey", ""),
        _bound_use_polling=bound_polling,
        started=[],
    )
    app._start_hotkey = lambda: app.started.append("main")
    app._start_clipboard_hotkey = lambda: app.started.append("clip")
    app._start_ocr_hotkey = lambda: app.started.append("ocr")
    return app


SETTINGS = {
    "hotkey": "",
    "clipboard_hotkey": "super+v",
    "ocr_hotkey": "super+shift+t",
    "hotkey_use_polling": True,
}


def test_saves_dont_rebind_when_main_hotkey_is_cleared(main_mod, monkeypatch):
    app = _hotkey_app(main_mod, monkeypatch, "x11", dict(SETTINGS), bound_polling=False)
    # First save after turning polling on: one rebuild of what's bound...
    main_mod.App._apply_hotkey_settings(app)
    assert app.started == ["clip", "ocr"]
    assert app._bound_use_polling is True
    # ...and none on the saves after it, although the main hotkey is empty.
    app.started.clear()
    app.clipboard_hotkey, app.ocr_hotkey = FakeHotkey(), FakeHotkey()
    app._bound_clipboard_hotkey, app._bound_ocr_hotkey = "super+v", "super+shift+t"
    for _ in range(3):
        main_mod.App._apply_hotkey_settings(app)
    assert app.started == []
    assert app.clipboard_hotkey.stops == 0
    assert app.ocr_hotkey.stops == 0


def test_polling_flip_is_ignored_on_wayland(main_mod, monkeypatch):
    app = _hotkey_app(main_mod, monkeypatch, "wayland_kde", dict(SETTINGS), bound_polling=False)
    main_mod.App._apply_hotkey_settings(app)
    assert app.started == []
    assert app.clipboard_hotkey.stops == 0


def test_bound_polling_initialised_from_settings(main_mod, monkeypatch, tmp_path):
    from core import settings

    s = settings.Settings(tmp_path / "settings.json")
    s.set("hotkey", "")
    s.set("hotkey_use_polling", True)
    monkeypatch.setattr(main_mod, "get_settings", lambda: s)
    monkeypatch.setattr(main_mod, "PopupWindow", lambda **kw: types.SimpleNamespace())
    monkeypatch.setattr(main_mod.plugin_loader, "load_all", lambda: None)
    for name in (
        "_start_watcher",
        "_start_clipboard_hotkey",
        "_start_ocr_hotkey",
        "_maybe_start_dblclick_watcher",
        "_maybe_first_run",
        "_maybe_prefetch_model",
    ):
        monkeypatch.setattr(main_mod.App, name, lambda self: None)
    app = main_mod.App(enable_tray=False)
    assert app._bound_use_polling is True


# ---- 4. SIGTERM / SIGHUP -----------------------------------------------------


def test_sigint_sigterm_sighup_quit_the_app(main_mod, monkeypatch):
    handlers = {}
    monkeypatch.setattr(
        main_mod.GLib, "unix_signal_add", lambda prio, sig, fn: handlers.setdefault(sig, fn)
    )
    quits = []
    main_mod._install_signal_handlers(types.SimpleNamespace(quit=lambda: quits.append(1)))
    assert set(handlers) == {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    assert handlers[signal.SIGTERM]() is True  # keep the source
    assert quits == [1]


# ---- 5. "Paste from history" hops back to the GTK thread ---------------------


def test_paste_from_history_goes_through_idle_add(main_mod, monkeypatch):
    idle = []
    monkeypatch.setattr(main_mod.GLib, "idle_add", lambda fn, *a: idle.append(fn))
    items = []
    app = types.SimpleNamespace(
        settings={"clipboard_history_enabled": True},
        popup=types.SimpleNamespace(show_actions=lambda it, x, y, **_kw: items.extend(it)),
    )
    app._on_clipboard_hotkey = lambda: pytest.fail("called on the popup's worker thread")
    assert main_mod.App._show_no_selection_popup(app, 1, 2) is False
    (callback,) = [cb for _icon, label, cb in items if label == "Paste from history"]
    callback()
    assert idle == [app._on_clipboard_hotkey]


def test_clipboard_fallback_notifies_through_helper(main_mod, monkeypatch):
    monkeypatch.setattr(main_mod, "_read_selection", lambda source: "")
    notes = []
    monkeypatch.setattr(main_mod, "_notify", lambda title, body: notes.append(body))
    main_mod.App._clipboard_fallback(types.SimpleNamespace())
    assert notes and "Clipboard plugin" in notes[0]


# ---- 6. read-only detection uses the backend ---------------------------------


@pytest.fixture
def editable(monkeypatch):
    from triggers import editable_detect

    monkeypatch.setattr(editable_detect, "_start_focus_listener", lambda: None)
    monkeypatch.setattr(editable_detect, "_cached_focus_editable", lambda: None)
    monkeypatch.setattr(editable_detect, "_atspi_focus_editable", lambda: None)

    def use(name, haystacks):
        backend = types.SimpleNamespace(name=name, active_window_haystacks=lambda: haystacks)
        monkeypatch.setattr(editable_detect, "get_backend", lambda: backend)
        return editable_detect

    return use


@pytest.mark.parametrize(
    ("name", "haystacks", "extra", "expected"),
    [
        # KDE Wayland: [resourceClass, caption] from KWin
        ("wayland_kde", ["org.kde.okular", "manual.pdf — okular"], (), False),
        ("wayland_kde", ["org.kde.dolphin", "home — dolphin"], (), False),
        ("wayland_kde", ["org.kde.kate", "notes.txt — kate"], (), True),
        ("wayland_kde", ["org.kde.kate", "notes.txt — kate"], ("Kate",), False),
        # X11: [title, xprop WM_CLASS line]; the title alone never matches
        ("x11", ["geography notes", 'wm_class(string) = "kate", "kate"'], (), True),
        ("x11", ["manual.pdf", 'wm_class(string) = "evince", "evince"'], (), False),
        ("x11", ["eog"], (), True),  # no WM_CLASS line: title only, ignored
        ("wayland_kde", [], (), True),  # lookup failed: permissive default
    ],
)
def test_readonly_detection_through_backend(editable, name, haystacks, extra, expected):
    mod = editable(name, haystacks)
    assert mod.is_focus_editable(extra_readonly_classes=extra) is expected


def test_readonly_detection_survives_a_failing_lookup(editable, monkeypatch):
    mod = editable("wayland_kde", [])

    def boom():
        raise RuntimeError("dbus gone")

    assert mod.is_focus_editable(haystacks=boom) is True


# ---- 7. hotkey._run closes its X connection on every exit --------------------


class FakeRoot:
    def __init__(self, refuse=False):
        self.refuse = refuse
        self.ungrabbed = []

    def grab_key(self, *a, **kw):
        if self.refuse:
            raise RuntimeError("BadAccess")

    def ungrab_key(self, keycode, mask):
        self.ungrabbed.append((keycode, mask))


class FakeDisplay:
    instances: list = []

    def __init__(self, keycode=38, refuse=False):
        self.keycode = keycode
        self.root = FakeRoot(refuse)
        self.closed = False
        FakeDisplay.instances.append(self)

    def keysym_to_keycode(self, keysym):
        return self.keycode

    def screen(self):
        return types.SimpleNamespace(root=self.root)

    def sync(self):
        pass

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    ("combo", "display_kwargs"),
    [
        ("hyperdrive+x", {}),  # parse error
        ("ctrl+notakey", {}),  # unknown keysym
        ("ctrl+a", {"keycode": 0}),  # no keycode
        ("ctrl+a", {"refuse": True}),  # every grab refused
    ],
)
def test_hotkey_run_closes_display_on_early_return(monkeypatch, combo, display_kwargs):
    from triggers import hotkey

    FakeDisplay.instances.clear()
    monkeypatch.setattr(hotkey.display, "Display", lambda: FakeDisplay(**display_kwargs))
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)  # conflict notify-send
    hk = hotkey.Hotkey(combo, lambda: None)
    hk._run()
    (dpy,) = FakeDisplay.instances
    assert dpy.closed
    assert hk._dpy is None


# ---- 2. X11 double-click watcher stops -------------------------------------


def test_dblclick_watcher_enables_on_record_display_and_stops(monkeypatch):
    import Xlib.display

    from triggers import mouse_watcher

    disabled = threading.Event()
    calls = []

    class RecDisplay:
        def __init__(self, role):
            self.role = role
            self.display = None
            self.closed = False

        def record_get_version(self, *a):
            return types.SimpleNamespace(major_version=1, minor_version=13)

        def record_create_context(self, *a):
            calls.append(("create", self.role))
            return 7

        def sync(self):
            calls.append(("sync", self.role))

        def record_enable_context(self, ctx, handler):
            calls.append(("enable", self.role))
            # Blocks like the real RECORD reply loop, until disabled from
            # the other connection.
            disabled.wait(5)

        def record_disable_context(self, ctx):
            calls.append(("disable", self.role))
            if self.role == "local":
                disabled.set()

        def record_free_context(self, ctx):
            calls.append(("free", self.role))

        def flush(self):
            pass

        def close(self):
            self.closed = True

    roles = iter(["record", "local"])
    monkeypatch.setattr(Xlib.display, "Display", lambda: RecDisplay(next(roles)))
    w = mouse_watcher.DoubleClickWatcher(lambda x, y: None)
    w.start()
    for _ in range(100):
        if ("enable", "record") in calls:
            break
        threading.Event().wait(0.01)
    assert ("enable", "record") in calls
    assert ("sync", "local") in calls
    w.stop()
    assert not w._thread.is_alive()
    assert ("disable", "local") in calls
    assert w._record_display is None and w._local_display is None
