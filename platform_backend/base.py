"""Platform backend interface for vinoPop's OS-integration layer.

vinoPop started life X11-only. To run on Fedora KDE Plasma (Wayland) without
forking, every OS-specific operation - reading the selection, querying the
pointer, watching for selection changes, grabbing a global hotkey, injecting
keystrokes, positioning the popup - goes through a `PlatformBackend`.

Two concrete backends:
  - `x11.X11Backend`        - the original behaviour (xclip / Xlib / xdotool)
  - `wayland_kde.WaylandKdeBackend` - KDE Plasma 6 / Wayland (wl-clipboard /
    KWin cursorPos over DBus / gtk4-layer-shell / RemoteDesktop portal or
    ydotool / KGlobalAccel)

`platform_backend.get_backend()` picks one at startup (see __init__.detect).
The rest of the app talks only to the returned object and never imports Xlib /
xclip / wl-clipboard directly.

Selection watchers and hotkeys are returned as live objects implementing the
small protocols below - the same shape the original `watcher.SelectionWatcher`
and `hotkey.Hotkey` already had, so callers barely change.
"""

from __future__ import annotations

import abc
import subprocess
from collections.abc import Callable
from typing import Protocol

# One-time notification per session when a key-injection backend is missing.
# Keeps Cut/Paste/Backspace from failing completely silently.
_missing_injector_warned: set[str] = set()


def _warn_missing_injector(backend_name: str, message: str) -> None:
    """Show a desktop notification once per session if key injection fails."""
    if backend_name in _missing_injector_warned:
        return
    _missing_injector_warned.add(backend_name)
    try:
        subprocess.run(
            [
                "notify-send",
                "--hint=byte:transient:1",
                "-t",
                "6000",
                "-i",
                "dialog-warning",
                "vinoPop",
                message,
            ],
            check=False,
        )
    except Exception:
        pass


class SelectionWatcher(Protocol):
    """Fires on_selection(text, x, y) when the primary selection changes."""

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def set_debounce_ms(self, ms: int) -> None: ...


class Hotkey(Protocol):
    """A registered global hotkey that calls on_trigger() when pressed."""

    def start(self) -> None: ...
    def stop(self, wait: bool = True, timeout: float = 1.5) -> None: ...


class DoubleClickWatcher(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


class PlatformBackend(abc.ABC):
    """The OS-integration surface the rest of vinoPop depends on."""

    #: short identifier, e.g. "x11" or "wayland_kde"
    name: str = "base"

    #: True if the popup window should open its own Xlib display for
    #: pointer/Esc polling (X11 only). Wayland popups rely on GTK events.
    popup_uses_xlib: bool = False

    #: True if pointer_position() already returns logical pixels (Wayland/KDE,
    #: where KWin cursorPos is in compositor/logical space). X11 returns
    #: physical root coords, so the popup divides by the monitor scale there.
    pointer_is_logical: bool = False

    # ---- session ---------------------------------------------------------
    @abc.abstractmethod
    def check_session(self) -> None:
        """Validate the session is supported; print + sys.exit if not."""

    # ---- selection / clipboard ------------------------------------------
    @abc.abstractmethod
    def read_selection(self, source: str) -> str:
        """Read 'primary' or 'clipboard' selection text (or '')."""

    @abc.abstractmethod
    def set_clipboard(self, text: str) -> None:
        """Put text on the CLIPBOARD selection."""

    # ---- pointer ---------------------------------------------------------
    @abc.abstractmethod
    def pointer_position(self) -> tuple[int, int]:
        """Global pointer position in physical pixels (root coords)."""

    # ---- keystroke injection --------------------------------------------
    @abc.abstractmethod
    def send_key(self, combo: str) -> None:
        """Send a key chord in xdotool syntax, e.g. 'ctrl+v', 'Return',
        'BackSpace', 'ctrl+a'. Backends translate as needed."""

    @abc.abstractmethod
    def type_text(self, text: str) -> None:
        """Type literal text into the focused window."""

    def paste(self) -> None:
        """Send the paste chord. Default is Ctrl+V; backends may override."""
        self.send_key("ctrl+v")

    def can_paste(self) -> bool:
        """Whether this backend can actually inject a paste keystroke."""
        return True

    # ---- opening URLs ----------------------------------------------------
    @abc.abstractmethod
    def open_url(self, url: str) -> None:
        """Open a URL in the default browser and bring it to the foreground.
        On Wayland the foreground part needs a compositor nudge - X11 raises
        the browser on its own."""

    # ---- active window (for the blocklist) ------------------------------
    @abc.abstractmethod
    def active_window_haystacks(self) -> list[str]:
        """Lowercased strings (title, WM_CLASS/app-id) for blocklist matching.
        Empty list when unavailable."""

    # ---- component factories --------------------------------------------
    @abc.abstractmethod
    def make_selection_watcher(
        self, on_selection: Callable[[str, int, int], None], debounce_ms: int
    ) -> SelectionWatcher: ...

    @abc.abstractmethod
    def make_hotkey(
        self,
        hotkey_str: str,
        on_trigger: Callable[[], None],
        use_polling: bool = False,
        role: str | None = None,
    ) -> Hotkey:
        """role ('popup', 'clipboard', 'ocr') names the hotkey where the
        desktop keeps a registry of them (KGlobalAccel); X11 ignores it."""

    @abc.abstractmethod
    def make_double_click_watcher(
        self, on_double_click: Callable[[int, int], None]
    ) -> DoubleClickWatcher | None:
        """A global Ctrl+double-click watcher, or None if unsupported."""

    # ---- popup positioning ----------------------------------------------
    # GTK 4 dropped Gtk.Window.move() / type hints / keep-above, so floating
    # windows (the popup bar, AI bubble, ...) are placed only through these
    # two calls. X11 (incl. XWayland): override-redirect + XMoveWindow via
    # python-xlib. Wayland with wlr-layer-shell: gtk4-layer-shell margins.
    @abc.abstractmethod
    def prepare_popup_window(self, win) -> None:
        """Prepare a floating Gtk.Window for this backend. Called once, before
        the window is first realized/presented (X11: override-redirect on
        realize; Wayland: gtk4-layer-shell init)."""

    def init_popup_window(self, win) -> None:
        """Old name of prepare_popup_window(), kept for callers that predate
        the GTK 4 port."""
        self.prepare_popup_window(win)

    @abc.abstractmethod
    def move_popup_window(self, win, x: int, y: int) -> None:
        """Move the popup to logical coords (x, y). May be called before the
        window is realized; the position is then applied on realize."""
