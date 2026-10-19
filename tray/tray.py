"""System tray icon (KStatusNotifierItem via separate Qt process) for vinoPop.

Spawns a lean Qt process that uses KStatusNotifierItem - native KDE/Wayland
protocol, no XWayland dependency, no GTK thread conflicts.
"""

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

# A tray process that keeps dying (PySide6 missing, no StatusNotifier host)
# is restarted a few times with growing pauses, then left alone: poll() runs
# on the GTK main loop and must never spend its time waiting for it.
_RESTART_PAUSES_S = (2.0, 5.0, 15.0, 30.0, 60.0)
_PYSIDE_HINT = "install PySide6 (pip install --user PySide6, or your distro's python3-pyside6)"


def _tray_preexec() -> None:
    """Runs in the tray child between fork and exec.

    1. PR_SET_PDEATHSIG: ask the kernel to SIGTERM this process the instant
       its parent (the main daemon) dies. Without this, killing or crashing
       the daemon left the Qt tray subprocess orphaned (reparented to systemd)
       and its tray icon lingered forever -- which is how two vinoPop icons
       could end up side by side after a few restarts.
    2. setsid(): give the tray its own session so a Ctrl-C in the daemon's
       terminal doesn't also tear it down (the original intent of the
       start_new_session flag this replaces).

    The death-signal is armed FIRST, then we re-check the parent is still
    alive, to close the tiny fork-then-parent-exits race before setsid()."""
    try:
        import ctypes

        PR_SET_PDEATHSIG = 1
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
        # If the parent vanished between fork and now, PDEATHSIG already
        # missed its window -- exit rather than become the next orphan.
        if os.getppid() == 1:
            os._exit(0)
    except Exception:
        pass
    try:
        os.setsid()
    except OSError:
        pass


class FrameReader:
    """Reassembles the length-prefixed JSON frames of the tray protocol from a
    non-blocking socket (shared with tray_qt.py).

    Reading frame by frame with recv(n) loses the bytes of a half-arrived
    message, and cannot tell "nothing yet" from "the other side is gone": both
    looked like an empty read, so a dead peer was never noticed."""

    MAX_FRAME = 1_000_000

    def __init__(self) -> None:
        self._buf = bytearray()
        self._closed = False

    def read(self, sock: socket.socket) -> list[dict]:
        """Every complete message that has arrived. Raises ConnectionError once
        the peer has closed the socket and its last messages were handed out."""
        while not self._closed:
            try:
                chunk = sock.recv(65536)
            except BlockingIOError:
                break
            if not chunk:
                self._closed = True
                break
            self._buf += chunk
        messages: list[dict] = []
        while len(self._buf) >= 4:
            (size,) = struct.unpack("!I", self._buf[:4])
            if size > self.MAX_FRAME:
                raise ConnectionError(f"oversized frame ({size} bytes)")
            if len(self._buf) < 4 + size:
                break
            raw = bytes(self._buf[4 : 4 + size])
            del self._buf[: 4 + size]
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
    sock.sendall(struct.pack("!I", len(raw)) + raw)


class Tray:
    """Same API as the old GTK/Ayatana Tray, but backed by a Qt KSNI subprocess."""

    def __init__(
        self,
        on_toggle_watcher: Callable[[bool], None],
        get_watcher_active: Callable[[], bool],
        on_show_popup_now: Callable[[], None],
        on_open_settings: Callable[[], None],
        on_open_plugins: Callable[[], None],
        on_open_about: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._on_toggle_watcher = on_toggle_watcher
        self._get_watcher_active = get_watcher_active
        self._on_show_popup_now = on_show_popup_now
        self._on_open_settings = on_open_settings
        self._on_open_plugins = on_open_plugins
        self._on_open_about = on_open_about
        self._on_quit = on_quit

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
        """Launch the Qt tray subprocess."""
        SOCKET_DIR.mkdir(parents=True, exist_ok=True)
        # Remove stale socket
        try:
            os.unlink(str(SOCKET_DIR / "tray.sock"))
        except OSError:
            pass
        try:
            self._proc = subprocess.Popen(
                [sys.executable, TRAY_SCRIPT],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                preexec_fn=_tray_preexec,  # die with parent + own session
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
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self._gave_up = True

    def _child_died(self) -> bool:
        return self._proc is not None and self._proc.poll() is not None

    def _connect(self, wait: bool = False) -> bool:
        """Connect to the tray subprocess's Unix socket.

        With `wait` (startup only) retry for up to ~3 s, but stop at once if the
        child has already exited. Otherwise make a single, non-blocking attempt:
        this runs from poll() on the GTK main loop."""
        socket_path = str(SOCKET_DIR / "tray.sock")
        for _attempt in range(30 if wait else 1):
            if self._child_died():
                break
            if os.path.exists(socket_path):
                sock = None
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(3)
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
                    # Sync initial watcher state
                    self.refresh()
                    print("[tray] connected to tray subprocess")
                    return True
            if wait:
                time.sleep(0.1)
        if wait:
            if self._child_died():
                print(
                    f"[tray] tray process exited with code {self._proc.poll()}: {_PYSIDE_HINT}"  # type: ignore[union-attr]
                )
            else:
                print("[tray] WARNING: could not connect to tray subprocess")
        return False

    def _reconnect(self) -> bool:
        """Try to reconnect if the socket was lost. Never blocks."""
        if self._gave_up:
            return False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._connected = False
        # If the subprocess died, restart it - a bounded number of times,
        # with a growing pause between tries.
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
        try:
            subprocess.run(
                [
                    "notify-send",
                    "-i",
                    "vinopop",
                    "vinoPop tray icon unavailable",
                    f"The tray process keeps exiting. To get the tray icon, {_PYSIDE_HINT}.",
                ],
                check=False,
            )
        except OSError:
            pass

    def _on_no_tray_host(self) -> None:
        """The desktop has no tray to show our icon in, so Settings and
        Plugins are unreachable: say so once and open Settings."""
        if self._warned_no_host:
            return
        self._warned_no_host = True
        self._notify_no_host()
        self._on_open_settings()

    @staticmethod
    def _notify_no_host() -> None:
        try:
            subprocess.run(
                [
                    "notify-send",
                    "-i",
                    "vinopop",
                    "vinoPop has no tray icon",
                    "This desktop has no system tray, so vinoPop opened Settings "
                    "instead. On GNOME, the AppIndicator extension adds one.",
                ],
                check=False,
            )
        except OSError:
            pass

    def _dispatch(self, event: str, value: object) -> None:
        """Route an event from the tray subprocess to the right callback."""
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
            pass  # keepalive

    def refresh(self) -> None:
        """Update tray state from app - called by main.py."""
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
        """Ask the tray subprocess to re-render its icon - called when the
        tray_icon_style setting changes, so it updates live (no restart)."""
        if not self._connected or self._sock is None:
            return
        try:
            _send_message(self._sock, {"cmd": "reload_icon"})
        except OSError:
            self._connected = False

    def poll(self) -> None:
        """Read any pending messages from the tray subprocess.

        Must be called periodically from the GTK main loop (via GLib.timeout_add).
        """
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

    def shutdown(self) -> None:
        """Tell tray subprocess to quit and wait for it."""
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
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
