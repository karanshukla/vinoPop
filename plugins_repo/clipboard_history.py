"""Clipboard history + snippets picker.

Two backing stores:
  - history.json   : rolling buffer of the last N clipboard entries
  - snippets.json  : pinned/named entries that never expire

The picker dialog has Recent + Snippets tabs, a search box, and pastes
the chosen entry at the cursor of whichever app had focus when the
picker was summoned (via xdotool windowactivate + ctrl+v).

Activated either from the popup ("Clipboard…" plugin button) or from
the global clipboard_hotkey (default Super+Shift+V).

Settings (~/.config/vinopop/settings.json):
  "clipboard_history_size":   25
  "clipboard_history_images": true
  "clipboard_history_max_text_kb": 1024    (bigger copies aren't kept)
  "clipboard_history_max_image_mb": 20
  "clipboard_hotkey":         "super+shift+v"   (handled by main.py)
"""

from __future__ import annotations

import json
import os
import re
import select as _select
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Graphene, Gtk, Pango  # noqa: E402

try:
    gi.require_version("GdkX11", "4.0")
    from gi.repository import GdkX11  # noqa: E402
except (ImportError, ValueError):
    GdkX11 = None


def _x11_surface(window):
    """The window's GdkX11.X11Surface, or None off X11 / before realize."""
    if GdkX11 is None:
        return None
    try:
        surface = window.get_surface()
    except Exception:
        return None
    if surface is not None and isinstance(surface, GdkX11.X11Surface):
        return surface
    return None


