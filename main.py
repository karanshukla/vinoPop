#!/usr/bin/env python3
"""vinoPop entry point: tray icon + selection watcher + hotkey + popup."""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

# `--doctor` (over SSH, or with a broken display) and `--trigger` (a D-Bus
# poke at the running daemon) exit before GTK is imported.
if __name__ == "__main__" and "--doctor" in sys.argv[1:]:
    from vino_ai import doctor

    sys.exit(doctor.main(as_json="--json" in sys.argv[1:]))

if __name__ == "__main__":
    from core import remote

    if (_action := remote.trigger_arg(sys.argv[1:])) is not None:
        sys.exit(remote.send(_action))

from platform_backend import detect as detect_backend  # noqa: E402
from platform_backend import is_wayland_session  # noqa: E402


def _notify(title: str, body: str) -> None:
    from core import actions

    actions.notify(title, body, timeout_ms=None)


def _load_layer_shell() -> bool:
    """Load gtk4-layer-shell, then its typelib. Must run before GTK loads
    libwayland-client: its hooks only work first in symbol lookup."""
    import ctypes

    try:
        ctypes.CDLL("libgtk4-layer-shell.so.0", mode=ctypes.RTLD_GLOBAL)
    except OSError:
        return False
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Gtk4LayerShell", "1.0")
        from gi.repository import Gtk4LayerShell  # noqa: F401
    except (ImportError, ValueError):
        return False
    return True


def _choose_gdk_backend() -> str:
    """Pick GDK's backend before GTK initialises and return a line for the
    log. GTK 4 can place a window only through gtk4-layer-shell or as an X11
    override-redirect window, so anything but KDE with layer-shell runs on
    XWayland (pinned by
    `tests/test_core_gdk_backend.py::test_a_preset_gdk_backend_is_left_alone`,
    `tests/test_core_gdk_backend.py::test_an_x11_session_stays_on_x11`,
    `tests/test_core_gdk_backend.py::test_a_non_kde_wayland_desktop_runs_on_xwayland`,
    `tests/test_core_gdk_backend.py::test_kde_without_layer_shell_runs_on_xwayland`,
    `tests/test_core_gdk_backend.py::test_kde_with_layer_shell_runs_native_wayland`,
    `tests/test_core_gdk_backend.py::test_without_xwayland_the_backend_is_not_forced`)."""
    if os.environ.get("GDK_BACKEND"):
        return f"GDK_BACKEND={os.environ['GDK_BACKEND']} (preset, left alone)"
    if not is_wayland_session():
        return "X11 session - GDK on X11"
    if detect_backend() == "x11":
        if (os.environ.get("VINOPOP_BACKEND") or "").strip().lower() == "x11":
            reason = "VINOPOP_BACKEND=x11"
        else:
            desktop = os.environ.get("XDG_CURRENT_DESKTOP") or "this desktop"
            reason = f"{desktop} is not KDE Plasma"
    elif not _load_layer_shell():
        reason = "Gtk4LayerShell (gtk4-layer-shell) is not installed"
    else:
        return "Wayland session - native Wayland with gtk4-layer-shell"
    if not os.environ.get("DISPLAY"):
        return f"Wayland session, {reason}, and no XWayland DISPLAY - popup placement unavailable"
    os.environ["GDK_BACKEND"] = "x11"
    return f"Wayland session, {reason} - running on XWayland (GDK_BACKEND=x11)"


_GDK_BACKEND_CHOICE = _choose_gdk_backend()

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from core import vino_family  # noqa: E402
from core.classifier import classify  # noqa: E402
from core.settings import get_settings  # noqa: E402
from core.version import __version__  # noqa: E402
from core.xdg_paths import (  # noqa: E402
    CACHE_DIR,
    CONFIG_DIR,
    LOG_FILE,
    PrivateRotatingFileHandler,
    make_private,
    private_dir,
)
from platform_backend import get_backend  # noqa: E402

# No XInitThreads: with it the hotkey threads fail to grab keys, silently
# (docs/how-it-works.md#threads-and-x11).
from plugin_system import plugin_loader  # noqa: E402
from triggers.editable_detect import focused_selection_rect, is_focus_editable  # noqa: E402
from triggers.toggle import CLOSE, OPEN, HotkeyToggle  # noqa: E402
from ui import (
    gtk4_app,  # noqa: E402
    theme,  # noqa: E402
)
from ui.popup import PopupWindow  # noqa: E402

if TYPE_CHECKING:
    from triggers.watcher import SelectionWatcher

