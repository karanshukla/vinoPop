#!/usr/bin/env python3
"""vinoPop's tray child process: a QSystemTrayIcon, talking to the daemon
over tray.py's socket protocol.

QSystemTrayIcon, not KF6's KStatusNotifierItem (broken D-Bus registration on
Fedora 44): it registers the StatusNotifierItem and exports the menu over
DBusMenu, which plasmashell draws itself. On KWin Wayland that is the only
menu that shows; a parentless QMenu.popup never maps.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCursor, QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import vino_family  # noqa: E402
from core.xdg_paths import CACHE_DIR as SOCKET_DIR  # noqa: E402
from core.xdg_paths import CONFIG_DIR, HICOLOR_APPS
from tray.tray import SOCKET_NAME, FrameReader, _send_message

ICON_DIR = str(Path(__file__).resolve().parent.parent / "icons")
# At login the panel can start after us.
_TRAY_HOST_GRACE_MS = 15000
_TRAY_HOST_RETRY_MS = 1000
_SOCKET_POLL_MS = 100
_PARENT_POLL_MS = 2000
SETTINGS_FILE = CONFIG_DIR / "settings.json"

ICON_STYLES = ("color", "light", "dark")
DEFAULT_ICON_STYLE = "color"
# Fixed glyph colours: light for dark panels, dark for light ones
_MONO_COLORS = {"light": "#f4f5f6", "dark": "#2a2e32"}
_ICON_SIZES = (16, 22, 24, 32, 48, 64)
_SYMBOLIC_ICON = "vinopop-tray-symbolic"
_APP_ICON = "vinopop"


def _tray_icon_style() -> str:
    """The tray_icon_style setting, one of ICON_STYLES."""
    try:
        d = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        v = str(d.get("tray_icon_style", DEFAULT_ICON_STYLE)).strip().lower()
        return v if v in ICON_STYLES else DEFAULT_ICON_STYLE
    except Exception:
        return DEFAULT_ICON_STYLE


def _icon(what: str) -> QIcon:
    """A menu icon from the desktop's theme, by what the item does."""
    return QIcon.fromTheme(vino_family.MENU_ICONS[what])


