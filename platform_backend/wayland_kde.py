"""KDE Plasma 6 / Wayland platform backend: wl-clipboard for the selection,
KWin scripting for the pointer and the active window, gtk4-layer-shell for
the popup. docs/how-it-works.md, "The KDE Wayland backend"."""

from __future__ import annotations

import os
import select as _select
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable

from gi.repository import GLib

from .base import PlatformBackend, is_copy_button, take_expected
from .evdev_input import (
    BTN_LEFT,
    EVDEV_TO_XKB_KEYCODE,
    KEY_LEFTALT,
    KEY_LEFTCTRL,
    KEY_LEFTMETA,
    KEY_LEFTSHIFT,
    KEY_PRESSED,
    KEY_RELEASED,
    KEY_RIGHTALT,
    KEY_RIGHTCTRL,
    KEY_RIGHTMETA,
    KEY_RIGHTSHIFT,
    MODIFIER_KEYS,
    InputEvent,
    get_input_hub,
)
from .gestures import GESTURE_LEAD_S, SelectionGestures
from .kde_hotkey import WaylandKdeHotkey, _qt_keycode  # noqa: F401 - re-exported
from .keyinject import KeyInjector
from .monitors import layer_margins, monitor_at
from .xkb import detect_kbd_rmlvo as _detect_kbd_rmlvo

_LAYER = None
WL_TIMEOUT_S = 2.0
KWIN_QUERY_TIMEOUT_S = 3.0