LOCK_FILE = CACHE_DIR / "vinopop.lock"
FIRST_RUN_MARKER = CONFIG_DIR / ".first-run-done"
FIRST_RUN_MARKERS = (
    FIRST_RUN_MARKER,
    CONFIG_DIR / ".default-plugins-seeded",
    CONFIG_DIR / ".default-recipes-seeded",
)
LOG_MAX_BYTES = 512_000
LOG_BACKUP_COUNT = 3
TRAY_POLL_MS = 200
PLUGIN_RELOAD_DEBOUNCE_MS = 400
WELCOME_DELAY_MS = 800
ELECTRON_PASTE_COMMIT_S = 0.08
ANCHOR_TO_POINTER = None
UNKNOWN_PID = "unknown"

# The kernel drops the flock when this fd closes, so it stays open for good.
_lock_fd: int | None = None


def _lock_holder_pid() -> str:
    try:
        with open(LOCK_FILE, encoding="utf-8") as f:
            return f.read().strip() or UNKNOWN_PID
    except OSError:
        return UNKNOWN_PID


def _acquire_single_instance_lock() -> None:
    """Refuse to start a second copy: open the running one's Settings
    instead, or say it's running when it doesn't answer (pinned by
    `tests/test_single_instance.py::test_second_copy_opens_settings_in_the_running_one`,
    `tests/test_single_instance.py::test_second_copy_notifies_when_the_running_one_does_not_answer`).
    A flock dies with its process, so a crash leaves no stale lock."""
    global _lock_fd
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        os.close(fd)
        from core import remote

        if remote.send("settings") == 0:
            sys.exit(0)
        message = (
            f"vinoPop is already running (PID {_lock_holder_pid()}). "
            "Open the tray icon to use it. To force-restart, kill the "
            "existing process first: pkill -x vinopop"
        )
        print(f"[vinopop] {message}", file=sys.stderr)
        from core import actions

        actions.notify("vinoPop is already running", message, urgency="normal")
        sys.exit(0)
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    _lock_fd = fd


log = logging.getLogger("vinopop")

_ocr_busy = threading.Lock()
_popup_prep = threading.Lock()


def _make_rotated_logs_private() -> None:
    for i in range(1, LOG_BACKUP_COUNT + 1):
        make_private(LOG_FILE.with_name(f"{LOG_FILE.name}.{i}"))


def _setup_logging(debug: bool) -> None:
    private_dir(CACHE_DIR)
    _make_rotated_logs_private()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    level = logging.DEBUG if debug else logging.INFO

    file_handler = PrivateRotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(level)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    stream_handler.setLevel(level)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def _read_selection(source: str) -> str:
    return get_backend().read_selection(source)


def _pointer_position() -> tuple[int, int]:
    return get_backend().pointer_position()


def _pointer_or_origin() -> tuple[int, int]:
    try:
        return _pointer_position()
    except Exception:
        return 0, 0


def _log_selection(ctype_value: str, text: str) -> None:
    """The selected text itself only at --debug: it can be a password (pinned
    by `tests/test_core_main.py::test_info_logs_only_the_selection_length`,
    `tests/test_core_main.py::test_debug_logs_the_selected_text`)."""
    log.info("[%s] %d-char selection", ctype_value, len(text))
    log.debug("[%s] %r", ctype_value, text[:60].replace("\n", "↵"))


def _anchor_rect(settings):
    """The selection's rectangle, or ANCHOR_TO_POINTER when the setting is
    off, AT-SPI is off or the app reports no selection geometry."""
    if not bool(settings.get("popup_anchor_to_selection")):
        return ANCHOR_TO_POINTER
    try:
        return focused_selection_rect()
    except Exception as exc:  # noqa: BLE001
        log.info("[anchor] selection-rect lookup failed: %s", exc)
        return ANCHOR_TO_POINTER


def _paste_and_enter_submit_key() -> str:
    """Which key submits in the focused app, by the Paste & Enter plugin's rule."""
    try:
        import importlib

        ea = (
            importlib.import_module("vinopop_user_editing_actions")
            if "vinopop_user_editing_actions" in sys.modules
            else importlib.import_module("editing_actions")
        )
        return ea._submit_keystroke_for_focus()
    except Exception:
        return "Return"


def _freeze_startup_objects() -> bool:
    """The startup object graph lives as long as the daemon: stop re-scanning it."""
    import gc

    gc.collect()
    gc.freeze()
    return GLib.SOURCE_REMOVE


def _reset_first_run() -> None:
    for marker in FIRST_RUN_MARKERS:
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass


def _once(fn: Callable[[], list[str]]) -> Callable[[], list[str]]:
    """Memoise a no-argument lookup, so one popup costs one KWin round-trip."""
    cache: list[list[str]] = []

    def get() -> list[str]:
        if not cache:
            cache.append(fn())
        return cache[0]

    return get