class TrayQt:
    def __init__(self) -> None:
        self._app = QApplication(sys.argv)
        self._app.setQuitOnLastWindowClosed(False)
        self._app.setApplicationName("vinopop-tray")
        self._app.setDesktopFileName("vinopop")

        self._install_icon()

        self._menu = QMenu()
        self._setup_menu()

        self._tray = QSystemTrayIcon()
        self._tray.setIcon(self._load_icon())
        self._tray.setToolTip("vinoPop - text selection actions")
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)
        self._tray.show()

        self._sock: socket.socket | None = None
        self._client: socket.socket | None = None
        self._reader = FrameReader()
        self._running = True
        self._setup_socket()

        self._timer = QTimer()
        self._timer.timeout.connect(self._check_socket)
        self._timer.start(_SOCKET_POLL_MS)

        self._initial_ppid = os.getppid()
        self._ppid_timer = QTimer()
        self._ppid_timer.timeout.connect(self._check_parent_alive)
        self._ppid_timer.start(_PARENT_POLL_MS)

        avail = QSystemTrayIcon.isSystemTrayAvailable()
        print(f"[tray-qt] Started (QSystemTrayIcon, tray_available={avail})", flush=True)
        QTimer.singleShot(_TRAY_HOST_GRACE_MS, self._report_missing_host)

    def _report_missing_host(self) -> None:
        """Tell the daemon there's no tray host (GNOME without AppIndicator),
        once it's connected."""
        if QSystemTrayIcon.isSystemTrayAvailable():
            return
        if self._client is None:
            QTimer.singleShot(_TRAY_HOST_RETRY_MS, self._report_missing_host)
            return
        print("[tray-qt] no system tray host: the icon won't show", flush=True)
        self._emit("no_tray_host", None)

    def _load_icon(self) -> QIcon:
        """The icon for tray_icon_style. "color" loads by theme name: Plasma
        recolours ColorScheme-Text parts only in an icon the SNI names. Where
        that fails, the user picks a fixed "light" or "dark" glyph."""
        style = _tray_icon_style()
        if style in _MONO_COLORS:
            ic = self._render_symbolic(_MONO_COLORS[style])
            if ic is not None and not ic.isNull():
                return ic
        ic = QIcon.fromTheme(_SYMBOLIC_ICON)
        if not ic.isNull():
            return ic
        for name in (_SYMBOLIC_ICON, _APP_ICON):
            p = Path(ICON_DIR) / f"{name}.svg"
            if p.is_file():
                ic = QIcon(str(p))
                if not ic.isNull():
                    return ic
        return QIcon.fromTheme(_APP_ICON) or QIcon.fromTheme("applications-internet")

    def _render_symbolic(self, color: str) -> QIcon | None:
        """The symbolic SVG (fill="currentColor") drawn in `color` at each tray size."""
        p = Path(ICON_DIR) / f"{_SYMBOLIC_ICON}.svg"
        if not p.is_file():
            return None
        try:
            from PySide6.QtCore import QByteArray, Qt
            from PySide6.QtGui import QImage, QPainter, QPixmap
            from PySide6.QtSvg import QSvgRenderer

            svg = p.read_text(encoding="utf-8").replace("currentColor", color)
            renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
            icon = QIcon()
            for size in _ICON_SIZES:
                img = QImage(size, size, QImage.Format_ARGB32)
                img.fill(Qt.transparent)
                painter = QPainter(img)
                renderer.render(painter)
                painter.end()
                icon.addPixmap(QPixmap.fromImage(img))
            return icon
        except Exception as exc:  # noqa: BLE001
            print(f"[tray-qt] symbolic render failed: {exc}", flush=True)
            return None

    def _install_icon(self) -> None:
        import shutil

        user_dir = HICOLOR_APPS
        user_dir.mkdir(parents=True, exist_ok=True)
        for name in (_SYMBOLIC_ICON, _APP_ICON):
            src = Path(ICON_DIR) / f"{name}.svg"
            dst = user_dir / f"{name}.svg"
            if src.is_file():
                try:
                    shutil.copy2(src, dst)
                except OSError:
                    pass

    def _on_activated(self, reason) -> None:
        """Left or middle click: the menu on X11, Settings on Wayland, where
        the click arrives over D-Bus without an input event and a client-side
        QMenu.popup is refused. Right-click is plasmashell's DBusMenu."""
        R = QSystemTrayIcon.ActivationReason
        if reason not in (R.Trigger, R.MiddleClick):
            return
        if QGuiApplication.platformName() == "wayland":
            self._emit("settings", None)
        else:
            self._menu.popup(QCursor.pos())

    def _setup_menu(self) -> None:
        """Actions get an icon (core/vino_family.py's tray rules); the
        checkable item doesn't."""
        h = self._menu.addAction("vinoPop")
        h.setEnabled(False)
        self._menu.addSeparator()
        self._toggle_action = self._menu.addAction("Auto-popup on selection")
        self._toggle_action.setCheckable(True)
        self._toggle_action.setChecked(True)
        self._toggle_action.triggered.connect(lambda checked: self._emit("toggle_watcher", checked))
        a = self._menu.addAction(_icon("popup"), "Show popup now")
        a.triggered.connect(lambda: self._emit("show_popup", None))
        self._menu.addSeparator()
        a = self._menu.addAction(_icon("settings"), "Settings…")
        a.triggered.connect(lambda: self._emit("settings", None))
        a = self._menu.addAction(_icon("plugins"), "Plugins…")
        a.triggered.connect(lambda: self._emit("plugins", None))
        a = self._menu.addAction(_icon("about"), "About vinoPop")
        a.triggered.connect(lambda: self._emit("about", None))
        self._menu.addSeparator()
        a = self._menu.addAction(_icon("quit"), "Quit vinoPop")
        a.triggered.connect(lambda: self._emit("quit", None))

    def _emit(self, event: str, value: object) -> None:
        if self._client:
            try:
                _send_message(self._client, {"event": event, "value": value})
            except OSError:
                pass

    def _setup_socket(self) -> None:
        SOCKET_DIR.mkdir(parents=True, exist_ok=True)
        socket_path = str(SOCKET_DIR / SOCKET_NAME)
        try:
            os.unlink(socket_path)
        except OSError:
            pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(socket_path)
        self._sock.listen(1)
        self._sock.setblocking(False)
        info_file = SOCKET_DIR / "tray.info"
        info_file.write_text(f"{socket_path}\n{os.getpid()}\n")

    def _check_parent_alive(self) -> None:
        """Quit once the parent PID changes. PR_SET_PDEATHSIG misses a
        reparent to a systemd subreaper, and an orphan leaves a ghost icon."""
        try:
            if os.getppid() != self._initial_ppid:
                print("[tray-qt] parent daemon gone -- exiting", flush=True)
                self._running = False
                self._app.quit()
        except Exception:
            pass

    def _check_socket(self) -> None:
        if not self._running:
            self._app.quit()
            return
        try:
            if self._client is None:
                try:
                    self._client, _addr = self._sock.accept()
                except BlockingIOError:
                    return
                self._reader = FrameReader()
            self._client.setblocking(False)
            messages = self._reader.read(self._client)
        except OSError:  # ConnectionError too: the daemon hung up
            self._disconnect_client()
            return
        for msg in messages:
            self._handle_command(msg)

    def _disconnect_client(self) -> None:
        if self._client:
            try:
                self._client.close()
            except OSError:
                pass
            self._client = None

    def _handle_command(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd == "set_watcher_active":
            self._toggle_action.setChecked(bool(msg.get("value", True)))
        elif cmd == "reload_icon":
            try:
                self._tray.setIcon(self._load_icon())
            except Exception as exc:  # noqa: BLE001
                print(f"[tray-qt] reload_icon failed: {exc}", flush=True)
        elif cmd == "set_visible":
            self._tray.setVisible(bool(msg.get("value", True)))
        elif cmd == "quit":
            self._running = False
        elif cmd == "ping" and self._client:
            try:
                _send_message(self._client, {"event": "pong", "value": None})
            except OSError:
                pass

    def run(self) -> None:
        self._app.exec()
        if self._client:
            try:
                self._client.close()
            except OSError:
                pass
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass


if __name__ == "__main__":
    TrayQt().run()
