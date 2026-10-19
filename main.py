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

# `--doctor` runs before GTK is imported so it also works over SSH or
# with a broken display.
if __name__ == "__main__" and "--doctor" in sys.argv[1:]:
    from vino_ai import doctor

    sys.exit(doctor.main(as_json="--json" in sys.argv[1:]))

from platform_backend import detect as detect_backend  # noqa: E402
from platform_backend import is_wayland_session  # noqa: E402


def _notify(title: str, body: str) -> None:
    from core import actions

    actions.notify(title, body, timeout_ms=None)


def _load_layer_shell() -> bool:
    """Load gtk4-layer-shell BEFORE GTK/libwayland-client are loaded (its
    Wayland hooks only work when it comes first in symbol lookup - the
    upstream-documented ctypes trick for Python), then its typelib."""
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
    """Decide where GDK runs, before GTK initialises. GTK 4 dropped
    Gtk.Window.move(), so the popup can only be placed via gtk4-layer-shell
    or as an X11 override-redirect window. Native Wayland is only used with
    the KDE backend (platform_backend.detect()); every other Wayland desktop,
    or KDE without layer-shell, runs the whole process on XWayland.
    Returns a line for the log."""
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

# XInitThreads is intentionally NOT called here. We tried it (since
# multiple threads touch X - watcher, hotkey, popup tick, clipboard
# watcher) but it interacts badly with the python-xlib + PyGObject mix
# in this process: the hotkey threads silently fail to grab keys when
# XInitThreads runs at import time. Python-xlib uses its own per-Display
# socket and Python-level Lock, so it doesn't actually need XInitThreads;
# Gdk/GTK's internal lock plus separate-Display-per-thread is the
# isolation we rely on. If "XIO: fatal IO error" crashes start showing
# up under load, revisit by giving each Xlib-using component its own
# connection (already mostly the case) rather than re-enabling
# XInitThreads.
from plugin_system import plugin_loader  # noqa: E402
from triggers.editable_detect import focused_selection_rect, is_focus_editable  # noqa: E402
from ui import (
    gtk4_app,  # noqa: E402
    theme,  # noqa: E402
)
from ui.popup import PopupWindow  # noqa: E402

if TYPE_CHECKING:
    from triggers.watcher import SelectionWatcher

LOCK_FILE = CACHE_DIR / "vinopop.lock"
FIRST_RUN_MARKER = CONFIG_DIR / ".first-run-done"

# Held for the lifetime of the process; the kernel releases the flock when
# the fd is closed (i.e. when we exit, even ungracefully). Storing it at
# module scope ensures it's not garbage-collected mid-run.
_lock_fd: int | None = None


def _acquire_single_instance_lock() -> None:
    """Refuse to start a second copy. Uses fcntl.flock - robust against
    crashes (kernel releases the lock automatically when the process dies,
    no stale-lockfile cleanup needed)."""
    global _lock_fd
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        os.close(fd)
        # Read the existing PID for the user-facing message
        try:
            with open(LOCK_FILE, encoding="utf-8") as f:
                existing_pid = f.read().strip() or "unknown"
        except OSError:
            existing_pid = "unknown"
        message = (
            f"vinoPop is already running (PID {existing_pid}). "
            "Open the tray icon to use it. To force-restart, kill the "
            "existing process first: pkill -x vinopop"
        )
        print(f"[vinopop] {message}", file=sys.stderr)
        from core import actions

        actions.notify("vinoPop is already running", message, urgency="normal")
        sys.exit(0)
    # Write our PID inside the (now-locked) file for diagnostics
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    _lock_fd = fd


log = logging.getLogger("vinopop")

# One OCR capture at a time (see App._on_ocr_hotkey).
_ocr_busy = threading.Lock()
# One popup lookup at a time (see App._off_main).
_popup_prep = threading.Lock()


