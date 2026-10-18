"""KDE Plasma 6 / Wayland platform backend.

Proven on Fedora 44 KDE Plasma (KWin 6.6.5) during the Fase 0 spike:
  - clipboard / primary    -> wl-clipboard (wl-copy / wl-paste)
  - selection change watch  -> `wl-paste --primary --watch`
  - global pointer position -> KWin `workspace.cursorPos` over DBus
                               (see _kwin_cursor.py; asked through one
                               long-lived _kwin_query.py process)
  - keystroke injection     -> wtype
  - popup positioning        -> gtk4-layer-shell (anchor top-left + margins);
                               XWayland override-redirect when it's missing
  - global hotkey            -> KGlobalAccel over DBus (best-effort, see below)

The global hotkey path is the one piece that cannot be verified without a
physical key press; its Qt-keycode/flags encoding is a researched best-guess.
It is fully guarded and non-fatal: if registration fails the app runs normally
and auto-popup-on-selection (the headline feature) is unaffected.
"""

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
from .monitors import layer_margins, monitor_at

_LAYER = None


def _kwin_subprocess_env() -> dict:
    """Environment for the `python3 -m platform_backend._kwin_*` helper
    subprocesses. Puts the package root on PYTHONPATH so the child can
    import platform_backend even when its cwd isn't the app directory."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root + (os.pathsep + existing if existing else "")
    return env


# Kept apart from the imports above so this module's input pieces can change
# without touching the GTK/layer-shell ones.
from .evdev_input import (
    BTN_LEFT,
    KEY_LEFTCTRL,
    KEY_LEFTSHIFT,
    KEY_RIGHTCTRL,
    KEY_RIGHTSHIFT,
    MODIFIER_KEYS,
    InputEvent,
    get_input_hub,
)
from .kde_hotkey import WaylandKdeHotkey, _qt_keycode  # noqa: F401 - re-exported
from .keyinject import KeyInjector
from .xkb import detect_kbd_rmlvo as _detect_kbd_rmlvo


def _layer_shell():
    """Import Gtk4LayerShell lazily so importing this module doesn't hard-fail
    when the typelib is missing (we want a clear error only when we actually
    try to show the popup). main.py preloads libgtk4-layer-shell before GTK
    so its Wayland hooks are in place; without the typelib main.py has already
    moved the process onto XWayland and this is never reached."""
    global _LAYER
    if _LAYER is None:
        import gi

        gi.require_version("Gtk4LayerShell", "1.0")
        from gi.repository import Gtk4LayerShell

        _LAYER = Gtk4LayerShell
    return _LAYER


def _die_with_parent() -> None:
    """preexec_fn: SIGTERM this child when the daemon dies, however it dies.
    Without it every restart left a `wl-paste --watch` behind, all of them
    pulling each new selection out of the source app."""
    import ctypes
    import signal

    PR_SET_PDEATHSIG = 1
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)


# ---------------------------------------------------------------------------
# Selection watcher: `wl-paste --primary --watch` notifies us; we then read
# the primary selection and the cursor position, mirroring the X11 watcher's
# event->read design.
# ---------------------------------------------------------------------------

# A selecting gesture this long before a PRIMARY burst still counts for it
# (Shift+arrow lands a few ms before the ping it causes).
_GESTURE_LEAD_S = 0.5
# Quiet time after the gesture ends before reading PRIMARY. Apps that set it
# on button release do so within a frame or two.
_AFTER_GESTURE_S = 0.05


class _SelectionGestures:
    """Remembers when the user last did something that selects text: a drag,
    a double/triple click, Shift+click, Shift+key or Ctrl+A. Apps also set
    PRIMARY on their own (a terminal re-claims it on focus), and a plain
    click to focus that window is not a selection.

    evdev reports buttons without coordinates, so a drag is a press held
    past _DRAG_S. With no readable /dev/input every change counts."""

    _DRAG_S = 0.15
    _MULTI_CLICK_S = 0.4
    _SHIFT = {KEY_LEFTSHIFT, KEY_RIGHTSHIFT}
    _CTRL = {KEY_LEFTCTRL, KEY_RIGHTCTRL}
    _KEY_A = 30

    def __init__(self, hub=None) -> None:
        self._hub = hub
        self._handle: int | None = None
        self._held: set[int] = set()
        self._press_t = 0.0
        self._release_t = 0.0
        self._multi = False
        self.button_down = False
        self.last = 0.0  # monotonic time of the last selecting gesture

    def start(self) -> None:
        if self._handle is not None:
            return
        if self._hub is None:
            self._hub = get_input_hub()
        self._handle = self._hub.subscribe(self._on_event)

    def stop(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None and self._hub is not None:
            self._hub.unsubscribe(handle)

    def settle_s(self, burst_t0: float, default: float) -> float:
        """How long the PRIMARY stream must stay quiet before the selection
        counts as done. The full debounce exists to wait out a drag; once
        the gesture behind this burst has ended (button up, key pressed),
        a short pause is enough."""
        if self.button_down or self.last < burst_t0 - _GESTURE_LEAD_S:
            return default
        return min(default, max(_AFTER_GESTURE_S, self.last + _AFTER_GESTURE_S - time.monotonic()))

    def selected_since(self, t: float) -> bool:
        if self._hub is None or not self._hub.devices():
            return True
        return self.last >= t

    def _mark(self) -> None:
        self.last = time.monotonic()

    def _on_event(self, ev: InputEvent) -> None:
        if ev.code == BTN_LEFT:
            if ev.value == 1:
                self._multi = ev.time - self._release_t < self._MULTI_CLICK_S
                self._press_t = ev.time
                self.button_down = True
            elif ev.value == 0:
                self.button_down = False
                if (
                    self._multi
                    or self._held & self._SHIFT
                    or ev.time - self._press_t >= self._DRAG_S
                ):
                    self._mark()
                self._release_t = ev.time
            return
        if ev.code in MODIFIER_KEYS:
            if ev.value == 1:
                self._held.add(ev.code)
            elif ev.value == 0:
                self._held.discard(ev.code)
            return
        if ev.value in (1, 2) and (
            self._held & self._SHIFT or (self._held & self._CTRL and ev.code == self._KEY_A)
        ):
            self._mark()


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
    _MAX_BACKOFF_S = 30.0

    def __init__(
        self,
        backend: WaylandKdeBackend,
        on_selection: Callable[[str, int, int], None],
        debounce_ms: int = 150,
        gestures: _SelectionGestures | None = None,
    ) -> None:
        self._backend = backend
        self._on_selection = on_selection
        self._debounce_s = max(0.0, debounce_ms / 1000.0)
        self._gestures = gestures or _SelectionGestures()
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
        if not self._gestures.selected_since(burst_t0 - _GESTURE_LEAD_S):
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
        """Run `args` and feed its stdout to `consume` until stop(). wl-paste
        exits when the compositor restarts; restart it with backoff rather
        than leave auto-popup dead while the setting says on."""
        delay = 1.0
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
                delay = 1.0
            print(f"[wayland] {' '.join(args)} exited; restarting in {delay:.0f}s")
            self._stop.wait(delay)
            delay = min(delay * 2, self._MAX_BACKOFF_S)

    def _ping(self, fd: int, timeout: float) -> bool | None:
        """True on a ping, False on timeout, None once the stream ends."""
        readable, _, _ = _select.select([fd], [], [], timeout)
        if not readable:
            return False
        try:
            return True if os.read(fd, 4096) else None
        except OSError:
            return None

    def _consume_clipboard(self, fd: int) -> None:
        first = True  # --watch pings once for what's already there
        while not self._stop.is_set():
            got = self._ping(fd, 0.5)
            if got is None:
                return
            if got and not first:
                self._clip_changed_at = time.monotonic()
            first = first and not got

    def _consume_primary(self, fd: int) -> None:
        # `--watch echo` pings (a bare newline) on every primary-selection
        # change. A single drag emits a BURST of these as the selection grows,
        # so we treat the pipe as an event stream and DEBOUNCE: only after the
        # pings go quiet for debounce_s do we read the selection + cursor and
        # fire once. Without this the popup re-reads the (still moving) cursor
        # on every ping and visibly chases the mouse for a second or two.
        first = True
        while not self._stop.is_set():
            # Block until at least one ping arrives (0.5 s wake-ups so stop()
            # is honoured promptly).
            got = self._ping(fd, 0.5)
            if got is None:
                return
            if not got:
                continue
            burst_t0 = time.monotonic()
            if not first:
                self._backend.prefetch_active_window()
            # Capture the cursor at the FIRST event of the burst, then re-
            # capture on every subsequent selection change. The last capture
            # therefore lands on the final change -- the drag release, right at
            # the end of the selected text. We deliberately do NOT query the
            # pointer again after the settle delay: by then the user has let go
            # and started moving the mouse toward the popup, and that drift was
            # exactly why the popup appeared away from the selection instead of
            # anchored over it. (On KWin Wayland there is no API for the text
            # selection's rectangle, so the release point is the best anchor we
            # have.)
            cap_x = cap_y = None
            try:
                cap_x, cap_y = self._backend.pointer_position()
            except Exception:  # noqa: BLE001
                pass
            # Drain the rest of the burst: keep consuming until the stream is
            # quiet for one debounce window.
            settle = max(0.2, self._debounce_s)
            ended = False
            while True:
                more = self._ping(fd, self._gestures.settle_s(burst_t0, settle))
                if not more:
                    ended = more is None
                    break
                try:
                    cap_x, cap_y = self._backend.pointer_position()
                except Exception:  # noqa: BLE001
                    pass
            if self._stop.is_set() or ended:
                return
            # The very first settled value is whatever was already selected
            # when wl-paste started - don't pop up for it.
            if first:
                first = False
                continue
            text = self._backend.read_selection("primary")
            if not text or not text.strip():
                continue
            if not self._should_show(text, burst_t0):
                continue
            if cap_x is None:
                try:
                    cap_x, cap_y = self._backend.pointer_position()
                except Exception as exc:  # noqa: BLE001
                    print(f"[wayland] pointer query failed: {exc}")
                    cap_x, cap_y = 0, 0
            try:
                self._on_selection(text, cap_x, cap_y)
            except Exception as exc:  # noqa: BLE001
                print(f"[wayland] selection callback error: {exc}")


# Global hotkey: KGlobalAccel, see kde_hotkey.py.
# Keyboard layout detection: xkb.detect_kbd_rmlvo.


class _XkbModifierState:
    """libxkbcommon ctypes wrapper that tracks LOGICAL modifier state from
    raw evdev keycodes, honouring the user's layout + options. This is what
    lets a Ctrl/Alt swap (kxkbrc Options=ctrl:swap_lalt_lctl) be respected:
    we ask xkb 'is Control effectively active?' instead of hard-coding which
    physical keycode is Ctrl."""

    _MOD_NAME = {
        "ctrl": b"Control",
        "shift": b"Shift",
        "alt": b"Mod1",
        "super": b"Mod4",
    }
    _XKB_STATE_MODS_EFFECTIVE = 1 << 3

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
                (model or "pc105").encode(),
                (layout or "us").encode(),
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
            self._xkb.xkb_state_update_key(self._state, evdev_code + 8, 1 if down else 0)

    def is_active(self, mod: str):
        """True/False if the named modifier is effectively held, or None when
        xkb is unavailable (caller falls back to raw keycodes)."""
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
    """evdev-based modifier+double-click watcher for native Wayland.

    The X11 path (XRecord) only sees XWayland windows on KWin, so the
    double-click-modifier feature was dead for native Wayland apps. This
    reads mouse buttons AND modifier keys straight from /dev/input - the
    same mechanism the snippet-trigger and outside-click watchers already
    use, so it works for every toolkit. Needs the user in the 'input'
    group; silently inert otherwise.

    Devices come from the shared evdev hub (evdev_input.py): every keyboard
    and pointer by capability, keyd's virtual keyboard and hotplugged ones
    included. Events arrive on the hub's thread.

    Mirrors mouse_watcher.DoubleClickWatcher's semantics (300 ms window,
    8 px tolerance, gated on the configured modifier). evdev button events
    carry no coordinates, so the cursor position comes from the KWin
    pointer query - fetched only while the gating modifier is held, so the
    DBus round-trip happens for our gesture, not on every click.
    """

    _MOD_CODES = {
        "ctrl": {29, 97},  # KEY_LEFTCTRL / KEY_RIGHTCTRL
        "shift": {42, 54},  # KEY_LEFTSHIFT / KEY_RIGHTSHIFT
        "alt": {56, 100},  # KEY_LEFTALT / KEY_RIGHTALT
        "super": {125, 126},  # KEY_LEFTMETA / KEY_RIGHTMETA
    }
    _ALL_MODS = {29, 97, 42, 54, 56, 100, 125, 126}
    _BTN_LEFT = 0x110
    _DOUBLE_CLICK_MS = 300
    _POSITION_TOLERANCE_PX = 8

    def __init__(self, backend, on_double_click, hub=None) -> None:
        self._backend = backend
        self._cb = on_double_click
        self._hub = hub
        self._handle: int | None = None
        self._held: set[int] = set()  # raw modifier keycodes down (xkb fallback)
        self._xkb = None  # layout-aware modifier state (built on first event)
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
        # Unsubscribing the last consumer joins the hub's reader, so the
        # /dev/input fds are closed before a fresh watcher is started on the
        # next settings toggle.
        handle, self._handle = self._handle, None
        if handle is not None and self._hub is not None:
            self._hub.unsubscribe(handle)

    def _required_mod_name(self) -> str:
        """The configured modifier name, read fresh each click so changing
        the setting takes effect without a restart."""
        try:
            from settings import get_settings

            name = (get_settings().get("double_click_modifier") or "ctrl").lower()
        except Exception:
            name = "ctrl"
        return name if name in self._MOD_CODES else "ctrl"

    def _required_codes(self) -> set:
        """Raw-keycode fallback set, used only when xkb is unavailable."""
        return self._MOD_CODES.get(self._required_mod_name(), self._MOD_CODES["ctrl"])

    def _on_event(self, ev: InputEvent) -> None:
        if self._xkb is None:
            # Layout-aware modifier state so a Ctrl/Alt swap is respected. If
            # libxkbcommon is missing we fall back to raw keycodes.
            self._xkb = _XkbModifierState()
        if ev.code == self._BTN_LEFT:
            if ev.value == 1:
                self._on_left_press(ev.time)
            return
        # Keyboard key: feed xkb (press=1/release=0; ignore autorepeat=2)
        # and keep the raw-code fallback set.
        if ev.value in (0, 1):
            self._xkb.update(ev.code, ev.value == 1)
        if ev.code in self._ALL_MODS:
            if ev.value == 1:
                self._held.add(ev.code)
            elif ev.value == 0:
                self._held.discard(ev.code)

    def _on_left_press(self, t: float | None = None) -> None:
        # Only act on our gesture: the configured modifier must be held.
        # This also keeps the KWin pointer query off the plain-click path.
        # `t` is the kernel timestamp, so the ~100 ms pointer query below
        # doesn't eat into the double-click window.
        name = self._required_mod_name()
        active = self._xkb.is_active(name) if self._xkb else None
        if active is None:
            # xkb unavailable - fall back to raw physical keycodes (ignores
            # any Ctrl/Alt swap, but better than nothing).
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
            # Tiny settle so the app sees the click first, then we show
            # the menu (same as the X11 watcher).
            GLib.timeout_add(20, self._fire, x, y)
            return
        self._last_t = now
        self._last_xy = (x, y)

    def _fire(self, x: int, y: int) -> bool:
        try:
            self._cb(x, y)
        except Exception as exc:  # noqa: BLE001
            print(f"[dblclick] callback failed: {exc}")
        return False  # one-shot


class _KWinHelper:
    """Client for the long-lived _kwin_query process. A one-shot helper per
    query cost ~61 ms, ~55 of it Python startup. Respawned after a timeout
    or crash."""

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
                [sys.executable, "-m", "platform_backend._kwin_query"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                newline="\n",  # only "\n" ends an answer, not "\r" in a title
                bufsize=1,
                env=_kwin_subprocess_env(),
                preexec_fn=_die_with_parent,
            )
        except OSError as exc:
            print(f"[wayland] could not start the KWin helper: {exc}")
            self._proc = None
        return self._proc

    def query(self, kind: str, timeout: float = 3.0) -> str:
        """The helper's answer line, or "" on any failure."""
        with self._lock:
            proc = self._ensure()
            if proc is None or proc.stdin is None or proc.stdout is None:
                return ""
            line = ""
            try:
                proc.stdin.write(kind + "\n")
                proc.stdin.flush()
                readable, _, _ = _select.select([proc.stdout], [], [], timeout)
                if readable:
                    line = proc.stdout.readline()
            except (OSError, ValueError):
                pass
            if not line.endswith("\n"):
                # Timed out or died: a late answer would be read as the reply
                # to the next query, so start over with a fresh process.
                print(f"[wayland] KWin helper gave no answer to {kind!r}; restarting it")
                self._proc = None
                threading.Thread(target=_reap, args=(proc,), daemon=True).start()
                return ""
            return line.rstrip("\n")