def _apply_x11_hints(window) -> None:
    """X11-only window-manager hints GTK 4 no longer exposes on Gtk.Window:
    skip taskbar / pager and keep-above. Skip hints go through GdkX11;
    keep-above has no GDK API left, so it's an EWMH request via wmctrl
    when that's installed (best-effort - the picker still works without)."""
    surface = _x11_surface(window)
    if surface is None:
        return
    try:
        surface.set_skip_taskbar_hint(True)
        surface.set_skip_pager_hint(True)
    except Exception:
        pass
    if not shutil.which("wmctrl"):
        return
    try:
        hex_id = f"0x{int(surface.get_xid()):08x}"
    except Exception:
        return

    def _above() -> None:
        try:
            subprocess.run(
                ["wmctrl", "-i", "-r", hex_id, "-b", "add,above"],
                check=False,
                capture_output=True,
                timeout=1.0,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    # Off the main thread - wmctrl round-trips to the WM.
    threading.Thread(target=_above, daemon=True, name="clipboard-above").start()


def _force_to_front(window) -> None:
    """Raise the picker above every other window on X11, including any
    Settings / Plugin Manager dialog already open. Cinnamon's WM ranks
    windows by focus-history first, keep_above second; without an
    explicit X11 raise the picker can be marked keep_above yet still
    appear behind a more-recently-focused vinoPop dialog.

    The fix is present() plus an explicit toplevel focus request with
    the X server time, after the keep-above hint is in place, which
    together force a proper restack.
    """
    try:
        window.present()
        surface = _x11_surface(window)
        if surface is not None:
            # Permanent keep-above while the picker is visible. Cleared
            # implicitly when the window is destroyed.
            _apply_x11_hints(window)
            try:
                ts = GdkX11.x11_get_server_time(surface)
            except Exception:
                ts = 0
            # Demand keyboard focus with a real timestamp so it survives
            # focus-stealing prevention.
            try:
                surface.focus(ts)
            except Exception:
                pass
    except Exception:
        try:
            window.present()
        except Exception:
            pass


from plugin_base import Plugin

try:
    from settings import get_settings

    _settings = get_settings()
except Exception:
    _settings = None


def _cfg(key: str, default):
    if _settings is None:
        return default
    val = _settings.get(key, None)
    return val if val is not None else default


# Live setters - read on each call so slider changes / settings.json
# edits take effect without a daemon restart. The old module-level
# snapshot froze these at import time.
def _history_size() -> int:
    return max(1, int(_cfg("clipboard_history_size", 25)))


def _capture_images() -> bool:
    return bool(_cfg("clipboard_history_images", True))


def _poll_interval() -> float:
    return max(0.1, float(_cfg("clipboard_poll_interval", 0.5)))


def _max_text_bytes() -> int:
    return max(1, int(_cfg("clipboard_history_max_text_kb", 1024))) * 1024


def _max_image_bytes() -> int:
    return max(1, int(_cfg("clipboard_history_max_image_mb", 20))) * 1024 * 1024


# Kept for backwards compat with the startup log line. These constants
# only reflect the value at import; treat them as informational, not
# authoritative.
HISTORY_SIZE = _history_size()
CAPTURE_IMAGES = _capture_images()
POLL_INTERVAL = _poll_interval()

from xdg_paths import CACHE_DIR as _LP_CACHE
from xdg_paths import CONFIG_DIR as _LP_CONFIG
from xdg_paths import make_private, open_private, private_dir

CACHE_DIR = _LP_CACHE / "clipboard"
HISTORY_FILE = CACHE_DIR / "history.json"
SNIPPETS_FILE = _LP_CONFIG / "snippets.json"
IMAGES_DIR = CACHE_DIR / "images"


# ----- data --------------------------------------------------------------


@dataclass
class Entry:
    id: str
    timestamp: float
    kind: str  # "text" | "image"
    text: str = ""
    image_path: str = ""
    name: str = ""  # only used for snippets
    # Snippet auto-expansion shortcode(s). Comma-separated to allow more
    # than one trigger per snippet (";email, ;e, em" all -> same text).
    # Single-trigger entries from older saves load as-is because the
    # parser strips whitespace and ignores empty parts.
    trigger: str = ""
    # Count of times this snippet has been pasted - via picker click,
    # trigger expansion, or programmatic paste. Persisted in snippets.json
    # so it survives daemon restarts. Defaults to 0 on older files.
    usage_count: int = 0
    # Free-text category for grouping in the picker (e.g. "Email",
    # "Code", "Personal"). Empty = "General" bucket. Keeps the picker
    # navigable once you have 20+ snippets.
    category: str = ""

    def trigger_list(self) -> list[str]:
        """Parse `trigger` into a list of clean shortcodes."""
        return [t.strip() for t in self.trigger.split(",") if t.strip()]

    def preview(self, max_len: int = 80) -> str:
        if self.name:
            return self.name
        if self.kind == "image":
            return f"🖼  Image - {Path(self.image_path).name}"
        # Slice before replacing: the picker builds a row per entry, and a
        # long copy shouldn't be walked in full for an 80-char label.
        s = self.text[: max_len + 1].replace("\n", "↵").replace("\t", "  ")
        return s[:max_len] + "…" if len(s) > max_len else s

    def search_haystack(self) -> str:
        # The image path is a home folder plus a hex id, so searching for
        # "home", the username or "a1" would match every image.
        body = "image" if self.kind == "image" else self.text
        src = (self.name, self.trigger, body)
        # Cached per entry because the picker filter calls this for every
        # row on every keystroke. Keyed on the fields (not invalidated by
        # setters) so renaming or editing a snippet can't leave it stale;
        # the tuple compare is an identity check while they're unchanged.
        cached = self.__dict__.get("_haystack")
        if cached is not None and cached[0] == src:
            return cached[1]
        hay = "\n".join(src).lower()
        self.__dict__["_haystack"] = (src, hay)
        return hay


_history: list[Entry] = []
_snippets: list[Entry] = []
_history_lock = threading.Lock()
_snippets_lock = threading.Lock()
_watcher_thread: threading.Thread | None = None
_watcher_stop = threading.Event()


# ----- persistence -------------------------------------------------------


def _atomic_write_json(path: Path, data) -> None:
    private_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open_private(tmp) as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def _load_list(path: Path, target: list[Entry]) -> bool:
    """Return True on a successful load (including empty list), False if
    the file existed but couldn't be parsed. Callers use this to decide
    whether subsequent reference-tracking operations (like orphan-image
    sweeps) are safe - sweeping when the load failed would treat every
    image as unreferenced and delete the lot."""
    if not path.is_file():
        return True
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("expected a JSON list of entries")
        known = {f.name for f in fields(Entry)}
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                # Fields from a newer/older version are ignored, not a reason
                # to drop the whole entry (it would be lost on the next save)
                target.append(Entry(**{k: v for k, v in item.items() if k in known}))
            except TypeError:
                continue
        return True
    except (OSError, ValueError) as exc:  # JSONDecodeError is a ValueError
        print(f"[clipboard] could not load {path}: {exc}")
        # The next save would replace the unreadable file with an empty list
        # and lose everything in it, so keep a copy first.
        try:
            backup = path.with_suffix(path.suffix + ".unreadable")
            if not backup.exists():
                shutil.copy2(path, backup)
                print(f"[clipboard] kept a copy at {backup}")
        except OSError:
            pass
        return False


def _save_history() -> None:
    with _history_lock:
        data = [asdict(e) for e in _history]
    try:
        _atomic_write_json(HISTORY_FILE, data)
    except OSError as exc:
        print(f"[clipboard] could not save history: {exc}")


def _save_snippets() -> None:
    with _snippets_lock:
        data = [asdict(e) for e in _snippets]
    try:
        _atomic_write_json(SNIPPETS_FILE, data)
    except OSError as exc:
        print(f"[clipboard] could not save snippets: {exc}")


def _add_entry(entry: Entry) -> None:
    with _history_lock:
        if _history and _history[0].kind == entry.kind:
            if entry.kind == "text" and _history[0].text == entry.text:
                return
            if entry.kind == "image" and _history[0].image_path == entry.image_path:
                return
        _history.insert(0, entry)
        while len(_history) > _history_size():
            old = _history.pop()
            if old.kind == "image" and old.image_path:
                try:
                    Path(old.image_path).unlink(missing_ok=True)
                except OSError:
                    pass
    _save_history()


def _snippet_uses_image(image_path: str) -> bool:
    """True if a snippet points at this image file. Pinning shares the file
    rather than copying it, so history must not delete it from under one."""
    with _snippets_lock:
        return any(s.kind == "image" and s.image_path == image_path for s in _snippets)


def _remove_history_entry(entry_id: str) -> bool:
    """Delete one history entry (the picker's per-row Delete) and its image
    file unless a snippet still uses it. Saves straight away: this is how
    you get rid of something you shouldn't have copied. Returns False if
    the entry was already gone."""
    with _history_lock:
        removed = [e for e in _history if e.id == entry_id]
        if not removed:
            return False
        _history[:] = [e for e in _history if e.id != entry_id]
    _save_history()
    for e in removed:
        if e.kind == "image" and e.image_path and not _snippet_uses_image(e.image_path):
            try:
                Path(e.image_path).unlink(missing_ok=True)
            except OSError:
                pass
    return True


def _pin_entry(entry: Entry, name: str = "") -> None:
    """Copy a history entry into the persistent snippets store."""
    new_id = uuid.uuid4().hex[:12]
    snippet = Entry(
        id=new_id,
        timestamp=time.time(),
        kind=entry.kind,
        text=entry.text,
        image_path=entry.image_path,
        name=name,
    )
    with _snippets_lock:
        _snippets.insert(0, snippet)
    _save_snippets()
    _rebuild_trigger_index()


def _unpin_snippet(snippet_id: str) -> None:
    with _snippets_lock:
        _snippets[:] = [s for s in _snippets if s.id != snippet_id]
    _save_snippets()
    _rebuild_trigger_index()


def _create_snippet(name: str, text: str, trigger: str = "", category: str = "") -> None:
    """Make a brand-new snippet from scratch (not derived from history)."""
    snippet = Entry(
        id=uuid.uuid4().hex[:12],
        timestamp=time.time(),
        kind="text",
        text=text,
        name=name,
        trigger=trigger.strip(),
        category=category.strip(),
    )
    with _snippets_lock:
        _snippets.insert(0, snippet)
    _save_snippets()
    _rebuild_trigger_index()


def _set_snippet_category(snippet_id: str, category: str) -> None:
    with _snippets_lock:
        for s in _snippets:
            if s.id == snippet_id:
                s.category = category.strip()
                break
    _save_snippets()


def _bump_usage(snippet_id: str) -> None:
    """Increment a snippet's usage counter and persist. Cheap fire-and-
    forget - failure to save isn't worth blocking the paste over."""
    with _snippets_lock:
        for s in _snippets:
            if s.id == snippet_id:
                s.usage_count = int(s.usage_count or 0) + 1
                break
    try:
        _save_snippets()
    except Exception as exc:
        print(f"[clipboard] usage save failed (ignored): {exc}")


def _set_snippet_trigger(snippet_id: str, trigger: str) -> None:
    with _snippets_lock:
        for s in _snippets:
            if s.id == snippet_id:
                s.trigger = trigger.strip()
                break
    _save_snippets()
    _rebuild_trigger_index()


# ----- trigger index -----------------------------------------------------

# Built from snippets every time the list changes. Keyed by trigger
# string (case-sensitive). Read by the XRecord watcher thread; built
# on the GLib thread via _rebuild_trigger_index().
_trigger_index: dict[str, Entry] = {}
_trigger_index_lock = threading.Lock()


def _find_trigger_conflicts(triggers: list[str], own_id: str = "") -> list[tuple]:
    """Return [(trigger, snippet_name), ...] for any of `triggers` that
    are already claimed by a different snippet. own_id is excluded so a
    snippet being edited doesn't clash with itself."""
    if not triggers:
        return []
    conflicts: list[tuple] = []
    seen: set[str] = set()
    with _snippets_lock:
        for trig in triggers:
            for s in _snippets:
                if s.id == own_id:
                    continue
                if trig in s.trigger_list() and trig not in seen:
                    conflicts.append((trig, s.name or s.preview(40)))
                    seen.add(trig)
                    break
    return conflicts


def _rebuild_trigger_index() -> None:
    new_index: dict[str, Entry] = {}
    with _snippets_lock:
        for s in _snippets:
            if s.kind != "text":
                continue
            # Each snippet can register multiple triggers via comma
            # separation. setdefault keeps first-seen-wins; snippets
            # are stored newest-first, so the most-recently-edited
            # snippet wins on collision (intentional).
            for trig in s.trigger_list():
                new_index.setdefault(trig, s)
    with _trigger_index_lock:
        _trigger_index.clear()
        _trigger_index.update(new_index)


def _rename_snippet(snippet_id: str, new_name: str) -> None:
    with _snippets_lock:
        for s in _snippets:
            if s.id == snippet_id:
                s.name = new_name
                break
    _save_snippets()
    _rebuild_trigger_index()


def _set_snippet_text(snippet_id: str, new_text: str) -> None:
    with _snippets_lock:
        for s in _snippets:
            if s.id == snippet_id:
                s.text = new_text
                break
    _save_snippets()


# ----- trigger watcher (XRecord) -----------------------------------------

# Characters that, when typed, cause us to check whether the buffer ends
# with a known trigger. Whitespace only - espanso / AutoKey use the same
# rule. Including punctuation here breaks snippets that *start* with a
# punctuation prefix (e.g. ";mvh"), so we keep the boundary set minimal.
_TRIGGER_CHARS = set(" \t\n")
_TRIGGER_BUFFER_MAX = 64

_trigger_watcher: _TriggerWatcher | None = None


class _TriggerWatcher:
    """Background XRecord listener. When the user types a snippet trigger
    followed by a word-boundary character (space, tab, enter, punctuation),
    expand it in place: backspace the shortcode + the boundary char,
    paste the rendered snippet, optionally reposition the caret.

    Privacy: this thread sees every keystroke, but stores at most the
    last 64 characters in memory and never writes them to disk. Disabled
    by default; opt-in via the 'snippet_triggers_enabled' setting.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        # Two Display handles - XRecord needs a dedicated one for the
        # passive context; the other is used for keycode→char lookups.
        self._record_display = None
        self._local_display = None
        self._ctx = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="vinopop-triggers",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # disable_context returns control of the recording display so
        # the blocking enable_context() call in _run() unwinds.
        try:
            if self._local_display is not None and self._ctx is not None:
                self._local_display.record_disable_context(self._ctx)
                self._local_display.flush()
        except Exception:
            pass

    # -- internals --

    def _run(self) -> None:
        try:
            from Xlib import XK, X
            from Xlib import display as Xdisplay
            from Xlib.ext import record
            from Xlib.protocol import rq
        except ImportError:
            print("[triggers] python-xlib missing record extension - disabled")
            return
        try:
            self._record_display = Xdisplay.Display()
            self._local_display = Xdisplay.Display()
            v = self._local_display.record_get_version(0, 0)
            print(f"[triggers] XRecord {v.major_version}.{v.minor_version} ready")
        except Exception as exc:
            print(f"[triggers] could not open X displays: {exc}")
            return

        self._ctx = self._local_display.record_create_context(
            0,
            [record.AllClients],
            [
                {
                    "core_requests": (0, 0),
                    "core_replies": (0, 0),
                    "ext_requests": (0, 0, 0, 0),
                    "ext_replies": (0, 0, 0, 0),
                    "delivered_events": (0, 0),
                    "device_events": (X.KeyPress, X.KeyRelease),
                    "errors": (0, 0),
                    "client_started": False,
                    "client_died": False,
                }
            ],
        )

        def handler(reply):
            if reply.category != record.FromServer:
                return
            if reply.client_swapped:
                return
            data = reply.data
            while len(data):
                event, data = rq.EventField(None).parse_binary_value(
                    data,
                    self._record_display.display,
                    None,
                    None,
                )
                if event.type == X.KeyPress:
                    self._on_keypress(event, X, XK)

        try:
            self._local_display.record_enable_context(self._ctx, handler)
        except Exception as exc:
            print(f"[triggers] record_enable_context exited: {exc}")
        finally:
            try:
                self._local_display.record_free_context(self._ctx)
            except Exception:
                pass
            self._ctx = None

    def _on_keypress(self, event, X, XK) -> None:
        # Drop chord modifiers (Ctrl/Alt) so Ctrl-S doesn't pollute the
        # buffer with 's'. Pure shift is fine - that's just capitalisation.
        state = event.state
        if state & (X.ControlMask | X.Mod1Mask | X.Mod4Mask):
            # Ctrl, Alt, Super → reset buffer so a chord can't be mistaken
            # for a partial trigger.
            with self._lock:
                self._buffer.clear()
            return

        shift = bool(state & X.ShiftMask)
        caps = bool(state & X.LockMask)
        index = 1 if shift else 0

        keysym = self._local_display.keycode_to_keysym(event.detail, index)
        if keysym == 0:
            return

        # Special non-printable handling
        if keysym == XK.XK_BackSpace:
            with self._lock:
                if self._buffer:
                    self._buffer.pop()
            return
        if keysym in (XK.XK_Return, XK.XK_KP_Enter):
            self._handle_char("\n")
            return
        if keysym == XK.XK_Tab:
            self._handle_char("\t")
            return
        if keysym == XK.XK_Escape:
            with self._lock:
                self._buffer.clear()
            return
        # Ignore pure modifier keys, arrow keys, F-keys, etc.
        if 0xFF00 <= keysym <= 0xFFFF and keysym not in (XK.XK_space,):
            return

        ch = self._keysym_to_char(keysym, caps, shift, XK)
        if not ch:
            return
        self._handle_char(ch)

    @staticmethod
    def _keysym_to_char(keysym, caps_lock: bool, shift: bool, XK) -> str | None:
        # XK.keysym_to_string covers Latin-1 and common Unicode aliases.
        s = XK.keysym_to_string(keysym)
        if s is None or len(s) > 1:
            # Latin-1 fallback for codes XK doesn't have a name for.
            if 0x20 <= keysym <= 0xFF:
                s = chr(keysym)
            else:
                return None
        # Caps Lock without Shift uppercases alphabetic chars; with Shift
        # the two cancel out. (Approximation - non-Latin caps behavior
        # varies by layout.)
        if caps_lock and not shift and s.isalpha():
            return s.upper()
        if caps_lock and shift and s.isalpha():
            return s.lower()
        return s

    def _handle_char(self, ch: str) -> None:
        is_boundary = ch in _TRIGGER_CHARS
        with self._lock:
            if is_boundary:
                # Check whether buffer ends with a known trigger. Try the
                # longest matches first so "abcd" wins over "cd" if both
                # are registered. Case-insensitive; the actual typed
                # form is captured separately so case-propagation can
                # apply later.
                buf = "".join(self._buffer)
                buf_lc = buf.lower()
                with _trigger_index_lock:
                    triggers = sorted(_trigger_index.keys(), key=len, reverse=True)
                    matched = None
                    for trig in triggers:
                        trig_lc = trig.lower()
                        if not buf_lc.endswith(trig_lc):
                            continue
                        if not _trigger_word_boundary_ok(buf, trig_lc):
                            continue
                        typed = buf[-len(trig_lc) :]
                        matched = (trig, typed, _trigger_index[trig])
                        break
                self._buffer.clear()
                if matched is not None:
                    trig, typed, snippet = matched
                    GLib.idle_add(_fire_trigger_expansion, trig, typed, ch, snippet)
                    return
                # Boundary char that isn't part of a trigger: start fresh.
                return
            self._buffer.append(ch)
            if len(self._buffer) > _TRIGGER_BUFFER_MAX:
                # Trim oldest. Keep the tail; trigger matches are
                # always at the end.
                del self._buffer[: len(self._buffer) - _TRIGGER_BUFFER_MAX]


def _trigger_word_boundary_ok(buf: str, trig_lc: str) -> bool:
    """The trigger has already been confirmed to be a suffix of buf
    (case-insensitive). Reject the match unless the trigger occupies a
    word boundary on its leading side:
      - it's at the very start of the buffer, OR
      - the char immediately before it is NOT a word char (letters /
        digits / underscore), OR
      - the trigger itself starts with a non-word char (e.g. ';mvh') -
        the punctuation is its own boundary.
    Trailing boundary is implicit because we're called from a boundary
    handler (space/tab/enter just triggered the check).
    """
    if len(buf) == len(trig_lc):
        return True
    before = buf[-(len(trig_lc) + 1)]
    if not (before.isalnum() or before == "_"):
        return True
    return bool(not (trig_lc[0].isalnum() or trig_lc[0] == "_"))


def _detect_case_style(typed: str) -> str:
    """Classify the case pattern of what the user actually typed.
    Returns one of: 'lower' / 'title' / 'upper' / 'mixed'. Triggers
    with only digits/symbols return 'lower' (no transformation)."""
    letters = [c for c in typed if c.isalpha()]
    if not letters:
        return "lower"
    if all(c.islower() for c in letters):
        return "lower"
    if all(c.isupper() for c in letters):
        # Need at least 2 letters to call it intentional UPPER - a
        # single capital letter is ambiguous with title-case.
        return "upper" if len(letters) >= 2 else "title"
    # First letter capital, rest lower → title
    if letters[0].isupper() and all(c.islower() for c in letters[1:]):
        return "title"
    return "mixed"


def _propagate_case(typed: str, output: str) -> str:
    """Apply the case pattern from `typed` (what the user actually
    typed) to `output` (the rendered snippet text). Mixed/lower leaves
    output alone; title capitalises the first alpha char; upper
    uppercases everything."""
    style = _detect_case_style(typed)
    if style in ("lower", "mixed"):
        return output
    if style == "upper":
        return output.upper()
    if style == "title":
        for i, c in enumerate(output):
            if c.isalpha():
                return output[:i] + c.upper() + output[i + 1 :]
    return output


def _active_window_haystacks() -> list[str]:
    """Lower-cased window title + WM_CLASS of the focused window. Used
    by the trigger blocklist to decide whether to expand here. xprop
    rather than xdotool getwindowclassname because the latter is missing
    on Mint/Debian builds."""
    out: list[str] = []
    try:
        wid = subprocess.run(
            ["xdotool", "getactivewindow"],
            capture_output=True,
            text=True,
            timeout=0.3,
        ).stdout.strip()
        if not wid:
            return out
        try:
            name = subprocess.run(
                ["xdotool", "getwindowname", wid],
                capture_output=True,
                text=True,
                timeout=0.3,
            ).stdout.strip()
            if name:
                out.append(name.lower())
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            klass = subprocess.run(
                ["xprop", "-id", wid, "WM_CLASS"],
                capture_output=True,
                text=True,
                timeout=0.3,
            ).stdout.strip()
            if klass:
                out.append(klass.lower())
        except (OSError, subprocess.SubprocessError):
            pass
    except (OSError, subprocess.SubprocessError):
        pass
    return out


# ----- Wayland trigger watcher (evdev + libxkbcommon + ydotool) ----------
# XRecord only sees XWayland clients and xdotool can't reach native Wayland
# windows, so the X watcher is blind on a Wayland session. Here we read the
# raw keyboard devices via /dev/input (user must be in the 'input' group),
# translate keycodes through the live xkb layout with libxkbcommon, and
# inject expansions with ydotool. Detection/matching is inherited from
# _TriggerWatcher; only the input source and the injection path differ.

_KEY_BACKSPACE = 14
# Modifiers that abort a partial trigger (a chord, not typing). RIGHTALT
# (AltGr, 100) is deliberately excluded - it's needed to type @ { } [ ] on
# many layouts and xkb resolves it for us.
_ABORT_MODS = {29, 97, 56, 125, 126}  # ctrl L/R, left-alt, super L/R
# While we inject with ydotool, those synthetic keystrokes come back through
# /dev/input. The injector sets this; the watcher ignores events until then.
_kbd_suppress_until = 0.0


def _is_wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def _ydotool_env() -> dict:
    sock = os.environ.get("YDOTOOL_SOCKET") or f"/run/user/{os.getuid()}/.ydotool_socket"
    return {**os.environ, "YDOTOOL_SOCKET": sock}


def _detect_xkb_layout() -> tuple:
    """(layout, variant, model) for the active keyboard, best-effort via
    localectl. Falls back to us/pc105. Multi-layout configs use the first."""
    layout, variant, model = "us", "", "pc105"
    try:
        out = subprocess.run(
            ["localectl", "status"], capture_output=True, text=True, timeout=2.0
        ).stdout
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("X11 Layout:"):
                layout = s.split(":", 1)[1].strip() or layout
            elif s.startswith("X11 Variant:"):
                variant = s.split(":", 1)[1].strip()
            elif s.startswith("X11 Model:"):
                model = s.split(":", 1)[1].strip() or model
    except Exception:
        pass
    return layout.split(",")[0], variant.split(",")[0], model


class _XkbMapper:
    """libxkbcommon ctypes wrapper: evdev key code -> UTF-8 char under the
    live modifier state."""

    def __init__(self, layout: str, variant: str, model: str) -> None:
        import ctypes as C

        self._ok = False
        try:
            xkb = C.CDLL("libxkbcommon.so.0")
        except OSError:
            return
        self._xkb = xkb

        class _RN(C.Structure):
            _fields_ = [
                ("rules", C.c_char_p),
                ("model", C.c_char_p),
                ("layout", C.c_char_p),
                ("variant", C.c_char_p),
                ("options", C.c_char_p),
            ]

        xkb.xkb_context_new.restype = C.c_void_p
        xkb.xkb_context_new.argtypes = [C.c_int]
        xkb.xkb_keymap_new_from_names.restype = C.c_void_p
        xkb.xkb_keymap_new_from_names.argtypes = [C.c_void_p, C.POINTER(_RN), C.c_int]
        xkb.xkb_state_new.restype = C.c_void_p
        xkb.xkb_state_new.argtypes = [C.c_void_p]
        xkb.xkb_state_key_get_utf8.restype = C.c_int
        xkb.xkb_state_key_get_utf8.argtypes = [C.c_void_p, C.c_uint32, C.c_char_p, C.c_size_t]
        xkb.xkb_state_update_key.restype = C.c_int
        xkb.xkb_state_update_key.argtypes = [C.c_void_p, C.c_uint32, C.c_int]
        ctx = xkb.xkb_context_new(0)

        def _mk(lay, var, mod):
            rn = _RN(
                b"evdev",
                (mod or "pc105").encode(),
                (lay or "us").encode(),
                var.encode() if var else None,
                None,
            )
            return xkb.xkb_keymap_new_from_names(ctx, C.byref(rn), 0)

        km = _mk(layout, variant, model) or _mk("us", "", "pc105")
        if not km:
            return
        self._state = xkb.xkb_state_new(km)
        self._buf = C.create_string_buffer(16)
        self._ok = bool(self._state)

    def char_for_press(self, code: int) -> str:
        if not self._ok:
            return ""
        self._xkb.xkb_state_key_get_utf8(self._state, code + 8, self._buf, 16)
        return self._buf.raw.split(b"\x00", 1)[0].decode("utf-8", "replace")

    def update(self, code: int, down: bool) -> None:
        if self._ok:
            self._xkb.xkb_state_update_key(self._state, code + 8, 1 if down else 0)


class _WaylandTriggerWatcher(_TriggerWatcher):
    """evdev + xkb + ydotool variant of the trigger watcher for Wayland."""

    def __init__(self) -> None:
        super().__init__()
        layout, variant, model = _detect_xkb_layout()
        self._mapper = _XkbMapper(layout, variant, model)
        self._fds: list = []
        print(f"[triggers] Wayland watcher (layout={layout} variant={variant or '-'})")

    def _open_keyboards(self) -> list:
        import glob

        # by-path/*-event-kbd is exactly the physical keyboards - and it
        # excludes ydotool's virtual uinput device (no bus path), so we
        # never read back the very keys we inject.
        paths = set()
        for p in glob.glob("/dev/input/by-path/*-event-kbd"):
            try:
                paths.add(os.path.realpath(p))
            except OSError:
                pass
        if not paths:
            paths = set(glob.glob("/dev/input/event*"))
        fds = []
        for path in sorted(paths):
            try:
                fds.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
            except OSError as exc:
                print(f"[triggers] cannot open {path}: {exc}")
        return fds

    def _run(self) -> None:
        if not getattr(self._mapper, "_ok", False):
            print("[triggers] libxkbcommon unavailable - Wayland triggers off")
            return
        self._fds = self._open_keyboards()
        if not self._fds:
            print(
                "[triggers] no readable keyboards - add yourself to the "
                "'input' group? Wayland triggers off"
            )
            return
        print(f"[triggers] watching {len(self._fds)} keyboard device(s)")
        import struct

        ev_fmt = "llHHi"
        ev_size = struct.calcsize(ev_fmt)
        EV_KEY = 0x01
        try:
            while not self._stop.is_set():
                r, _, _ = _select.select(self._fds, [], [], 0.5)
                for fd in r:
                    try:
                        data = os.read(fd, ev_size * 64)
                    except (BlockingIOError, OSError):
                        continue
                    for off in range(0, len(data) - ev_size + 1, ev_size):
                        _s, _us, et, code, value = struct.unpack_from(ev_fmt, data, off)
                        if et == EV_KEY:
                            self._on_key(code, value)
        finally:
            for fd in self._fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._fds = []

    def _on_key(self, code: int, value: int) -> None:
        if value == 0:  # release
            self._mapper.update(code, False)
            return
        # press (1) or autorepeat (2)
        abort = code in _ABORT_MODS
        is_bs = code == _KEY_BACKSPACE
        ch = "" if (abort or is_bs) else self._mapper.char_for_press(code)
        if value == 1:
            self._mapper.update(code, True)
        if time.monotonic() < _kbd_suppress_until:
            return  # our own injected keystrokes - don't re-buffer them
        if abort:
            with self._lock:
                self._buffer.clear()
            return
        if is_bs:
            with self._lock:
                if self._buffer:
                    self._buffer.pop()
            return
        if not ch:
            return
        if ch == "\r":
            ch = "\n"
        if ch.isprintable() or ch in _TRIGGER_CHARS:
            self._handle_char(ch)


def _trigger_blocked() -> bool:
    """Return True if the active window matches any pattern in the
    user's trigger_blocklist_patterns setting."""
    patterns = list(_cfg("trigger_blocklist_patterns", []) or [])
    if not patterns:
        return False
    return _matches_blocklist(patterns, _active_window_haystacks())


def _fire_trigger_expansion(
    trigger: str,
    typed: str,
    boundary_char: str,
    snippet: Entry,
) -> bool:
    """GLib-main-thread callback. Backspace over the typed shortcode +
    boundary, then paste the rendered snippet. `typed` is what the user
    actually typed (used for case-propagation; may differ in case from
    the canonical `trigger`). Returns False so idle_add fires only
    once."""
    if snippet.kind != "text":
        return False
    # Snippets with {ask:} can't run inline from a trigger - a blocking
    # dialog mid-keystroke is jarring. Skip silently; the user can still
    # invoke them via the picker.
    if "{ask:" in snippet.text:
        print(f"[triggers] '{trigger}' has {{ask:}} - skipping (use picker)")
        return False
    # Per-app/site blocklist: skip expansion in user-configured apps
    # (password managers, terminals, banking sites, etc.).
    if _trigger_blocked():
        print(f"[triggers] '{trigger}' skipped - active window is in blocklist")
        return False
    rendered, cursor_left, _ = render_placeholders(
        snippet.text,
        lambda _label: None,
    )
    # Case-propagation: typed 'Rraak' → Title-case output;
    # typed 'RRAAK' → UPPER output.
    rendered = _propagate_case(typed, rendered)
    _bump_usage(snippet.id)

    def worker_x11():
        # Check every tool up front: bailing out after the BackSpaces below
        # would leave the user with neither their trigger nor the expansion.
        if not shutil.which("xdotool") or not shutil.which("xclip"):
            print("[triggers] xdotool/xclip not found - cannot expand")
            return
        # Wait for the user to physically release the boundary key
        # (space/tab/enter) before we start synthesising keystrokes.
        # Without this, xdotool's --clearmodifiers reads the live key
        # state mid-press and leaves modifiers like Ctrl in a "stuck"
        # logical state in the receiving app (scroll-zooms instead of
        # scroll, etc.). 60 ms is generous; humans don't tap-release
        # that fast.
        time.sleep(0.06)
        # Delete the typed shortcode + the boundary char.
        # --delay 0 drops events in Electron/Chrome targets that filter
        # for human-rate key timing; 16 ms (~1 frame at 60 Hz) is fast
        # enough that the user never sees the typed chars, but slow
        # enough that every BackSpace lands. --clearmodifiers is
        # unnecessary here - Backspace doesn't care about modifier
        # state, and the modifier dance occasionally leaves Ctrl wedged.
        n_delete = len(trigger) + 1
        subprocess.run(
            ["xdotool", "key", "--repeat", str(n_delete), "--delay", "16", "BackSpace"],
            check=False,
        )
        # Give the receiving app a moment to settle before we move on
        # to clipboard staging + paste.
        time.sleep(0.08)
        # Stage the rendered text on the clipboard.
        try:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=rendered.encode("utf-8"),
                check=False,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError):
            return
        subprocess.run(
            ["xdotool", "key", "--clearmodifiers", "ctrl+v"],
            check=False,
        )
        if cursor_left > 0:
            time.sleep(0.05)
            subprocess.run(
                [
                    "xdotool",
                    "key",
                    "--clearmodifiers",
                    "--repeat",
                    str(cursor_left),
                    "--delay",
                    "0",
                    "Left",
                ],
                check=False,
            )
        # Defensive: force-release every modifier we might have nudged.
        # xdotool's --clearmodifiers is supposed to restore modifier
        # state, but the restoration races with the user's own key-up
        # event for the boundary key and sometimes leaves Ctrl/Shift
        # logically stuck (Firefox starts zooming on scroll, etc.).
        # Cheap insurance: explicit keyups on the usual suspects.
        time.sleep(0.02)
        subprocess.run(
            ["xdotool", "keyup", "ctrl", "shift", "alt", "super", "Control_L", "Control_R"],
            check=False,
        )

    def worker_wayland():
        if not shutil.which("ydotool") or not shutil.which("wl-copy"):
            print("[triggers] ydotool/wl-copy not found - cannot expand on Wayland")
            return
        global _kbd_suppress_until
        env = _ydotool_env()
        n_delete = len(trigger) + 1
        # Suppress reading our own injected keystrokes for the whole run.
        _kbd_suppress_until = time.monotonic() + 1.2 + 0.02 * (n_delete + cursor_left)
        time.sleep(0.06)
        # 1. Delete the typed shortcode + boundary char. Single keys inject
        #    fine via ydotool (keycode 14 = BackSpace).
        bs = []
        for _ in range(n_delete):
            bs += ["14:1", "14:0"]
        subprocess.run(["ydotool", "key", *bs], env=env, check=False)
        time.sleep(0.03)
        # 2. Stage the expansion on BOTH clipboard and primary. This carries
        #    full unicode (ae/oe/aa etc.) that 'ydotool type' silently drops,
        #    since type maps through a US keycode table.
        try:
            subprocess.run(["wl-copy"], input=rendered.encode("utf-8"), check=False, timeout=2.0)
            subprocess.run(
                ["wl-copy", "--primary"], input=rendered.encode("utf-8"), check=False, timeout=2.0
            )
        except (OSError, subprocess.SubprocessError):
            return
        time.sleep(0.05)
        # 3. Paste with Shift+Insert (LEFTSHIFT=42, INSERT=110). ydotool's
        #    Ctrl chords don't register on KWin, but Shift+Insert does - and
        #    it pastes the staged unicode verbatim.
        subprocess.run(["ydotool", "key", "42:1", "110:1", "110:0", "42:0"], env=env, check=False)
        if cursor_left > 0:
            time.sleep(0.05)
            lf = []
            for _ in range(cursor_left):
                lf += ["105:1", "105:0"]
            subprocess.run(["ydotool", "key", *lf], env=env, check=False)
        _kbd_suppress_until = time.monotonic() + 0.25

    worker = worker_wayland if _is_wayland() else worker_x11
    threading.Thread(target=worker, daemon=True, name="trigger-expansion").start()
    return False


