"""Key injection on Wayland: Paste / Cut / Select all / Backspace.

Backends, tried in this order until one reports success:

1. xdg-desktop-portal RemoteDesktop, NotifyKeyboardKeysym. No root, no
   daemon, no group. It sends keysyms, and the compositor (KWin:
   FakeInputBackend keyboard_keysym) finds the key for each one in the
   user's live keymap, so Ctrl+A is Ctrl+A on AZERTY and Dvorak too. The
   first use shows KDE's "Remote control requested" dialog; we ask for
   persist_mode=2, so ticking "Allow restoring" there makes later sessions
   start without asking (restore token saved under ~/.local/state/vinopop).
2. ydotool + ydotoold: writes evdev keycodes through /dev/uinput. Needs the
   daemon running and its socket reachable. Keycodes are physical, so we map
   keysyms to keycodes through the user's xkb layout first (see xkb.Keymap).
3. wtype: zwp_virtual_keyboard_v1. KWin doesn't implement that protocol
   (wtype exits with "Compositor does not support the virtual keyboard
   protocol"), so it's only tried off KDE (Sway, Hyprland, ...).

When all of them fail, one desktop notification per session says what to
enable. Nothing here imports GTK at module level; the portal client uses Gio
on the injector's own thread.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import xkb

APP_ID = "io.github.karanshukla.vinoPop"
YDOTOOL_TIMEOUT_S = 2.0

# US QWERTY evdev codes (linux/input-event-codes.h), for when the layout is
# us/unknown or libxkbcommon is missing.
QWERTY_KEYCODES = {
    **{d: 2 + i for i, d in enumerate("1234567890")},  # KEY_1=2 .. KEY_0=11
    "a": 30,
    "b": 48,
    "c": 46,
    "d": 32,
    "e": 18,
    "f": 33,
    "g": 34,
    "h": 35,
    "i": 23,
    "j": 36,
    "k": 37,
    "l": 38,
    "m": 50,
    "n": 49,
    "o": 24,
    "p": 25,
    "q": 16,
    "r": 19,
    "s": 31,
    "t": 20,
    "u": 22,
    "v": 47,
    "w": 17,
    "x": 45,
    "y": 21,
    "z": 44,
    "return": 28,
    "enter": 28,
    "kp_enter": 96,
    "backspace": 14,
    "tab": 15,
    "escape": 1,
    "esc": 1,
    "space": 57,
    "delete": 111,
    "minus": 12,
    "equal": 13,
    "home": 102,
    "end": 107,
    "prior": 104,
    "next": 109,
    "left": 105,
    "right": 106,
    "up": 103,
    "down": 108,
}
QWERTY_MODIFIER_KEYCODES = {
    "ctrl": 29,
    "control": 29,
    "shift": 42,
    "alt": 56,
    "super": 125,
    "win": 125,
    "meta": 125,
}


@dataclass(frozen=True)
class Chord:
    modifiers: tuple[str, ...]  # lower-cased xdotool names: ctrl, shift, ...
    key: str  # xdotool key name as given: 'v', 'Return', 'BackSpace'


def parse_chord(combo: str) -> Chord | None:
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    if not parts:
        return None
    mods = tuple(m.lower() for m in parts[:-1])
    if any(m not in xkb.MODIFIER_KEYSYMS for m in mods):
        print(f"[keys] unknown modifier in {combo!r}")
        return None
    return Chord(mods, parts[-1])


def chord_keysyms(chord: Chord) -> list[tuple[int, int]] | None:
    """(keysym, pressed) sequence for the portal: modifiers down, key down/up,
    modifiers up in reverse."""
    key = xkb.keysym_from_name(chord.key)
    if not key:
        print(f"[keys] unknown key {chord.key!r}")
        return None
    mods = [xkb.MODIFIER_KEYSYMS[m] for m in chord.modifiers]
    seq = [(m, 1) for m in mods] + [(key, 1), (key, 0)]
    return seq + [(m, 0) for m in reversed(mods)]


# ---------------------------------------------------------------------------
# ydotool
# ---------------------------------------------------------------------------
def ydotool_socket() -> str | None:
    """The socket ydotool will talk to, if it exists. Same lookup order as
    the ydotool client: $YDOTOOL_SOCKET, $XDG_RUNTIME_DIR/.ydotool_socket,
    /tmp/.ydotool_socket."""
    env = os.environ.get("YDOTOOL_SOCKET")
    if env:
        return env if os.path.exists(env) else None
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    for path in (os.path.join(runtime, ".ydotool_socket"), "/tmp/.ydotool_socket"):  # nosec B108 - ydotoold's default path, only checked for existence
        if os.path.exists(path):
            return path
    return None


def ydotool_usable() -> bool:
    return bool(shutil.which("ydotool")) and ydotool_socket() is not None


def _ydotool_env() -> dict[str, str]:
    env = os.environ.copy()
    sock = ydotool_socket()
    if sock and not env.get("YDOTOOL_SOCKET"):
        env["YDOTOOL_SOCKET"] = sock
    return env


def _ydotool_run(sub_argv: list[str], runner: Callable = subprocess.run) -> bool:
    """Run `ydotool <sub_argv>`. True only if it exited 0 within the timeout:
    with ydotoold down the client prints "failed to connect socket" and
    exits 2, which used to count as success."""
    try:
        out = runner(
            ["ydotool", *sub_argv],
            env=_ydotool_env(),
            capture_output=True,
            text=True,
            timeout=YDOTOOL_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"[keys] ydotool timed out after {YDOTOOL_TIMEOUT_S:.0f}s (ydotoold stuck?)")
        return False
    except OSError as exc:
        print(f"[keys] ydotool failed: {exc}")
        return False
    if out.returncode != 0:
        detail = (out.stdout or out.stderr or "").strip().splitlines()
        print(f"[keys] ydotool exited {out.returncode}: {detail[-1] if detail else ''}")
        return False
    return True


def ydotool_key_events(
    chord: Chord, rmlvo: tuple[str, str, str, str] | None = None
) -> list[str] | None:
    """CODE:STATE tokens for `ydotool key`, with keycodes taken from the
    user's layout so the compositor turns them back into the intended
    keysyms. `rmlvo` defaults to the detected layout."""
    if rmlvo is None:
        rmlvo = xkb.detect_kbd_rmlvo()
    keymap = None
    if not (rmlvo[1] in ("", "us") and not rmlvo[2] and not rmlvo[3]):
        keymap = xkb.keymap_for(rmlvo)

    def code_for(keysym: int, fallback: int | None) -> tuple[int, bool] | None:
        if keymap is not None and keysym:
            hit = keymap.keycode_for(keysym)
            if hit is not None:
                return hit
        return (fallback, False) if fallback is not None else None

    def mod_code(name: str) -> int:
        hit = code_for(xkb.MODIFIER_KEYSYMS[name], None)
        return hit[0] if hit is not None else QWERTY_MODIFIER_KEYCODES[name]

    mods = [mod_code(m) for m in chord.modifiers]
    keysym = xkb.keysym_from_name(chord.key)
    fallback = QWERTY_KEYCODES.get(chord.key) or QWERTY_KEYCODES.get(chord.key.lower())
    hit = code_for(keysym, fallback)
    if hit is None:
        print(f"[keys] ydotool: unknown key {chord.key!r}")
        return None
    key_code, needs_shift = hit
    if needs_shift and mod_code("shift") not in mods:
        mods.append(mod_code("shift"))
    events = [f"{c}:1" for c in mods]
    events += [f"{key_code}:1", f"{key_code}:0"]
    events += [f"{c}:0" for c in reversed(mods)]
    return events


# ---------------------------------------------------------------------------
# wtype (wlroots only)
# ---------------------------------------------------------------------------
def on_kwin() -> bool:
    return "KDE" in (os.environ.get("XDG_CURRENT_DESKTOP") or "").upper()


def wtype_usable() -> bool:
    return not on_kwin() and bool(shutil.which("wtype"))


def wtype_args(chord: Chord) -> list[str]:
    mod_map = {
        "ctrl": "ctrl",
        "control": "ctrl",
        "alt": "alt",
        "shift": "shift",
        "super": "logo",
        "win": "logo",
        "meta": "logo",
    }
    mods = [mod_map[m] for m in chord.modifiers]
    args: list[str] = []
    for m in mods:
        args += ["-M", m]
    args += ["-k", chord.key]
    for m in reversed(mods):
        args += ["-m", m]
    return args


def _wtype_run(args: list[str]) -> bool:
    try:
        out = subprocess.run(
            ["wtype", *args], capture_output=True, text=True, timeout=2.0, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[keys] wtype failed: {exc}")
        return False
    if out.returncode != 0:
        print(f"[keys] wtype exited {out.returncode}: {out.stderr.strip()}")
        return False
    return True


# ---------------------------------------------------------------------------
# xdg-desktop-portal RemoteDesktop
# ---------------------------------------------------------------------------
_PORTAL_BUS = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_RD_IFACE = "org.freedesktop.portal.RemoteDesktop"
_DEVICE_KEYBOARD = 1
_PERSIST_UNTIL_REVOKED = 2


def _token_path() -> str:
    try:
        from core.xdg_paths import STATE_DIR

        base = str(STATE_DIR)
    except Exception:  # noqa: BLE001 - xdg_paths is optional outside the app
        base = os.path.join(os.path.expanduser("~/.local/state"), "vinopop")
    return os.path.join(base, "remote-desktop.token")


def portal_keyboard_available() -> bool | None:
    """True if xdg-desktop-portal offers RemoteDesktop with a keyboard,
    False if it doesn't, None if we couldn't ask (no Gio / no session bus)."""
    try:
        from gi.repository import Gio, GLib
    except Exception:  # noqa: BLE001
        return None
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            _PORTAL_BUS,
            _PORTAL_PATH,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (_RD_IFACE, "AvailableDeviceTypes")),
            GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE,
            1500,
            None,
        )
    except Exception as exc:  # noqa: BLE001 - GLib.Error: no portal / no RemoteDesktop
        if "UnknownInterface" in str(exc) or "No such interface" in str(exc):
            return False
        if "ServiceUnknown" in str(exc):
            return False
        return None
    return bool(int(reply.unpack()[0]) & _DEVICE_KEYBOARD)


class PortalDenied(Exception):
    """The user declined the Remote Control request."""


class PortalKeyboard:
    """A RemoteDesktop portal session with KEYBOARD access.

    Must be used from a single thread (the injector's): it owns a private
    D-Bus connection whose signals are dispatched on that thread's
    GLib.MainContext. The session is opened on first use and closed after
    IDLE_CLOSE_S without keys, because KDE shows a "Remote Control" tray
    item for as long as it is open."""

    IDLE_CLOSE_S = 300.0
    START_TIMEOUT_S = 120.0  # the user is reading a dialog
    CALL_TIMEOUT_S = 5.0

    def __init__(self, token_path: str | None = None) -> None:
        self._token_path = token_path or _token_path()
        self._conn = None
        self._ctx = None
        self._session: str | None = None
        self._closed_sub = None
        self._last_use = 0.0
        self._counter = 0
        self.denied = False

    # ---- plumbing ------------------------------------------------------
    def _connect(self):
        from gi.repository import Gio, GLib

        if self._conn is not None:
            return self._conn
        self._ctx = GLib.MainContext.new()
        self._ctx.push_thread_default()  # this thread is ours for good
        addr = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
        self._conn = Gio.DBusConnection.new_for_address_sync(
            addr,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )
        # Name ourselves to the portal (xdg-desktop-portal >= 1.19), so the
        # dialog says "vinoPop" and the stored permission is keyed to us.
        # Must precede any other portal call on this connection.
        try:
            self._conn.call_sync(
                _PORTAL_BUS,
                _PORTAL_PATH,
                "org.freedesktop.host.portal.Registry",
                "Register",
                GLib.Variant("(sa{sv})", (APP_ID, {})),
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
        except Exception as exc:  # noqa: BLE001 - older portal, or already registered
            print(f"[keys] portal app-id registration skipped: {exc}")
        return self._conn

    def _pump(self) -> None:
        while self._ctx is not None and self._ctx.pending():
            self._ctx.iteration(False)

    def _request(self, method: str, args, timeout: float) -> dict:
        """Call a portal method that answers through a Request object and
        wait for its Response. Returns the results dict."""
        from gi.repository import Gio, GLib

        conn = self._connect()
        self._counter += 1
        token = f"vinopop{os.getpid()}_{self._counter}"
        sender = conn.get_unique_name()[1:].replace(".", "_")
        path = f"{_PORTAL_PATH}/request/{sender}/{token}"
        got: dict = {}

        def on_response(_c, _s, _p, _i, _n, params, *_):
            got["code"], got["results"] = params.unpack()

        sub = conn.signal_subscribe(
            _PORTAL_BUS,
            "org.freedesktop.portal.Request",
            "Response",
            path,
            None,
            Gio.DBusSignalFlags.NONE,
            on_response,
        )
        try:
            params = args(token)
            conn.call_sync(
                _PORTAL_BUS,
                _PORTAL_PATH,
                _RD_IFACE,
                method,
                params,
                GLib.VariantType("(o)"),
                Gio.DBusCallFlags.NONE,
                int(self.CALL_TIMEOUT_S * 1000),
                None,
            )
            deadline = time.monotonic() + timeout
            while "code" not in got and time.monotonic() < deadline:
                self._pump()
                if "code" not in got:
                    time.sleep(0.02)
        finally:
            conn.signal_unsubscribe(sub)
        if "code" not in got:
            raise TimeoutError(f"{method}: no answer in {timeout:.0f}s")
        if got["code"] == 1:
            raise PortalDenied(method)
        if got["code"] != 0:
            raise RuntimeError(f"{method} failed (response {got['code']})")
        return got["results"]

    def _load_token(self) -> str:
        try:
            with open(self._token_path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _save_token(self, token: str) -> None:
        try:
            os.makedirs(os.path.dirname(self._token_path), exist_ok=True)
            fd = os.open(self._token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(token)
        except OSError as exc:
            print(f"[keys] could not save the portal restore token: {exc}")

    # ---- session -------------------------------------------------------
    def _open(self) -> str:
        from gi.repository import Gio, GLib

        res = self._request(
            "CreateSession",
            lambda tok: GLib.Variant(
                "(a{sv})",
                (
                    {
                        "handle_token": GLib.Variant("s", tok),
                        "session_handle_token": GLib.Variant("s", tok),
                    },
                ),
            ),
            self.CALL_TIMEOUT_S,
        )
        session = str(res["session_handle"])
        opts = {
            "types": GLib.Variant("u", _DEVICE_KEYBOARD),
            "persist_mode": GLib.Variant("u", _PERSIST_UNTIL_REVOKED),
        }
        restore = self._load_token()
        if restore:
            opts["restore_token"] = GLib.Variant("s", restore)
        self._request(
            "SelectDevices",
            lambda tok: GLib.Variant(
                "(oa{sv})", (session, {**opts, "handle_token": GLib.Variant("s", tok)})
            ),
            self.CALL_TIMEOUT_S,
        )
        try:
            res = self._request(
                "Start",
                lambda tok: GLib.Variant(
                    "(osa{sv})", (session, "", {"handle_token": GLib.Variant("s", tok)})
                ),
                self.START_TIMEOUT_S,
            )
        except Exception:
            self._close_handle(session)
            raise
        if not int(res.get("devices", 0)) & _DEVICE_KEYBOARD:
            self._close_handle(session)
            raise PortalDenied("keyboard not granted")
        if res.get("restore_token"):
            self._save_token(str(res["restore_token"]))  # single use: keep the new one

        def on_closed(*_):
            print("[keys] portal session closed by the desktop")
            self._session = None

        self._closed_sub = self._conn.signal_subscribe(
            _PORTAL_BUS,
            "org.freedesktop.portal.Session",
            "Closed",
            session,
            None,
            Gio.DBusSignalFlags.NONE,
            on_closed,
        )
        print("[keys] portal RemoteDesktop session started")
        return session

    def _close_handle(self, session: str) -> None:
        from gi.repository import Gio

        try:
            self._conn.call_sync(
                _PORTAL_BUS,
                session,
                "org.freedesktop.portal.Session",
                "Close",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
        except Exception:  # noqa: BLE001 - already gone
            pass

    def close(self) -> None:
        if self._session is not None:
            if self._closed_sub is not None:
                self._conn.signal_unsubscribe(self._closed_sub)
                self._closed_sub = None
            self._close_handle(self._session)
            self._session = None

    def close_if_idle(self) -> None:
        if self._session is not None and time.monotonic() - self._last_use > self.IDLE_CLOSE_S:
            self.close()

    def send(self, seq: list[tuple[int, int]]) -> bool:
        """Press/release keysyms. False (never raises) when the portal can't."""
        from gi.repository import Gio, GLib

        if self.denied:
            return False
        try:
            self._pump()  # deliver a pending Session.Closed
            if self._session is None:
                self._session = self._open()
            for keysym, pressed in seq:
                self._conn.call_sync(
                    _PORTAL_BUS,
                    _PORTAL_PATH,
                    _RD_IFACE,
                    "NotifyKeyboardKeysym",
                    GLib.Variant("(oa{sv}iu)", (self._session, {}, keysym, pressed)),
                    None,
                    Gio.DBusCallFlags.NONE,
                    2000,
                    None,
                )
        except PortalDenied as exc:
            print(f"[keys] Remote Control declined ({exc}); not asking again this session")
            self.denied = True
            return False
        except Exception as exc:  # noqa: BLE001 - GLib.Error, timeout, no portal
            print(f"[keys] portal injection failed: {exc}")
            self._session = None
            return False
        self._last_use = time.monotonic()
        return True


# ---------------------------------------------------------------------------
# The injector: one worker thread, backends in order, one notice on failure
# ---------------------------------------------------------------------------
class KeyInjector:
    """Queue key chords and text for the focused window.

    send_key() / type_text() return at once; a worker thread delivers jobs
    in order (so Paste, then Return, still arrive in that order) and never
    blocks the GTK main loop, which matters while the portal's permission
    dialog is up."""

    def __init__(
        self,
        notify: Callable[[str], None] | None = None,
        portal: PortalKeyboard | None = None,
        portal_available: Callable[[], bool | None] = portal_keyboard_available,
    ) -> None:
        self._notify = notify or _notify_once
        self._portal = portal
        self._portal_available = portal_available
        self._portal_ok: bool | None = None
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ---- public --------------------------------------------------------
    def send_key(self, combo: str) -> None:
        self._submit(("key", combo))

    def type_text(self, text: str) -> None:
        self._submit(("text", text))

    def can_inject(self) -> bool:
        """Whether some backend is plausibly working right now."""
        if self._use_portal():
            return True
        return ydotool_usable() or wtype_usable()

    def backend_names(self) -> list[str]:
        names = []
        if self._use_portal():
            names.append("portal")
        if ydotool_usable():
            names.append("ydotool")
        if wtype_usable():
            names.append("wtype")
        return names

    # ---- worker --------------------------------------------------------
    def _submit(self, job: tuple[str, str]) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True, name="vinopop-keys")
                self._thread.start()
        self._jobs.put(job)

    def _run(self) -> None:
        while True:
            try:
                job = self._jobs.get(timeout=30.0)
            except queue.Empty:
                if self._portal is not None:
                    self._portal.close_if_idle()
                continue
            try:
                self.deliver(*job)
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                print(f"[keys] {job[0]} injection crashed: {exc}")

    def _use_portal(self) -> bool:
        if self._portal is not None and self._portal.denied:
            return False
        if self._portal_ok is None:
            self._portal_ok = bool(self._portal_available())
        return self._portal_ok

    def _get_portal(self) -> PortalKeyboard:
        if self._portal is None:
            self._portal = PortalKeyboard()
        return self._portal

    # ---- delivery (synchronous; the worker calls these) ----------------
    def deliver(self, kind: str, payload: str) -> bool:
        """Try each backend in order. True if one of them took it."""
        if kind == "key":
            ok = self._deliver_key(payload)
        else:
            ok = self._deliver_text(payload)
        if not ok:
            self._report_failure(payload if kind == "key" else "text")
        return ok

    def _deliver_key(self, combo: str) -> bool:
        chord = parse_chord(combo)
        if chord is None:
            return False
        if self._use_portal():
            seq = chord_keysyms(chord)
            if seq is not None and self._get_portal().send(seq):
                return True
        if shutil.which("ydotool"):
            events = ydotool_key_events(chord)
            if events is not None and _ydotool_run(["key", *events]):
                return True
        return wtype_usable() and _wtype_run(wtype_args(chord))

    def _deliver_text(self, text: str) -> bool:
        if self._use_portal():
            seq: list[tuple[int, int]] = []
            for ch in text:
                ks = xkb.keysym_for_char(ch)
                seq += [(ks, 1), (ks, 0)]
            if self._get_portal().send(seq):
                return True
        # ydotool types through its own US table: fine for ASCII on QWERTY.
        # `--` stops option parsing so text starting with '-' is typed.
        if shutil.which("ydotool") and _ydotool_run(["type", "--", text]):
            return True
        return wtype_usable() and _wtype_run([text])

    def _report_failure(self, what: str) -> None:
        print(f"[keys] no key-injection backend worked for {what!r}")
        if self._portal is not None and self._portal.denied:
            hint = (
                "You declined vinoPop's Remote Control request, so Paste/Cut can't press "
                "keys. Restart vinoPop to be asked again, or install ydotool and start ydotoold."
            )
        elif on_kwin():
            hint = (
                "Paste/Cut need key injection. Allow vinoPop when KDE asks for Remote "
                "Control (needs xdg-desktop-portal-kde), or install ydotool and start "
                "ydotoold. See docs/FEDORA-KDE.md."
            )
        else:
            hint = (
                "Paste/Cut need key injection: xdg-desktop-portal with RemoteDesktop, "
                "ydotool + ydotoold, or wtype."
            )
        self._notify(hint)


def _notify_once(message: str) -> None:
    from .base import _warn_missing_injector

    # Keyed by the message: "declined" still shows after a "not set up" notice.
    _warn_missing_injector(f"wayland_keys:{message}", message)
