"""Is the focused widget editable? Cut, Paste and the like are hidden
when it isn't. AT-SPI answers per widget when it is opted into; otherwise a
list of viewer window classes does, and no answer means editable.
docs/how-it-works.md, "Editable detection"."""

from __future__ import annotations

import logging
import threading
import time as _time
from collections.abc import Callable

from platform_backend import get_backend

_log = logging.getLogger("vinopop")

# The widget that last gained focus, from the AT-SPI listener's thread
_focus_cache: dict = {
    "editable": None,  # None until an event arrives
    "timestamp": 0.0,  # monotonic
    "role": "",
}
_focus_cache_lock = threading.Lock()
_focus_listener_started = False
_focus_listener_ref = None  # the C side holds no reference to the listener
_FOCUS_SEARCH_DEPTH = 10
_FALLBACK_LINE_HEIGHT_PX = 24


def _atspi_environment_safe() -> tuple[bool, str]:
    """Whether loading Atspi is safe, and why not. Another user's
    at-spi-bus-launcher can take our query and abort the process (SIGTRAP)."""
    import glob as _glob
    import os as _os

    try:
        runtime_dir = _os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{_os.getuid()}"
        bus_socket = _os.path.join(runtime_dir, "at-spi", "bus_0")
        if not _os.path.exists(bus_socket):
            return False, f"bus socket missing at {bus_socket}"
        if not _os.access(bus_socket, _os.R_OK | _os.W_OK):
            return False, f"bus socket unreadable at {bus_socket}"
        my_uid = _os.getuid()
        for proc_dir in _glob.glob("/proc/[0-9]*"):
            try:
                with open(f"{proc_dir}/comm") as f:
                    name = f.read().strip()
                if not name.startswith("at-spi-bus-launc"):
                    continue
                proc_uid = _os.stat(proc_dir).st_uid
                if proc_uid != my_uid:
                    pid = proc_dir.rsplit("/", 1)[-1]
                    return False, (
                        f"a foreign-uid at-spi-bus-launcher "
                        f"(pid={pid}, uid={proc_uid}) is running - "
                        f"refusing to risk a dbind crash"
                    )
            except (OSError, ValueError):
                continue
        return True, ""
    except Exception as exc:
        return False, f"probe error: {exc}"


_HAS_ATSPI = False
_atspi_ok, _atspi_skip_reason = _atspi_environment_safe()
if _atspi_ok:
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # type: ignore[attr-defined]

        _HAS_ATSPI = True
    except (ImportError, ValueError):
        _log.info("[editable] Atspi gi bindings unavailable - will use WM_CLASS heuristic only")
else:
    _log.info("[editable] AT-SPI skipped (%s) - using WM_CLASS only", _atspi_skip_reason)


# WM_CLASS substrings of apps whose focused widget is almost always read-only
_READONLY_APP_CLASSES = (
    # PDF / document viewers
    "evince",
    "okular",
    "atril",
    "qpdfview",
    "mupdf",
    "xpdf",
    "zathura",
    # Image viewers
    "feh",
    "geeqie",
    "gthumb",
    "eog",
    "gpicview",
    "qiv",
    "nomacs",
    # Media players
    "mpv",
    "vlc",
    "smplayer",
    "totem",
    "celluloid",
    # File managers (renaming in place is the rare exception)
    "nautilus",
    "nemo",
    "caja",
    "thunar",
    "pcmanfm",
    "dolphin",
    # Read-only ebook readers
    "calibre",
    "fbreader",
)


def _on_focus_event(event) -> None:
    """Remember whether the widget that gained focus is editable. Runs on
    the AT-SPI listener's thread."""
    try:
        if event is None or event.source is None:
            return
        # detail1: 1 gained, 0 lost
        if int(getattr(event, "detail1", 0)) != 1:
            return
        source = event.source
        state = source.get_state_set()
        editable = bool(state.contains(Atspi.StateType.EDITABLE))
        try:
            role = source.get_role_name()
        except Exception:
            role = "?"
        with _focus_cache_lock:
            _focus_cache["editable"] = editable
            _focus_cache["timestamp"] = _time.monotonic()
            _focus_cache["role"] = role
        _log.info("[editable] focus-event: role=%s editable=%s", role, editable)
    except Exception as exc:
        _log.info("[editable] focus-event handler error: %s", exc)