class WaylandKdeBackend(PlatformBackend):
    name = "wayland_kde"
    popup_uses_xlib = False
    # KWin's workspace.cursorPos is in logical/compositor pixels, the same
    # space as GTK's monitor geometry - so the popup must NOT divide by scale.
    pointer_is_logical = True

    def __init__(self) -> None:
        self._pointer_lock = threading.Lock()
        # (monotonic time the query finished, coords, succeeded)
        self._pointer_last: tuple[float, tuple[int, int], bool] | None = None
        self._kwin = _KWinHelper()
        self._active_lock = threading.Lock()
        self._active_pending: tuple[float, threading.Event, list[list[str]]] | None = None
        # KGlobalAccel signal delivery needs dbus-python bound to the GLib
        # main loop. Set it as the default before any SessionBus is created
        # so the hotkey's add_signal_receiver works.
        try:
            from dbus.mainloop.glib import DBusGMainLoop

            DBusGMainLoop(set_as_default=True)
        except Exception:
            pass

    # ---- session ---------------------------------------------------------
    def check_session(self) -> None:
        if not os.environ.get("WAYLAND_DISPLAY"):
            print("[wayland] WAYLAND_DISPLAY not set - wrong backend.", file=sys.stderr)
            sys.exit(2)
        self._kwin.start()  # pay its Python startup now, not on the first popup
        desktop = (os.environ.get("XDG_CURRENT_DESKTOP") or "").upper()
        if "KDE" not in desktop:
            print(
                f"[wayland] WARNING: XDG_CURRENT_DESKTOP={desktop!r} is not "
                "KDE. Cursor positioning relies on KWin; popup placement may "
                "not work on this compositor.",
                file=sys.stderr,
            )

    # ---- selection / clipboard ------------------------------------------
    def read_selection(self, source: str) -> str:
        # --type text: an image-only clipboard would otherwise hand back PNG bytes.
        args = ["wl-paste", "--no-newline", "--type", "text"]
        if source.lower() == "primary":
            args.append("--primary")
        try:
            out = subprocess.run(args, capture_output=True, timeout=2.0)
            if out.returncode != 0:
                return ""  # empty selection -> wl-paste exits non-zero
            return out.stdout.decode("utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError):
            return ""

    def set_clipboard(self, text: str) -> None:
        if not shutil.which("wl-copy"):
            print("[wayland] wl-copy missing - cannot set clipboard")
            return
        try:
            subprocess.run(["wl-copy"], input=text.encode("utf-8"), check=False, timeout=2.0)
        except subprocess.SubprocessError as exc:
            print(f"[wayland] wl-copy failed: {exc}")

    # ---- pointer ---------------------------------------------------------
    # Callers that arrive while a query is in flight share its result, and a
    # successful result is reused for this long. The selection watcher, the
    # double-click thread and the main thread (hotkey) often ask at the same
    # moment; one KWin round-trip serves them all.
    _POINTER_CACHE_S = 0.1

    def pointer_position(self) -> tuple[int, int]:
        arrived = time.monotonic()
        with self._pointer_lock:
            last = self._pointer_last
            if last is not None:
                done_at, xy, ok = last
                # done_at >= arrived: that query finished while we waited on
                # the lock, so it's as fresh as one we'd start now.
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
        # No coords: the KWin round-trip timed out or KWin was busy. This is
        # the silent path that lands the popup at the top-left corner.
        print(f"[wayland] cursor query gave no coords ({out!r}) -> falling back to (0,0)")
        return (0, 0), False

    def can_paste(self) -> bool:
        # Portal, ydotool or wtype, whichever is set up (keyinject.py).
        return self.injector().can_inject()

    # ---- keystroke injection --------------------------------------------
    #
    # See keyinject.py: xdg-desktop-portal RemoteDesktop first (keysyms, so
    # layout-correct, no daemon), then ydotool (keycodes mapped through the
    # user's xkb layout), then wtype off KDE only (KWin has no
    # zwp_virtual_keyboard_v1). Delivery is queued on the injector's thread.

    def injector(self) -> KeyInjector:
        inj = getattr(self, "_injector", None)
        if inj is None:
            inj = self._injector = KeyInjector()
        return inj

    def send_key(self, combo: str) -> None:
        self.injector().send_key(combo)

    def type_text(self, text: str) -> None:
        self.injector().type_text(text)

    # ---- active window ---------------------------------------------------
    def active_window_haystacks(self) -> list[str]:
        with self._active_lock:
            pending, self._active_pending = self._active_pending, None
        if pending is not None and time.monotonic() - pending[0] < self._ACTIVE_PREFETCH_S:
            pending[1].wait(3.0)
            if pending[2]:
                return pending[2][0]
        return self._query_active()

    # A prefetch this old is a different moment from the caller's; ask again.
    _ACTIVE_PREFETCH_S = 2.0

    def prefetch_active_window(self) -> None:
        """Start the active-window lookup now, for the next
        active_window_haystacks() call to pick up. The selection watcher
        calls this as a selection starts, so the lookup overlaps the settle
        wait instead of following it."""
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

    # ---- opening URLs ----------------------------------------------------
    def open_url(self, url: str) -> None:
        try:
            subprocess.Popen(["xdg-open", url], start_new_session=True)
        except FileNotFoundError:
            print("[wayland] xdg-open not available")
            return
        # Wayland focus-stealing prevention leaves the browser in the
        # background when launched from our (unfocused) popup. KWin scripts are
        # privileged and can force-raise it - do that shortly after.
        threading.Thread(target=self._raise_browser, daemon=True).start()

    def _browser_keyword(self) -> str:
        kw = getattr(self, "_browser_kw", None)
        if kw is not None:
            return kw
        kw = "firefox"
        try:
            out = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            ident = out.stdout.strip().lower()
            if ident.endswith(".desktop"):
                ident = ident[: -len(".desktop")]
            if ident:
                # org.mozilla.firefox -> firefox; google-chrome -> google-chrome
                kw = ident.split(".")[-1] if "." in ident else ident
        except (OSError, subprocess.SubprocessError):
            pass
        self._browser_kw = kw
        return kw

    def _raise_browser(self) -> None:
        time.sleep(0.5)  # let the tab register before we raise the window
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
            time.sleep(0.3)
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

    # ---- component factories --------------------------------------------
    def make_selection_watcher(self, on_selection, debounce_ms):
        return WaylandSelectionWatcher(self, on_selection, debounce_ms=debounce_ms)

    def make_hotkey(self, hotkey_str, on_trigger, use_polling=False, role=None):
        return WaylandKdeHotkey(hotkey_str, on_trigger, role=role)

    def can_bind_hotkey(self, hotkey_str):
        return _qt_keycode(hotkey_str) is not None

    def make_double_click_watcher(self, on_double_click):
        # XRecord (the old path) only sees XWayland windows on KWin, so the
        # gesture was dead for native Wayland apps. Read mouse buttons +
        # modifier keys from /dev/input instead - kernel-level, so it covers
        # XWayland AND native Wayland windows. Needs the user in 'input';
        # the watcher logs and stays inert otherwise.
        return WaylandDoubleClickWatcher(self, on_double_click)

    # ---- popup positioning ----------------------------------------------
    def prepare_popup_window(self, win) -> None:
        # No gtk4-layer-shell (main.py then puts GDK on XWayland): place the
        # popup the X11 way. Selection / pointer / hotkey stay on the native
        # KWin paths either way.
        from .x11 import gdk_display_is_x11, x11_prepare_popup_window

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
        # KeyboardMode.NONE: the popup must NOT take keyboard focus. With
        # ON_DEMAND the layer surface grabs focus on map then immediately gets
        # a focus-out, and PopupWindow's focus-loss handler hides it before it's
        # even visible. NONE keeps it on-screen; dismissal is via the
        # leave/initial grace timers (Esc-to-dismiss is a documented Wayland
        # gap for now).
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
        # (x, y) is global logical; layer-shell margins are relative to the
        # surface's own output. Pin the surface to the output holding the
        # point and subtract its origin, or a popup on a monitor at x=1920
        # lands 1920 px too far right. A point on no output (the off-screen
        # parking spot) leaves the output alone: it just needs to be hidden.
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