def _active_window_blocked(
    patterns: list[str], haystacks_fn: Callable[[], list[str]] | None = None
) -> bool:
    """Whether the focused window's title or class contains any block
    pattern, case-insensitively."""

    if not patterns:
        return False
    haystacks = (haystacks_fn or get_backend().active_window_haystacks)()
    if not haystacks:
        return False
    for p in patterns:
        p_lc = (p or "").strip().lower()
        if p_lc and any(p_lc in h for h in haystacks):
            return True
    return False


class App:
    def __init__(self, enable_tray: bool = True) -> None:
        self.settings = get_settings()
        plugin_loader.load_all()

        self.popup = PopupWindow(
            initial_grace_ms=self.settings.get_int("auto_hide_initial_ms"),
            leave_grace_ms=self.settings.get_int("auto_hide_leave_ms"),
            on_open_plugin_order=lambda: self.open_plugins(tab="order"),
        )

        self.min_len = self.settings.get_int("min_selection_length")
        self.ignore_ws = bool(self.settings.get("ignore_whitespace_only"))

        self.watcher: SelectionWatcher | None = None
        self.hotkey = None
        self.clipboard_hotkey = None
        self.ocr_hotkey = None
        self._popup_toggle = HotkeyToggle()
        self.dblclick_watcher = None
        self._reload_pending_id: int | None = None
        self._bound_hotkey: str = ""
        self._bound_clipboard_hotkey: str = ""
        self._bound_ocr_hotkey: str = ""
        self._bound_use_polling: bool = bool(self.settings.get("hotkey_use_polling", False))
        self._watcher_active = False
        self.tray = None
        self.restart_requested = False
        self._settings_dialog = None
        self._plugin_dialog = None
        self._prefetch_thread: threading.Thread | None = None

        if bool(self.settings.get("show_on_selection")):
            self._start_watcher()

        self._start_hotkey()
        self._start_clipboard_hotkey()
        self._start_ocr_hotkey()
        self._start_remote()
        self._maybe_start_dblclick_watcher()
        if enable_tray:
            self._start_tray()
        self._maybe_first_run()
        self._maybe_prefetch_model()

    def _maybe_prefetch_model(self) -> None:
        """Start the background model download, once, if the user said yes
        in onboarding or Settings (pinned by
        `tests/test_prefetch_consent.py::test_fresh_config_does_not_download`,
        `tests/test_prefetch_consent.py::test_yes_from_onboarding_or_settings_downloads`)."""
        if not self.settings.get("ai_prefetch_model"):
            return
        if self._prefetch_thread is not None and self._prefetch_thread.is_alive():
            return
        try:
            from vino_ai import available, models

            if not available()[0] or not models.prefetch_consented(self.settings):
                return

            def _done(model, error):
                body = f"{model.label} is ready." if error is None else f"Download failed: {error}"
                _notify("AI model", body)

            self._prefetch_thread = models.prefetch(_done)
        except Exception:
            log.exception("model prefetch failed to start")

    def _off_main(self, fn: Callable[..., None], *args: object, drop_if_busy: bool = False) -> None:
        """Run fn(*args) on a worker thread, one popup lookup at a time, since
        xclip, KWin, Xlib and AT-SPI can each block for seconds. drop_if_busy
        ignores the call while another lookup runs, so autorepeat can't queue
        popups (pinned by
        `tests/test_daemon_lifecycle.py::test_popup_hotkey_runs_off_main_and_drops_presses_while_busy`)."""
        if drop_if_busy and not _popup_prep.acquire(blocking=False):
            log.info("[popup] previous trigger still being handled - ignoring")
            return

        def _run() -> None:
            if not drop_if_busy:
                _popup_prep.acquire()
            try:
                fn(*args)
            except Exception:
                log.exception("[popup] preparing the popup failed")
            finally:
                _popup_prep.release()

        threading.Thread(target=_run, daemon=True, name="vinopop-popup-prep").start()

    def _show_for_text(self, text: str, x: int, y: int) -> None:
        """Show the selection popup for text. Safe from any thread."""
        self._off_main(self._prepare_popup, text, x, y)

    def _prepare_popup(self, text: str, x: int, y: int, via_hotkey: bool = False) -> None:
        """Worker thread: filters and probes, then shows on the GTK thread,
        with one active-window lookup for the blocklist and the read-only
        check (pinned by
        `tests/test_daemon_lifecycle.py::test_prepare_popup_shows_on_the_gtk_thread_with_one_window_lookup`).
        The minimum length is the watcher's filter, not this one's (pinned by
        `tests/test_min_selection.py::test_the_hotkey_shows_a_selection_shorter_than_the_minimum`)."""
        if not text:
            log.info("[show-for] suppressed -- empty text")
            return
        if self.ignore_ws and not text.strip():
            log.info("[show-for] suppressed -- whitespace-only")
            return
        haystacks = _once(get_backend().active_window_haystacks)
        patterns = list(self.settings.get("blocklist_patterns") or [])
        if _active_window_blocked(patterns, haystacks):
            log.info("[blocked] suppressed popup -- active window matches blocklist")
            return
        ctype = classify(text)
        _log_selection(ctype.value, text)
        extra_ro = tuple(self.settings.get("readonly_app_classes") or [])
        editable = is_focus_editable(extra_readonly_classes=extra_ro, haystacks=haystacks)
        rect = _anchor_rect(self.settings)

        def _show() -> bool:
            self._note_use()
            self.popup.show_for(
                text, x, y, ctype, editable=editable, rect=rect, via_hotkey=via_hotkey
            )
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_show)

    def _on_popup_hotkey(self) -> None:
        """The popup hotkey, on the GTK thread: closes the popup when it's
        showing, opens it otherwise, and ignores autorepeat (pinned by
        `tests/test_daemon_lifecycle.py::test_popup_hotkey_closes_a_showing_popup`,
        `tests/test_daemon_lifecycle.py::test_popup_hotkey_opens_a_hidden_popup`,
        `tests/test_daemon_lifecycle.py::test_a_held_popup_hotkey_keeps_the_popup_it_opened`)."""
        popup = self.popup
        action = self._popup_toggle.press(popup.win.get_visible(), popup.hotkey_shown_at)
        if action == CLOSE:
            log.info("[hotkey-fire] popup showing - closing it")
            popup.hide()
        elif action == OPEN:
            self.show_popup_now(via_hotkey=True)
        else:
            log.info("[hotkey-fire] ignored: key still held")

    def show_popup_now(self, via_hotkey: bool = False) -> None:
        """Hotkey / tray entry point. Returns at once; the lookup runs on a
        worker and presses that arrive meanwhile are dropped."""
        self._off_main(self._gather_hotkey_popup, via_hotkey, drop_if_busy=True)

    def _gather_hotkey_popup(self, via_hotkey: bool = False) -> None:
        t0 = time.monotonic()
        source = self.settings.get("hotkey_source") or "primary"
        text = _read_selection(source)
        log.info(
            "[hotkey-fire] read %s: %d chars in %.0f ms",
            source,
            len(text),
            (time.monotonic() - t0) * 1000,
        )
        x, y = _pointer_or_origin()
        if not text:
            log.info("[hotkey-fire] no selection - showing paste menu")
            self._prepare_no_selection_popup(x, y, via_hotkey)
            return
        log.info("[hotkey-fire] showing popup at (%d, %d) for %d-char selection", x, y, len(text))
        self._prepare_popup(text, x, y, via_hotkey)

    def _prepare_no_selection_popup(self, x: int, y: int, via_hotkey: bool = False) -> None:
        """Worker thread: blocklist check, then the edit menu on the GTK thread."""
        if _active_window_blocked(list(self.settings.get("blocklist_patterns") or [])):
            log.info("[blocked] suppressed no-selection popup -- active window blocked")
            return
        GLib.idle_add(self._show_no_selection_popup, x, y, via_hotkey)

    def _show_no_selection_popup(self, x: int, y: int, via_hotkey: bool = False) -> bool:
        """The edit menu for an editable field with nothing selected, on the
        GTK thread. "Paste from history" hops back to the GTK thread from the
        popup's worker (pinned by
        `tests/test_daemon_lifecycle.py::test_paste_from_history_goes_through_idle_add`)."""

        def _send_keys(combo: str) -> Callable[[], None]:
            def _fire() -> None:
                get_backend().send_key(combo)

            return _fire

        def _paste_then_enter() -> None:
            submit_key = _paste_and_enter_submit_key()
            get_backend().paste()
            time.sleep(ELECTRON_PASTE_COMMIT_S)
            get_backend().send_key(submit_key)

        items: list[tuple[str, str, Callable[[], None]]] = [
            ("edit-paste-symbolic", "Paste", _send_keys("ctrl+v")),
            ("go-jump-symbolic", "Paste & Enter", _paste_then_enter),
        ]
        if bool(self.settings.get("clipboard_history_enabled", True)):
            items.append(
                (
                    "vinopop-clipboard-symbolic",
                    "Paste from history",
                    lambda: GLib.idle_add(self._on_clipboard_hotkey),
                )
            )
        items += [
            ("edit-select-all-symbolic", "Select all", _send_keys("ctrl+a")),
            ("edit-clear-symbolic", "Backspace", _send_keys("BackSpace")),
        ]
        self._note_use()
        self.popup.show_actions(items, x, y, via_hotkey=via_hotkey)
        return GLib.SOURCE_REMOVE

    def _maybe_start_dblclick_watcher(self) -> None:
        """Start or stop the watcher to match double_click_popup_enabled. Idempotent."""
        enabled = bool(self.settings.get("double_click_popup_enabled", False))
        if enabled:
            if self.dblclick_watcher is None:
                self.dblclick_watcher = get_backend().make_double_click_watcher(
                    self._on_global_double_click
                )
            if self.dblclick_watcher is not None:
                self.dblclick_watcher.start()
        elif self.dblclick_watcher is not None:
            self.dblclick_watcher.stop()
            self.dblclick_watcher = None

    def _on_global_double_click(self, x: int, y: int) -> None:
        """A double-click in an editable field pops the edit menu. The watcher
        has already dropped double-clicks that selected a word."""
        self._off_main(self._prepare_double_click_popup, x, y)

    def _prepare_double_click_popup(self, x: int, y: int) -> None:
        try:
            if not is_focus_editable():
                return
        except Exception:
            return
        self._prepare_no_selection_popup(x, y)

    def _start_watcher(self) -> None:
        if self.watcher is not None:
            return

        def on_selection(text: str, x: int, y: int) -> None:
            if not text:
                return
            if self._below_min_selection(text):
                log.info("[watcher] skipping short selection (%d < %d)", len(text), self.min_len)
                return
            self._show_for_text(text, x, y)

        self.watcher = get_backend().make_selection_watcher(
            on_selection,
            self.settings.get_int("selection_debounce_ms"),
        )
        self.watcher.start()
        self._watcher_active = True
        log.info("selection watcher started")

    def _below_min_selection(self, text: str) -> bool:
        """The auto-popup's opt-in filter for very short selections (pinned by
        `tests/test_min_selection.py::test_the_watcher_skips_a_selection_one_short_of_the_minimum`,
        `tests/test_min_selection.py::test_the_watcher_shows_a_selection_at_the_minimum`,
        `tests/test_min_selection.py::test_the_filter_is_off_until_enabled`)."""
        return bool(self.settings.get("min_selection_length_enabled")) and len(text) < self.min_len

    def _stop_watcher(self) -> None:
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
        self._watcher_active = False
        log.info("selection watcher stopped")

    def toggle_watcher(self, active: bool) -> None:
        if active and self.watcher is None:
            self._start_watcher()
        elif not active and self.watcher is not None:
            self._stop_watcher()
        self.settings.set("show_on_selection", bool(active))
        self.settings.save()

    def _sync_watcher_with_settings(self) -> None:
        """Start or stop the watcher to match show_on_selection, so the
        Settings switch applies without a restart, and bring the tray's
        checkbox along with it."""
        want = bool(self.settings.get("show_on_selection"))
        if want and self.watcher is None:
            self._start_watcher()
        elif not want and self.watcher is not None:
            self._stop_watcher()
        if self.tray is not None:
            try:
                self.tray.refresh()
            except Exception:
                pass

    def watcher_active(self) -> bool:
        return self._watcher_active

    def _start_hotkey(self) -> None:
        hotkey_str = (self.settings.get("hotkey") or "").strip()
        if not hotkey_str:
            log.info("hotkey disabled in settings")
            self._bound_hotkey = ""
            return
        use_polling = bool(self.settings.get("hotkey_use_polling", False))
        self.hotkey = get_backend().make_hotkey(
            hotkey_str, self._on_popup_hotkey, use_polling=use_polling, role="popup"
        )
        self.hotkey.start()
        self._bound_hotkey = hotkey_str

    def _start_remote(self) -> None:
        """`vinopop --trigger ...` from a shortcut set in the desktop's own
        settings (core/remote.py). Calls arrive on the GTK thread, like a
        KGlobalAccel press."""
        from core import remote

        remote.serve(
            {
                "popup": self._on_popup_hotkey,
                "ocr": self._on_ocr_hotkey,
                "clipboard": self._on_clipboard_hotkey,
                "settings": self.open_settings,
            }
        )

    def _start_ocr_hotkey(self) -> None:
        hotkey_str = (self.settings.get("ocr_hotkey") or "").strip()
        if not hotkey_str:
            return
        try:
            from capture.screen_ocr import is_supported

            ok, reason = is_supported()
            if not ok:
                log.info("ocr hotkey not bound: %s", reason)
                return
        except Exception:
            log.exception("ocr support probe failed")
            return
        use_polling = bool(self.settings.get("hotkey_use_polling", False))
        self.ocr_hotkey = get_backend().make_hotkey(
            hotkey_str, self._on_ocr_hotkey, use_polling=use_polling, role="ocr"
        )
        self.ocr_hotkey.start()
        self._bound_ocr_hotkey = hotkey_str
        log.info("[ocr] hotkey '%s' bound (polling=%s)", hotkey_str, use_polling)

    def _on_ocr_hotkey(self) -> None:
        """One capture at a time (pinned by
        `tests/test_daemon_lifecycle.py::test_ocr_hotkey_drops_triggers_while_a_capture_runs`,
        `tests/test_daemon_lifecycle.py::test_the_next_ocr_press_works_once_the_capture_is_done`,
        `tests/test_daemon_lifecycle.py::test_ocr_lock_released_when_the_capture_raises`)."""
        self._note_use()
        if not _ocr_busy.acquire(blocking=False):
            log.info("[ocr] capture already running - ignoring trigger")
            return

        def _capture() -> None:
            try:
                from capture.screen_ocr import run_ocr_to_clipboard

                run_ocr_to_clipboard()
            except Exception:
                log.exception("[ocr] capture failed")
            finally:
                _ocr_busy.release()

        try:
            threading.Thread(target=_capture, daemon=True, name="ocr-capture").start()
        except Exception:
            _ocr_busy.release()
            raise

    def _start_clipboard_hotkey(self) -> None:
        if not bool(self.settings.get("clipboard_history_enabled", True)):
            log.info("clipboard plugin disabled - not binding clipboard hotkey")
            self._bound_clipboard_hotkey = ""
            return
        hotkey_str = (self.settings.get("clipboard_hotkey") or "").strip()
        if not hotkey_str:
            log.info("clipboard hotkey disabled in settings")
            self._bound_clipboard_hotkey = ""
            return
        use_polling = bool(self.settings.get("hotkey_use_polling", False))
        self.clipboard_hotkey = get_backend().make_hotkey(
            hotkey_str, self._on_clipboard_hotkey, use_polling=use_polling, role="clipboard"
        )
        self.clipboard_hotkey.start()
        self._bound_clipboard_hotkey = hotkey_str

    def _on_clipboard_hotkey(self) -> None:
        """Open the Clipboard plugin's picker, noting the focused window
        before the picker takes focus so a paste lands back there."""
        self._note_use()
        mod = sys.modules.get("vinopop_user_clipboard_history")
        if mod is None or not hasattr(mod, "open_picker"):
            self._off_main(self._clipboard_fallback)
            return
        target = mod._get_active_window()
        mod.open_picker(target)

    def _clipboard_fallback(self) -> None:
        """No Clipboard plugin: pop up the clipboard text instead (worker)."""
        text = _read_selection("clipboard")
        if not text:
            _notify(
                "Clipboard picker unavailable", "Install the Clipboard plugin to get the picker."
            )
            return
        x, y = _pointer_or_origin()
        self._prepare_popup(text, x, y)

    def _apply_hotkey_settings(self) -> None:
        """Rebind whichever hotkeys changed since they were grabbed. Compares
        against the _bound_* mirrors: the Settings dialog has already changed
        the settings singleton by the time this runs. Polling is X11-only and
        tracked whatever is bound, so a save rebinds nothing it needn't (pinned by
        `tests/test_daemon_lifecycle.py::test_the_first_save_after_turning_polling_on_rebinds_once`,
        `tests/test_daemon_lifecycle.py::test_saves_dont_rebind_when_main_hotkey_is_cleared`,
        `tests/test_daemon_lifecycle.py::test_polling_flip_is_ignored_on_wayland`,
        `tests/test_daemon_lifecycle.py::test_an_unchanged_ocr_hotkey_binds_once_ocr_becomes_usable`)."""
        new_hotkey = (self.settings.get("hotkey") or "").strip()
        new_clip = (self.settings.get("clipboard_hotkey") or "").strip()
        new_polling = bool(self.settings.get("hotkey_use_polling", False))
        polling_changed = get_backend().name == "x11" and new_polling != self._bound_use_polling
        if polling_changed:
            log.info(
                "hotkey polling mode changed: %s → %s - rebuilding the hotkey threads",
                self._bound_use_polling,
                new_polling,
            )
        self._bound_use_polling = new_polling
        if new_hotkey != self._bound_hotkey or polling_changed:
            log.info(
                "hotkey: %r → %r (polling=%s) - rebinding",
                self._bound_hotkey,
                new_hotkey,
                new_polling,
            )
            if self.hotkey is not None:
                self.hotkey.stop()
                self.hotkey = None
            self._bound_hotkey = ""
            if new_hotkey:
                self._start_hotkey()
        if new_clip != self._bound_clipboard_hotkey or polling_changed:
            log.info(
                "clipboard hotkey: %r → %r (polling=%s) - rebinding",
                self._bound_clipboard_hotkey,
                new_clip,
                new_polling,
            )
            if self.clipboard_hotkey is not None:
                self.clipboard_hotkey.stop()
                self.clipboard_hotkey = None
            self._bound_clipboard_hotkey = ""
            if new_clip:
                self._start_clipboard_hotkey()
        new_ocr = (self.settings.get("ocr_hotkey") or "").strip()
        if (
            new_ocr != self._bound_ocr_hotkey
            or polling_changed
            or (new_ocr and self.ocr_hotkey is None)
        ):
            if self.ocr_hotkey is not None:
                self.ocr_hotkey.stop()
                self.ocr_hotkey = None
            self._bound_ocr_hotkey = ""
            if new_ocr:
                self._start_ocr_hotkey()

    def _poll_tray(self) -> bool:
        if self.tray is not None:
            self.tray.poll()
        return GLib.SOURCE_CONTINUE

    def _start_tray(self) -> None:
        from tray.tray import Tray

        self.tray = Tray(
            on_toggle_watcher=self.toggle_watcher,
            get_watcher_active=self.watcher_active,
            on_show_popup_now=self.show_popup_now,
            on_open_settings=self.open_settings,
            on_open_plugins=self.open_plugins,
            on_open_about=self.open_about,
            on_quit=self.quit,
            get_hide_after_minutes=lambda: self.settings.get_int("tray_idle_minutes"),
        )
        GLib.timeout_add(TRAY_POLL_MS, self._poll_tray)

    def _note_use(self) -> None:
        """Keeps the tray icon in view, or brings it back (tray_idle_minutes)."""
        if self.tray is not None:
            self.tray.note_use()

    def open_settings(self) -> None:
        log.info("opening settings dialog…")
        self._note_use()
        if self._settings_dialog is None:
            try:
                from ui.settings_gui import SettingsDialog
            except Exception:
                log.exception("settings_gui import failed")
                return

            self._settings_dialog = SettingsDialog(
                on_changed=self._apply_saved_settings,
                on_restart=self.restart,
                on_open_plugins=self.open_plugins,
            )
        try:
            self._settings_dialog.show()
        except Exception:
            log.exception("settings dialog crashed")

    def _apply_saved_settings(self) -> None:
        self.settings = get_settings()
        self.min_len = self.settings.get_int("min_selection_length")
        self.ignore_ws = bool(self.settings.get("ignore_whitespace_only"))
        self.popup._initial_grace_ms = self.settings.get_int("auto_hide_initial_ms")
        self.popup._leave_grace_ms = self.settings.get_int("auto_hide_leave_ms")
        if self.tray is not None:
            try:
                self.tray.reload_icon()
            except Exception:
                pass
        self._sync_watcher_with_settings()
        if self.watcher is not None:
            self.watcher.set_debounce_ms(self.settings.get_int("selection_debounce_ms"))
        self._schedule_plugin_reload()
        self._apply_hotkey_settings()
        self._maybe_start_dblclick_watcher()
        self._maybe_prefetch_model()
        log.info("settings reloaded")

    def open_plugins(self, tab: str | None = None) -> None:
        log.info("opening plugin manager…")
        self._note_use()
        if self._plugin_dialog is None:
            try:
                from ui.plugin_manager import PluginManagerDialog
            except Exception:
                log.exception("plugin_manager import failed")
                return

            def on_changed():
                plugin_loader.load_all()
                log.info("plugins reloaded")

            self._plugin_dialog = PluginManagerDialog(on_changed=on_changed)
        try:
            self._plugin_dialog.show(tab=tab)
        except Exception:
            log.exception("plugin manager dialog crashed")

    def open_about(self) -> None:
        about = Adw.AboutDialog()
        about.set_application_name("vinoPop")
        about.set_version(__version__)
        about.set_comments(
            f"A PopClip-inspired floating action popup for Linux ({get_backend().name})."
        )
        about.set_license_type(Gtk.License.MIT_X11)
        about.set_application_icon("vinopop")
        project_url = (
            os.environ.get("VINOPOP_PROJECT_URL") or "https://github.com/karanshukla/vinoPop"
        )
        about.set_website(project_url)
        about.set_issue_url(f"{project_url.rstrip('/')}/issues")
        about.set_developer_name("Karan Shukla")
        about.add_credit_section(
            "Based on LinuxPop by",
            ["Robert Berge https://github.com/GaimsDevSoftware/linuxpop"],
        )
        about.add_link(vino_family.SITE_LABEL, vino_family.SITE)
        for app in vino_family.others("vinoPop"):
            about.add_link(f"{app.name}: {app.blurb}", app.page)
        about.present(None)

    def _maybe_first_run(self) -> None:
        if FIRST_RUN_MARKER.is_file():
            return
        FIRST_RUN_MARKER.parent.mkdir(parents=True, exist_ok=True)
        FIRST_RUN_MARKER.touch()

        def _open_welcome():
            try:
                from ui.onboarding import show_onboarding

                show_onboarding(
                    self.settings,
                    on_open_plugins=self.open_plugins,
                    on_download_model=self._maybe_prefetch_model,
                )
            except Exception:
                log.exception("onboarding failed; falling back to welcome dialog")
                try:
                    from ui.welcome import show_welcome_dialog

                    show_welcome_dialog(
                        self.settings,
                        on_open_plugins=self.open_plugins,
                    )
                    return GLib.SOURCE_REMOVE
                except Exception:
                    log.exception("welcome dialog failed; falling back to notify")
                from core import actions

                actions.notify(
                    "vinoPop is running",
                    "Select text anywhere to see actions. Tray icon has settings & plugins.",
                    timeout_ms=8000,
                )
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(WELCOME_DELAY_MS, _open_welcome)

    def _schedule_plugin_reload(self) -> None:
        """Debounced, so a burst of saves reloads the plugins once."""
        if self._reload_pending_id is not None:
            GLib.source_remove(self._reload_pending_id)

        def _do_reload() -> bool:
            self._reload_pending_id = None
            plugin_loader.load_all()
            return GLib.SOURCE_REMOVE

        self._reload_pending_id = GLib.timeout_add(PLUGIN_RELOAD_DEBOUNCE_MS, _do_reload)

    def quit(self) -> None:
        gtk4_app.quit()

    def restart(self) -> None:
        """Quit, then main() starts vinoPop again in this process."""
        log.info("restarting vinoPop")
        self.restart_requested = True
        self.quit()

    def stop_inputs(self) -> None:
        for source in (
            self.watcher,
            self.hotkey,
            self.clipboard_hotkey,
            self.ocr_hotkey,
            self.dblclick_watcher,
        ):
            if source:
                source.stop()