def _maybe_start_trigger_watcher() -> None:
    """Honour the snippet_triggers_enabled setting. Idempotent."""
    global _trigger_watcher
    enabled = bool(_cfg("snippet_triggers_enabled", False))
    if enabled:
        if _trigger_watcher is None:
            _trigger_watcher = _WaylandTriggerWatcher() if _is_wayland() else _TriggerWatcher()
        _trigger_watcher.start()
    else:
        if _trigger_watcher is not None:
            _trigger_watcher.stop()


# ----- clipboard reading -------------------------------------------------

# Clipboard reads must be backend-aware. xclip only sees the X11 (XWayland)
# clipboard, so on a native Wayland session it silently misses everything
# copied from native-Wayland apps - the history then "doesn't record" most
# copies. On Wayland we read via wl-paste (wl-clipboard); on X11 via xclip.


def _read_clipboard_targets() -> list[str]:
    if _is_wayland():
        try:
            out = subprocess.run(
                ["wl-paste", "--list-types"],
                capture_output=True,
                timeout=0.5,
            )
            return out.stdout.decode("utf-8", errors="replace").splitlines()
        except (OSError, subprocess.SubprocessError):
            return []
    try:
        out = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            capture_output=True,
            timeout=0.5,
        )
        return out.stdout.decode("utf-8", errors="replace").splitlines()
    except (OSError, subprocess.SubprocessError):
        return []


def _read_clipboard_target(target: str) -> bytes | None:
    """Raw bytes of one clipboard target, or None if it can't be read."""
    if _is_wayland():
        args = ["wl-paste", "-n", "-t", target]
    else:
        args = ["xclip", "-selection", "clipboard", "-t", target, "-o"]
    try:
        out = subprocess.run(args, capture_output=True, timeout=0.5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


# Password managers mark their copies so clipboard managers leave them alone:
# KeePassXC, KWallet and Plasma offer x-kde-passwordManagerHint with the value
# "secret"; some apps use the macOS concealed type instead. Matched
# case-insensitively because X11 atom names are echoed as the app wrote them.
_PASSWORD_HINT_TARGET = "x-kde-passwordmanagerhint"
_CONCEALED_TARGETS = ("application/x-nspasteboard-concealed-type",)


def _is_password_copy(targets: list[str]) -> bool:
    lowered = {t.strip().lower(): t.strip() for t in targets}
    if any(t in lowered for t in _CONCEALED_TARGETS):
        return True
    if _PASSWORD_HINT_TARGET not in lowered:
        return False
    # Only "secret" is defined, but skip too if the value can't be read:
    # keeping a password by mistake is worse than missing one copy.
    value = _read_clipboard_target(lowered[_PASSWORD_HINT_TARGET])
    if not value:
        return True
    return value.decode("utf-8", errors="replace").strip().lower() == "secret"


def _matches_blocklist(patterns: list[str], haystacks: list[str]) -> bool:
    for p in patterns:
        p_lc = (p or "").strip().lower()
        if p_lc and any(p_lc in h for h in haystacks):
            return True
    return False


def _capture_blocked() -> bool:
    """True if the focused window (normally the app the copy came from)
    matches the popup's blocklist_patterns. People list their password
    manager and bank there; their copies shouldn't land in history either."""
    patterns = list(_cfg("blocklist_patterns", []) or [])
    if not patterns:
        return False
    try:
        from platform_backend import get_backend

        haystacks = get_backend().active_window_haystacks()
    except Exception as exc:  # noqa: BLE001
        print(f"[clipboard] active-window check failed: {exc}")
        return False
    return _matches_blocklist(patterns, haystacks)


def _skip_capture(targets: list[str]) -> bool:
    """Checked once per new copy, after the dedup check, so polling doesn't
    re-run the active-window query every interval. Silent on purpose: not
    even the log should note that a password went by."""
    return _is_password_copy(targets) or _capture_blocked()


def _read_clipboard_text() -> str:
    if _is_wayland():
        # Prefer plain text; fall back to whatever wl-paste offers. -n so we
        # don't pick up wl-paste's appended trailing newline (xclip is raw).
        for args in (["wl-paste", "-n", "-t", "text/plain"], ["wl-paste", "-n"]):
            try:
                out = subprocess.run(args, capture_output=True, timeout=0.5)
            except (OSError, subprocess.SubprocessError):
                continue
            if out.returncode == 0 and out.stdout:
                return out.stdout.decode("utf-8", errors="replace")
        return ""
    try:
        out = subprocess.run(
            ["xclip", "-selection", "clipboard", "-o"],
            capture_output=True,
            timeout=0.5,
        )
        return out.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""


def _read_clipboard_image() -> bytes | None:
    if _is_wayland():
        try:
            out = subprocess.run(
                ["wl-paste", "-t", "image/png"],
                capture_output=True,
                timeout=1.0,
            )
            if out.returncode == 0 and out.stdout:
                return out.stdout
        except (OSError, subprocess.SubprocessError):
            pass
        return None
    try:
        out = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            capture_output=True,
            timeout=1.0,
        )
        if out.returncode == 0 and out.stdout:
            return out.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    return None


_state = {"last_text": "", "last_image_hash": b""}

# Persistent dedup-hash file. Without this, every daemon restart with
# an image still on the clipboard re-saves it as a "new" entry.
_DEDUP_STATE_FILE = CACHE_DIR / "last_image.sha1"


def _load_dedup_state() -> None:
    """Restore the last seen clipboard image hash from disk so we don't
    re-capture the same image just because the daemon was restarted."""
    try:
        _state["last_image_hash"] = _DEDUP_STATE_FILE.read_bytes()
    except OSError:
        pass  # first run or missing -- fine


def _save_dedup_state(h: bytes) -> None:
    try:
        private_dir(CACHE_DIR)
        with open_private(_DEDUP_STATE_FILE, "wb") as f:
            f.write(h)
    except OSError:
        pass


def _sweep_orphan_images() -> None:
    """Delete image files in the cache that are not referenced by any
    history or snippet entry. Cleans up files left behind by older
    daemon runs whose entries have since been bumped out of history."""
    if not IMAGES_DIR.is_dir():
        return
    referenced: set[str] = set()
    with _history_lock:
        for e in _history:
            if e.kind == "image" and e.image_path:
                referenced.add(Path(e.image_path).name)
    with _snippets_lock:
        for e in _snippets:
            if e.kind == "image" and e.image_path:
                referenced.add(Path(e.image_path).name)
    removed = 0
    for p in IMAGES_DIR.glob("*.png"):
        if p.name not in referenced:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"[clipboard] swept {removed} orphan image(s) from cache")


def _tighten_permissions() -> None:
    """One-time fix-up for files older versions wrote with the default
    umask (0644 files in 0755 folders): only the user should be able to
    read what they copied."""
    for d in (_LP_CACHE, CACHE_DIR, IMAGES_DIR, _LP_CONFIG):
        make_private(d)
    for f in (HISTORY_FILE, SNIPPETS_FILE, _DEDUP_STATE_FILE):
        make_private(f)
        make_private(f.with_suffix(f.suffix + ".unreadable"))
    if IMAGES_DIR.is_dir():
        for p in IMAGES_DIR.glob("*.png"):
            make_private(p)


def _capture_current_clipboard() -> None:
    if _watcher_stop.is_set():
        return  # this copy of the module was replaced by a reload
    try:
        targets = _read_clipboard_targets()
        has_image = _capture_images() and any(t.startswith("image/") for t in targets)
        has_text = any(
            t in ("UTF8_STRING", "TEXT", "STRING", "text/plain", "text/plain;charset=utf-8")
            for t in targets
        )
        if has_image:
            data = _read_clipboard_image()
            if data:
                import hashlib

                h = hashlib.sha1(data, usedforsecurity=False).digest()
                if h != _state["last_image_hash"]:
                    _state["last_image_hash"] = h
                    _save_dedup_state(h)
                    if _skip_capture(targets):
                        return
                    if len(data) > _max_image_bytes():
                        print(f"[clipboard] not keeping a {len(data) // 1024} KB image (too big)")
                        return
                    private_dir(CACHE_DIR)
                    private_dir(IMAGES_DIR)
                    eid = uuid.uuid4().hex[:12]
                    path = IMAGES_DIR / f"{eid}.png"
                    try:
                        with open_private(path, "wb") as f:
                            f.write(data)
                        _add_entry(
                            Entry(
                                id=eid,
                                timestamp=time.time(),
                                kind="image",
                                image_path=str(path),
                            )
                        )
                    except OSError as exc:
                        print(f"[clipboard] could not save image: {exc}")
        elif has_text:
            text = _read_clipboard_text()
            if text and text != _state["last_text"] and text.strip():
                _state["last_text"] = text
                if _skip_capture(targets):
                    return
                size = len(text.encode("utf-8", errors="replace"))
                if size > _max_text_bytes():
                    # Skipped rather than cut: pasting half a log back
                    # would be worse than not having it in history.
                    print(f"[clipboard] not keeping a {size // 1024} KB copy (too big)")
                    return
                _add_entry(
                    Entry(
                        id=uuid.uuid4().hex[:12],
                        timestamp=time.time(),
                        kind="text",
                        text=text,
                    )
                )
    except Exception as exc:  # noqa: BLE001
        print(f"[clipboard] capture error: {exc}")


def _watcher_loop() -> None:
    try:
        from Xlib import display
        from Xlib.ext import xfixes
    except ImportError:
        _poll_only_loop()
        return
    try:
        dpy = display.Display()
        dpy.xfixes_query_version()
        root = dpy.screen().root
        clipboard_atom = dpy.intern_atom("CLIPBOARD")
        dpy.xfixes_select_selection_input(
            root,
            clipboard_atom,
            xfixes.XFixesSetSelectionOwnerNotifyMask,
        )
        dpy.flush()
    except Exception as exc:  # noqa: BLE001
        print(f"[clipboard] XFixes unavailable ({exc}); polling")
        _poll_only_loop()
        return

    print("[clipboard] using XFixes selection events (CLIPBOARD)")
    fd = dpy.fileno()
    try:
        _capture_current_clipboard()
        while not _watcher_stop.is_set():
            try:
                readable, _, _ = _select.select([fd], [], [], 5.0)
            except (OSError, ValueError):
                break
            if not readable:
                continue
            had_event = False
            while dpy.pending_events():
                _ = dpy.next_event()
                had_event = True
            if had_event and not _watcher_stop.is_set():
                time.sleep(0.05)
                _capture_current_clipboard()
    finally:
        try:
            dpy.close()
        except Exception:
            pass


def _poll_only_loop() -> None:
    while not _watcher_stop.is_set():
        _capture_current_clipboard()
        time.sleep(_poll_interval())


def shutdown() -> None:
    """Stop this copy's background threads. plugin_loader calls it before it
    re-executes the file as a fresh module (every settings save or plugin
    change), otherwise the old watcher keeps capturing and rewriting
    history.json beside the new one, and the old trigger watcher expands
    every snippet a second time."""
    global _watcher_thread, _trigger_watcher
    _watcher_stop.set()
    _watcher_thread = None
    if _trigger_watcher is not None:
        try:
            _trigger_watcher.stop()
        except Exception as exc:  # noqa: BLE001
            print(f"[clipboard] trigger watcher did not stop cleanly: {exc}")
        _trigger_watcher = None