def _setup_logging(debug: bool) -> None:
    private_dir(CACHE_DIR)
    # Logs older versions wrote with the default umask.
    for i in range(1, 4):
        make_private(LOG_FILE.with_name(f"{LOG_FILE.name}.{i}"))
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    level = logging.DEBUG if debug else logging.INFO

    file_handler = PrivateRotatingFileHandler(
        LOG_FILE, maxBytes=512_000, backupCount=3, encoding="utf-8"
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
    """Return True if the focused window's title or class matches any of the
    user's block patterns (case-insensitive substring). The backend supplies
    the haystacks (X11: xdotool+xprop; KDE Wayland: KWin script)."""
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
        # Optional global double-click watcher. Created lazily when the
        # double_click_popup_enabled setting is on - PopClip-style
        # click-in-text-field popup.
        self.dblclick_watcher = None
        # Debounce id for plugin reloads triggered by settings saves -
        # prevents a load_all storm when many keys change in quick
        # succession (textarea editing, bulk toggles).
        self._reload_pending_id: int | None = None
        # Track what's actually grabbed right now, independent of the
        # settings singleton. The on_changed callback fires AFTER
        # settings_gui has already mutated the singleton, so comparing
        # "new setting" vs "current singleton" never sees a diff. These
        # mirrors are the source of truth for the live-rebind diff.
        self._bound_hotkey: str = ""
        self._bound_clipboard_hotkey: str = ""
        self._bound_ocr_hotkey: str = ""
        # Same mirror for the polling toggle - flipping it has to
        # rebuild both hotkey threads (poll vs grab is decided at
        # thread start, not per-event).
        self._bound_use_polling: bool = bool(self.settings.get("hotkey_use_polling", False))
        self._watcher_active = False
        self.tray = None
        self.restart_requested = False
        # Single-instance dialogs: created lazily, reused on subsequent opens
        self._settings_dialog = None
        self._plugin_dialog = None
        # Background model download, so a second start (Settings switch,
        # onboarding) doesn't stack another thread on the same download.
        self._prefetch_thread: threading.Thread | None = None

        if bool(self.settings.get("show_on_selection")):
            self._start_watcher()

        self._start_hotkey()
        self._start_clipboard_hotkey()
        self._start_ocr_hotkey()
        self._maybe_start_dblclick_watcher()
        if enable_tray:
            self._start_tray()
        self._maybe_first_run()
        self._maybe_prefetch_model()

    def _maybe_prefetch_model(self) -> None:
        """Start the background model download if the user said yes
        (onboarding or Settings). Without that the first AI click downloads
        it, so nobody gets a ~4 GB pull they didn't ask for."""
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

    # ---- popup invocation ----------------------------------------------------
    #
    # Everything that can block - xclip, the KWin pointer and active-window
    # helpers (up to 3 s each), xdotool/xprop, AT-SPI - runs on a worker
    # thread under _popup_prep. Only the final show goes back to the GTK
    # thread, so a slow KWin round-trip can't freeze the UI.

    def _off_main(self, fn: Callable[..., None], *args: object, drop_if_busy: bool = False) -> None:
        """Run fn(*args) on a worker thread, one popup lookup at a time.
        drop_if_busy (the hotkey) ignores the call while another lookup is
        running, so autorepeat or a triple-press can't queue three popups."""
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

    def _prepare_popup(self, text: str, x: int, y: int) -> None:
        """Worker thread: filters and probes, then shows on the GTK thread."""
        # No min-length check here on purpose - that filter belongs to
        # the watcher (auto-popup on selection), not to this routine.
        # show_popup_now() routes empty text into the no-selection
        # paste menu, but non-empty text of *any* length should still
        # surface the selection popup when the user explicitly fires
        # the hotkey. PopClip works the same way: 'minimum size' is
        # an auto-popup filter, long-press always shows the popup.
        if not text:
            log.info("[show-for] suppressed -- empty text")
            return
        if self.ignore_ws and not text.strip():
            log.info("[show-for] suppressed -- whitespace-only")
            return
        # One active-window lookup shared by the blocklist and the
        # read-only check (on KDE each one is a KWin round-trip).
        haystacks = _once(get_backend().active_window_haystacks)
        # Skip when the active app/site is on the user's blocklist.
        # Checked here (just before the popup would appear) so plugins
        # and the watcher don't have to know about it.
        patterns = list(self.settings.get("blocklist_patterns") or [])
        if _active_window_blocked(patterns, haystacks):
            log.info("[blocked] suppressed popup -- active window matches blocklist")
            return
        ctype = classify(text)
        # What was selected only at --debug: it can be a password or a token.
        log.info("[%s] %d-char selection", ctype.value, len(text))
        log.debug("[%s] %r", ctype.value, text[:60].replace("\n", "↵"))
        # AT-SPI / WM_CLASS probe - drives which plugins are eligible.
        # Done here (not in popup.show_for) so the popup module stays
        # accessibility-agnostic and we can pipe extra user blocklist
        # classes in from settings without circular imports.
        extra_ro = tuple(self.settings.get("readonly_app_classes") or [])
        editable = is_focus_editable(extra_readonly_classes=extra_ro, haystacks=haystacks)
        # Try to anchor the popup to the selected-text rectangle (via
        # AT-SPI screen-coord extents) instead of the mouse pointer. Returns
        # None - and we fall back to (x, y) - whenever AT-SPI is off /
        # unavailable / the app exposes no selection geometry, so this is a
        # zero-regression enhancement. Gated behind popup_anchor_to_selection.
        rect = None
        if bool(self.settings.get("popup_anchor_to_selection")):
            try:
                rect = focused_selection_rect()
            except Exception as exc:  # noqa: BLE001
                log.info("[anchor] selection-rect lookup failed: %s", exc)
                rect = None

        def _show() -> bool:
            self.popup.show_for(text, x, y, ctype, editable=editable, rect=rect)
            return False

        GLib.idle_add(_show)

    def show_popup_now(self) -> None:
        """Hotkey / tray entry point. Returns at once; the lookup runs on a
        worker and presses that arrive meanwhile are dropped."""
        self._off_main(self._gather_hotkey_popup, drop_if_busy=True)

    def _gather_hotkey_popup(self) -> None:
        # Stamped so a "had to press the hotkey 3 times" report comes
        # with breadcrumbs in vinopop.log: did the trigger reach us at
        # all? did xclip return empty? did the popup get suppressed by
        # the min-length / blocklist filters?
        t0 = time.monotonic()
        source = self.settings.get("hotkey_source") or "primary"
        text = _read_selection(source)
        log.info(
            "[hotkey-fire] read %s: %d chars in %.0f ms",
            source,
            len(text),
            (time.monotonic() - t0) * 1000,
        )
        try:
            x, y = _pointer_position()
        except Exception:
            x, y = 0, 0
        if not text:
            # PopClip-style: hotkey without a selection still shows a
            # popup, but populated with paste-oriented entry points
            # (clipboard history, snippets) instead of the usual
            # transforms-and-actions for the selected text. The popup is
            # the single portal; the dedicated clipboard hotkey stays
            # available as a power-user shortcut.
            log.info("[hotkey-fire] no selection - showing paste menu")
            self._prepare_no_selection_popup(x, y)
            return
        log.info("[hotkey-fire] showing popup at (%d, %d) for %d-char selection", x, y, len(text))
        self._prepare_popup(text, x, y)

    def _prepare_no_selection_popup(self, x: int, y: int) -> None:
        """Worker thread: blocklist check, then the edit menu on the GTK thread."""
        if _active_window_blocked(list(self.settings.get("blocklist_patterns") or [])):
            log.info("[blocked] suppressed no-selection popup -- active window blocked")
            return
        GLib.idle_add(self._show_no_selection_popup, x, y)

    def _show_no_selection_popup(self, x: int, y: int) -> bool:
        """Build a PopClip-style edit menu for the no-selection case.

        Shown when the popup hotkey fires while you're sitting in an
        editable field with nothing selected. Mirrors PopClip's "click
        in text field" popup: paste, select-all, backspace, plus the
        clipboard picker as the dedicated entry point for snippets and
        recent items. GTK thread; returns False so idle_add runs it once.
        """

        def _send_keys(combo: str) -> Callable[[], None]:
            def _fire() -> None:
                get_backend().send_key(combo)

            return _fire

        def _paste_then_enter() -> None:
            # Borrow the editing_actions submit-key heuristic so this
            # entry follows the same rule as the regular Paste & Enter
            # plugin: plain Return for terminals / search bars / chat
            # web; Ctrl+Return for Slack / Discord / Teams / Element /
            # Thunderbird where Return inserts a newline.
            try:
                import importlib

                ea = (
                    importlib.import_module("vinopop_user_editing_actions")
                    if "vinopop_user_editing_actions" in sys.modules
                    else importlib.import_module("editing_actions")
                )
                submit_key = ea._submit_keystroke_for_focus()
            except Exception:
                submit_key = "Return"
            get_backend().paste()
            # Brief settle so Electron / React inputs commit the paste's
            # text state before the submit key is interpreted.
            time.sleep(0.08)
            get_backend().send_key(submit_key)

        items: list[tuple[str, str, Callable[[], None]]] = []
        items.append(
            (
                "edit-paste-symbolic",
                "Paste",
                _send_keys("ctrl+v"),
            )
        )
        items.append(
            (
                "mail-send-symbolic",
                "Paste & Enter",
                _paste_then_enter,
            )
        )
        if bool(self.settings.get("clipboard_history_enabled", True)):
            items.append(
                (
                    "vinopop-clipboard-symbolic",
                    "Paste from history",
                    # Popup callbacks run on a worker thread; the picker
                    # and the fallback popup are GTK, so hop back first.
                    lambda: GLib.idle_add(self._on_clipboard_hotkey),
                )
            )
        items.append(
            (
                "edit-select-all-symbolic",
                "Select all",
                _send_keys("ctrl+a"),
            )
        )
        items.append(
            (
                "edit-clear-symbolic",
                "Backspace",
                _send_keys("BackSpace"),
            )
        )
        self.popup.show_actions(items, x, y)
        return False

    def _maybe_start_dblclick_watcher(self) -> None:
        """Honour the double_click_popup_enabled setting. Idempotent.
        Called at startup and again from the settings callback."""
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
        """PopClip-style: double-click inside an empty editable field
        pops the edit menu. The watcher has already compared PRIMARY
        before and after the second click - if a word got selected,
        the watcher dropped the call entirely, so by the time we're
        here we know it's a real "double-click in empty field" gesture.
        We only need to confirm the focused widget is editable."""
        self._off_main(self._prepare_double_click_popup, x, y)

    def _prepare_double_click_popup(self, x: int, y: int) -> None:
        try:
            if not is_focus_editable():
                return
        except Exception:
            return
        self._prepare_no_selection_popup(x, y)

    # ---- watcher -------------------------------------------------------------

    def _start_watcher(self) -> None:
        if self.watcher is not None:
            return

        def on_selection(text: str, x: int, y: int) -> None:
            # Watcher-only filter, opt-in via settings:
            # 'min_selection_length_enabled' gates whether we trim
            # very short selections at all. Default off (PopClip
            # convention - show the popup for any selection).
            # The hotkey path bypasses this entirely; see
            # _show_for_text's docstring.
            if not text:
                return
            if bool(self.settings.get("min_selection_length_enabled")) and len(text) < self.min_len:
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

    # ---- hotkey --------------------------------------------------------------

    def _start_hotkey(self) -> None:
        hotkey_str = (self.settings.get("hotkey") or "").strip()
        if not hotkey_str:
            log.info("hotkey disabled in settings")
            self._bound_hotkey = ""
            return
        use_polling = bool(self.settings.get("hotkey_use_polling", False))
        self.hotkey = get_backend().make_hotkey(
            hotkey_str, self.show_popup_now, use_polling=use_polling, role="popup"
        )
        self.hotkey.start()
        self._bound_hotkey = hotkey_str

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
        # One capture at a time: KWin passes key autorepeat through to
        # KGlobalAccel and the first capture spends seconds importing the
        # OCR stack, so a held chord used to stack three overlays.
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

        # Capture runs on a worker thread - the daemon's main loop must
        # stay responsive while the user is dragging the region.
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
        """Capture the currently focused window BEFORE the picker steals
        focus, then open the picker. Paste-on-select restores focus to
        that window so Ctrl+V lands there.

        plugin_loader gives every user plugin a sys.modules name of the
        form 'vinopop_user_<stem>', so we look up the picker that way.
        """
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
        try:
            x, y = _pointer_position()
        except Exception:
            x, y = 0, 0
        self._prepare_popup(text, x, y)

    def _apply_hotkey_settings(self) -> None:
        """Rebind whichever hotkeys changed since they were grabbed. Compares
        against the _bound_* mirrors, not the settings singleton (settings_gui
        has already mutated it by the time on_changed runs)."""
        new_hotkey = (self.settings.get("hotkey") or "").strip()
        new_clip = (self.settings.get("clipboard_hotkey") or "").strip()
        new_polling = bool(self.settings.get("hotkey_use_polling", False))
        # Polling is an X11-only grab mode; the Wayland backend ignores it, so a
        # flip there must not tear down (and re-register) every shortcut.
        polling_changed = get_backend().name == "x11" and new_polling != self._bound_use_polling
        if polling_changed:
            log.info(
                "hotkey polling mode changed: %s → %s - rebuilding the hotkey threads",
                self._bound_use_polling,
                new_polling,
            )
        # Tracked on its own, not only when the main hotkey binds: with the main
        # hotkey cleared it used to stay False, so every save rebound the rest.
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
        # Rebind the OCR hotkey when it was edited or cleared, and bind it
        # if it just became usable (the user installed the OCR dependencies
        # via the Settings Install button), so neither needs a restart.
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

    # ---- tray + dialogs ------------------------------------------------------

    def _poll_tray(self) -> bool:
        if self.tray is not None:
            self.tray.poll()
        return True  # keep timeout alive

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
        )
        GLib.timeout_add(200, self._poll_tray)

    def open_settings(self) -> None:
        log.info("opening settings dialog…")
        if self._settings_dialog is None:
            try:
                from ui.settings_gui import SettingsDialog
            except Exception:
                log.exception("settings_gui import failed")
                return

            def on_changed():
                # Compare against what's currently *grabbed*, NOT the settings
                # singleton - settings_gui already mutated the singleton
                # before reaching us, so a singleton-vs-singleton diff
                # always reports "no change".
                self.settings = get_settings()
                self.min_len = self.settings.get_int("min_selection_length")
                self.ignore_ws = bool(self.settings.get("ignore_whitespace_only"))
                self.popup._initial_grace_ms = self.settings.get_int("auto_hide_initial_ms")
                self.popup._leave_grace_ms = self.settings.get_int("auto_hide_leave_ms")
                # Live-update the tray icon if the user changed its style.
                if self.tray is not None:
                    try:
                        self.tray.reload_icon()
                    except Exception:
                        pass
                self._sync_watcher_with_settings()
                if self.watcher is not None:
                    self.watcher.set_debounce_ms(self.settings.get_int("selection_debounce_ms"))
                # Reload plugins so settings that gate plugin registration
                # (e.g. ai_services) take effect immediately. Debounced so
                # a burst of saves (textarea editing, multi-toggle bulk
                # changes) only triggers one reload after activity quiets.
                self._schedule_plugin_reload()
                self._apply_hotkey_settings()
                # Live-apply the double-click watcher toggle.
                self._maybe_start_dblclick_watcher()
                # The model download switch starts it now, not at next launch.
                self._maybe_prefetch_model()
                log.info("settings reloaded")

            self._settings_dialog = SettingsDialog(
                on_changed=on_changed,
                on_restart=self.restart,
                on_open_plugins=self.open_plugins,
            )
        try:
            # Subsequent calls just present() the existing window.
            self._settings_dialog.show()
        except Exception:
            log.exception("settings dialog crashed")

    def open_plugins(self, tab: str | None = None) -> None:
        log.info("opening plugin manager…")
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
        # Adw.AboutDialog (libadwaita 1.5); with no parent window it opens as
        # its own window. Non-modal: no GTK 4 equivalent of dialog.run().
        about = Adw.AboutDialog()
        about.set_application_name("vinoPop")
        about.set_version(__version__)
        about.set_comments(
            f"A PopClip-inspired floating action popup for Linux ({get_backend().name})."
        )
        about.set_license_type(Gtk.License.MIT_X11)
        about.set_application_icon("vinopop")
        # Forks can point this at their own repo via VINOPOP_PROJECT_URL.
        project_url = (
            os.environ.get("VINOPOP_PROJECT_URL") or "https://github.com/karanshukla/vinoPop"
        )
        about.set_website(project_url)
        about.set_issue_url(f"{project_url.rstrip('/')}/issues")
        about.set_developer_name("Karan Shukla")
        # vinoPop is a fork of LinuxPop; credit the original project.
        about.add_credit_section(
            "Based on LinuxPop by",
            ["Robert Berge https://github.com/GaimsDevSoftware/linuxpop"],
        )
        about.present(None)

    # ---- first-run experience ------------------------------------------------

    def _maybe_first_run(self) -> None:
        if FIRST_RUN_MARKER.is_file():
            return
        FIRST_RUN_MARKER.parent.mkdir(parents=True, exist_ok=True)
        FIRST_RUN_MARKER.touch()

        # Welcome dialog (one-time): explains usage. Falls back to
        # notify-send if GTK can't construct the dialog for some reason.
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
                    return False
                except Exception:
                    log.exception("welcome dialog failed; falling back to notify")
                from core import actions

                actions.notify(
                    "vinoPop is running",
                    "Select text anywhere to see actions. Tray icon has settings & plugins.",
                    timeout_ms=8000,
                )
            return False

        GLib.timeout_add(800, _open_welcome)

    # ---- lifecycle -----------------------------------------------------------

    def _schedule_plugin_reload(self) -> None:
        if self._reload_pending_id is not None:
            GLib.source_remove(self._reload_pending_id)

        def _do_reload() -> bool:
            self._reload_pending_id = None
            plugin_loader.load_all()
            return False

        self._reload_pending_id = GLib.timeout_add(400, _do_reload)

    def quit(self) -> None:
        gtk4_app.quit()

    def restart(self) -> None:
        """Quit, then main() starts vinoPop again in this process."""
        log.info("restarting vinoPop")
        self.restart_requested = True
        self.quit()