def _kwin_subprocess_env() -> dict:
    """The environment with the app's folder on PYTHONPATH, so
    `python3 -m platform_backend._kwin_*` imports from any cwd."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root + (os.pathsep + existing if existing else "")
    return env


def _layer_shell():
    """Gtk4LayerShell, imported on first use (without it main.py has put the
    process on XWayland)."""
    global _LAYER
    if _LAYER is None:
        import gi

        gi.require_version("Gtk4LayerShell", "1.0")
        from gi.repository import Gtk4LayerShell

        _LAYER = Gtk4LayerShell
    return _LAYER


def _die_with_parent() -> None:
    """preexec_fn: SIGTERM this child when the daemon dies, however it dies,
    so a restart leaves no `wl-paste --watch` behind."""
    import ctypes
    import signal

    PR_SET_PDEATHSIG = 1
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)


def _reap(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except OSError:
        pass


class WaylandSelectionWatcher:
    """`wl-paste --watch` pings on every change; once the pings go quiet the
    selection is read and shown at the pointer's last position."""

    _MAX_BACKOFF_S = 30.0
    _WAKE_S = 0.5  # longest stop() can go unnoticed
    _MIN_SETTLE_S = 0.2
    _FIRST_BACKOFF_S = 1.0
    _READ_CHUNK = 4096

    def __init__(
        self,
        backend: WaylandKdeBackend,
        on_selection: Callable[[str, int, int], None],
        debounce_ms: int = 150,
        gestures: SelectionGestures | None = None,
    ) -> None:
        self._backend = backend
        self._on_selection = on_selection
        self._debounce_s = max(0.0, debounce_ms / 1000.0)
        self._gestures = gestures or SelectionGestures()
        self._procs: list[subprocess.Popen] = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._clip_changed_at: float | None = None

    def _is_copy_button(self, text: str, burst_t0: float) -> bool:
        return is_copy_button(
            text, burst_t0, self._clip_changed_at, lambda: self._backend.read_selection("clipboard")
        )

    def _should_show(self, text: str, burst_t0: float) -> bool:
        if take_expected(text):
            return True
        if self._is_copy_button(text, burst_t0):
            print("[wayland] ignored a copy-button selection (PRIMARY == new CLIPBOARD)")
            return False
        if not self._gestures.selected_since(burst_t0 - GESTURE_LEAD_S):
            print("[wayland] ignored a PRIMARY change with no selecting gesture")
            return False
        return True

    def set_debounce_ms(self, ms: int) -> None:
        self._debounce_s = max(0.0, ms / 1000.0)

    def start(self) -> None:
        if not shutil.which("wl-paste"):
            print("[wayland] wl-paste missing - selection watch disabled")
            return
        self._stop.clear()
        self._gestures.start()
        self._threads = [
            threading.Thread(
                target=self._supervise,
                args=(["wl-paste", "--primary", "--watch", "echo"], self._consume_primary),
                daemon=True,
                name="vinopop-wl-watch",
            ),
            threading.Thread(
                target=self._supervise,
                args=(["wl-paste", "--watch", "echo"], self._consume_clipboard),
                daemon=True,
                name="vinopop-wl-clip",
            ),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        self._gestures.stop()
        for proc in list(self._procs):
            try:
                proc.terminate()
            except Exception:
                pass

    def _supervise(self, args: list[str], consume: Callable[[int], None]) -> None:
        """Run `args` and feed its stdout to `consume` until stop(), restarting
        it with backoff: wl-paste exits when the compositor restarts."""
        delay = self._FIRST_BACKOFF_S
        while not self._stop.is_set():
            try:
                proc = subprocess.Popen(
                    args, stdout=subprocess.PIPE, bufsize=0, preexec_fn=_die_with_parent
                )
            except OSError as exc:
                print(f"[wayland] could not start {' '.join(args)}: {exc}")
                return
            self._procs.append(proc)
            started = time.monotonic()
            try:
                if proc.stdout is not None and not self._stop.is_set():
                    consume(proc.stdout.fileno())
            finally:
                self._procs.remove(proc)
                _reap(proc)
            if self._stop.is_set():
                return
            if time.monotonic() - started > self._MAX_BACKOFF_S:
                delay = self._FIRST_BACKOFF_S
            print(f"[wayland] {' '.join(args)} exited; restarting in {delay:.0f}s")
            self._stop.wait(delay)
            delay = min(delay * 2, self._MAX_BACKOFF_S)

    def _ping(self, fd: int, timeout: float) -> bool | None:
        """True on a ping, False on timeout, None once the stream ends."""
        readable, _, _ = _select.select([fd], [], [], timeout)
        if not readable:
            return False
        try:
            return True if os.read(fd, self._READ_CHUNK) else None
        except OSError:
            return None

    def _consume_clipboard(self, fd: int) -> None:
        """Timestamp clipboard changes. `wl-paste --watch` pings once at start
        for what's already there; that one isn't a change."""
        first = True
        while not self._stop.is_set():
            got = self._ping(fd, self._WAKE_S)
            if got is None:
                return
            if got and not first:
                self._clip_changed_at = time.monotonic()
            first = first and not got

    def _consume_primary(self, fd: int) -> None:
        """Show each settled PRIMARY change, skipping --watch's start-up ping."""
        first = True
        while not self._stop.is_set():
            got = self._ping(fd, self._WAKE_S)
            if got is None:
                return
            if not got:
                continue
            burst_t0 = time.monotonic()
            if not first:
                self._backend.prefetch_active_window()
            anchor, ended = self._drain_burst(fd, burst_t0)
            if self._stop.is_set() or ended:
                return
            if first:
                first = False
                continue
            text = self._backend.read_selection("primary")
            if not text or not text.strip():
                continue
            if not self._should_show(text, burst_t0):
                continue
            self._show(text, anchor)

    def _pointer_or(self, fallback: tuple[int, int] | None) -> tuple[int, int] | None:
        try:
            return self._backend.pointer_position()
        except Exception:  # noqa: BLE001
            return fallback

    def _drain_burst(self, fd: int, burst_t0: float) -> tuple[tuple[int, int] | None, bool]:
        """Read pings until they go quiet. Returns the pointer position at the
        last ping (not after the quiet: the mouse is heading for the popup by
        then), and whether the stream ended."""
        anchor = self._pointer_or(None)
        settle = max(self._MIN_SETTLE_S, self._debounce_s)
        while True:
            more = self._ping(fd, self._gestures.settle_s(burst_t0, settle))
            if not more:
                return anchor, more is None
            anchor = self._pointer_or(anchor)

    def _show(self, text: str, anchor: tuple[int, int] | None) -> None:
        if anchor is None:
            try:
                anchor = self._backend.pointer_position()
            except Exception as exc:  # noqa: BLE001
                print(f"[wayland] pointer query failed: {exc}")
                anchor = (0, 0)
        try:
            self._on_selection(text, anchor[0], anchor[1])
        except Exception as exc:  # noqa: BLE001
            print(f"[wayland] selection callback error: {exc}")


XKB_KEY_UP, XKB_KEY_DOWN = 0, 1


class _XkbModifierState:
    """Which modifiers are logically held, from evdev keycodes run through
    libxkbcommon with the user's layout and options (so a Ctrl/Alt swap
    counts)."""

    _MOD_NAME = {
        "ctrl": b"Control",
        "shift": b"Shift",
        "alt": b"Mod1",
        "super": b"Mod4",
    }
    _XKB_STATE_MODS_EFFECTIVE = 1 << 3
    _DEFAULT_MODEL, _DEFAULT_LAYOUT = "pc105", "us"

    def __init__(self) -> None:
        import ctypes as C

        self._ok = False
        try:
            xkb = C.CDLL("libxkbcommon.so.0")
        except OSError:
            return
        self._xkb = xkb

        class _RN(C.Structure):
            _fields_ = [
                ("rules", C.c_char_p),
                ("model", C.c_char_p),
                ("layout", C.c_char_p),
                ("variant", C.c_char_p),
                ("options", C.c_char_p),
            ]

        xkb.xkb_context_new.restype = C.c_void_p
        xkb.xkb_context_new.argtypes = [C.c_int]
        xkb.xkb_keymap_new_from_names.restype = C.c_void_p
        xkb.xkb_keymap_new_from_names.argtypes = [C.c_void_p, C.POINTER(_RN), C.c_int]
        xkb.xkb_state_new.restype = C.c_void_p
        xkb.xkb_state_new.argtypes = [C.c_void_p]
        xkb.xkb_state_update_key.restype = C.c_int
        xkb.xkb_state_update_key.argtypes = [C.c_void_p, C.c_uint32, C.c_int]
        xkb.xkb_state_mod_name_is_active.restype = C.c_int
        xkb.xkb_state_mod_name_is_active.argtypes = [C.c_void_p, C.c_char_p, C.c_int]
        ctx = xkb.xkb_context_new(0)
        if not ctx:
            return
        model, layout, variant, options = _detect_kbd_rmlvo()

        def _mk(opts):
            rn = _RN(
                b"evdev",
                (model or self._DEFAULT_MODEL).encode(),
                (layout or self._DEFAULT_LAYOUT).encode(),
                variant.encode() if variant else None,
                opts.encode() if opts else None,
            )
            return xkb.xkb_keymap_new_from_names(ctx, C.byref(rn), 0)

        km = _mk(options) or _mk("")
        if not km:
            return
        self._state = xkb.xkb_state_new(km)
        self._ok = bool(self._state)
        print(
            f"[dblclick] xkb state ok={self._ok} layout={layout} "
            f"variant={variant or '-'} options={options or '-'}"
        )

    def update(self, evdev_code: int, down: bool) -> None:
        if self._ok:
            direction = XKB_KEY_DOWN if down else XKB_KEY_UP
            self._xkb.xkb_state_update_key(
                self._state, evdev_code + EVDEV_TO_XKB_KEYCODE, direction
            )

    def is_active(self, mod: str):
        """Whether the named modifier is effectively held; None when xkb
        can't tell."""
        if not self._ok:
            return None
        name = self._MOD_NAME.get(mod)
        if not name:
            return None
        return (
            self._xkb.xkb_state_mod_name_is_active(
                self._state, name, self._XKB_STATE_MODS_EFFECTIVE
            )
            == 1
        )


class WaylandDoubleClickWatcher:
    """Modifier + double-click from /dev/input through the evdev hub (XRecord
    only sees XWayland windows on KWin). Events arrive on the hub's thread.
    Inert without the 'input' group."""

    _MOD_CODES = {
        "ctrl": {KEY_LEFTCTRL, KEY_RIGHTCTRL},
        "shift": {KEY_LEFTSHIFT, KEY_RIGHTSHIFT},
        "alt": {KEY_LEFTALT, KEY_RIGHTALT},
        "super": {KEY_LEFTMETA, KEY_RIGHTMETA},
    }
    _DEFAULT_MOD = "ctrl"
    _DOUBLE_CLICK_MS = 300
    _POSITION_TOLERANCE_PX = 8
    _AFTER_CLICK_MS = 20  # the app under the pointer sees the click first

    def __init__(self, backend, on_double_click, hub=None) -> None:
        self._backend = backend
        self._cb = on_double_click
        self._hub = hub
        self._handle: int | None = None
        self._held: set[int] = set()  # modifier keycodes down, when xkb can't tell
        self._xkb = None  # built on the first event
        self._last_t = 0.0  # kernel timestamp of the last qualifying press
        self._last_xy = (0, 0)

    def start(self) -> None:
        if self._handle is not None:
            return
        if self._hub is None:
            self._hub = get_input_hub()
        self._handle = self._hub.subscribe(self._on_event)
        print("[dblclick] watching /dev/input (Wayland)")

    def stop(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None and self._hub is not None:
            self._hub.unsubscribe(handle)

    def _required_mod_name(self) -> str:
        """The configured modifier, read on each click so a change applies live."""
        try:
            from core.settings import get_settings

            name = (get_settings().get("double_click_modifier") or self._DEFAULT_MOD).lower()
        except Exception:
            name = self._DEFAULT_MOD
        return name if name in self._MOD_CODES else self._DEFAULT_MOD

    def _required_codes(self) -> set:
        return self._MOD_CODES[self._required_mod_name()]

    def _on_event(self, ev: InputEvent) -> None:
        if self._xkb is None:
            self._xkb = _XkbModifierState()
        if ev.code == BTN_LEFT:
            if ev.value == KEY_PRESSED:
                self._on_left_press(ev.time)
            return
        if ev.value in (KEY_RELEASED, KEY_PRESSED):
            self._xkb.update(ev.code, ev.value == KEY_PRESSED)
        if ev.code in MODIFIER_KEYS:
            if ev.value == KEY_PRESSED:
                self._held.add(ev.code)
            elif ev.value == KEY_RELEASED:
                self._held.discard(ev.code)

    def _on_left_press(self, t: float | None = None) -> None:
        """`t` is the event's kernel timestamp."""
        name = self._required_mod_name()
        active = self._xkb.is_active(name) if self._xkb else None
        if active is None:
            active = bool(self._held & self._required_codes())
        if not active:
            return
        x, y = self._backend.pointer_position()
        now = time.monotonic() if t is None else t
        elapsed_ms = (now - self._last_t) * 1000
        dx = abs(x - self._last_xy[0])
        dy = abs(y - self._last_xy[1])
        if (
            elapsed_ms < self._DOUBLE_CLICK_MS
            and dx < self._POSITION_TOLERANCE_PX
            and dy < self._POSITION_TOLERANCE_PX
        ):
            self._last_t = 0.0
            self._last_xy = (0, 0)
            GLib.timeout_add(self._AFTER_CLICK_MS, self._fire, x, y)
            return
        self._last_t = now
        self._last_xy = (x, y)

    def _fire(self, x: int, y: int) -> bool:
        try:
            self._cb(x, y)
        except Exception as exc:  # noqa: BLE001
            print(f"[dblclick] callback failed: {exc}")
        return False  # GLib.SOURCE_REMOVE


class _KWinHelper:
    """Client for the long-lived _kwin_query process, respawned after a
    timeout or crash."""

    _ARGV = [sys.executable, "-m", "platform_backend._kwin_query"]

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        with self._lock:
            self._ensure()

    def _ensure(self) -> subprocess.Popen | None:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        try:
            self._proc = subprocess.Popen(
                self._ARGV,
                # Binary pipes (pinned by
                # `tests/test_wayland_watcher.py::test_kwin_answer_cannot_break_framing`)
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                env=_kwin_subprocess_env(),
                preexec_fn=_die_with_parent,
            )
        except OSError as exc:
            print(f"[wayland] could not start the KWin helper: {exc}")
            self._proc = None
        return self._proc

    def query(self, kind: str, timeout: float = KWIN_QUERY_TIMEOUT_S) -> str:
        """The helper's answer line, or "" on any failure."""
        with self._lock:
            proc = self._ensure()
            if proc is None or proc.stdin is None or proc.stdout is None:
                return ""
            line = ""
            try:
                proc.stdin.write(kind.encode() + b"\n")
                proc.stdin.flush()
                readable, _, _ = _select.select([proc.stdout], [], [], timeout)
                if readable:
                    line = proc.stdout.readline().decode("utf-8", "replace")
            except (OSError, ValueError):
                pass
            if not line.endswith("\n"):
                # A late answer would be read as the reply to the next query.
                print(f"[wayland] KWin helper gave no answer to {kind!r}; restarting it")
                self._proc = None
                threading.Thread(target=_reap, args=(proc,), daemon=True).start()
                return ""
            return line.rstrip("\n")


class WaylandKdeBackend(PlatformBackend):
    name = "wayland_kde"
    popup_uses_xlib = False
    pointer_is_logical = True  # KWin's cursorPos, like GTK's monitor geometry

    def __init__(self) -> None:
        self._pointer_lock = threading.Lock()
        # (monotonic time the query finished, coords, whether it succeeded)
        self._pointer_last: tuple[float, tuple[int, int], bool] | None = None
        self._kwin = _KWinHelper()
        self._active_lock = threading.Lock()
        self._active_pending: tuple[float, threading.Event, list[list[str]]] | None = None
        # Before any SessionBus exists, or KGlobalAccel's signals never arrive
        try:
            from dbus.mainloop.glib import DBusGMainLoop

            DBusGMainLoop(set_as_default=True)
        except Exception:
            pass

    def check_session(self) -> None:
        if not os.environ.get("WAYLAND_DISPLAY"):
            print("[wayland] WAYLAND_DISPLAY not set - wrong backend.", file=sys.stderr)
            sys.exit(2)
        self._kwin.start()  # its Python startup, now rather than on the first popup
        desktop = (os.environ.get("XDG_CURRENT_DESKTOP") or "").upper()
        if "KDE" not in desktop:
            print(
                f"[wayland] WARNING: XDG_CURRENT_DESKTOP={desktop!r} is not "
                "KDE. Cursor positioning relies on KWin; popup placement may "
                "not work on this compositor.",
                file=sys.stderr,
            )

    def read_selection(self, source: str) -> str:
        """The selection's text; "" when it's empty or holds no text (an image
        clipboard would otherwise come back as PNG bytes)."""
        args = ["wl-paste", "--no-newline", "--type", "text"]
        if source.lower() == "primary":
            args.append("--primary")
        try:
            out = subprocess.run(args, capture_output=True, timeout=WL_TIMEOUT_S)
            if out.returncode != 0:
                return ""
            return out.stdout.decode("utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError):
            return ""

    def set_clipboard(self, text: str) -> None:
        if not shutil.which("wl-copy"):
            print("[wayland] wl-copy missing - cannot set clipboard")
            return
        try:
            subprocess.run(
                ["wl-copy"], input=text.encode("utf-8"), check=False, timeout=WL_TIMEOUT_S
            )
        except subprocess.SubprocessError as exc:
            print(f"[wayland] wl-copy failed: {exc}")

    _POINTER_CACHE_S = 0.1

    def pointer_position(self) -> tuple[int, int]:
        """KWin's cursor position. A query that finished while this call waited
        on the lock, or a good one under _POINTER_CACHE_S old, is reused (pinned
        by `tests/test_placement.py::test_concurrent_pointer_queries_share_one_result`,
        `tests/test_placement.py::test_pointer_cache_expires`)."""
        arrived = time.monotonic()
        with self._pointer_lock:
            last = self._pointer_last
            if last is not None:
                done_at, xy, ok = last
                if done_at >= arrived or (ok and arrived - done_at < self._POINTER_CACHE_S):
                    return xy
            xy, ok = self._query_pointer()
            self._pointer_last = (time.monotonic(), xy, ok)
            return xy

    def _query_pointer(self) -> tuple[tuple[int, int], bool]:
        out = self._kwin.query("cursor")
        parts = out.split()
        try:
            if len(parts) == 2:
                return (int(parts[0]), int(parts[1])), True
        except ValueError:
            pass
        print(f"[wayland] cursor query gave no coords ({out!r}) -> falling back to (0,0)")
        return (0, 0), False

    def can_paste(self) -> bool:
        return self.injector().can_inject()

    def injector(self) -> KeyInjector:
        inj = getattr(self, "_injector", None)
        if inj is None:
            inj = self._injector = KeyInjector()
        return inj

    def send_key(self, combo: str) -> None:
        self.injector().send_key(combo)

    def type_text(self, text: str) -> None:
        self.injector().type_text(text)

    def active_window_haystacks(self) -> list[str]:
        with self._active_lock:
            pending, self._active_pending = self._active_pending, None
        if pending is not None and time.monotonic() - pending[0] < self._ACTIVE_PREFETCH_S:
            pending[1].wait(KWIN_QUERY_TIMEOUT_S)
            if pending[2]:
                return pending[2][0]
        return self._query_active()

    # An older prefetch describes another moment than the caller's.
    _ACTIVE_PREFETCH_S = 2.0

    def prefetch_active_window(self) -> None:
        """Start the active-window lookup now, for the next
        active_window_haystacks() call to pick up."""
        done = threading.Event()
        result: list[list[str]] = []
        with self._active_lock:
            self._active_pending = (time.monotonic(), done, result)

        def run() -> None:
            result.append(self._query_active())
            done.set()

        threading.Thread(target=run, daemon=True, name="vinopop-active-prefetch").start()

    def _query_active(self) -> list[str]:
        cls, _, caption = self._kwin.query("active").partition("\t")
        return [s.lower() for s in (cls, caption) if s]

    def open_url(self, url: str) -> None:
        try:
            subprocess.Popen(["xdg-open", url], start_new_session=True)
        except FileNotFoundError:
            print("[wayland] xdg-open not available")
            return
        # KWin's focus-stealing prevention would leave it in the background.
        threading.Thread(target=self._raise_browser, daemon=True).start()

    _FALLBACK_BROWSER = "firefox"
    _TAB_OPEN_S = 0.5
    _SCRIPT_RUN_S = 0.3

    def _browser_keyword(self) -> str:
        """The default browser's resourceClass substring: org.mozilla.firefox
        gives firefox, google-chrome stays google-chrome."""
        kw = getattr(self, "_browser_kw", None)
        if kw is not None:
            return kw
        kw = self._FALLBACK_BROWSER
        try:
            out = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                capture_output=True,
                text=True,
                timeout=WL_TIMEOUT_S,
            )
            ident = out.stdout.strip().lower()
            if ident.endswith(".desktop"):
                ident = ident[: -len(".desktop")]
            if ident:
                kw = ident.split(".")[-1] if "." in ident else ident
        except (OSError, subprocess.SubprocessError):
            pass
        self._browser_kw = kw
        return kw

    def _raise_browser(self) -> None:
        time.sleep(self._TAB_OPEN_S)
        kw = self._browser_keyword().replace('"', "").replace("\\", "")
        js = (
            f'var s="{kw}";'
            "var L=(workspace.windowList?workspace.windowList():workspace.clientList());"
            "for(var i=0;i<L.length;i++){var w=L[i];try{"
            "if(String(w.resourceClass).toLowerCase().indexOf(s)>=0){"
            "w.minimized=false;workspace.activeWindow=w;break;}}catch(e){}}"
        )
        import tempfile

        path = None
        try:
            import dbus

            bus = dbus.SessionBus()
            fd, path = tempfile.mkstemp(suffix=".js", prefix="vinopop-raise-")
            os.write(fd, js.encode("utf-8"))
            os.close(fd)
            scripting = dbus.Interface(
                bus.get_object("org.kde.KWin", "/Scripting"),
                "org.kde.kwin.Scripting",
            )
            sid = int(scripting.loadScript(path))
            dbus.Interface(
                bus.get_object("org.kde.KWin", f"/Scripting/Script{sid}"),
                "org.kde.kwin.Script",
            ).run()
            time.sleep(self._SCRIPT_RUN_S)
            try:
                scripting.unloadScript(path)
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            print(f"[wayland] raise-browser failed: {exc}")
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def make_selection_watcher(self, on_selection, debounce_ms):
        return WaylandSelectionWatcher(self, on_selection, debounce_ms=debounce_ms)

    def make_hotkey(self, hotkey_str, on_trigger, use_polling=False, role=None):
        return WaylandKdeHotkey(hotkey_str, on_trigger, role=role)

    def can_bind_hotkey(self, hotkey_str):
        return _qt_keycode(hotkey_str) is not None

    def make_double_click_watcher(self, on_double_click):
        return WaylandDoubleClickWatcher(self, on_double_click)

    def prepare_popup_window(self, win) -> None:
        from .x11 import gdk_display_is_x11, x11_prepare_popup_window

        # main.py put GDK on XWayland when gtk4-layer-shell is missing.
        if gdk_display_is_x11(win.get_display()):
            x11_prepare_popup_window(win)
            return
        L = _layer_shell()
        try:
            if not L.is_supported():
                print("[wayland] compositor has no wlr-layer-shell - popup placement will be off")
        except Exception:
            pass
        L.init_for_window(win)
        L.set_layer(win, L.Layer.OVERLAY)
        # Not ON_DEMAND: a popup that takes focus loses it and hides at once.
        try:
            L.set_keyboard_mode(win, L.KeyboardMode.NONE)
        except Exception:
            pass
        L.set_anchor(win, L.Edge.LEFT, True)
        L.set_anchor(win, L.Edge.TOP, True)

    def move_popup_window(self, win, x: int, y: int) -> None:
        from .x11 import gdk_display_is_x11, x11_move_popup_window

        if gdk_display_is_x11(win.get_display()):
            x11_move_popup_window(win, x, y)
            return
        L = _layer_shell()
        L.set_anchor(win, L.Edge.LEFT, True)
        L.set_anchor(win, L.Edge.TOP, True)
        # Margins are relative to the surface's output (pinned by
        # `tests/test_placement.py::test_layer_popup_on_second_monitor`,
        # `tests/test_placement.py::test_offscreen_parking_keeps_the_output`).
        try:
            mon = monitor_at(win.get_display().get_monitors(), x, y, fallback=False)
        except Exception:
            mon = None
        if mon is not None:
            if getattr(win, "_vinopop_layer_monitor", None) is not mon:
                L.set_monitor(win, mon)
                win._vinopop_layer_monitor = mon
            x, y = layer_margins(x, y, mon.get_geometry())
        L.set_margin(win, L.Edge.LEFT, int(x))
        L.set_margin(win, L.Edge.TOP, int(y))