def _start_watcher_once() -> None:
    global _watcher_thread
    if _watcher_thread is not None and _watcher_thread.is_alive():
        return
    _tighten_permissions()
    history_ok = _load_list(HISTORY_FILE, _history)
    snippets_ok = _load_list(SNIPPETS_FILE, _snippets)
    _load_dedup_state()  # keep dedup hash across daemon restarts
    # Only sweep when both reference sources loaded cleanly. A failed
    # load returns an empty list, which would mark every cached image
    # as orphan and wipe the cache - silently destroying history that
    # might be recoverable by hand-editing history.json.
    if history_ok and snippets_ok:
        _sweep_orphan_images()
    else:
        print("[clipboard] skipping orphan-image sweep - a reference file failed to load")
    _watcher_stop.clear()
    _watcher_thread = threading.Thread(
        target=_watcher_loop,
        daemon=True,
        name="vinopop-clipboard",
    )
    _watcher_thread.start()
    _rebuild_trigger_index()
    _maybe_start_trigger_watcher()
    print(
        f"[clipboard] watcher started (size={HISTORY_SIZE}, "
        f"images={CAPTURE_IMAGES}, snippets={len(_snippets)}, "
        f"triggers={len(_trigger_index)})"
    )


# ----- paste-at-cursor ---------------------------------------------------


def _get_active_window() -> str | None:
    if not shutil.which("xdotool"):
        return None
    try:
        out = subprocess.run(
            ["xdotool", "getactivewindow"],
            capture_output=True,
            text=True,
            timeout=0.5,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


# Terminal WM_CLASS values that use Ctrl+Shift+V for paste instead of
# the standard Ctrl+V. Match is case-insensitive substring against
# WM_CLASS pulled via xprop.
_TERMINAL_CLASSES = (
    "gnome-terminal",
    "konsole",
    "xfce4-terminal",
    "terminator",
    "tilix",
    "mate-terminal",
    "alacritty",
    "kitty",
    "xterm",
    "x-terminal-emulator",
)


def _paste_keystroke_for_window(wid: str | None) -> str:
    """Most apps paste with Ctrl+V; terminals want Ctrl+Shift+V. Detect
    the target's WM_CLASS so the picker pastes correctly into a terminal
    too."""
    if not wid or not shutil.which("xprop"):
        return "ctrl+v"
    try:
        out = subprocess.run(
            ["xprop", "-id", wid, "WM_CLASS"],
            capture_output=True,
            text=True,
            timeout=0.5,
        )
        wm_class = out.stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return "ctrl+v"
    for term in _TERMINAL_CLASSES:
        if term in wm_class:
            return "ctrl+shift+v"
    return "ctrl+v"


def _paste_to_window(
    entry: Entry,
    target_window: str | None,
    cursor_left: int = 0,
) -> None:
    """Put the entry on the clipboard, focus the target window, send Ctrl+V.

    If cursor_left > 0, send that many Left arrows after the paste so the
    caret lands where a {cursor} placeholder was rendered out.
    """
    if entry.kind == "text":
        if _is_wayland():
            subprocess.run(
                ["wl-copy"],
                input=entry.text.encode("utf-8"),
                check=False,
                timeout=2.0,
            )
        else:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=entry.text.encode("utf-8"),
                check=False,
                timeout=2.0,
            )
    elif entry.kind == "image":
        if not Path(entry.image_path).is_file():
            return
        if _is_wayland():
            try:
                with open(entry.image_path, "rb") as fh:
                    subprocess.run(
                        ["wl-copy", "-t", "image/png"],
                        stdin=fh,
                        check=False,
                        timeout=2.0,
                    )
            except OSError:
                return
        else:
            subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-i", entry.image_path],
                check=False,
            )

    import logging

    _log = logging.getLogger("vinopop")
    _log.info(
        "[clipboard.paste] target_window=%s kind=%s cursor_left=%d",
        target_window,
        entry.kind,
        cursor_left,
    )

    def worker():
        if not (target_window and shutil.which("xdotool")):
            _log.warning("[clipboard.paste] skipped - no target or no xdotool")
            return
        paste_key = _paste_keystroke_for_window(target_window)
        is_terminal = paste_key == "ctrl+shift+v"
        # The hotkey (e.g. super+shift+v) leaves modifiers logically held
        # in X until the user releases them. Without an explicit keyup,
        # xdotool's --clearmodifiers races with the real key-up events
        # and the paste lands as garbage (or not at all). Mirror what
        # the snippet trigger expansion does.
        time.sleep(0.06)
        subprocess.run(
            [
                "xdotool",
                "keyup",
                "ctrl",
                "shift",
                "alt",
                "super",
                "Control_L",
                "Control_R",
                "Super_L",
                "Super_R",
                "Shift_L",
                "Shift_R",
                "Alt_L",
                "Alt_R",
            ],
            check=False,
        )
        # Activate twice via two different paths. muffin / gnome-terminal-
        # server is finicky: xdotool windowactivate alone sometimes returns
        # before the focus has actually transferred (the `_NET_ACTIVE_WINDOW`
        # round-trip races the compositor's redraw). wmctrl -ia goes
        # through EWMH directly and is what actions._focus_via_wmctrl uses
        # as the primary path. Belt + braces here because the picker has
        # already stolen focus, unlike the no-focus-steal popup.
        act_x = subprocess.run(
            ["xdotool", "windowactivate", "--sync", target_window],
            check=False,
            capture_output=True,
            text=True,
        )
        if act_x.returncode != 0:
            _log.warning(
                "[clipboard.paste] windowactivate failed rc=%d %s",
                act_x.returncode,
                act_x.stderr.strip(),
            )
        if shutil.which("wmctrl"):
            try:
                hex_id = f"0x{int(target_window):08x}"
                subprocess.run(
                    ["wmctrl", "-ia", hex_id], check=False, capture_output=True, timeout=1.0
                )
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
        # Cinnamon/muffin sometimes queues the activate for the next
        # compositor frame; 100 ms wasn't enough. Terminals need extra
        # time because VTE only commits its focus-in handler after the
        # compositor's enter-notify, which lags ~50 ms behind activation.
        time.sleep(0.30 if is_terminal else 0.18)
        # Confirm focus before we fire so a missed activation is loud in
        # the log instead of just a silent no-paste.
        try:
            cur = subprocess.run(
                ["xdotool", "getactivewindow"],
                capture_output=True,
                text=True,
                timeout=0.5,
            )
            active = cur.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            active = ""
        _log.info(
            "[clipboard.paste] active=%s target=%s sending=%s",
            active or "?",
            target_window,
            paste_key,
        )
        if active and active != target_window:
            # Try one more time before giving up - some WMs need a poke.
            subprocess.run(
                ["xdotool", "windowactivate", "--sync", target_window],
                check=False,
                timeout=1.0,
            )
            time.sleep(0.12)
        # Type the text directly instead of sending a paste accelerator
        # for short single-line text. Two wins:
        #  1. Terminals - bypasses bracketed-paste markers that get out
        #     of sync with readline (the original reason this branch
        #     existed).
        #  2. Electron + React editors (Claude desktop, Slack, Discord,
        #     ChatGPT desktop, Cursor, Notion desktop, etc.). Synthetic
        #     `ctrl+v` lands as isTrusted=false and ProseMirror/Lexical/
        #     Slate reject it silently - paste ends up on the clipboard
        #     but not in the editor. Individual keypresses via xdotool
        #     type are real-enough that the editor accepts them.
        # Multi-line / long text still uses the paste accelerator
        # because typing N newlines would execute N commands in a
        # shell, and typing kilobytes char-by-char is too slow.
        use_type = entry.kind == "text" and "\n" not in entry.text and len(entry.text) <= 2048
        if use_type:
            _log.info("[clipboard.paste] using xdotool type (%d chars)", len(entry.text))
            ks = subprocess.run(
                ["xdotool", "type", "--clearmodifiers", "--delay", "1", "--", entry.text],
                check=False,
                capture_output=True,
                text=True,
            )
        else:
            ks = subprocess.run(
                ["xdotool", "key", "--clearmodifiers", paste_key],
                check=False,
                capture_output=True,
                text=True,
            )
        if ks.returncode != 0:
            _log.warning("[clipboard.paste] paste rc=%d %s", ks.returncode, ks.stderr.strip())
        if cursor_left > 0:
            # Give the paste a tick to land before we move the caret.
            time.sleep(0.05)
            subprocess.run(
                [
                    "xdotool",
                    "key",
                    "--clearmodifiers",
                    "--repeat",
                    str(cursor_left),
                    "--delay",
                    "0",
                    "Left",
                ],
                check=False,
            )
        # Defensive: clear any modifier the WM might have re-asserted
        # while we activated the window.
        subprocess.run(
            [
                "xdotool",
                "keyup",
                "ctrl",
                "shift",
                "alt",
                "super",
                "Control_L",
                "Control_R",
                "Super_L",
                "Super_R",
            ],
            check=False,
        )

    threading.Thread(target=worker, daemon=True, name="clipboard-paste").start()


# ----- placeholders ------------------------------------------------------

# Recognises:
#   {date} {time} {datetime} {weekday} {clipboard} {cursor} {name}
#   {date:FORMAT}  - strftime; FORMAT runs up to the next "}"
#   {ask:Label}    - label runs up to the next "}"
_PLACEHOLDER_RE = re.compile(
    r"\{("
    r"date(?::[^}]+)?|time|datetime|weekday|clipboard|selection|cursor|name"
    r"|ask:[^}]+"
    r"|shell:[^}]+"
    r"|var:[A-Za-z0-9_-]+"
    r")\}"
)


def _resolve_var(name: str) -> str:
    """Look up a user-defined variable from snippet_variables setting.
    Returns empty string for unknown keys - users will see the empty
    space and know to define the variable in Settings."""
    if _settings is None:
        return ""
    vars_dict = _settings.get("snippet_variables") or {}
    if not isinstance(vars_dict, dict):
        return ""
    return str(vars_dict.get(name, ""))


def _read_primary_selection() -> str:
    """Read the current X11 PRIMARY selection (the highlighted text).
    Returns empty string if nothing is highlighted or xclip is missing."""
    try:
        # Backend-aware: wl-paste on Wayland, where xclip can't see PRIMARY
        from platform_backend import get_backend

        return get_backend().read_selection("primary") or ""
    except Exception:  # noqa: BLE001 - fall back to the X11 tool below
        pass
    try:
        out = subprocess.run(
            ["xclip", "-selection", "primary", "-o"],
            capture_output=True,
            timeout=0.5,
        )
        return out.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""


def _render_shell_token(cmd: str) -> str:
    """Run `cmd` through bash, return stdout (trailing newline stripped).
    Gated behind snippet_shell_enabled - when off, returns the literal
    {shell:...} token so the user sees that it didn't run. Timeout 5 s
    so a runaway snippet can't freeze the paste.
    """
    if not bool(_cfg("snippet_shell_enabled", False)):
        return f"{{shell:{cmd}}}"
    cmd = cmd.strip()
    if not cmd:
        return ""
    try:
        out = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        return out.stdout.rstrip("\n")
    except subprocess.TimeoutExpired:
        return f"[shell timeout: {cmd}]"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"[shell error: {exc}]"


# Sentinel for the cursor position. U+E000 is in the BMP private-use area
# and will never appear in real clipboard text; safer than NUL which some
# pipelines drop.
_CURSOR_SENTINEL = "\ue000CURSOR\ue000"


_DATE_MATH_RE = re.compile(r"^([+-]\d+)([dwmy])(?::(.+))?$")


def _render_date_token(spec: str, now_struct) -> str:
    """Implements {date:...} variants.

    `spec` is what comes after 'date:' in the token. `now_struct` is
    a time.struct_time so callers (and tests) can pin the clock.

    - Pure strftime: `%A`, `%Y-%m-%d %H:%M`, ...
    - Math: `+7d`, `-1w`, `+3m`, `-2y`. m=30 days, y=365 days (good
      enough for everyday writing - calendar-correct month math is a
      surprise gift no one asked for).
    - Math + format: `+7d:%A`.
    """
    m = _DATE_MATH_RE.match(spec)
    if m is None:
        # No leading +/- → treat as strftime against now.
        return time.strftime(spec, now_struct)
    offset_num = int(m.group(1))
    unit = m.group(2)
    fmt = m.group(3) or "%Y-%m-%d"
    import datetime as _dt

    base = _dt.datetime.fromtimestamp(time.mktime(now_struct))
    days = {"d": 1, "w": 7, "m": 30, "y": 365}[unit]
    shifted = base + _dt.timedelta(days=offset_num * days)
    return shifted.strftime(fmt)


def _full_user_name() -> str:
    """Best-effort: full name from /etc/passwd GECOS, falling back to
    $USER, then "User". GECOS is comma-separated; field 0 is the name."""
    try:
        import pwd

        gecos = pwd.getpwuid(os.getuid()).pw_gecos or ""
        first = gecos.split(",", 1)[0].strip()
        if first:
            return first
    except (KeyError, OSError, ImportError):
        pass
    return os.environ.get("USER") or "User"


def render_placeholders(
    text: str,
    ask_callback: Callable[[list[tuple[str, list[str] | None]]], dict | None],
) -> tuple[str, int, bool]:
    """Expand placeholder tokens in a snippet body.

    Supported: {date} {time} {datetime} {weekday} {date:...} {name}
    {clipboard} {cursor} {ask:Label} {ask:Label|Opt1|Opt2|...} {shell:CMD}.

    `ask_callback` is called ONCE with a list of (label, options) pairs
    found in the snippet. `options` is None for free-text fields, or a
    list of strings for dropdown fields ({ask:Status|Open|Closed}).
    Callback returns {label: answer} or None on cancel. Multiple
    {ask:Label} for the same Label share one field - three sequential
    prompts collapse into one dialog with three fields.

    Returns (rendered_text, cursor_left_count, cancelled).
      - cursor_left_count is the number of Left arrows to send after paste
        so the caret lands where the first {cursor} token was.
      - cancelled=True if the user dismissed the {ask:} dialog - caller
        should abort the paste.

    {clipboard} is captured BEFORE we mutate the clipboard ourselves.
    Only the first {cursor} is honoured; additional ones are stripped.
    """
    if "{" not in text:
        return text, 0, False

    # Pre-pass: collect every unique {ask:Label} so a single dialog
    # gathers all answers. Order = first appearance for predictability.
    # Each field is (label, options-or-None) where options=None means
    # free-text and a list of strings means dropdown.
    ask_fields: list[tuple[str, list[str] | None]] = []
    seen_labels: set[str] = set()
    for m in _PLACEHOLDER_RE.finditer(text):
        tok = m.group(1)
        if tok.startswith("ask:"):
            spec = tok[4:]
            if "|" in spec:
                parts = spec.split("|")
                label = parts[0].strip() or "Value"
                options = [p.strip() for p in parts[1:] if p.strip()] or None
            else:
                label = spec.strip() or "Value"
                options = None
            if label not in seen_labels:
                seen_labels.add(label)
                ask_fields.append((label, options))
    ask_answers: dict | None = None
    if ask_fields:
        ask_answers = ask_callback(ask_fields)
        if ask_answers is None:
            return text, 0, True

    clipboard_value: str | None = None
    cancelled = False

    def repl(m: re.Match[str]) -> str:
        nonlocal clipboard_value, cancelled
        if cancelled:
            return ""
        token = m.group(1)
        now = time.localtime()
        if token == "date":
            return time.strftime("%Y-%m-%d", now)
        if token.startswith("date:"):
            # Three shapes:
            #   {date:%A}        - strftime now
            #   {date:+7d}       - now + 7d, default %Y-%m-%d
            #   {date:+7d:%A}    - now + 7d, formatted
            # Math units: d (day), w (week), m (≈30 days), y (≈365 days).
            spec = token[5:]
            try:
                return _render_date_token(spec, now)
            except (ValueError, TypeError):
                return m.group(0)
        if token == "time":
            return time.strftime("%H:%M", now)
        if token == "datetime":
            return time.strftime("%Y-%m-%d %H:%M", now)
        if token == "weekday":
            # %A is locale-aware: "Tirsdag" on nb_NO, "Tuesday" on en_US.
            return time.strftime("%A", now)
        if token == "name":
            return _full_user_name()
        if token == "clipboard":
            if clipboard_value is None:
                clipboard_value = _read_clipboard_text() or ""
            return clipboard_value
        if token == "selection":
            # PRIMARY (the currently highlighted text). Captured fresh
            # at expansion time - whatever the user has selected when
            # the snippet fires gets pulled in. Enables transform-style
            # snippets: "Quote: {selection}", "TODO: {selection}".
            return _read_primary_selection()
        if token == "cursor":
            return _CURSOR_SENTINEL
        if token.startswith("ask:"):
            spec = token[4:]
            # Strip off |options to get just the label key used in the
            # answers dict.
            label = spec.split("|", 1)[0].strip() or "Value"
            if ask_answers is not None and label in ask_answers:
                return ask_answers[label]
            return ""
        if token.startswith("shell:"):
            return _render_shell_token(token[6:])
        if token.startswith("var:"):
            return _resolve_var(token[4:])
        return m.group(0)

    rendered = _PLACEHOLDER_RE.sub(repl, text)
    if cancelled:
        return text, 0, True

    cursor_left = 0
    if _CURSOR_SENTINEL in rendered:
        idx = rendered.find(_CURSOR_SENTINEL)
        before = rendered[:idx].replace(_CURSOR_SENTINEL, "")
        after = rendered[idx + len(_CURSOR_SENTINEL) :].replace(_CURSOR_SENTINEL, "")
        rendered = before + after
        cursor_left = len(after)
    return rendered, cursor_left, False