def _install_signal_handlers(app: App) -> None:
    """Quit through main()'s shutdown path on SIGINT, SIGTERM and SIGHUP:
    without it KGlobalAccel keeps the hotkeys' chords registered (pinned by
    `tests/test_daemon_lifecycle.py::test_sigint_sigterm_sighup_quit_the_app`).
    GLib's handlers, unlike Python's, fire while the main loop sits in C."""

    def _quit(*_args: object) -> bool:
        app.quit()
        return GLib.SOURCE_CONTINUE

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, _quit)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="vinopop",
        description="PopClip-inspired floating action popup for Linux (X11 + KDE Wayland).",
    )
    p.add_argument("--version", action="version", version=f"vinopop {__version__}")
    p.add_argument(
        "--no-tray", action="store_true", help="Run without the tray icon (selection + hotkey only)"
    )
    p.add_argument("--debug", action="store_true", help="Verbose logging")
    p.add_argument(
        "--reset-first-run", action="store_true", help="Force the welcome flow on next launch"
    )
    p.add_argument(
        "--doctor",
        action="store_true",
        help="Check what offline AI needs (OpenVINO, devices, ovfetch, models) and exit",
    )
    p.add_argument("--json", action="store_true", help="With --doctor: machine-readable output")
    p.add_argument(
        "--trigger",
        choices=("popup", "ocr", "clipboard"),
        help="Run that hotkey's action in the running vinoPop and exit, for a shortcut "
        "set in your desktop's settings",
    )
    return p.parse_args(argv)


