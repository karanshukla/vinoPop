"""The PlatformBackend interface: every OS-specific operation (selection,
pointer, selection watching, global hotkeys, key injection, popup placement)
goes through it, so the rest of the app never touches Xlib, xclip or
wl-clipboard.

Backends: `x11.X11Backend` (XWayland and Xorg) and
`wayland_kde.WaylandKdeBackend` (KDE Plasma 6 Wayland).
`platform_backend.get_backend()` picks one at startup.
"""

from __future__ import annotations

import abc
import time
from collections.abc import Callable
from typing import Protocol

COPY_BURST_S = 0.3
_EXPECT_TTL_S = 3.0
_INJECTOR_WARNING_MS = 6000

_expected: tuple[str, float] | None = None


def expect_selection(text: str) -> None:
    """vinoPop is about to put `text` on PRIMARY itself (OCR) and wants the
    popup for it, gesture or not. Good once, for _EXPECT_TTL_S (pinned by
    `tests/test_platform_selection_rules.py::test_an_expected_selection_is_taken_just_inside_its_ttl`,
    `tests/test_platform_selection_rules.py::test_an_expected_selection_is_stale_just_after_its_ttl`,
    `tests/test_platform_selection_rules.py::test_an_expected_selection_is_taken_only_once`)."""
    global _expected
    _expected = (text, time.monotonic())


def take_expected(text: str) -> bool:
    global _expected
    if _expected is None or _expected[0] != text:
        return False
    fresh = time.monotonic() - _expected[1] < _EXPECT_TTL_S
    _expected = None
    return fresh


def is_copy_button(
    text: str, burst_t0: float, clip_changed_at: float | None, read_clipboard: Callable[[], str]
) -> bool:
    """A page's Copy button selects a hidden field and copies it: CLIPBOARD
    changes to the PRIMARY text within COPY_BURST_S of the PRIMARY burst
    (pinned by
    `tests/test_platform_selection_rules.py::test_a_clipboard_change_just_inside_the_burst_is_a_copy_button`,
    `tests/test_platform_selection_rules.py::test_a_clipboard_change_just_before_the_burst_is_not`,
    `tests/test_x11_watcher.py::test_reselecting_what_was_copied_long_ago_is_shown`)."""
    if clip_changed_at is None or clip_changed_at < burst_t0 - COPY_BURST_S:
        return False
    return read_clipboard() == text


_missing_injector_warned: set[str] = set()


def _warn_missing_injector(backend_name: str, message: str) -> None:
    """Notify once per session per `backend_name` that key injection failed
    (pinned by `tests/test_platform_selection_rules.py::test_a_missing_injector_is_reported_once_per_key`)."""
    if backend_name in _missing_injector_warned:
        return
    _missing_injector_warned.add(backend_name)
    from core import actions

    actions.notify("Keystroke actions unavailable", message, "dialog-warning", _INJECTOR_WARNING_MS)


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

    name: str = "base"

    #: The popup opens its own Xlib display for pointer/Esc polling (X11).
    popup_uses_xlib: bool = False

    #: pointer_position() is in logical pixels (KWin), not physical root
    #: coordinates the popup must divide by the monitor scale (X11).
    pointer_is_logical: bool = False

    @abc.abstractmethod
    def check_session(self) -> None:
        """Validate the session is supported; print + sys.exit if not."""

    @abc.abstractmethod
    def read_selection(self, source: str) -> str:
        """Read 'primary' or 'clipboard' selection text (or '')."""

    @abc.abstractmethod
    def set_clipboard(self, text: str) -> None:
        """Put text on the CLIPBOARD selection."""

    @abc.abstractmethod
    def pointer_position(self) -> tuple[int, int]:
        """Global pointer position; see pointer_is_logical for the units."""

    @abc.abstractmethod
    def send_key(self, combo: str) -> None:
        """Send a key chord in xdotool syntax: 'ctrl+v', 'Return', 'BackSpace'."""

    @abc.abstractmethod
    def type_text(self, text: str) -> None:
        """Type literal text into the focused window."""

    def paste(self) -> None:
        """Send the paste chord."""
        self.send_key("ctrl+v")

    def can_paste(self) -> bool:
        """Whether this backend can actually inject a paste keystroke."""
        return True

    # ---- opening URLs ----------------------------------------------------
    @abc.abstractmethod
    def open_url(self, url: str) -> None:
        """Open a URL in the default browser, in the foreground."""

    @abc.abstractmethod
    def active_window_haystacks(self) -> list[str]:
        """Lowercased title and WM_CLASS/app-id for blocklist matching; empty
        when unavailable."""

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

    def can_bind_hotkey(self, hotkey_str: str) -> bool:
        """Whether make_hotkey() could grab this combo at all; the Settings
        recorder refuses one that can't (pinned by
        `tests/test_hotkey.py::test_x11_can_bind_hotkey`,
        `tests/test_hotkey.py::test_qt_keycode_rejects_what_it_cannot_encode`)."""
        return True

    @abc.abstractmethod
    def make_double_click_watcher(
        self, on_double_click: Callable[[int, int], None]
    ) -> DoubleClickWatcher | None:
        """A global Ctrl+double-click watcher, or None if unsupported."""

    # GTK 4 has no Gtk.Window.move(), type hints or keep-above: floating
    # windows are placed only through these two calls.
    @abc.abstractmethod
    def prepare_popup_window(self, win) -> None:
        """Prepare a floating Gtk.Window, once, before it is first realized."""

    def init_popup_window(self, win) -> None:
        """Alias of prepare_popup_window(), for plugins."""
        self.prepare_popup_window(win)

    @abc.abstractmethod
    def move_popup_window(self, win, x: int, y: int) -> None:
        """Move the popup to logical (x, y); before realize, applied on realize."""
