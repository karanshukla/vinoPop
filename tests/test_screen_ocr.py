"""What an OCR capture does with its text and image, with describe off and on."""

import subprocess
import threading

import pytest

from capture import screen_ocr


@pytest.fixture
def capture(monkeypatch, tmp_path):
    image = tmp_path / "capture.png"
    image.write_bytes(b"png")
    calls = {"notify": [], "staged": [], "describe": [], "offer": []}
    state = {"text": "hello", "describe": False}
    monkeypatch.setattr(screen_ocr, "is_supported", lambda: (True, ""))
    monkeypatch.setattr(screen_ocr, "_capture_image", lambda: (image, None))
    monkeypatch.setattr(screen_ocr, "_recognize", lambda _p: state["text"])
    monkeypatch.setattr(screen_ocr, "_describe_enabled", lambda: state["describe"])
    monkeypatch.setattr(screen_ocr, "_notify", lambda *a, **_k: calls["notify"].append(a))
    monkeypatch.setattr(screen_ocr, "_stage_text", calls["staged"].append)
    monkeypatch.setattr(screen_ocr, "_describe", lambda *a: calls["describe"].append(a))
    monkeypatch.setattr(screen_ocr, "_offer_describe", lambda *a: calls["offer"].append(a))
    return image, state, calls


def test_text_goes_to_the_clipboard_and_the_image_is_deleted(capture):
    image, _state, calls = capture
    screen_ocr.run_ocr_to_clipboard()
    assert calls["staged"] == ["hello"] and calls["notify"][0][0] == "OCR captured"
    assert not calls["offer"] and not image.exists()


def test_no_text_without_describe_says_so(capture):
    image, state, calls = capture
    state["text"] = ""
    screen_ocr.run_ocr_to_clipboard()
    assert "No text found" in calls["notify"][0][1]
    assert not calls["describe"] and not image.exists()


def test_no_text_with_describe_describes_straight_away(capture):
    image, state, calls = capture
    state.update(text="", describe=True)
    screen_ocr.run_ocr_to_clipboard()
    assert calls["describe"] == [(image, "")] and not calls["notify"]
    assert image.exists()  # the bubble's worker deletes it


def test_text_with_describe_offers_the_button(capture):
    image, state, calls = capture
    state["describe"] = True
    screen_ocr.run_ocr_to_clipboard()
    assert calls["staged"] == ["hello"] and not calls["notify"]
    assert calls["offer"][0][2:] == (image, "hello") and image.exists()


class _SyncThread:
    def __init__(self, target, **_kw):
        self._target = target

    def start(self):
        self._target()


@pytest.mark.parametrize("stdout,described", [("describe\n", True), ("", False)])
def test_offer_describes_only_on_click(monkeypatch, tmp_path, stdout, described):
    image = tmp_path / "capture.png"
    image.write_bytes(b"png")
    seen = []
    monkeypatch.setattr(threading, "Thread", _SyncThread)
    monkeypatch.setattr(
        screen_ocr.subprocess,
        "run",
        lambda args, **_k: subprocess.CompletedProcess(args, 0, stdout, ""),
    )
    monkeypatch.setattr(screen_ocr, "_describe", lambda *a: seen.append(a))
    screen_ocr._offer_describe("OCR captured", "5 chars", image, "hello")
    assert (seen == [(image, "hello")]) is described
    assert image.exists() is described


@pytest.mark.parametrize(
    "backend, tool",
    [("x11", "xclip"), ("wayland_kde", "wl-copy")],
)
def test_ocr_text_is_staged_with_the_backends_clipboard_tool(monkeypatch, backend, tool):
    # GNOME has a WAYLAND_DISPLAY too, but mutter has no data-control
    # protocol, so the XWayland backend must stay on xclip (#95)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr("platform_backend.detect", lambda: backend)
    monkeypatch.setattr(screen_ocr.shutil, "which", lambda name: f"/usr/bin/{name}")
    runs = []
    monkeypatch.setattr(screen_ocr.subprocess, "run", lambda args, **_k: runs.append(args[0]))
    screen_ocr._stage_text("hello")
    assert runs == [tool, tool]  # CLIPBOARD and PRIMARY


# ---- the overlay: GTK steps on the main loop, the grab on the worker (#74) --


