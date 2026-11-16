"""vinoPop's tray icon: the daemon side of the tray_qt.py child process,
spoken to over a Unix socket of length-prefixed JSON frames. A separate
process keeps Qt off the GTK main loop."""

from __future__ import annotations

import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from core.xdg_paths import CACHE_DIR as SOCKET_DIR

TRAY_SCRIPT = str(Path(__file__).resolve().parent / "tray_qt.py")
SOCKET_NAME = "tray.sock"
FRAME_HEADER = struct.Struct("!I")  # payload length, network order
_RECV_CHUNK = 65536
_CONNECT_TIMEOUT_S = 3
_CONNECT_RETRY_S = 0.1
_CONNECT_ATTEMPTS = 30  # ~3 s at startup
_SHUTDOWN_WAIT_S = 3
_SECONDS_PER_MINUTE = 60

# A tray child that keeps dying is restarted after each of these pauses, then
# left alone (pinned by
# `tests/test_tray.py::test_polling_a_dead_tray_never_blocks_and_gives_up`,
# `tests/test_tray.py::test_restarts_wait_out_their_pause`).
_RESTART_PAUSES_S = (2.0, 5.0, 15.0, 30.0, 60.0)
_PYSIDE_HINT = "install PySide6 (pip install --user PySide6, or your distro's python3-pyside6)"


PR_SET_PDEATHSIG = 1
INIT_PID = 1


