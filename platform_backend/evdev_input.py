"""Raw /dev/input reading for everything that needs physical input on
Wayland (the double-click watcher, the selection-gesture tracker).

Reads every /dev/input/event* whose capability bits make it a keyboard
(EV_KEY with a modifier key) or a pointer (BTN_LEFT); not the by-path links,
which keyd's virtual keyboard and Bluetooth devices lack. Excluded by name:
ydotoold's device (our injected keys would look like the user's) and our own.
/dev/input is rescanned every RESCAN_S seconds, for hotplug and for
permissions that arrive after the node.

One reader thread serves every subscriber, while there is one. Callbacks run
on it, so keep them short and thread-safe. Needs read access to
/dev/input/event* (the `input` group).
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
# InputEvent.value
KEY_RELEASED, KEY_PRESSED, KEY_AUTOREPEAT = 0, 1, 2
# xkb (and X) keycodes are evdev codes + 8
EVDEV_TO_XKB_KEYCODE = 8

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

EXCLUDED_NAME_PARTS = ("ydotoold virtual device", "vinopop")

_EVENT = struct.Struct("llHHi")  # struct input_event on 64-bit Linux


# linux/ioctl.h: _IOC(dir, type, nr, size)
_IOC_READ = 2
_IOC_DIRSHIFT, _IOC_SIZESHIFT, _IOC_TYPESHIFT = 30, 16, 8
_EVDEV_IOC_TYPE = ord("E")
_EVIOCGNAME_NR = 0x06
_EVIOCGBIT_NR = 0x20
_NAME_BUF = 256


def _ioc_read(nr: int, size: int) -> int:
    return (
        (_IOC_READ << _IOC_DIRSHIFT)
        | (size << _IOC_SIZESHIFT)
        | (_EVDEV_IOC_TYPE << _IOC_TYPESHIFT)
        | nr
    )


def eviocgbit(ev: int, size: int) -> int:
    return _ioc_read(_EVIOCGBIT_NR + ev, size)


def eviocgname(size: int) -> int:
    return _ioc_read(_EVIOCGNAME_NR, size)


def _bitmap_size(max_code: int) -> int:
    return max_code // 8 + 1


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
        raw = ioctl(fd, eviocgname(_NAME_BUF), _NAME_BUF)
        name = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
    except OSError:
        name = ""
    try:
        ev_size, key_size = _bitmap_size(EV_MAX), _bitmap_size(KEY_MAX)
        ev_bits = ioctl(fd, eviocgbit(0, ev_size), ev_size)
        key_bits = ioctl(fd, eviocgbit(EV_KEY, key_size), key_size)
    except OSError:
        return DeviceInfo(path, name, frozenset())
    return DeviceInfo(path, name, classify(ev_bits, key_bits))


@dataclass(frozen=True)
class InputEvent:
    """One EV_KEY event; value is KEY_PRESSED, KEY_RELEASED or KEY_AUTOREPEAT."""

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
            # Joined so the fds close before a new subscriber reopens them.
            t = self._thread
            if t is not None and t is not threading.current_thread():
                t.join(timeout=timeout)

    def devices(self) -> list[DeviceInfo]:
        return list(self._fds.values())

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
