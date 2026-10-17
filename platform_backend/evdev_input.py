"""Raw /dev/input reading, shared by everything that needs physical input on
Wayland (the modifier+double-click watcher today; the selection watcher's
"was this a real user gesture" check next).

Which devices: every /dev/input/event* whose capability bits say it is a
keyboard (EV_KEY with a modifier key) or a pointer (BTN_LEFT). Not the
by-path links: keyd's virtual keyboard, Bluetooth and other hotplugged
devices have none, and with keyd grabbing the physical keyboard its virtual
device is the only place modifier presses show up. Excluded by name only:
ydotoold's virtual device (our own injected keys would otherwise look like
the user's) and anything vinoPop itself creates.

Hotplug: /dev/input is rescanned every RESCAN_S seconds, which also picks up
a node whose permissions (udev ACL / input group) arrive after it appears.

Consumers subscribe with a callback; one reader thread serves them all and
runs only while someone is subscribed. Callbacks run on that thread, so keep
them short and thread-safe.

Needs read access to /dev/input/event* (the `input` group on Fedora).
"""

from __future__ import annotations

import errno
import glob
import os
import select
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

EV_KEY = 0x01
EV_MAX = 0x1F
KEY_MAX = 0x2FF
BTN_LEFT = 0x110

KEY_LEFTCTRL, KEY_RIGHTCTRL = 29, 97
KEY_LEFTSHIFT, KEY_RIGHTSHIFT = 42, 54
KEY_LEFTALT, KEY_RIGHTALT = 56, 100
KEY_LEFTMETA, KEY_RIGHTMETA = 125, 126
MODIFIER_KEYS = frozenset(
    {
        KEY_LEFTCTRL,
        KEY_RIGHTCTRL,
        KEY_LEFTSHIFT,
        KEY_RIGHTSHIFT,
        KEY_LEFTALT,
        KEY_RIGHTALT,
        KEY_LEFTMETA,
        KEY_RIGHTMETA,
    }
)

KEYBOARD = "keyboard"
POINTER = "pointer"

# Lower-cased substrings of device names we never read.
EXCLUDED_NAME_PARTS = ("ydotoold virtual device", "vinopop")

_EVENT = struct.Struct("llHHi")  # struct input_event on 64-bit Linux


def _ioc_read(nr: int, size: int) -> int:
    # _IOC(_IOC_READ, 'E', nr, size)
    return (2 << 30) | (size << 16) | (ord("E") << 8) | nr


def eviocgbit(ev: int, size: int) -> int:
    return _ioc_read(0x20 + ev, size)


def eviocgname(size: int) -> int:
    return _ioc_read(0x06, size)


def _bit(buf: bytes, n: int) -> bool:
    return n // 8 < len(buf) and bool(buf[n // 8] >> (n % 8) & 1)


def classify(ev_bits: bytes, key_bits: bytes) -> frozenset[str]:
    """Device kinds from EVIOCGBIT(0) and EVIOCGBIT(EV_KEY) results."""
    if not _bit(ev_bits, EV_KEY):
        return frozenset()
    kinds = set()
    if any(_bit(key_bits, k) for k in MODIFIER_KEYS):
        kinds.add(KEYBOARD)
    if _bit(key_bits, BTN_LEFT):
        kinds.add(POINTER)
    return frozenset(kinds)


def is_excluded(name: str) -> bool:
    low = name.lower()
    return any(part in low for part in EXCLUDED_NAME_PARTS)


@dataclass(frozen=True)
class DeviceInfo:
    path: str
    name: str
    kinds: frozenset[str]


def _default_ioctl(fd: int, request: int, size: int) -> bytes:
    import fcntl

    return bytes(fcntl.ioctl(fd, request, bytes(size)))