def _tray_preexec() -> None:
    """Runs in the tray child between fork and exec: SIGTERM it when the
    daemon dies, so an orphan can't leave a second icon behind, and give it
    its own session so a Ctrl-C in the daemon's terminal spares it. The
    parent is re-checked after arming the signal, which misses a parent
    that exited before then."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
        if os.getppid() == INIT_PID:
            os._exit(0)
    except Exception:
        pass
    try:
        os.setsid()
    except OSError:
        pass


class FrameReader:
    """Reassembles the tray protocol's frames from a non-blocking socket, on
    both sides. Buffers across reads so a half-arrived frame isn't lost, and
    tells "nothing yet" apart from "the peer is gone"."""

    MAX_FRAME = 1_000_000

    def __init__(self) -> None:
        self._buf = bytearray()
        self._closed = False

    def read(self, sock: socket.socket) -> list[dict]:
        """Every complete message that has arrived. Raises ConnectionError once
        the peer has closed the socket and its last messages were handed out."""
        while not self._closed:
            try:
                chunk = sock.recv(_RECV_CHUNK)
            except BlockingIOError:
                break
            if not chunk:
                self._closed = True
                break
            self._buf += chunk
        messages: list[dict] = []
        head = FRAME_HEADER.size
        while len(self._buf) >= head:
            (size,) = FRAME_HEADER.unpack(self._buf[:head])
            if size > self.MAX_FRAME:
                raise ConnectionError(f"oversized frame ({size} bytes)")
            if len(self._buf) < head + size:
                break
            raw = bytes(self._buf[head : head + size])
            del self._buf[: head + size]
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(message, dict):
                messages.append(message)
        if self._closed and not messages:
            raise ConnectionError("socket closed")
        return messages


def _send_message(sock: socket.socket, msg: dict) -> None:
    raw = json.dumps(msg).encode("utf-8")
    sock.sendall(FRAME_HEADER.pack(len(raw)) + raw)


class Tray:
    """The tray icon as main.py sees it: callbacks in, state out."""

    def __init__(
        self,
        on_toggle_watcher: Callable[[bool], None],
        get_watcher_active: Callable[[], bool],
        on_show_popup_now: Callable[[], None],
        on_open_settings: Callable[[], None],
        on_open_plugins: Callable[[], None],
        on_open_about: Callable[[], None],
        on_quit: Callable[[], None],
        get_hide_after_minutes: Callable[[], int] = lambda: 0,
    ) -> None:
        self._on_toggle_watcher = on_toggle_watcher
        self._get_watcher_active = get_watcher_active
        self._on_show_popup_now = on_show_popup_now
        self._on_open_settings = on_open_settings
        self._on_open_plugins = on_open_plugins
        self._on_open_about = on_open_about
        self._on_quit = on_quit
        self._get_hide_after_minutes = get_hide_after_minutes
        self._used_at = time.monotonic()
        self._hidden = False

        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._connected = False
        self._reader = FrameReader()
        self._restarts = 0
        self._next_restart_at = 0.0
        self._gave_up = False
        self._warned_no_host = False

        if not os.path.isfile(TRAY_SCRIPT):
            print("[tray] tray_qt.py not found - tray disabled")
            self._gave_up = True
            return

        self._start_process()
        self._connect(wait=True)

    def _start_process(self) -> None:
        SOCKET_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.unlink(str(SOCKET_DIR / SOCKET_NAME))
        except OSError:
            pass
        try:
            self._proc = subprocess.Popen(
                [sys.executable, TRAY_SCRIPT],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                preexec_fn=_tray_preexec,
            )
            print(f"[tray] spawned Qt tray process (pid={self._proc.pid})")
        except OSError as exc:
            print(f"[tray] failed to start tray process: {exc}")
            self._proc = None

    def stop(self) -> None:
        """End the tray process now, for a restart: it only dies with
        this process, and exec() keeps the process alive."""
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=_SHUTDOWN_WAIT_S)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self._gave_up = True

    def _child_died(self) -> bool:
        return self._proc is not None and self._proc.poll() is not None

    def _connect(self, wait: bool = False) -> bool:
        """Connect to the child's socket. With `wait` (startup only) retry
        for ~3 s unless the child has exited; otherwise one attempt, since
        poll() runs on the GTK main loop (pinned by
        `tests/test_tray.py::test_a_tray_process_that_dies_at_once_does_not_block_startup`)."""
        socket_path = str(SOCKET_DIR / SOCKET_NAME)
        for _attempt in range(_CONNECT_ATTEMPTS if wait else 1):
            if self._child_died():
                break
            if os.path.exists(socket_path):
                sock = None
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(_CONNECT_TIMEOUT_S)
                    sock.connect(socket_path)
                    sock.setblocking(True)
                except OSError:
                    if sock is not None:
                        sock.close()
                else:
                    self._sock = sock
                    self._reader = FrameReader()
                    self._connected = True
                    self._restarts = 0
                    self._hidden = False  # a new tray process starts shown
                    self.refresh()
                    print("[tray] connected to tray subprocess")
                    return True
            if wait:
                time.sleep(_CONNECT_RETRY_S)
        if wait:
            if self._child_died():
                print(
                    f"[tray] tray process exited with code {self._proc.poll()}: {_PYSIDE_HINT}"  # type: ignore[union-attr]
                )
            else:
                print("[tray] WARNING: could not connect to tray subprocess")
        return False

    def _reconnect(self) -> bool:
        """Reconnect, restarting a dead child when its pause is over. Never blocks."""
        if self._gave_up:
            return False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._connected = False
        if self._proc is None or self._child_died():
            if self._restarts >= len(_RESTART_PAUSES_S):
                self._gave_up = True
                print(f"[tray] the tray process keeps exiting; giving up ({_PYSIDE_HINT})")
                self._notify_gave_up()
                return False
            now = time.monotonic()
            if now < self._next_restart_at:
                return False
            self._next_restart_at = now + _RESTART_PAUSES_S[self._restarts]
            self._restarts += 1
            self._start_process()
        return self._connect(wait=False)

    @staticmethod
    def _notify_gave_up() -> None:
        from core import actions

        actions.notify(
            "Tray icon unavailable",
            f"The tray process keeps exiting. To get the tray icon, {_PYSIDE_HINT}.",
            timeout_ms=None,
            transient=False,
        )

    def _on_no_tray_host(self) -> None:
        """With no tray, Settings is unreachable: say so once and open it
        (pinned by `tests/test_tray.py::test_no_tray_host_warns_once_and_opens_settings`)."""
        if self._warned_no_host:
            return
        self._warned_no_host = True
        self._notify_no_host()
        self._on_open_settings()

    @staticmethod
    def _notify_no_host() -> None:
        from core import actions

        actions.notify(
            "No tray icon",
            "This desktop has no system tray, so vinoPop opened Settings "
            "instead. On GNOME, the AppIndicator extension adds one.",
            timeout_ms=None,
            transient=False,
        )

    def _dispatch(self, event: str, value: object) -> None:
        """Route an event from the tray child to its callback."""
        if event == "toggle_watcher":
            self._on_toggle_watcher(bool(value))
        elif event == "show_popup":
            self._on_show_popup_now()
        elif event == "settings":
            self._on_open_settings()
        elif event == "plugins":
            self._on_open_plugins()
        elif event == "about":
            self._on_open_about()
        elif event == "quit":
            self._on_quit()
        elif event == "no_tray_host":
            self._on_no_tray_host()
        elif event == "pong":
            pass

    def refresh(self) -> None:
        """Send the watcher state to the tray."""
        if not self._connected or self._sock is None:
            self._reconnect()
            return
        try:
            active = self._get_watcher_active()
            _send_message(self._sock, {"cmd": "set_watcher_active", "value": active})
        except OSError:
            self._connected = False
            self._reconnect()

    def reload_icon(self) -> None:
        """Re-render the icon after tray_icon_style changed."""
        if not self._connected or self._sock is None:
            return
        try:
            _send_message(self._sock, {"cmd": "reload_icon"})
        except OSError:
            self._connected = False

    def note_use(self) -> None:
        """vinoPop was just used: show the icon again if it idled out.
        Safe from any thread."""
        self._used_at = time.monotonic()

    def _sync_visible(self) -> None:
        minutes = self._get_hide_after_minutes()
        idle_s = time.monotonic() - self._used_at
        hide = minutes > 0 and idle_s >= minutes * _SECONDS_PER_MINUTE
        if hide == self._hidden or self._sock is None:
            return
        _send_message(self._sock, {"cmd": "set_visible", "value": not hide})
        self._hidden = hide

    def poll(self) -> None:
        """Handle the tray child's messages; call it periodically on the GTK
        main loop."""
        if not self._connected or self._sock is None:
            self._reconnect()
            return
        try:
            self._sock.setblocking(False)
            messages = self._reader.read(self._sock)
        except OSError:  # includes ConnectionError: the tray process is gone
            self._connected = False
            self._reconnect()
            return
        for msg in messages:
            self._dispatch(msg.get("event", ""), msg.get("value"))
        try:
            self._sync_visible()
        except OSError:
            self._connected = False

    def shutdown(self) -> None:
        """Tell the tray child to quit and wait for it."""
        if self._connected and self._sock:
            try:
                _send_message(self._sock, {"cmd": "quit"})
            except OSError:
                pass
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._proc:
            try:
                self._proc.wait(timeout=_SHUTDOWN_WAIT_S)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