def _start_focus_listener() -> None:
    """Register the AT-SPI focus listener, once, for the daemon's lifetime,
    and only with editable_atspi_listener_enabled on."""
    global _focus_listener_started, _focus_listener_ref
    if _focus_listener_started or not _HAS_ATSPI:
        return
    try:
        from core.settings import get_settings

        if not bool(get_settings().get("editable_atspi_listener_enabled")):
            _log.info(
                "[editable] AT-SPI focus listener disabled by setting "
                "(editable_atspi_listener_enabled=false)"
            )
            return
    except Exception as exc:
        _log.info(
            "[editable] could not read AT-SPI listener setting (%s) - leaving listener off", exc
        )
        return
    _focus_listener_started = True

    def runner():
        try:
            try:
                Atspi.init()
            except Exception:
                pass
            listener = Atspi.EventListener.new(_on_focus_event)
            listener.register("object:state-changed:focused")
            global _focus_listener_ref
            _focus_listener_ref = listener
            _log.info("[editable] AT-SPI focus listener registered - starting event loop")
            Atspi.event_main()
        except Exception as exc:
            _log.info("[editable] AT-SPI listener init failed: %s", exc)

    threading.Thread(target=runner, daemon=True, name="vinopop-atspi-listener").start()


def _cached_focus_editable(max_age_s: float = 30.0) -> bool | None:
    """What the last focus event said, or None when there was none within
    `max_age_s`: the user may have switched apps since without an event."""
    with _focus_cache_lock:
        editable = _focus_cache["editable"]
        ts = _focus_cache["timestamp"]
        role = _focus_cache["role"]
    if editable is None:
        return None
    age = _time.monotonic() - ts
    if age > max_age_s:
        return None
    _log.info(
        "[editable] using cached focus event (%.1f s old): role=%s editable=%s", age, role, editable
    )
    return editable


def _find_focused_in(node, max_depth: int = 10):
    """The first accessible under `node` with STATE_FOCUSED, searched
    `max_depth` levels down so a huge tree can't outlast the timeout."""
    if node is None or max_depth <= 0:
        return None
    try:
        state = node.get_state_set()
        if state.contains(Atspi.StateType.FOCUSED):
            return node
        for i in range(node.get_child_count()):
            child = node.get_child_at_index(i)
            found = _find_focused_in(child, max_depth - 1)
            if found is not None:
                return found
    except Exception:
        return None
    return None


def _atspi_opted_in() -> bool:
    try:
        from core.settings import get_settings

        return bool(get_settings().get("editable_atspi_listener_enabled"))
    except Exception:
        return False


def _active_windows(desktop):
    """(app, window) for each window with STATE_ACTIVE, the one in the
    foreground. test_only_the_active_window_is_searched_for_the_focused_widget"""
    for i in range(desktop.get_child_count()):
        try:
            app = desktop.get_child_at_index(i)
            n_win = app.get_child_count() if app is not None else 0
        except Exception:
            continue
        for j in range(n_win):
            try:
                win = app.get_child_at_index(j)
                if win is None or not win.get_state_set().contains(Atspi.StateType.ACTIVE):
                    continue
            except Exception:
                continue
            yield app, win


def _atspi_focus_editable(timeout: float = 0.15) -> bool | None:
    """Whether the focused widget of the active window is editable, or None
    when AT-SPI has no answer within `timeout`."""
    if not _HAS_ATSPI or not _atspi_opted_in():
        return None

    result: list[bool | None] = [None]
    diag: list[str] = []

    def worker() -> None:
        try:
            desktop = Atspi.get_desktop(0)
            if desktop is None:
                diag.append("desktop=None")
                return
            for app, win in _active_windows(desktop):
                focused = _find_focused_in(win, max_depth=_FOCUS_SEARCH_DEPTH)
                if focused is None:
                    # The frame's own state would lie about the input inside it:
                    # test_an_active_window_with_no_focused_widget_gives_no_answer
                    diag.append(
                        f"app={app.get_name()!r} "
                        f"win.role={win.get_role_name()} "
                        f"-- no focused descendant (likely Electron) "
                        f"-- punting to WM_CLASS"
                    )
                    return
                try:
                    fstate = focused.get_state_set()
                    editable = bool(fstate.contains(Atspi.StateType.EDITABLE))
                    result[0] = editable
                    diag.append(
                        f"app={app.get_name()!r} "
                        f"win.role={win.get_role_name()} "
                        f"focused.role={focused.get_role_name()} "
                        f"editable={editable}"
                    )
                except Exception as exc:
                    diag.append(f"state-fetch failed: {exc}")
                return
            diag.append(f"no active window across {desktop.get_child_count()} apps")
        except Exception as exc:
            diag.append(f"atspi error: {exc}")

    t = threading.Thread(target=worker, daemon=True, name="vinopop-atspi-probe")
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        _log.info("[editable] AT-SPI probe timed out (>%.0f ms)", timeout * 1000)
        return None
    if diag:
        _log.info("[editable] AT-SPI: %s", " | ".join(diag))
    return result[0]