def probe(
    fd: int, path: str, ioctl: Callable[[int, int, int], bytes] = _default_ioctl
) -> DeviceInfo:
    """Name and kinds of an open evdev node. `ioctl(fd, request, size)`
    returns the filled buffer; tests pass a fake."""
    try:
        raw = ioctl(fd, eviocgname(256), 256)
        name = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
    except OSError:
        name = ""
    try:
        ev_bits = ioctl(fd, eviocgbit(0, EV_MAX // 8 + 1), EV_MAX // 8 + 1)
        key_bits = ioctl(fd, eviocgbit(EV_KEY, KEY_MAX // 8 + 1), KEY_MAX // 8 + 1)
    except OSError:
        return DeviceInfo(path, name, frozenset())
    return DeviceInfo(path, name, classify(ev_bits, key_bits))


@dataclass(frozen=True)
class InputEvent:
    """One EV_KEY event. value: 1 press, 0 release, 2 autorepeat."""

    code: int
    value: int
    kinds: frozenset[str]  # what the source device is (keyboard / pointer)
    time: float  # kernel timestamp, seconds


Subscriber = Callable[[InputEvent], None]


class InputEventHub:
    """Reads EV_KEY events from every keyboard and pointer and hands them to
    subscribers. Use get_input_hub() rather than constructing one, so all
    consumers share the open devices and the thread."""

    RESCAN_S = 2.0

    def __init__(
        self,
        dev_dir: str = "/dev/input",
        ioctl: Callable[[int, int, int], bytes] = _default_ioctl,
        opener: Callable[[str], int] | None = None,
    ) -> None:
        self._dev_dir = dev_dir
        self._ioctl = ioctl
        self._open = opener or (lambda p: os.open(p, os.O_RDONLY | os.O_NONBLOCK))
        self._lock = threading.Lock()  # guards _subs
        self._life = threading.Lock()  # serialises reader start/stop
        self._subs: dict[int, Subscriber] = {}
        self._next_id = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._fds: dict[int, DeviceInfo] = {}  # fd -> device
        self._paths: set[str] = set()  # paths with an open fd
        self._skipped: dict[str, int] = {}  # path -> inode, not a device we want
        self.denied = 0  # nodes we couldn't open on the last scan

    # ---- subscriptions -------------------------------------------------
    def subscribe(self, callback: Subscriber) -> int:
        with self._life:
            with self._lock:
                self._next_id += 1
                self._subs[self._next_id] = callback
                handle = self._next_id
            t = self._thread
            if t is not None and t.is_alive() and not self._stop.is_set():
                return handle
            if t is not None:
                t.join(timeout=1.0)  # a stopping reader still owns the fds
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(self._stop,), daemon=True, name="vinopop-evdev"
            )
            self._thread.start()
        return handle

    def unsubscribe(self, handle: int, timeout: float = 0.6) -> None:
        with self._life:
            with self._lock:
                self._subs.pop(handle, None)
                if self._subs:
                    return
            self._stop.set()
            t = self._thread
            # Join so the fds are closed before a fresh subscriber reopens them.
            if t is not None and t is not threading.current_thread():
                t.join(timeout=timeout)

    def devices(self) -> list[DeviceInfo]:
        return list(self._fds.values())

    # ---- device set ----------------------------------------------------
    def scan(self) -> list[DeviceInfo]:
        """Open new keyboards / pointers under dev_dir. Returns the ones added."""
        added = []
        denied = 0
        for path in sorted(glob.glob(os.path.join(self._dev_dir, "event*"))):
            if path in self._paths:
                continue
            try:
                ino = os.stat(path).st_ino
            except OSError:
                continue
            if self._skipped.get(path) == ino:
                continue
            try:
                fd = self._open(path)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EPERM):
                    denied += 1
                continue
            info = probe(fd, path, self._ioctl)
            if not info.kinds or is_excluded(info.name):
                os.close(fd)
                self._skipped[path] = ino
                continue
            self._fds[fd] = info
            self._paths.add(path)
            added.append(info)
        self.denied = denied
        return added

    def _drop(self, fd: int) -> None:
        info = self._fds.pop(fd, None)
        if info is not None:
            self._paths.discard(info.path)
        try:
            os.close(fd)
        except OSError:
            pass

    def _close_all(self) -> None:
        for fd in list(self._fds):
            self._drop(fd)
        self._skipped.clear()

    # ---- reader --------------------------------------------------------
    def _run(self, stop: threading.Event) -> None:
        try:
            for info in self.scan():
                print(f"[evdev] reading {info.path} ({info.name}; {', '.join(sorted(info.kinds))})")
            if not self._fds:
                print(
                    "[evdev] no readable keyboard/mouse in /dev/input - add yourself to the "
                    "'input' group? Wayland double-click is off until one appears"
                )
            next_scan = time.monotonic() + self.RESCAN_S
            while not stop.is_set():
                wait = max(0.0, min(0.3, next_scan - time.monotonic()))
                fds = list(self._fds)
                if fds:
                    try:
                        readable, _, _ = select.select(fds, [], [], wait)
                    except (OSError, ValueError):
                        readable = []
                else:
                    stop.wait(wait)
                    readable = []
                for fd in readable:
                    self._read(fd)
                if time.monotonic() >= next_scan:
                    for info in self.scan():
                        print(f"[evdev] new device {info.path} ({info.name})")
                    next_scan = time.monotonic() + self.RESCAN_S
        finally:
            self._close_all()

    def _read(self, fd: int) -> None:
        info = self._fds.get(fd)
        if info is None:
            return
        try:
            data = os.read(fd, _EVENT.size * 64)
        except BlockingIOError:
            return
        except OSError:
            self._drop(fd)  # ENODEV: unplugged
            return
        if not data:
            self._drop(fd)
            return
        for off in range(0, len(data) - _EVENT.size + 1, _EVENT.size):
            sec, usec, etype, code, value = _EVENT.unpack_from(data, off)
            if etype == EV_KEY:
                self.dispatch(InputEvent(code, value, info.kinds, sec + usec / 1e6))

    def dispatch(self, event: InputEvent) -> None:
        with self._lock:
            subs = list(self._subs.values())
        for cb in subs:
            try:
                cb(event)
            except Exception as exc:  # noqa: BLE001 - one bad consumer mustn't stop the rest
                print(f"[evdev] subscriber failed: {exc}")


_HUB: InputEventHub | None = None


def get_input_hub() -> InputEventHub:
    global _HUB
    if _HUB is None:
        _HUB = InputEventHub()
    return _HUB


def access_summary(dev_dir: str = "/dev/input") -> tuple[int, int, int]:
    """(keyboards, pointers, unreadable nodes) visible to this user right now.
    For the doctor; opens and closes each node once."""
    hub = InputEventHub(dev_dir)
    try:
        devs = hub.scan()
        return (
            sum(KEYBOARD in d.kinds for d in devs),
            sum(POINTER in d.kinds for d in devs),
            hub.denied,
        )
    finally:
        hub._close_all()