# ----- picker dialog -----------------------------------------------------

_PICKER_W = 580
_PICKER_H = 540
# Offset from the pointer so the picker doesn't open with the cursor
# already on a row (would risk an instant-select on the next click).
_PICKER_OFFSET_X = 12
_PICKER_OFFSET_Y = 12


def _is_wayland_kde() -> bool:
    try:
        from platform_backend import get_backend

        return get_backend().name == "wayland_kde"
    except Exception:
        return False


def _monitor_geometry_at(px: int, py: int):
    """Geometry (Gdk.Rectangle) of the monitor holding (px, py), or None.
    GTK 4 dropped Gdk.Display.get_monitor_at_point, so walk the list."""
    display = Gdk.Display.get_default()
    if display is None:
        return None
    monitors = display.get_monitors()
    first = None
    for i in range(monitors.get_n_items()):
        geom = monitors.get_item(i).get_geometry()
        if first is None:
            first = geom
        if geom.x <= px < geom.x + geom.width and geom.y <= py < geom.y + geom.height:
            return geom
    return first


def _position_at_pointer(win: Gtk.Window) -> None:
    """Place the picker near the pointer, clamped to its monitor.

    Borderless windows can't be moved by the user via a title bar (we
    don't have one), so opening at the pointer makes the picker land
    where the user is already looking. The drag handle on the brand
    row lets them move it after that. Positioning goes through
    platform_backend - GTK 4 has no Gtk.Window.move()."""
    # Wayland: a client can't self-position a toplevel; the compositor
    # places the (decorated) picker.
    if _is_wayland_kde():
        return
    try:
        from platform_backend import get_backend

        backend = get_backend()
        px, py = backend.pointer_position()
    except Exception:
        return
    geom = _monitor_geometry_at(px, py)
    if geom is None:
        # Fall back to a static default if monitor lookup fails.
        x, y = max(0, px - _PICKER_W // 2), max(0, py - _PICKER_H // 2)
    else:
        x = px + _PICKER_OFFSET_X
        y = py + _PICKER_OFFSET_Y
        # Clamp so the window fits inside the monitor.
        if x + _PICKER_W > geom.x + geom.width:
            x = px - _PICKER_OFFSET_X - _PICKER_W
        if y + _PICKER_H > geom.y + geom.height:
            y = py - _PICKER_OFFSET_Y - _PICKER_H
        x = max(geom.x, x)
        y = max(geom.y, y)
    try:
        backend.move_popup_window(win, x, y)
    except Exception as exc:
        print(f"[clipboard] could not position picker: {exc}")


def _run_nested(present: Callable[[], None], connect_done: Callable[[Callable], None]) -> None:
    """Block until a dialog is dismissed - the GTK 4 stand-in for
    Gtk.Dialog.run(). Spins a nested main loop exactly like run() did,
    so the synchronous callers (render_placeholders' ask callback, the
    confirm prompts) keep their straight-line shape. `connect_done`
    receives the function to call once the dialog is gone."""
    loop = GLib.MainLoop()

    def _done(*_a):
        if loop.is_running():
            loop.quit()
        return False

    connect_done(_done)
    present()
    loop.run()


class _PickerDialog:
    def __init__(self) -> None:
        self.dialog: Gtk.Window | None = None
        self.target_window: str | None = None
        self.search_entry: Gtk.SearchEntry | None = None
        self.notebook: Gtk.Notebook | None = None
        self.recent_listbox: Gtk.ListBox | None = None
        self.snippets_listbox: Gtk.ListBox | None = None
        self._filter_text = ""
        # Set while a transient child (rename/pin name prompt) is up so
        # focus-out on the picker doesn't blow away the parent the prompt
        # is anchored to. Cleared when the prompt is dismissed.
        self._modal_child_open = False
        # True while the user is dragging the window via the brand-row
        # handle. Suppresses the focus-out auto-dismiss during the drag.
        self._dragging = False

    def show(self, target_window: str | None = None) -> None:
        # Stamp each phase via the real logger (not print) so a UI freeze
        # leaves breadcrumbs in ~/.cache/vinopop/vinopop.log. Times
        # > 100 ms at any phase would explain a perceived freeze.
        import logging

        _log = logging.getLogger("vinopop")
        t0 = time.monotonic()

        def _stamp(label):
            _log.info("[clipboard.show] %s: %.0f ms", label, (time.monotonic() - t0) * 1000)

        _stamp("entered show()")

        if self.dialog is not None and self.dialog.get_visible():
            # Re-position at the current pointer so a second invocation
            # follows the cursor instead of staying where the previous
            # one was.
            _position_at_pointer(self.dialog)
            _force_to_front(self.dialog)
            _stamp("reused existing")
            return
        self.target_window = target_window

        # Adw.Window so the confirm / fill-in prompts (Adw.AlertDialog)
        # render as sheets inside the picker instead of separate windows.
        win = Adw.Window()
        # Tag the picker so the theme can force-clear the dark fallback
        # GTK paints over ListBox content. Without an explicit class
        # the row area kept a dark cast in the light theme.
        win.add_css_class("lp-picker")
        win.set_title("vinoPop - Clipboard & Snippets")
        win.set_default_size(_PICKER_W, _PICKER_H)
        win.set_icon_name("vinopop")
        # Borderless, ephemeral picker on X11 - moved via the brand-row drag
        # handle. On Wayland a client can't self-position an undecorated
        # window, so keep the picker a normal resizable toplevel there: the
        # compositor places it and the brand-row drag still moves it.
        _wl = _is_wayland_kde()
        if _wl:
            win.set_decorated(True)
            win.set_resizable(True)
        else:
            win.set_decorated(False)
            # GTK 3 made this non-resizable. In GTK 4 an undecorated window
            # has no resize edges anyway, and a non-resizable AdwWindow
            # would float its Adw.AlertDialog prompts as separate windows
            # instead of sheets inside the picker - so leave it resizable.
            win.set_resizable(True)
            # Positioning (and the skip-taskbar / keep-above behaviour of
            # a utility popup) is the platform backend's job in GTK 4.
            try:
                from platform_backend import get_backend

                get_backend().prepare_popup_window(win)
            except Exception as exc:
                _log.info("[clipboard.show] prepare_popup_window failed: %s", exc)
        # GTK 4's Gtk.Window.destroy() only drops GTK's own reference, so
        # "destroy" may fire late (or not while Python still holds the
        # window); _dismiss() clears state eagerly and these are backups
        # for the WM closing the window (Wayland decorations).
        win.connect("destroy", self._on_destroy)
        win.connect("close-request", lambda w: (self._on_destroy(w), False)[1])
        win.connect("map", lambda w: _apply_x11_hints(w))
        # Capture phase: the window sees keys before the focused child,
        # matching GTK 3's toplevel key-press-event ordering.
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        win.add_controller(key_ctrl)
        # GTK 4 has no focus-out-event on toplevels; "is-active" flipping
        # to False is the equivalent signal.
        win.connect("notify::is-active", self._on_active_changed)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_margin_top(8)
        outer.set_margin_bottom(8)
        outer.set_margin_start(8)
        outer.set_margin_end(8)

        # Branding header - keeps the picker recognisably "vinoPop"
        # without a window decoration to lean on (this is a borderless
        # popup). The icon is the full-colour app logo so it reads on
        # both the dark and the light theme.
        # A click gesture on the header begins a window-move drag - the
        # only way for the user to reposition a decoration-less window
        # without re-enabling the title bar.
        brand_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        brand_row.set_margin_bottom(6)
        brand_row.set_cursor_from_name("grab")
        header_click = Gtk.GestureClick()
        header_click.set_button(Gdk.BUTTON_PRIMARY)
        header_click.connect("pressed", self._on_header_pressed)
        brand_row.add_controller(header_click)
        brand_icon = Gtk.Image.new_from_icon_name("vinopop")
        brand_icon.set_pixel_size(24)
        brand_row.append(brand_icon)
        brand_text_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        brand_text_box.set_valign(Gtk.Align.CENTER)
        brand_text_box.set_hexpand(True)
        brand_name = Gtk.Label(xalign=0)
        brand_name.set_markup("<b>vinoPop</b>")
        brand_text_box.append(brand_name)
        brand_sub = Gtk.Label(label="Clipboard & snippets", xalign=0)
        brand_sub.add_css_class("dim-label")
        brand_text_box.append(brand_sub)
        brand_row.append(brand_text_box)
        outer.append(brand_row)

        # Search
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Type to filter…")
        self.search_entry.connect("search-changed", self._on_search_changed)
        outer.append(self.search_entry)

        # Tabs
        self.notebook = Gtk.Notebook()
        self.notebook.set_margin_top(6)
        self.notebook.set_vexpand(True)

        # Recent tab
        recent_scroll = Gtk.ScrolledWindow()
        recent_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        recent_scroll.set_vexpand(True)
        self.recent_listbox = Gtk.ListBox()
        self.recent_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.recent_listbox.set_filter_func(self._row_filter)
        self.recent_listbox.connect("row-activated", self._on_row_activated)
        recent_scroll.set_child(self.recent_listbox)
        self.notebook.append_page(recent_scroll, Gtk.Label(label="Recent"))

        # Snippets tab - listbox plus a "+ New snippet" button under it.
        snippets_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        snippets_scroll = Gtk.ScrolledWindow()
        snippets_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        snippets_scroll.set_vexpand(True)
        self.snippets_listbox = Gtk.ListBox()
        self.snippets_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.snippets_listbox.set_filter_func(self._row_filter)
        self.snippets_listbox.connect("row-activated", self._on_row_activated)
        snippets_scroll.set_child(self.snippets_listbox)
        snippets_box.append(snippets_scroll)
        new_snippet_btn = Gtk.Button(label="＋ New snippet…")
        new_snippet_btn.set_halign(Gtk.Align.START)
        new_snippet_btn.set_margin_top(4)
        new_snippet_btn.connect("clicked", self._on_new_snippet_clicked)
        snippets_box.append(new_snippet_btn)
        self.notebook.append_page(snippets_box, Gtk.Label(label="Snippets"))

        outer.append(self.notebook)

        # Hint
        hint = Gtk.Label(xalign=0, margin_top=6)
        hint.set_markup(
            "<small>Enter to paste · Tab to switch tab · Ctrl+P pin · Del remove · "
            "Ctrl+R rename · Snippets support <tt>{date} {time} {datetime} "
            "{clipboard} {cursor} {ask:Label}</tt> · Esc to close</small>"
        )
        hint.set_wrap(True)
        outer.append(hint)

        win.set_content(outer)
        self.dialog = win
        _stamp("built widgets")
        self._populate_recent()
        _stamp("populated recent")
        self._populate_snippets()
        _stamp("populated snippets")
        _position_at_pointer(win)
        win.present()
        _stamp("present")
        # Re-issue the move now that the window is realized and has a
        # size - some WMs ignore move requests on unmapped windows.
        _position_at_pointer(win)
        _force_to_front(win)
        _stamp("force_to_front")
        # Defer search-entry focus until AFTER the WM has had a chance to
        # process our focus request - calling grab_focus immediately after
        # present races the FocusIn event and silently fails.
        if self.search_entry is not None:
            GLib.idle_add(self._grab_search_focus)

    def _grab_search_focus(self) -> bool:
        if self.search_entry is not None and self.dialog is not None:
            self.search_entry.grab_focus()
        return False  # one-shot

    def _on_header_pressed(self, gesture: Gtk.GestureClick, _n_press, x, y) -> None:
        # Left-click on the header begins a window-move drag via the WM.
        # The focus-out that fires while the drag is in progress would
        # otherwise destroy the picker mid-drag; flag it so the focus-out
        # handler defers, and clear the flag a beat after the drag would
        # have completed (GTK has no clean "drag ended" callback for
        # Gdk.Toplevel.begin_move).
        win = self.dialog
        if win is None:
            return
        surface = win.get_surface()
        if surface is None:
            return
        # begin_move wants surface coordinates: header-local -> window ->
        # surface (the surface transform covers any CSD shadow margin).
        ok, pt = gesture.get_widget().compute_point(win, Graphene.Point().init(x, y))
        if not ok:
            return
        tx, ty = win.get_surface_transform()
        self._dragging = True
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        try:
            surface.begin_move(
                gesture.get_device(),
                gesture.get_current_button(),
                pt.x + tx,
                pt.y + ty,
                gesture.get_current_event_time(),
            )
        except Exception:
            pass
        # Clear the flag on a later main-loop tick so any late focus-out
        # events are still suppressed.
        GLib.timeout_add(200, self._clear_dragging)

    def _clear_dragging(self) -> bool:
        self._dragging = False
        return False  # one-shot

    def _on_active_changed(self, _win, _pspec) -> None:
        if self.dialog is None or self.dialog.is_active():
            return
        # Don't close while a rename / pin prompt is showing - that
        # dialog is transient_for=self.dialog and tearing it down would
        # orphan the prompt.
        if self._modal_child_open:
            return
        # Don't close mid-drag - the WM steals focus from us while we
        # move the window, and we want it back when the drag ends.
        if self._dragging:
            return
        # IBus/Cinnamon occasionally bounce focus away from the picker
        # for a single frame (input-method window opens, system tray
        # ping, etc.) and immediately give it back. Destroying on the
        # first focus-out makes the picker vanish mid-search. Defer the
        # check: if focus has actually moved elsewhere 120 ms from now,
        # close. Otherwise the picker is still focused - keep it.

        def _confirm_focus_lost() -> bool:
            if self.dialog is None:
                return False
            if self._modal_child_open:
                return False
            try:
                still_active = self.dialog.is_active()
            except Exception:
                still_active = False
            if not still_active:
                self._dismiss()
            return False  # one-shot

        GLib.timeout_add(120, _confirm_focus_lost)

    def _dismiss(self) -> None:
        """Close the picker and clear its state right away."""
        win = self.dialog
        if win is None:
            return
        self._on_destroy(win)
        win.destroy()

    def _on_destroy(self, win=None, *_):
        # A stale window's late "destroy" must not wipe a newer picker.
        if win is not None and win is not self.dialog:
            return
        import logging

        logging.getLogger("vinopop").info("[clipboard.show] window destroyed -- state cleared")
        self.dialog = None
        self.search_entry = None
        self.notebook = None
        self.recent_listbox = None
        self.snippets_listbox = None
        self._filter_text = ""

    # ---- population ----

    @staticmethod
    def _rows(listbox: Gtk.ListBox) -> list[Gtk.ListBoxRow]:
        """All rows of a listbox, in order. GTK 4 has no get_children(),
        and the listbox's widget children also include category headers."""
        rows = []
        i = 0
        while (row := listbox.get_row_at_index(i)) is not None:
            rows.append(row)
            i += 1
        return rows

    def _populate_recent(self) -> None:
        if self.recent_listbox is None:
            return
        self.recent_listbox.remove_all()
        with _history_lock:
            entries = list(_history)
        if not entries:
            self.recent_listbox.append(
                self._empty_row("History is empty", "Copy something - it appears here.")
            )
        else:
            for entry in entries:
                self.recent_listbox.append(self._make_row(entry, is_snippet=False))

    def _populate_snippets(self) -> None:
        if self.snippets_listbox is None:
            return
        self.snippets_listbox.remove_all()
        with _snippets_lock:
            entries = list(_snippets)
        if not entries:
            self.snippets_listbox.append(
                self._empty_row(
                    "No snippets yet", "Pin (★) an entry from Recent to save it here permanently."
                )
            )
        else:
            # Sort by (category, name) so the category-header function
            # can paint group dividers between buckets. Empty category
            # sorts last under a "General" header.
            def _sort_key(e):
                cat = (e.category or "").strip()
                return (cat.lower() or "￿", (e.name or "").lower())

            for entry in sorted(entries, key=_sort_key):
                self.snippets_listbox.append(self._make_row(entry, is_snippet=True))
            self.snippets_listbox.set_header_func(self._snippet_header_func)

    @staticmethod
    def _snippet_header_func(row, before) -> None:
        """Insert a small category header above the first row in each
        category group. Listbox calls this for every row; `before` is
        the previous row or None for the first one."""
        entry = getattr(row, "entry", None)
        if entry is None:
            row.set_header(None)
            return
        cat = (entry.category or "").strip() or "General"
        prev_entry = getattr(before, "entry", None) if before else None
        prev_cat = (prev_entry.category or "").strip() if prev_entry else None
        prev_cat = prev_cat or "General" if prev_entry else None
        if prev_cat == cat:
            row.set_header(None)
            return
        header = Gtk.Label(xalign=0, margin_top=6, margin_bottom=2, margin_start=10, margin_end=10)
        header.set_markup(f"<small><b>{GLib.markup_escape_text(cat.upper())}</b></small>")
        header.add_css_class("dim-label")
        row.set_header(header)

    def _empty_row(self, title: str, subtitle: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        t = Gtk.Label(xalign=0.5)
        t.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
        s = Gtk.Label(label=subtitle, xalign=0.5, wrap=True)
        s.add_css_class("dim-label")
        box.append(t)
        box.append(s)
        row.set_child(box)
        return row

    def _make_row(self, entry: Entry, is_snippet: bool) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.entry = entry  # type: ignore[attr-defined]
        row.is_snippet = is_snippet  # type: ignore[attr-defined]

        hbox = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )

        # Thumbnail / icon
        if entry.kind == "image" and Path(entry.image_path).is_file():
            try:
                pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    entry.image_path,
                    48,
                    48,
                    True,
                )
                img = Gtk.Image.new_from_paintable(Gdk.Texture.new_for_pixbuf(pix))
                img.set_pixel_size(48)
            except Exception:
                img = Gtk.Image.new_from_icon_name("image-x-generic-symbolic")
                img.set_pixel_size(24)
        else:
            icon_name = "starred-symbolic" if is_snippet else "edit-paste-symbolic"
            img = Gtk.Image.new_from_icon_name(icon_name)
            img.set_pixel_size(24)
        hbox.append(img)

        # Text
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vbox.set_hexpand(True)
        title = Gtk.Label(xalign=0)
        title_markup = f"<b>{GLib.markup_escape_text(entry.preview())}</b>"
        if is_snippet and entry.trigger:
            triggers_display = ", ".join(entry.trigger_list())
            title_markup += (
                f"  <small><span foreground='#5B7DF5'>"
                f"⇥ {GLib.markup_escape_text(triggers_display)}</span></small>"
            )
        title.set_markup(title_markup)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(60)
        vbox.append(title)

        if is_snippet and entry.name:
            # Show a preview of contents under the name, plus a usage
            # counter once the snippet has been used a few times - a
            # quiet "you reach for this one a lot" signal that doubles
            # as a hint for pruning unused snippets.
            preview = entry.text[:80] if entry.kind == "text" else entry.image_path
            preview = preview.replace("\n", " ↵ ")
            count = int(entry.usage_count or 0)
            count_suffix = ""
            if count >= 3:
                count_suffix = f"  ·  used {count} times" if count > 1 else "  ·  used once"
            sub = Gtk.Label(xalign=0)
            sub.set_markup(
                f"<small><span foreground='#8a92a8'>"
                f"{GLib.markup_escape_text(preview)}"
                f"{GLib.markup_escape_text(count_suffix)}"
                f"</span></small>"
            )
            sub.set_ellipsize(Pango.EllipsizeMode.END)
            sub.set_max_width_chars(80)
            vbox.append(sub)
        else:
            meta = time.strftime("%H:%M:%S", time.localtime(entry.timestamp))
            if entry.kind == "text":
                meta += f"  ·  {len(entry.text)} chars"
            sub = Gtk.Label(xalign=0)
            sub.set_markup(f"<small><span foreground='#8a92a8'>{meta}</span></small>")
            vbox.append(sub)
        hbox.append(vbox)

        # Action buttons (GTK 3 packed these from the end: rightmost first)
        if is_snippet:
            # Delete this snippet entirely. Used to be a "star/unpin"
            # button from the days when snippets were pinned clipboard
            # entries; now that snippets are first-class items with
            # triggers and bodies of their own, "delete" matches the
            # mental model better.
            delete_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            delete_btn.set_valign(Gtk.Align.CENTER)
            delete_btn.add_css_class("flat")
            delete_btn.set_tooltip_text("Delete this snippet")
            delete_btn.connect("clicked", self._on_unpin_clicked, entry)
            hbox.append(delete_btn)

            rename_btn = Gtk.Button.new_from_icon_name("document-edit-symbolic")
            rename_btn.set_valign(Gtk.Align.CENTER)
            rename_btn.add_css_class("flat")
            rename_btn.set_tooltip_text("Rename")
            rename_btn.connect("clicked", self._on_rename_clicked, entry)
            hbox.append(rename_btn)
        else:
            # No confirmation, unlike snippets: this is how you get rid of
            # a password you copied by mistake, and the copy is still on
            # the clipboard if you change your mind.
            delete_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            delete_btn.set_valign(Gtk.Align.CENTER)
            delete_btn.add_css_class("flat")
            delete_btn.set_tooltip_text("Remove from history (Delete)")
            delete_btn.connect("clicked", self._on_delete_history_clicked, entry)
            hbox.append(delete_btn)

            pin_btn = Gtk.Button.new_from_icon_name("non-starred-symbolic")
            pin_btn.set_valign(Gtk.Align.CENTER)
            pin_btn.add_css_class("flat")
            pin_btn.set_tooltip_text("Pin as snippet (also lets you name it)")
            pin_btn.connect("clicked", self._on_pin_clicked, entry)
            hbox.append(pin_btn)

        row.set_child(hbox)
        return row

    # ---- filter ----

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._filter_text = entry.get_text().strip().lower()
        if self.recent_listbox:
            self.recent_listbox.invalidate_filter()
        if self.snippets_listbox:
            self.snippets_listbox.invalidate_filter()

    def _row_filter(self, row: Gtk.ListBoxRow) -> bool:
        if not self._filter_text:
            return True
        entry = getattr(row, "entry", None)
        if entry is None:
            return False
        return self._filter_text in entry.search_haystack()

    # ---- actions ----

    def _on_row_activated(self, _listbox, row) -> None:
        entry = getattr(row, "entry", None)
        is_snippet = bool(getattr(row, "is_snippet", False))
        if entry is not None:
            self._paste_and_close(entry, is_snippet=is_snippet)

    def _paste_and_close(self, entry: Entry, is_snippet: bool = False) -> None:
        target = self.target_window  # captured before we showed
        if is_snippet:
            _bump_usage(entry.id)
        cursor_left = 0
        # Only snippets get placeholder substitution. Recent-history entries
        # are pasted verbatim - a "{date}" the user copied from somewhere
        # else should stay literal.
        if is_snippet and entry.kind == "text" and "{" in entry.text:
            rendered, cursor_left, cancelled = render_placeholders(
                entry.text,
                self._ask_placeholder_values,
            )
            if cancelled:
                # Keep the picker open so the user can try again.
                return
            if rendered != entry.text or cursor_left:
                entry = Entry(
                    id=entry.id,
                    timestamp=entry.timestamp,
                    kind=entry.kind,
                    text=rendered,
                    image_path=entry.image_path,
                    name=entry.name,
                )
        if self.dialog is not None:
            self._dismiss()
        _paste_to_window(entry, target, cursor_left=cursor_left)

    # ---- modal helpers (GTK 4 has no Gtk.Dialog.run) ----

    def _run_alert(
        self,
        dlg: Adw.AlertDialog,
        parent: Gtk.Widget | None = None,
        activate_widgets: tuple = (),
    ) -> str:
        """Present an Adw.AlertDialog over `parent` (the picker by default)
        and block until it closes. Returns the response id; the dialog's
        close-response when dismissed. Entries in `activate_widgets` submit
        the default response on Enter, like activates_default did."""
        parent = parent if parent is not None else self.dialog
        result: list[str | None] = [None]

        def _on_response(_d, rid):
            if result[0] is None:
                result[0] = rid

        def _submit(*_a):
            dlg.emit("response", dlg.get_default_response() or "ok")
            dlg.close()

        dlg.connect("response", _on_response)
        for w in activate_widgets:
            w.connect("activate", _submit)

        def _connect(done):
            dlg.connect("closed", done)
            # A picker torn down underneath the prompt must not leave the
            # nested loop spinning.
            if parent is not None:
                parent.connect("destroy", done)

        _run_nested(lambda: dlg.present(parent), _connect)
        return result[0] or dlg.get_close_response()

    def _make_form_window(
        self,
        title: str,
        parent: Gtk.Window | None,
        width: int,
        height: int,
        ok_label: str | None,
    ) -> tuple[Adw.Window, Gtk.Box, Gtk.Button | None]:
        """A modal Adw.Window with a header bar (Cancel / `ok_label`) and a
        vertical content box - the replacement for the bigger Gtk.Dialog
        forms. With ok_label=None it's a plain closable window."""
        win = Adw.Window(title=title, modal=True)
        if parent is not None:
            win.set_transient_for(parent)
        win.set_default_size(width, height)
        header = Adw.HeaderBar()
        ok_btn = None
        if ok_label is not None:
            header.set_show_start_title_buttons(False)
            header.set_show_end_title_buttons(False)
            cancel_btn = Gtk.Button(label="Cancel")
            cancel_btn.connect("clicked", lambda _b: win.close())
            header.pack_start(cancel_btn)
            ok_btn = Gtk.Button(label=ok_label)
            ok_btn.add_css_class("suggested-action")
            header.pack_end(ok_btn)
            win.set_default_widget(ok_btn)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.set_margin_top(8)
        content.set_margin_bottom(8)
        content.set_margin_start(12)
        content.set_margin_end(12)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(content)
        win.set_content(view)
        # Esc closes (= Cancel), as it did for Gtk.Dialog.
        keys = Gtk.EventControllerKey()

        def _on_key(_c, keyval, _code, _state):
            if keyval == Gdk.KEY_Escape:
                win.close()
                return True
            return False

        keys.connect("key-pressed", _on_key)
        win.add_controller(keys)
        return win, content, ok_btn

    @staticmethod
    def _run_form(win: Adw.Window, ok_btn: Gtk.Button | None, collect: Callable[[], object]):
        """Block until `win` closes. Returns collect() if OK was pressed
        (read before the widgets are torn down), else None."""
        result: list = [None]
        if ok_btn is not None:

            def _ok(_b):
                result[0] = collect()
                win.close()

            ok_btn.connect("clicked", _ok)
        _run_nested(win.present, lambda done: win.connect("close-request", done))
        return result[0]

    def _ask_placeholder_values(
        self,
        fields: list[tuple[str, list[str] | None]],
    ) -> dict | None:
        """Bridge for render_placeholders → blocking Adw.AlertDialog form
        with one widget per unique {ask:Label}. `fields` is a list of
        (label, options) pairs: options=None means free-text Entry,
        a list of strings becomes a Gtk.DropDown. Returns
        {label: value} on OK, None on Cancel."""
        if not fields:
            return {}
        self._modal_child_open = True
        try:
            title = "Snippet" if len(fields) > 1 else fields[0][0]
            dlg = Adw.AlertDialog(heading=title)
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("ok", "OK")
            dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
            dlg.set_default_response("ok")
            dlg.set_close_response("cancel")
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            widgets: dict = {}
            entries: list = []
            first_entry = None
            for i, (label, options) in enumerate(fields):
                lbl = Gtk.Label(xalign=0)
                lbl.set_markup(f"<b>{GLib.markup_escape_text(label)}</b>")
                content.append(lbl)
                if options:
                    combo = Gtk.DropDown.new(Gtk.StringList.new(list(options)), None)
                    combo.set_selected(0)
                    content.append(combo)
                    widgets[label] = combo
                else:
                    entry = Gtk.Entry()
                    entry.set_width_chars(40)
                    content.append(entry)
                    widgets[label] = entry
                    entries.append(entry)
                    if i == 0:
                        first_entry = entry
            dlg.set_extra_child(content)
            if first_entry is not None:
                dlg.set_focus(first_entry)
            answers = {}

            def _collect(*_a):
                for label, w in widgets.items():
                    if isinstance(w, Gtk.DropDown):
                        item = w.get_selected_item()
                        answers[label] = item.get_string() if item is not None else ""
                    else:
                        answers[label] = w.get_text()

            # Read the values as the response fires, before teardown.
            dlg.connect("response", _collect)
            response = self._run_alert(dlg, activate_widgets=tuple(entries))
            if response != "ok":
                return None
            return answers
        finally:
            self._modal_child_open = False

    def _on_pin_clicked(self, _btn, entry: Entry) -> None:
        result = self._ask_edit_snippet_meta(
            default_name=entry.preview(40),
            default_trigger="",
            default_category="",
            default_text=entry.text if entry.kind == "text" else "",
        )
        if result is None:
            return  # cancelled
        name, trigger, category, _new_text = result
        _pin_entry(entry, name)
        # _pin_entry inserts at index 0 - find that newest snippet and
        # write the trigger/category through. Avoids reaching into internals.
        with _snippets_lock:
            new_id = _snippets[0].id if _snippets else None
        if new_id is not None:
            if trigger:
                _set_snippet_trigger(new_id, trigger)
            if category:
                _set_snippet_category(new_id, category)
        self._populate_snippets()
        if self.notebook is not None:
            self.notebook.set_current_page(1)

    def _on_unpin_clicked(self, _btn, entry: Entry) -> None:
        """Delete a snippet. Confirms first since the body, triggers,
        and category all go away with it - no undo."""
        label = entry.name or entry.preview(40) or "this snippet"
        self._modal_child_open = True
        try:
            dlg = Adw.AlertDialog(
                heading=f'Delete "{label}"?',
                body=(
                    "The snippet, its triggers, and its category will "
                    "be removed. This can't be undone."
                ),
            )
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("delete", "Delete")
            # Style the destructive button red so the choice reads at a glance.
            dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_close_response("cancel")
            response = self._run_alert(dlg)
        finally:
            self._modal_child_open = False
        if response != "delete":
            return
        _unpin_snippet(entry.id)
        self._populate_snippets()

    def _on_delete_history_clicked(self, _btn, entry: Entry) -> None:
        listbox = self.recent_listbox
        if listbox is None:
            return

        def visible_rows():
            return [r for r in self._rows(listbox) if r.get_selectable() and self._row_filter(r)]

        # Remember the position so keyboard users land on the next entry.
        index = next((i for i, r in enumerate(visible_rows()) if r.entry is entry), 0)
        _remove_history_entry(entry.id)
        self._populate_recent()
        visible = visible_rows()
        if visible:
            row = visible[min(index, len(visible) - 1)]
            listbox.select_row(row)
            row.grab_focus()
        elif self.search_entry is not None:
            self.search_entry.grab_focus()

    def _on_rename_clicked(self, _btn, entry: Entry) -> None:
        result = self._ask_edit_snippet_meta(
            default_name=entry.name or entry.preview(40),
            default_trigger=entry.trigger,
            default_category=entry.category,
            default_text=entry.text if entry.kind == "text" else "",
        )
        if result is None:
            return
        new_name, new_trigger, new_category, new_text = result
        new_triggers = [t.strip() for t in new_trigger.split(",") if t.strip()]
        conflicts = _find_trigger_conflicts(new_triggers, own_id=entry.id)
        if conflicts and not self._confirm_trigger_conflicts(conflicts):
            return
        _rename_snippet(entry.id, new_name)
        _set_snippet_trigger(entry.id, new_trigger)
        _set_snippet_category(entry.id, new_category)
        if entry.kind == "text":
            _set_snippet_text(entry.id, new_text)
        self._populate_snippets()

    def _show_snippet_help_dialog(self, parent: Gtk.Window) -> None:
        """A friendly walkthrough of what snippets are, how triggers work,
        and what every placeholder does. Intentionally written for the
        person who is NOT a programmer - heavy on examples, light on jargon."""
        self._modal_child_open = True
        try:
            # Plain closable window (header-bar close button stands in
            # for the old dialog's "Close" action button).
            dlg, content, _ok = self._make_form_window("Snippet guide", parent, 620, 580, None)
            for set_margin in (
                content.set_margin_top,
                content.set_margin_bottom,
                content.set_margin_start,
                content.set_margin_end,
            ):
                set_margin(0)

            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroll.set_hexpand(True)
            scroll.set_vexpand(True)

            outer = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=14,
                margin_top=16,
                margin_bottom=16,
                margin_start=22,
                margin_end=22,
            )

            # Hero
            hero = Gtk.Label(xalign=0)
            hero.set_markup("<span size='xx-large' weight='bold'>Snippets, in plain words</span>")
            outer.append(hero)

            intro = Gtk.Label(xalign=0)
            intro.set_wrap(True)
            intro.set_markup(
                "<span foreground='#8a92a8'>A <b>snippet</b> is a "
                "piece of text you save once and reuse forever. Your "
                "email signature, a phone number, a reply to a "
                "frequently asked question, a bug-report template - "
                "anything you find yourself typing more than twice.</span>"
            )
            outer.append(intro)

            sep1 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep1.set_margin_top(4)
            sep1.set_margin_bottom(4)
            outer.append(sep1)

            # Three ways
            ways_head = Gtk.Label(xalign=0)
            ways_head.set_markup("<span size='large' weight='bold'>Three ways to use one</span>")
            outer.append(ways_head)

            for n, head, body in [
                (
                    "1.",
                    "Pick it from the list",
                    "Open the clipboard picker (your shortcut, usually "
                    "<tt>Super+Shift+V</tt>), go to the <b>Snippets</b> tab, "
                    "click the one you want. It pastes wherever your cursor is.",
                ),
                (
                    "2.",
                    "Type a trigger",
                    "If you give a snippet a shortcut like <tt>;email</tt>, "
                    "you can type <tt>;email</tt> followed by a space "
                    "anywhere on your computer and vinoPop replaces it "
                    "with the full text. Starting your triggers with a "
                    "non-letter character (<tt>;</tt>, <tt>:</tt>, "
                    "<tt>!</tt>, <tt>@</tt>) is the usual convention - it "
                    "keeps them from clashing with real words you type. "
                    "One snippet can have many triggers, comma-separated: "
                    "<tt>;email, ;e, em</tt>. Triggers must be turned on "
                    "in Settings -> Hotkeys.",
                ),
                (
                    "3.",
                    "Let it fill in the blanks",
                    "A snippet can contain little tags like <tt>{date}</tt> "
                    "or <tt>{ask:Name}</tt>. They're called <b>placeholders</b> "
                    "and they get filled in for you when you paste - "
                    "today's date, your clipboard, an answer to a question, etc.",
                ),
            ]:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
                num = Gtk.Label(xalign=0, yalign=0)
                num.set_markup(f"<span foreground='#5B7DF5' weight='bold' size='large'>{n}</span>")
                row.append(num)
                text = Gtk.Label(xalign=0, yalign=0)
                text.set_markup(f"<b>{head}</b>\n<span foreground='#8a92a8'>{body}</span>")
                text.set_wrap(True)
                text.set_hexpand(True)
                row.append(text)
                outer.append(row)

            sep2 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep2.set_margin_top(4)
            sep2.set_margin_bottom(4)
            outer.append(sep2)

            # Placeholders table
            ph_head = Gtk.Label(xalign=0)
            ph_head.set_markup("<span size='large' weight='bold'>Placeholders you can use</span>")
            outer.append(ph_head)

            grid = Gtk.Grid(column_spacing=14, row_spacing=8)
            grid.set_margin_top(4)
            ph_rows = [
                ("{date}", "Today's date - e.g. 2026-05-27."),
                ("{time}", "Current time - e.g. 14:30."),
                ("{datetime}", "Both at once - 2026-05-27 14:30."),
                ("{weekday}", "Name of the day, in your system language - e.g. Wednesday."),
                (
                    "{date:FORMAT}",
                    "Custom date format. <tt>%A</tt>=weekday, <tt>%d</tt>=day, "
                    "<tt>%B</tt>=month name, <tt>%Y</tt>=year, <tt>%V</tt>=week number. "
                    "Example: <tt>{date:%A %d %B}</tt> → Wednesday 27 May.",
                ),
                (
                    "{date:+7d}",
                    "Date math: shift by days (d), weeks (w), months "
                    "(m≈30 days), years (y). <tt>+7d</tt> = a week from now. "
                    "Combine with format: <tt>{date:+7d:%A}</tt>.",
                ),
                ("{name}", "Your full name, taken from your user account."),
                ("{clipboard}", "Whatever's currently on your clipboard at paste time."),
                (
                    "{selection}",
                    "Whatever you have highlighted on screen right now. "
                    "Lets you build wrap-the-selection snippets like "
                    '<tt>Quote: "{selection}"</tt> or <tt>TODO: {selection}</tt>.',
                ),
                (
                    "{cursor}",
                    "Marks where the typing cursor should land after paste. "
                    "Useful for templates like <tt>[ ] {cursor}</tt>.",
                ),
                (
                    "{ask:Label}",
                    "Pops up a small dialog and asks for a value when you "
                    "paste. Rename <tt>Label</tt> to whatever the prompt "
                    "should say. Use the same Label twice and it'll only "
                    "ask once.",
                ),
                (
                    "{ask:Label|A|B|C}",
                    "Same idea but with a dropdown of choices. "
                    "<tt>{ask:Status|Open|Closed|WIP}</tt> gives you a picker.",
                ),
                (
                    "{shell:CMD}",
                    "Runs a shell command and pastes its output. Off by "
                    "default for safety - turn on in Settings if you want it. "
                    "Example: <tt>{shell:date -u}</tt> for UTC time.",
                ),
            ]
            for i, (tag, desc) in enumerate(ph_rows):
                tag_lbl = Gtk.Label(xalign=0, yalign=0)
                tag_lbl.set_markup(
                    f"<tt><span foreground='#7C3AED'>{GLib.markup_escape_text(tag)}</span></tt>"
                )
                tag_lbl.set_selectable(True)
                grid.attach(tag_lbl, 0, i, 1, 1)
                desc_lbl = Gtk.Label(xalign=0, yalign=0)
                desc_lbl.set_markup(f"<span foreground='#9ba3b8'>{desc}</span>")
                desc_lbl.set_wrap(True)
                desc_lbl.set_hexpand(True)
                grid.attach(desc_lbl, 1, i, 1, 1)
            outer.append(grid)

            sep3 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep3.set_margin_top(4)
            sep3.set_margin_bottom(4)
            outer.append(sep3)

            # Recipes
            rec_head = Gtk.Label(xalign=0)
            rec_head.set_markup("<span size='large' weight='bold'>A few real-world recipes</span>")
            outer.append(rec_head)

            for title, body_text, note in [
                (
                    "Email signature with today's date",
                    "Best,\n{name}\n{date}",
                    "Trigger: <tt>;sig</tt>",
                ),
                (
                    "Support reply that asks for the details",
                    "Hi {ask:Customer},\n\n"
                    "Thanks for reaching out about {ask:Topic}.\n"
                    "Current status: {ask:Status|New|In progress|Resolved}.\n\n"
                    "Best,\n{name}",
                    "One dialog asks for all three at once.",
                ),
                (
                    "Meeting one week out",
                    "Meeting on {date:+7d:%A %d %B}",
                    "Renders e.g. <i>Meeting on Wednesday 03 June</i>.",
                ),
                (
                    "Empty checkbox where the cursor lands after",
                    "[ ] {cursor}",
                    "Paste, then start typing the to-do - the cursor is already in place.",
                ),
            ]:
                head = Gtk.Label(xalign=0)
                head.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
                outer.append(head)
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                box.set_margin_start(8)
                # Code block: a Frame with the .lp-card CSS class so the
                # surface follows the active theme (dark vs light). The
                # accent foreground is fine on either surface.
                code_frame = Gtk.Frame()
                code_frame.add_css_class("lp-card")
                code = Gtk.Label(xalign=0, selectable=True)
                code.set_markup(
                    f"<tt><span foreground='#5B7DF5'>"
                    f"{GLib.markup_escape_text(body_text)}</span></tt>"
                )
                code.set_wrap(True)
                code.set_margin_top(4)
                code.set_margin_bottom(4)
                code.set_margin_start(8)
                code.set_margin_end(8)
                code_frame.set_child(code)
                box.append(code_frame)
                note_lbl = Gtk.Label(xalign=0)
                note_lbl.set_markup(f"<small><span foreground='#8a92a8'>{note}</span></small>")
                note_lbl.set_wrap(True)
                box.append(note_lbl)
                outer.append(box)

            sep4 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep4.set_margin_top(4)
            sep4.set_margin_bottom(4)
            outer.append(sep4)

            # Case-tip
            case_head = Gtk.Label(xalign=0)
            case_head.set_markup("<span size='large' weight='bold'>One small case-trick</span>")
            outer.append(case_head)
            case_body = Gtk.Label(xalign=0)
            case_body.set_wrap(True)
            case_body.set_markup(
                "<span foreground='#9ba3b8'>"
                "Triggers match without caring about capital letters, "
                "but they copy your capitalisation to the output. "
                "If your trigger is <tt>;name</tt> and the snippet "
                "is <tt>alex morgan</tt>:"
                "</span>\n\n"
                "  <tt>;name </tt> -> alex morgan\n"
                "  <tt>;Name </tt> -> Alex morgan\n"
                "  <tt>;NAME </tt> -> ALEX MORGAN"
            )
            outer.append(case_body)

            sep5 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep5.set_margin_top(4)
            sep5.set_margin_bottom(4)
            outer.append(sep5)

            # Type a trigger literally
            esc_head = Gtk.Label(xalign=0)
            esc_head.set_markup(
                "<span size='large' weight='bold'>Typing a trigger without expanding it</span>"
            )
            outer.append(esc_head)
            esc_body = Gtk.Label(xalign=0)
            esc_body.set_wrap(True)
            esc_body.set_markup(
                "<span foreground='#9ba3b8'>"
                "Sometimes you need to write the literal trigger - "
                "for instance, when telling a colleague which "
                "shortcut you use. A few ways:"
                "</span>\n\n"
                "  Wrap it in quotes or brackets:  "
                '<tt>";email"</tt>, <tt>(;email)</tt>, '
                "<tt>[;email]</tt>\n"
                "    The trailing character isn't a space/tab/enter, "
                "so expansion never fires.\n\n"
                "  Press Esc mid-word: clears the trigger buffer "
                "so the next space won't expand.\n\n"
                "  Toggle <i>Snippet triggers</i> off in Settings "
                "if you'll be typing literal triggers a lot.\n\n"
                "<span foreground='#8a92a8'>"
                "Tip: a trigger that begins with a non-letter "
                "character (<tt>;</tt>, <tt>:</tt>, <tt>!</tt>, "
                "<tt>@</tt>, <tt>(</tt>, etc.) practically never "
                "fires by accident, because real words don't start "
                "that way."
                "</span>"
            )
            outer.append(esc_body)

            scroll.set_child(outer)
            content.append(scroll)
            self._run_form(dlg, None, lambda: None)
        finally:
            self._modal_child_open = False

    def _ask_edit_snippet_meta(
        self,
        default_name: str = "",
        default_trigger: str = "",
        default_category: str = "",
        default_text: str = "",
    ) -> tuple[str, str, str, str] | None:
        """Edit name + trigger + category + body for an existing snippet.
        Returns (name, trigger, category, text) on OK, None on Cancel."""
        self._modal_child_open = True
        try:
            dlg, content, ok_btn = self._make_form_window(
                "Edit snippet", self.dialog, 520, 460, "Save"
            )
            content.set_spacing(4)

            name_lbl = Gtk.Label(xalign=0)
            name_lbl.set_markup("<b>Name</b>")
            content.append(name_lbl)
            name_entry = Gtk.Entry()
            name_entry.set_text(default_name)
            name_entry.set_activates_default(True)
            name_entry.set_width_chars(36)
            content.append(name_entry)

            trig_lbl = Gtk.Label(xalign=0, margin_top=6)
            trig_lbl.set_markup("<b>Trigger(s)</b>  <small>(optional, comma-separated)</small>")
            content.append(trig_lbl)
            trig_entry = Gtk.Entry()
            trig_entry.set_text(default_trigger)
            trig_entry.set_activates_default(True)
            triggers_on = bool(_cfg("snippet_triggers_enabled", False))
            if triggers_on:
                trig_entry.set_placeholder_text("e.g. ;email, ;e - any of them auto-expands")
            else:
                trig_entry.set_placeholder_text("Enable 'Snippet triggers' in Settings to use")
            content.append(trig_entry)

            cat_lbl = Gtk.Label(xalign=0, margin_top=6)
            cat_lbl.set_markup("<b>Category</b>  <small>(optional)</small>")
            content.append(cat_lbl)
            cat_entry = Gtk.Entry()
            cat_entry.set_text(default_category)
            cat_entry.set_activates_default(True)
            cat_entry.set_placeholder_text("e.g. Email, Code, Personal")
            try:
                with _snippets_lock:
                    cats = sorted({(s.category or "").strip() for s in _snippets if s.category})
                if cats:
                    store = Gtk.ListStore(str)
                    for c in cats:
                        store.append([c])
                    completion = Gtk.EntryCompletion()
                    completion.set_model(store)
                    completion.set_text_column(0)
                    completion.set_inline_completion(True)
                    cat_entry.set_completion(completion)
            except Exception:
                pass
            content.append(cat_entry)

            body_lbl = Gtk.Label(xalign=0, margin_top=6)
            body_lbl.set_markup(
                "<b>Text</b>  <small>(the snippet body - what gets "
                "pasted when this snippet fires)</small>"
            )
            content.append(body_lbl)
            body_scroll = Gtk.ScrolledWindow()
            body_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            body_scroll.set_min_content_height(140)
            body_scroll.set_has_frame(True)
            body_scroll.set_vexpand(True)
            body_view = Gtk.TextView()
            body_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            body_view.set_accepts_tab(False)
            body_view.add_css_class("lp-cmd-edit")
            body_view.get_buffer().set_text(default_text)
            body_scroll.set_child(body_view)
            content.append(body_scroll)

            # Small "Snippet guide" button so help is reachable from the
            # rename/edit dialog too, not just New snippet.
            guide_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, margin_top=10)
            spacer = Gtk.Label()
            spacer.set_hexpand(True)
            guide_row.append(spacer)
            guide_btn = Gtk.Button(label="📖 Snippet guide")
            guide_btn.connect("clicked", lambda _b: self._show_snippet_help_dialog(parent=dlg))
            guide_row.append(guide_btn)
            content.append(guide_row)

            def _collect():
                buf = body_view.get_buffer()
                return (
                    name_entry.get_text().strip(),
                    trig_entry.get_text().strip(),
                    cat_entry.get_text().strip(),
                    buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True),
                )

            dlg.set_focus(name_entry)
            return self._run_form(dlg, ok_btn, _collect)
        finally:
            self._modal_child_open = False

    def _on_new_snippet_clicked(self, _btn) -> None:
        result = self._ask_new_snippet()
        if result is None:
            return
        name, body, trigger, category = result
        if not body.strip():
            return
        conflicts = _find_trigger_conflicts([t.strip() for t in trigger.split(",") if t.strip()])
        if conflicts and not self._confirm_trigger_conflicts(conflicts):
            return
        _create_snippet(name=name, text=body, trigger=trigger, category=category)
        self._populate_snippets()
        if self.notebook is not None:
            self.notebook.set_current_page(1)

    def _confirm_trigger_conflicts(
        self,
        conflicts: list[tuple],
    ) -> bool:
        """Show a small dialog listing which triggers are already in use
        by which other snippet, and ask whether to save anyway. Returns
        True for 'save anyway' (the new snippet wins the index slot),
        False for 'cancel'."""
        lines = [f"  '{trig}'  is already used by  '{owner}'" for trig, owner in conflicts]
        body = (
            "These triggers are already in use:\n\n"
            + "\n".join(lines)
            + "\n\nIf you save, the new snippet will take over those "
            "triggers. The other snippet keeps its other triggers, if any."
        )
        self._modal_child_open = True
        try:
            dlg = Adw.AlertDialog(heading="Trigger already in use", body=body)
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("ok", "Save anyway")
            dlg.set_close_response("cancel")
            return self._run_alert(dlg) == "ok"
        finally:
            self._modal_child_open = False

    def _ask_new_snippet(self) -> tuple[str, str, str, str] | None:
        """Modal dialog: name + body + trigger + category.
        Returns (name, body, trigger, category) on OK, None on Cancel."""
        self._modal_child_open = True
        try:
            dlg, content, ok_btn = self._make_form_window(
                "New snippet", self.dialog, 480, 360, "Save"
            )
            content.set_spacing(6)

            name_label = Gtk.Label(xalign=0)
            name_label.set_markup("<b>Name</b>")
            content.append(name_label)
            name_entry = Gtk.Entry()
            name_entry.set_placeholder_text("Short label (optional)")
            content.append(name_entry)

            triggers_on = bool(_cfg("snippet_triggers_enabled", False))
            trigger_label = Gtk.Label(xalign=0, margin_top=6)
            trigger_label.set_markup(
                "<b>Trigger(s)</b>  <small>(optional, comma-separated)</small>"
            )
            content.append(trigger_label)
            trigger_entry = Gtk.Entry()
            if triggers_on:
                trigger_entry.set_placeholder_text(
                    "e.g. ;email, ;e - any of them + space/tab auto-expands"
                )
            else:
                trigger_entry.set_placeholder_text(
                    "Set, then enable 'Snippet triggers' in Settings to auto-expand"
                )
            content.append(trigger_entry)

            cat_label = Gtk.Label(xalign=0, margin_top=6)
            cat_label.set_markup(
                "<b>Category</b>  <small>(optional - keeps the picker tidy)</small>"
            )
            content.append(cat_label)
            cat_entry = Gtk.Entry()
            cat_entry.set_placeholder_text("e.g. Email, Code, Personal. Leave blank for General.")
            # Auto-complete from categories that already exist so the
            # user doesn't end up with "Email" and "email" as separate
            # buckets just from capitalisation.
            try:
                with _snippets_lock:
                    cats = sorted({(s.category or "").strip() for s in _snippets if s.category})
                if cats:
                    store = Gtk.ListStore(str)
                    for c in cats:
                        store.append([c])
                    completion = Gtk.EntryCompletion()
                    completion.set_model(store)
                    completion.set_text_column(0)
                    completion.set_inline_completion(True)
                    cat_entry.set_completion(completion)
            except Exception:
                pass
            content.append(cat_entry)

            body_label = Gtk.Label(xalign=0, margin_top=6)
            body_label.set_markup(
                "<b>Text</b>  <small>(this is the snippet body - "
                "type or paste what you want to expand to)</small>"
            )
            content.append(body_label)
            body_scroll = Gtk.ScrolledWindow()
            body_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            body_scroll.set_min_content_height(180)
            # IN shadow draws the actual visible border around the scroll
            # area. Without this, no amount of CSS background on the inner
            # TextView reads as a "real" input field because the field
            # has no frame around it.
            body_scroll.set_has_frame(True)
            body_scroll.set_vexpand(True)
            body_view = Gtk.TextView()
            body_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            body_view.set_accepts_tab(False)
            # Reuse the same class the terminal-confirm dialog's editable
            # state uses - it gives the standard Entry-field look
            # (lighter background, padding, blue caret).
            body_view.add_css_class("lp-cmd-edit")
            body_scroll.set_child(body_view)
            content.append(body_scroll)

            help_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_top=6)
            help_intro = Gtk.Label(xalign=0)
            help_intro.set_markup(
                "<small>Click to insert a placeholder (filled in at paste time):</small>"
            )
            help_intro.set_hexpand(True)
            help_row.append(help_intro)
            help_btn = Gtk.Button(label="📖 Snippet guide")
            help_btn.set_tooltip_text(
                "Open a friendly walkthrough of snippets, triggers, and placeholders."
            )
            help_btn.connect("clicked", lambda _b: self._show_snippet_help_dialog(parent=dlg))
            help_row.append(help_btn)
            content.append(help_row)

            chips = Gtk.FlowBox()
            chips.set_selection_mode(Gtk.SelectionMode.NONE)
            chips.set_max_children_per_line(6)
            # GTK 4 sizes a FlowBox's minimum for one child per line, which
            # made the dialog ~1100 px tall; ask for a few per line instead.
            chips.set_min_children_per_line(3)
            chips.set_row_spacing(4)
            chips.set_column_spacing(4)

            def insert_token(token: str, select_offset: int = 0, select_len: int = 0) -> None:
                buf = body_view.get_buffer()
                if buf.get_has_selection():
                    buf.delete_selection(False, True)
                start_offset = buf.get_iter_at_mark(buf.get_insert()).get_offset()
                buf.insert_at_cursor(token)
                if select_len > 0:
                    sel_start = buf.get_iter_at_offset(start_offset + select_offset)
                    sel_end = buf.get_iter_at_offset(start_offset + select_offset + select_len)
                    buf.select_range(sel_start, sel_end)
                body_view.grab_focus()

            # (label, token, tooltip, select_offset_in_token, select_len)
            # select_* are non-zero for {ask:Label} / {date:FORMAT} so
            # the editable part is auto-selected and the user can just
            # type to overwrite it.
            chip_specs = [
                ("{date}", "{date}", "Current date (YYYY-MM-DD)", 0, 0),
                ("{time}", "{time}", "Current time (HH:MM)", 0, 0),
                ("{datetime}", "{datetime}", "Date + time together", 0, 0),
                ("{weekday}", "{weekday}", "Name of the day - follows your system language", 0, 0),
                (
                    "{date:FORMAT}",
                    "{date:%A %d %B}",
                    "Custom date format. Codes: %A=weekday, %d=day, %B=month name, %V=week number, %Y=year. Edit the highlighted part to change.",
                    6,
                    8,
                ),
                (
                    "{date:+7d}",
                    "{date:+7d}",
                    "Date math: shift by days (d) / weeks (w) / months (m≈30d) / y. Edit the number to change. Combine with format like {date:+7d:%A}.",
                    6,
                    3,
                ),
                ("{name}", "{name}", "Your full name (from your user account)", 0, 0),
                ("{clipboard}", "{clipboard}", "Current clipboard contents", 0, 0),
                (
                    "{selection}",
                    "{selection}",
                    "Whatever you currently have highlighted on screen (PRIMARY selection). Lets you wrap or transform selected text - e.g. 'Quote: {selection}'.",
                    0,
                    0,
                ),
                ("{cursor}", "{cursor}", "Where the caret lands after paste", 0, 0),
                (
                    "{ask:Label}",
                    "{ask:Label}",
                    "Prompt for a value at paste time. Multiple {ask:} fields show in one dialog. Rename 'Label' to what you want the prompt to say.",
                    5,
                    5,
                ),
                (
                    "{ask:Label|Opt}",
                    "{ask:Status|Open|Closed|WIP}",
                    "Dropdown variant: pipe-separated options after the label give a chooser instead of free text. Edit the label and options to match your case.",
                    5,
                    6,
                ),
                (
                    "{var:NAME}",
                    "{var:email}",
                    "Pull in a shared variable you defined once in Settings > Snippet variables. Use the same {var:email} or {var:signature} across many snippets - change it once, all snippets pick up the new value.",
                    5,
                    5,
                ),
                (
                    "{shell:CMD}",
                    "{shell:date -u}",
                    "Run a shell command and paste its output. Requires 'Shell expansion' enabled in Settings - disabled by default for safety.",
                    7,
                    8,
                ),
            ]
            for label, token, tip, sel_off, sel_len in chip_specs:
                btn = Gtk.Button(label=label)
                btn.set_tooltip_text(tip)
                # Monospace makes the brace syntax read like code.
                child = btn.get_child()
                if isinstance(child, Gtk.Label):
                    child.set_use_markup(False)
                    child.add_css_class("monospace")
                btn.connect(
                    "clicked",
                    lambda _b, t=token, so=sel_off, sl=sel_len: insert_token(t, so, sl),
                )
                chips.append(btn)
            content.append(chips)

            def _collect():
                buf = body_view.get_buffer()
                return (
                    name_entry.get_text().strip(),
                    buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True),
                    trigger_entry.get_text().strip(),
                    cat_entry.get_text().strip(),
                )

            dlg.set_focus(name_entry)
            return self._run_form(dlg, ok_btn, _collect)
        finally:
            self._modal_child_open = False

    def _ask_for_name(self, default: str = "") -> str | None:
        self._modal_child_open = True
        try:
            dlg = Adw.AlertDialog(heading="Snippet name")
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("ok", "OK")
            dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
            dlg.set_default_response("ok")
            dlg.set_close_response("cancel")
            entry = Gtk.Entry()
            entry.set_text(default)
            dlg.set_extra_child(entry)
            dlg.set_focus(entry)
            value = [""]
            dlg.connect("response", lambda *_a: value.__setitem__(0, entry.get_text().strip()))
            response = self._run_alert(dlg, activate_widgets=(entry,))
            if response != "ok":
                return None
            return value[0]
        finally:
            self._modal_child_open = False

    # ---- keyboard ----

    def _current_listbox(self) -> Gtk.ListBox | None:
        if self.notebook is None:
            return None
        page = self.notebook.get_current_page()
        return self.recent_listbox if page == 0 else self.snippets_listbox

    def _first_visible_row(self, listbox: Gtk.ListBox):
        """Return the first row that survives the current filter, or
        None if the listbox is empty or fully filtered out."""
        for row in self._rows(listbox):
            if row.get_child_visible() and (row.get_mapped() or self._filter_text == ""):
                # Re-check the filter explicitly because get_visible()
                # lags behind invalidate_filter() by a tick.
                if self._row_filter(row):
                    return row
        return None

    def _focus_within(self, widget: Gtk.Widget | None) -> bool:
        """True if the picker's focus widget is `widget` or inside it (a
        GTK 4 SearchEntry's real focus is its internal Gtk.Text)."""
        if widget is None or self.dialog is None:
            return False
        focused = self.dialog.get_focus()
        while focused is not None:
            if focused is widget:
                return True
            focused = focused.get_parent()
        return False

    def _on_key_pressed(self, _ctrl, keyval: int, _keycode: int, state) -> bool:
        # A prompt (Adw.AlertDialog) shown inside the picker gets its own
        # keys - Enter/Esc/typing belong to it, not to the list.
        if self._modal_child_open:
            return False
        return self._handle_key(keyval, state)

    def _handle_key(self, keyval: int, state) -> bool:
        if keyval == Gdk.KEY_Escape:
            if self.dialog is not None:
                self._dismiss()
            return True
        # Tab / Shift+Tab → flip between the Recent and Snippets tabs.
        # Replaces the default focus-cycling (search-entry → listbox →
        # +New button etc.) which is overkill for a 2-tab popup.
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            if self.notebook is not None and self.notebook.get_n_pages() > 1:
                cur = self.notebook.get_current_page()
                self.notebook.set_current_page(1 - cur)
            return True
        # Down / Up → jump straight into the listbox the first time
        # (skipping search-entry + tab-labels), then let GTK's native
        # ListBox navigation drive subsequent presses.
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            listbox = self._current_listbox()
            if listbox is None:
                return False
            # Already-in-listbox check: walk the focus chain up from
            # whichever widget currently has focus. Both Gtk.ListBox
            # itself and its individual rows count as "in" - we want
            # subsequent arrow presses to fall through to GTK's
            # built-in row navigation in either case.
            if self._focus_within(listbox):
                return False  # let GTK move the selection
            # First press from outside: focus the right-edge row and
            # let it own focus. With a row focused, GtkListBox's
            # built-in Down/Up handling moves to the adjacent row.
            if keyval == Gdk.KEY_Down:
                target = self._first_visible_row(listbox)
            else:
                target = None
                for row in self._rows(listbox):
                    if self._row_filter(row):
                        target = row
            if target is not None:
                listbox.select_row(target)
                target.grab_focus()
            else:
                listbox.grab_focus()
            return True
        # Enter → if focus is on a list row, paste it. Lets the user
        # arrow-down + Enter without ever touching the mouse.
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            listbox = self._current_listbox()
            if listbox is not None:
                selected = listbox.get_selected_row()
                if selected is not None:
                    listbox.emit("row-activated", selected)
                    return True
            return False
        # Ctrl+P → pin currently selected (if in Recent)
        if state & Gdk.ModifierType.CONTROL_MASK and keyval == Gdk.KEY_p:
            self._action_on_selected("pin")
            return True
        # Delete → remove the focused Recent row from history. Plain Delete
        # only while a row has focus (in the search box it edits the text);
        # Ctrl+Delete works from anywhere, like Ctrl+P.
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
            on_recent = self.notebook is not None and self.notebook.get_current_page() == 0
            if on_recent and (ctrl or self._focus_within(self.recent_listbox)):
                self._action_on_selected("delete")
                return True
            return False
        # Ctrl+R → rename (if in Snippets)
        if state & Gdk.ModifierType.CONTROL_MASK and keyval == Gdk.KEY_r:
            self._action_on_selected("rename")
            return True
        # Printable character with no Ctrl/Alt → start typing into the
        # search entry, regardless of where focus currently is. Lets
        # the user arrow-down to browse, then immediately type to
        # filter without explicitly clicking the search box.
        modifiers = (
            Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK
        )
        if not (state & modifiers):
            unicode_val = Gdk.keyval_to_unicode(keyval)
            if unicode_val and 0x20 <= unicode_val < 0x10FFFF:
                ch = chr(unicode_val)
                if ch.isprintable() and self.search_entry is not None:
                    if not self._focus_within(self.search_entry):
                        self.search_entry.grab_focus()
                        # Append the typed character ourselves - the
                        # original event has already been consumed by
                        # our handler, so falling through wouldn't
                        # reach the entry.
                        current = self.search_entry.get_text()
                        self.search_entry.set_text(current + ch)
                        self.search_entry.set_position(-1)
                        return True
        return False

    def _action_on_selected(self, action: str) -> None:
        if self.notebook is None:
            return
        page = self.notebook.get_current_page()
        listbox = self.recent_listbox if page == 0 else self.snippets_listbox
        if listbox is None:
            return
        row = listbox.get_selected_row()
        if row is None:
            return
        entry = getattr(row, "entry", None)
        if entry is None:
            return
        if action == "pin" and page == 0:
            self._on_pin_clicked(None, entry)
        elif action == "delete" and page == 0:
            self._on_delete_history_clicked(None, entry)
        elif action == "rename" and page == 1:
            self._on_rename_clicked(None, entry)


_picker = _PickerDialog()


def open_picker(target_window: str | None = None) -> None:
    """Module-level entry point used by both the popup plugin button and
    the global clipboard hotkey in main.py."""
    GLib.idle_add(_picker.show, target_window)


# ----- plugin registration ----------------------------------------------


def _open_from_popup(_text: str) -> None:
    # When invoked from the popup, capture the previously focused window
    # before opening the picker so paste-at-cursor still works.
    target = _get_active_window()
    open_picker(target)


def register(register_plugin) -> None:
    # Honour the master kill-switch so the user can turn the entire
    # clipboard plugin off (no watcher thread, no hotkey, no popup
    # button) without having to uninstall the file.
    if not bool(_cfg("clipboard_history_enabled", True)):
        print("[clipboard] disabled in settings - not registering")
        return
    _start_watcher_once()
    register_plugin(
        Plugin(
            name="clipboard-history",
            icon="vinopop-clipboard-symbolic",
            tooltip="Clipboard & snippets",
            handler=_open_from_popup,
            content_types=(),  # always available
            priority=5,
        )
    )