def _selection_rect_of(acc) -> tuple[int, int, int, int] | None:
    """Screen rectangle (x, y, w, h) of the text selected in an accessible,
    or None: no Text interface, nothing selected, or a rectangle no toolkit
    could mean."""
    try:
        n_sel = Atspi.Text.get_n_selections(acc)
    except Exception:
        return None
    if not n_sel or n_sel <= 0:
        return None
    try:
        rng = Atspi.Text.get_selection(acc, 0)
        start, end = rng.start_offset, rng.end_offset
    except Exception:
        return None
    if end <= start:
        return None
    try:
        r = Atspi.Text.get_range_extents(acc, start, end, Atspi.CoordType.SCREEN)
    except Exception:
        return None
    if r is None:
        return None
    x, y, w, h = int(r.x), int(r.y), int(r.width), int(r.height)
    if w <= 0:
        return None
    if h <= 0:  # KTextEditor (Kate, KWrite) reports 0-height extents
        h = _FALLBACK_LINE_HEIGHT_PX
    if w > 100_000 or h > 100_000 or x < -50_000 or y < -50_000:
        return None
    return (x, y, w, h)


def focused_selection_rect(timeout: float = 0.15) -> tuple[int, int, int, int] | None:
    """Screen rectangle (x, y, width, height) of the text selected in the
    focused widget, or None when AT-SPI is off, too slow or has no answer."""
    if not _HAS_ATSPI or not _atspi_opted_in():
        return None

    result: list[tuple[int, int, int, int] | None] = [None]

    def worker() -> None:
        try:
            desktop = Atspi.get_desktop(0)
            if desktop is None:
                return
            for _app, win in _active_windows(desktop):
                focused = _find_focused_in(win, max_depth=_FOCUS_SEARCH_DEPTH)
                if focused is not None:
                    result[0] = _selection_rect_of(focused)
                return
        except Exception:
            return

    t = threading.Thread(target=worker, daemon=True, name="vinopop-atspi-selrect")
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        _log.info("[anchor] AT-SPI selection-rect probe timed out (>%.0f ms)", timeout * 1000)
        return None
    if result[0] is not None:
        _log.info("[anchor] AT-SPI selection rect (screen px): %r", result[0])
    return result[0]


def _window_class(haystacks: list[str]) -> str:
    """The focused window's class from the backend's haystacks, or ''. Never
    the title: test_a_title_that_contains_a_viewers_name_is_not_a_viewer.
    X11 gives [title, xprop's WM_CLASS line], KDE [resourceClass, caption]."""
    for h in haystacks:
        if h.startswith("wm_class"):
            return h
    if get_backend().name == "x11":
        return ""  # xprop gave no WM_CLASS; the remaining entry is the title
    return haystacks[0] if haystacks else ""


def is_focus_editable(
    extra_readonly_classes: tuple[str, ...] = (),
    haystacks: Callable[[], list[str]] | None = None,
) -> bool:
    """True if the focused widget accepts edits. Tried in order: a recent
    focus event, the AT-SPI walk, the window-class list, and True.
    `haystacks` lets the caller share one active-window lookup."""
    _start_focus_listener()

    cached = _cached_focus_editable()
    if cached is not None:
        return cached

    atspi_answer = _atspi_focus_editable()
    if atspi_answer is not None:
        _log.info("[editable] AT-SPI tree-walk authoritative: editable=%s", atspi_answer)
        return atspi_answer

    try:
        wm = _window_class((haystacks or get_backend().active_window_haystacks)())
    except Exception as exc:  # noqa: BLE001
        _log.info("[editable] active-window lookup failed: %s", exc)
        wm = ""
    blocklist = _READONLY_APP_CLASSES + tuple(c.lower() for c in extra_readonly_classes)
    if not wm:
        _log.info("[editable] no WM_CLASS + no AT-SPI signal - defaulting to True")
        return True
    for needle in blocklist:
        if needle in wm:
            _log.info(
                "[editable] WM_CLASS=%r matched read-only entry %r "
                "(AT-SPI silent) - hiding edit-only plugins",
                wm,
                needle,
            )
            return False
    _log.info(
        "[editable] WM_CLASS=%r (AT-SPI silent, no cached event) - treating as editable=True", wm
    )
    return True