def _install_signal_handlers(app: App) -> None:
    """Quit cleanly on Ctrl+C, `kill`/`pkill` (SIGTERM) and a closed terminal
    or logout (SIGHUP). The shutdown path in main() unregisters the hotkeys;
    skipping it left KGlobalAccel actions registered, still holding the chords.
    GLib-level handlers fire reliably while the main loop sits in C."""

    def _quit(*_args: object) -> bool:
        app.quit()
        return True  # GLib.SOURCE_CONTINUE: a second signal still quits

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, _quit)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="vinopop",
        description="PopClip-inspired floating action popup for Linux (X11 + KDE Wayland).",
    )
    p.add_argument("--version", action="version", version=f"vinoPop {__version__}")
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
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Captured before anything can chdir; the restart below re-runs it.
    entry_point = os.path.abspath(sys.argv[0])
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.reset_first_run:
        # Wipe every first-run marker so the welcome flow AND the
        # plugin/recipe seeders fire again. Useful for testing the
        # curated default bundle from a clean state.
        for marker in (
            FIRST_RUN_MARKER,
            CONFIG_DIR / ".default-plugins-seeded",
            CONFIG_DIR / ".default-recipes-seeded",
        ):
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass

    _setup_logging(args.debug)
    log.info("[display] %s", _GDK_BACKEND_CHOICE)
    get_backend().check_session()
    # Single-instance guard - refuses to start a second copy. Run BEFORE
    # any GTK init or hotkey grabs so the second copy exits before
    # interfering with the existing instance.
    _acquire_single_instance_lock()
    from core import autostart

    if autostart.migrate_from_main_py():
        log.info("[autostart] now starts the vinopop launcher instead of main.py")

    # NOTE: previously we set SIGCHLD = SIG_IGN here as a cheap way to
    # auto-reap subprocess zombies. Reverted because it has been observed
    # to interact badly with subprocess.run() on Python 3.12 in this
    # process layout (background watcher + many short-lived xclip/xdotool
    # calls + GTK main loop), producing intermittent UI freezes when the
    # clipboard picker is opened. Accepting some zombie PIDs over uptime
    # is the lesser evil; a proper periodic reaper can come later.

    # GTK 4 + libadwaita init (opens the display chosen by
    # _choose_gdk_backend). Replaces Gtk.init via `import Gtk` in GTK 3.
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

    # Freeze the startup object graph so the GC never re-scans it. Cheap one-line
    # win for a long-running daemon - recommended by Python gc docs.
    def _freeze_gc_after_startup():
        import gc

        gc.collect()
        gc.freeze()
        return False

    GLib.idle_add(_freeze_gc_after_startup)

    gtk4_app.run()
    if app.watcher:
        app.watcher.stop()
    if app.hotkey:
        app.hotkey.stop()
    if app.clipboard_hotkey:
        app.clipboard_hotkey.stop()
    if app.ocr_hotkey:
        app.ocr_hotkey.stop()
    if app.dblclick_watcher:
        app.dblclick_watcher.stop()
    if app.restart_requested:
        if app.tray is not None:
            app.tray.stop()
        logging.shutdown()
        # The single-instance lock fd is close-on-exec, so the new
        # process can take it.
        # Re-run whichever entry point started us: the vinopop launcher, or
        # main.py from an old autostart entry.
        os.execv(sys.executable, [sys.executable, entry_point, *sys.argv[1:]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