def _exec_again(entry_point: str) -> None:
    """Restart in this process with the entry point that started it (the
    launcher, or main.py from an old autostart entry). os.open fds are
    close-on-exec, so the new process can take the single-instance lock."""
    os.execv(sys.executable, [sys.executable, entry_point, *sys.argv[1:]])


def main(argv: list[str] | None = None) -> int:
    # Before anything can chdir.
    entry_point = os.path.abspath(sys.argv[0])
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.reset_first_run:
        _reset_first_run()

    _setup_logging(args.debug)
    log.info("[display] %s", _GDK_BACKEND_CHOICE)
    get_backend().check_session()
    # Before GTK init and hotkey grabs, so a second copy exits without
    # disturbing the running one.
    _acquire_single_instance_lock()
    from core import autostart

    if autostart.migrate_from_main_py():
        log.info("[autostart] now starts the vinopop launcher instead of main.py")

    # SIGCHLD stays at its default: SIG_IGN froze the UI
    # (docs/how-it-works.md#threads-and-x11).
    gtk4_app.init()
    theme.install_premium_theme(get_settings().get("theme", "dark") or "dark")

    app = App(enable_tray=not args.no_tray)

    _install_signal_handlers(app)
    log.info(
        "vinoPop %s running (tray=%s, hotkey=%s, watcher=%s)",
        __version__,
        "on" if app.tray else "off",
        app.settings.get("hotkey") or "disabled",
        "on" if app.watcher else "off",
    )
    print("[vinopop] running - Ctrl+C to quit. Logs at " + str(LOG_FILE))
    GLib.idle_add(_freeze_startup_objects)

    gtk4_app.run()
    app.stop_inputs()
    if app.restart_requested:
        if app.tray is not None:
            app.tray.stop()
        logging.shutdown()
        _exec_again(entry_point)
    return 0


if __name__ == "__main__":
    sys.exit(main())