class _FakeSelector:
    """Records which thread each step ran on. `idle_add` below runs main-loop
    callbacks on a thread of its own, standing in for the GTK thread."""

    def __init__(self, tmp_path, monitors=1, grab_ok=True, show_error=None, cancel=False):
        self.tmp_path = tmp_path
        self.monitors = monitors
        self.grab_ok = grab_ok
        self.show_error = show_error
        self.cancel = cancel
        self.threads: dict[str, str] = {}
        self.calls: list = []
        self.gtk = threading.current_thread().name  # replaced by the fixture

    def _mark(self, step):
        self.threads[step] = threading.current_thread().name

    def monitor_count(self):
        self._mark("monitor_count")
        return self.monitors

    def pointer_hint(self):
        self._mark("pointer_hint")
        return (10, 20, False)

    def pick_monitor(self, pointer):
        self._mark("pick_monitor")
        self.calls.append(("pick", pointer))
        return "MON" if pointer else None

    def grab(self, monitor):
        self._mark("grab")
        self.calls.append(("grab", monitor))
        if not self.grab_ok:
            return None
        shot = self.tmp_path / "shot.png"
        shot.write_bytes(b"png")
        return str(shot)

    def show(self, shot, callback, monitor):
        self._mark("show")
        self.calls.append(("show", shot, monitor))
        if self.show_error:
            raise self.show_error
        # The real overlay calls back from the drag-end handler, later.
        threading.Thread(target=callback, args=(None if self.cancel else "/crop.png",)).start()


@pytest.fixture
def gtk_thread():
    """An idle_add that runs callbacks on one long-lived 'gtk' thread."""
    import queue

    q: queue.Queue = queue.Queue()

    def loop():
        while True:
            fn = q.get()
            if fn is None:
                return
            fn()

    t = threading.Thread(target=loop, name="gtk", daemon=True)
    t.start()
    yield q.put
    q.put(None)


def test_overlay_grabs_on_the_worker_and_shows_on_the_gtk_thread(tmp_path, gtk_thread):
    sel = _FakeSelector(tmp_path)
    assert screen_ocr._capture_via_overlay(sel, gtk_thread) == "/crop.png"
    assert sel.threads["monitor_count"] == "gtk"
    assert sel.threads["pick_monitor"] == "gtk"
    assert sel.threads["show"] == "gtk"
    assert sel.threads["grab"] == threading.current_thread().name
    # One monitor: no pointer round-trip, whole desktop
    assert "pointer_hint" not in sel.threads
    assert sel.calls == [("pick", None), ("grab", None), ("show", str(tmp_path / "shot.png"), None)]


def test_overlay_asks_the_pointer_off_the_gtk_thread_with_several_monitors(tmp_path, gtk_thread):
    sel = _FakeSelector(tmp_path, monitors=2)
    assert screen_ocr._capture_via_overlay(sel, gtk_thread) == "/crop.png"
    assert sel.threads["pointer_hint"] == threading.current_thread().name
    assert sel.calls[0] == ("pick", (10, 20, False)) and sel.calls[1] == ("grab", "MON")


def test_overlay_cancel_is_none(tmp_path, gtk_thread):
    assert screen_ocr._capture_via_overlay(_FakeSelector(tmp_path, cancel=True), gtk_thread) is None


def test_overlay_failed_grab_raises_so_the_desktop_tool_takes_over(tmp_path, gtk_thread):
    with pytest.raises(RuntimeError, match="grab"):
        screen_ocr._capture_via_overlay(_FakeSelector(tmp_path, grab_ok=False), gtk_thread)


def test_overlay_show_error_surfaces_and_deletes_the_shot(tmp_path, gtk_thread):
    sel = _FakeSelector(tmp_path, show_error=ValueError("no pixbuf"))
    with pytest.raises(ValueError, match="no pixbuf"):
        screen_ocr._capture_via_overlay(sel, gtk_thread)
    assert not (tmp_path / "shot.png").exists()


def test_a_frozen_gtk_loop_is_a_timeout_not_a_hang():
    with pytest.raises(TimeoutError):
        screen_ocr._on_gtk_thread(lambda: 1, 0.05, idle_add=lambda fn: None)


# ---- which capture tools count (GNOME has only the portal) ------------------


def _only(monkeypatch, tools, portal):
    monkeypatch.setattr(
        screen_ocr.shutil, "which", lambda n: f"/usr/bin/{n}" if n in tools else None
    )
    monkeypatch.setattr(screen_ocr, "_portal_available", lambda: portal)


def test_maim_does_not_count_on_wayland(monkeypatch):
    # Under XWayland maim only sees XWayland's windows and black.
    _only(monkeypatch, {"maim"}, portal=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert not screen_ocr._has_capture_tool()
    monkeypatch.delenv("WAYLAND_DISPLAY")
    assert screen_ocr._has_capture_tool()


def test_the_portal_alone_is_enough(monkeypatch):
    _only(monkeypatch, set(), portal=True)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert screen_ocr._has_capture_tool()


def test_region_falls_back_to_the_portal_picker(monkeypatch, tmp_path):
    _only(monkeypatch, {"maim"}, portal=True)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    shot = tmp_path / "portal.png"
    shot.write_bytes(b"png")
    monkeypatch.setattr(screen_ocr, "_portal_region", lambda: str(shot))
    out = tmp_path / "out.png"
    assert screen_ocr._capture_region(out)
    assert out.read_bytes() == b"png" and not shot.exists()
