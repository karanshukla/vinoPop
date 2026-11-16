# vinopop-plugin-version: 6
"""Clipboard history + snippets picker.

Two backing stores:
  - history.json   : rolling buffer of the last N clipboard entries
  - snippets.json  : pinned/named entries that never expire

The picker dialog has Recent + Snippets tabs, a search box, and pastes
the chosen entry at the cursor of whichever app had focus when the
picker was summoned (activates it over EWMH, then Ctrl+V over XTEST).

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
import logging
import os
import re
import select as _select
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
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

    threading.Thread(target=_above, daemon=True, name="clipboard-above").start()


def _force_to_front(window) -> None:
    """Raise the picker above every other window on X11, including a
    Settings / Plugin Manager dialog already open. Cinnamon ranks windows
    by focus history before keep-above, so the hint alone can leave the
    picker behind a more recently focused vinoPop dialog."""
    try:
        window.present()
        surface = _x11_surface(window)
        if surface is not None:
            _apply_x11_hints(window)
            try:
                server_time = GdkX11.x11_get_server_time(surface)
            except Exception:
                server_time = 0
            # Focus-stealing prevention ignores a request with timestamp 0
            try:
                surface.focus(server_time)
            except Exception:
                pass
    except Exception:
        try:
            window.present()
        except Exception:
            pass


from plugin_base import Plugin

_log = logging.getLogger("vinopop")

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


def _cfg_number(key: str, default, kind):
    """_cfg() for a number: a hand-edited non-number falls back to `default`
    (pinned by `tests/test_clipboard_store.py::test_non_numeric_history_size_falls_back_to_the_default`,
    `tests/test_clipboard_store.py::test_capture_still_works_with_a_bad_history_size`)."""
    from settings import as_number

    return as_number(key, _cfg(key, default), kind, default)


def _history_size() -> int:
    return max(1, _cfg_number("clipboard_history_size", 25, int))


def _capture_images() -> bool:
    return bool(_cfg("clipboard_history_images", True))


def _poll_interval() -> float:
    return max(0.1, _cfg_number("clipboard_poll_interval", 0.5, float))


def _max_text_bytes() -> int:
    return max(1, _cfg_number("clipboard_history_max_text_kb", 1024, int)) * 1024


def _max_image_bytes() -> int:
    return max(1, _cfg_number("clipboard_history_max_image_mb", 20, int)) * 1024 * 1024


# For the startup log line only; the getters above read the live setting.
HISTORY_SIZE = _history_size()
CAPTURE_IMAGES = _capture_images()
POLL_INTERVAL = _poll_interval()

from xdg_paths import CACHE_DIR as _LP_CACHE
from xdg_paths import CONFIG_DIR as _LP_CONFIG
from xdg_paths import DATA_DIR as _LP_DATA
from xdg_paths import make_private, open_private, private_dir

CACHE_DIR = _LP_CACHE / "clipboard"
HISTORY_FILE = CACHE_DIR / "history.json"
SNIPPETS_FILE = _LP_CONFIG / "snippets.json"
IMAGES_DIR = CACHE_DIR / "images"
# Outside the cache, so clearing it keeps pinned images (pinned by
# `tests/test_clipboard_store.py::test_a_pinned_image_survives_its_history_entry_ageing_out`)
SNIPPET_IMAGES_DIR = _LP_DATA / "snippet_images"


def _parse_triggers(text: str) -> list[str]:
    """The shortcodes in a comma-separated trigger field (";email, ;e")."""
    return [t.strip() for t in text.split(",") if t.strip()]


@dataclass
class Entry:
    """One row of history.json or snippets.json. Every field after `kind`
    has a default, so files saved before a field existed still load
    (pinned by `tests/test_clipboard_store.py::test_snippets_saved_before_the_flag_load_as_live`).

    `kind` is "text" or "image". `name`, `trigger` (comma-separated
    shortcodes), `usage_count` and `category` ("" is General) are for
    snippets. `image_hash` is the sha1 of an image's PNG, so a re-copy moves
    it up (pinned by `tests/test_clipboard_store.py::test_pasting_an_older_image_moves_it_up_without_a_second_file`).
    A `literal` entry pastes as written: text pinned from a selection is
    someone else's, so a {shell:...} in it must not run (pinned by
    `tests/test_clipboard_store.py::test_pin_as_snippet_saves_the_selection_as_literal`,
    `tests/test_snippet_shell.py::test_a_literal_snippet_never_reaches_the_shell`)."""

    id: str
    timestamp: float
    kind: str
    text: str = ""
    image_path: str = ""
    name: str = ""
    trigger: str = ""
    usage_count: int = 0
    category: str = ""
    image_hash: str = ""
    literal: bool = False

    def trigger_list(self) -> list[str]:
        return _parse_triggers(self.trigger)

    def preview(self, max_len: int = 80) -> str:
        """The row label. Sliced before the replaces, so a long copy isn't
        walked in full (pinned by
        `tests/test_clipboard_store.py::test_long_preview_matches_the_short_one`)."""
        if self.name:
            return self.name
        if self.kind == "image":
            return f"🖼  Image - {Path(self.image_path).name}"
        shown = self.text[: max_len + 1].replace("\n", "↵").replace("\t", "  ")
        return shown[:max_len] + "…" if len(shown) > max_len else shown

    def search_haystack(self) -> str:
        """What the picker's search matches, lower-cased, cached per row
        because the filter asks on every keystroke. Not the image path
        (pinned by `tests/test_clipboard_store.py::test_image_path_is_not_searchable`,
        `tests/test_clipboard_store.py::test_search_haystack_follows_edits`)."""
        body = "image" if self.kind == "image" else self.text
        searched_fields = (self.name, self.trigger, body)
        cached = self.__dict__.get("_haystack")
        if cached is not None and cached[0] == searched_fields:
            return cached[1]
        haystack = "\n".join(searched_fields).lower()
        self.__dict__["_haystack"] = (searched_fields, haystack)
        return haystack


_history: list[Entry] = []
_snippets: list[Entry] = []
_history_lock = threading.Lock()
_snippets_lock = threading.Lock()
_watcher_thread: threading.Thread | None = None
_watcher_stop = threading.Event()


def _atomic_write_json(path: Path, data) -> None:
    """Write through a private temp file of its own, so concurrent saves
    don't share one (pinned by `tests/test_clipboard_store.py::test_concurrent_saves_are_not_lost`,
    `tests/test_clipboard_store.py::test_snippets_file_is_private`)."""
    private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_list(path: Path, target: list[Entry]) -> bool:
    """Append the entries in `path` to `target`. False if the file exists
    but can't be parsed; it is then copied to `.unreadable` before the next
    save replaces it (pinned by
    `tests/test_clipboard_store.py::test_unreadable_snippets_are_backed_up_not_overwritten`,
    `tests/test_clipboard_store.py::test_wrong_shape_counts_as_unreadable`). An
    unknown field is dropped, not its entry (pinned by
    `tests/test_clipboard_store.py::test_entries_with_unknown_fields_survive_a_load`)."""
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
                target.append(Entry(**{k: v for k, v in item.items() if k in known}))
            except TypeError:
                continue
        return True
    except (OSError, ValueError) as exc:  # JSONDecodeError is a ValueError
        print(f"[clipboard] could not load {path}: {exc}")
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


def _same_content(a: Entry, b: Entry) -> bool:
    if a.kind != b.kind:
        return False
    if a.kind == "text":
        return a.text == b.text
    if a.image_hash and a.image_hash == b.image_hash:
        return True
    return bool(a.image_path) and a.image_path == b.image_path


def _promote_existing(entry: Entry) -> bool:
    """If history already holds this content, move that entry to the top
    and return True (pinned by
    `tests/test_clipboard_store.py::test_pasting_an_older_text_entry_moves_it_up`,
    `tests/test_clipboard_store.py::test_text_copied_again_after_an_image_comes_back_to_the_top`)."""
    with _history_lock:
        i = next((i for i, e in enumerate(_history) if _same_content(e, entry)), -1)
        if i < 0:
            return False
        if i > 0:
            found = _history.pop(i)
            found.timestamp = entry.timestamp
            _history.insert(0, found)
    if i > 0:
        _save_history()
    return True


def _add_entry(entry: Entry) -> None:
    if _promote_existing(entry):
        return
    evicted: list[Entry] = []
    with _history_lock:
        _history.insert(0, entry)
        while len(_history) > _history_size():
            evicted.append(_history.pop())
    _save_history()
    for old in evicted:
        _delete_history_image(old)


def _delete_history_image(entry: Entry) -> None:
    """Remove a dropped history entry's image file, unless a snippet uses it."""
    if entry.kind != "image" or not entry.image_path:
        return
    if _snippet_uses_image(entry.image_path):
        return
    try:
        Path(entry.image_path).unlink(missing_ok=True)
    except OSError:
        pass


def _snippet_uses_image(image_path: str) -> bool:
    """True if a snippet points at this image file: snippets pinned by
    older versions share the history copy (pinned by
    `tests/test_clipboard_store.py::test_eviction_keeps_an_image_an_older_pin_shares`,
    `tests/test_clipboard_store.py::test_delete_keeps_an_image_a_snippet_uses`)."""
    with _snippets_lock:
        return any(s.kind == "image" and s.image_path == image_path for s in _snippets)


def _remove_history_entry(entry_id: str) -> bool:
    """Delete one history entry and its image file unless a snippet still
    uses it, saving straight away. False if the entry was already gone
    (pinned by `tests/test_clipboard_store.py::test_delete_removes_the_entry_and_its_image`)."""
    with _history_lock:
        removed = [e for e in _history if e.id == entry_id]
        if not removed:
            return False
        _history[:] = [e for e in _history if e.id != entry_id]
    _save_history()
    for entry in removed:
        _delete_history_image(entry)
    return True


def _pin_entry(
    entry: Entry,
    name: str = "",
    trigger: str = "",
    category: str = "",
    text: str | None = None,
) -> str | None:
    """Copy a history entry into the persistent snippets store. `text`
    replaces the body of a text entry (the Pin dialog lets you edit it).
    Returns the new snippet's id, or None if an image couldn't be copied."""
    new_id = uuid.uuid4().hex[:12]
    image_path = ""
    if entry.kind == "image":
        dest = SNIPPET_IMAGES_DIR / f"{new_id}.png"
        try:
            private_dir(SNIPPET_IMAGES_DIR)
            with open(entry.image_path, "rb") as src, open_private(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
        except OSError as exc:
            print(f"[clipboard] could not copy the image to pin it: {exc}")
            return None
        image_path = str(dest)
    snippet = Entry(
        id=new_id,
        timestamp=time.time(),
        kind=entry.kind,
        text=entry.text if text is None or entry.kind != "text" else text,
        image_path=image_path,
        name=name,
        trigger=trigger.strip(),
        category=category.strip(),
        image_hash=entry.image_hash,
    )
    with _snippets_lock:
        _snippets.insert(0, snippet)
    _save_snippets()
    _rebuild_trigger_index()
    return new_id


def _unpin_snippet(snippet_id: str) -> None:
    """Remove a snippet and its own image copy. An older pin's image lives
    in the history cache and is left for history to clean up."""
    with _snippets_lock:
        removed = [s for s in _snippets if s.id == snippet_id]
        _snippets[:] = [s for s in _snippets if s.id != snippet_id]
    _save_snippets()
    _rebuild_trigger_index()
    for s in removed:
        p = Path(s.image_path) if s.kind == "image" and s.image_path else None
        if p is not None and p.parent == SNIPPET_IMAGES_DIR and not _snippet_uses_image(str(p)):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def _create_snippet(
    name: str, text: str, trigger: str = "", category: str = "", literal: bool = False
) -> None:
    """A new snippet not taken from history. `literal` pastes it as
    written, placeholders and all."""
    snippet = Entry(
        id=uuid.uuid4().hex[:12],
        timestamp=time.time(),
        kind="text",
        text=text,
        name=name,
        trigger=trigger.strip(),
        category=category.strip(),
        literal=literal,
    )
    with _snippets_lock:
        _snippets.insert(0, snippet)
    _save_snippets()
    _rebuild_trigger_index()


def _change_snippet(snippet_id: str, change: Callable[[Entry], None]) -> None:
    """Apply `change` to the snippet with this id, if it still exists."""
    with _snippets_lock:
        for snippet in _snippets:
            if snippet.id == snippet_id:
                change(snippet)
                break


def _set_snippet_category(snippet_id: str, category: str) -> None:
    _change_snippet(snippet_id, lambda s: setattr(s, "category", category.strip()))
    _save_snippets()


def _bump_usage(snippet_id: str) -> None:
    """Count one more paste of a snippet; a failed save doesn't block it."""
    _change_snippet(snippet_id, lambda s: setattr(s, "usage_count", int(s.usage_count or 0) + 1))
    try:
        _save_snippets()
    except Exception as exc:
        print(f"[clipboard] usage save failed (ignored): {exc}")


def _set_snippet_trigger(snippet_id: str, trigger: str) -> None:
    _change_snippet(snippet_id, lambda s: setattr(s, "trigger", trigger.strip()))
    _save_snippets()
    _rebuild_trigger_index()


# Rebuilt whenever snippets change; read by the trigger watcher's thread
_trigger_index: dict[str, Entry] = {}
_trigger_index_lock = threading.Lock()


def _find_trigger_conflicts(triggers: list[str], own_id: str = "") -> list[tuple]:
    """[(trigger, snippet label), ...] for each of `triggers` another
    snippet already claims; the snippet `own_id` doesn't clash with itself
    (pinned by `tests/test_plugins_clipboard_rules.py::test_a_trigger_another_snippet_has_is_a_conflict`,
    `tests/test_plugins_clipboard_rules.py::test_a_snippet_does_not_conflict_with_its_own_trigger`)."""
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
    """Text snippets by trigger. On a shared trigger the newest snippet
    wins (pinned by `tests/test_plugins_clipboard_rules.py::test_the_newest_snippet_wins_a_shared_trigger`,
    `tests/test_plugins_clipboard_rules.py::test_an_image_snippet_has_no_trigger`)."""
    new_index: dict[str, Entry] = {}
    with _snippets_lock:
        for s in _snippets:
            if s.kind != "text":
                continue
            for trig in s.trigger_list():
                new_index.setdefault(trig, s)
    with _trigger_index_lock:
        _trigger_index.clear()
        _trigger_index.update(new_index)


def _rename_snippet(snippet_id: str, new_name: str) -> None:
    _change_snippet(snippet_id, lambda s: setattr(s, "name", new_name))
    _save_snippets()
    _rebuild_trigger_index()


def _set_snippet_text(snippet_id: str, new_text: str) -> None:
    _change_snippet(snippet_id, lambda s: setattr(s, "text", new_text))
    _save_snippets()


# Typing one of these checks the buffer for a trigger. Whitespace only, so a
# trigger can start with punctuation (pinned by
# `tests/test_plugins_clipboard_rules.py::test_whitespace_ends_a_trigger`,
# `tests/test_plugins_clipboard_rules.py::test_punctuation_does_not_end_a_trigger`)
_TRIGGER_CHARS = set(" \t\n")
_TRIGGER_BUFFER_MAX = 64

_trigger_watcher: _TriggerWatcher | None = None


class _TriggerWatcher:
    """Background XRecord listener. A snippet trigger followed by
    whitespace is expanded in place: the shortcode and the whitespace are
    backspaced, the rendered snippet pasted, the caret moved to {cursor}.

    It sees every keystroke, so it keeps only the last
    _TRIGGER_BUFFER_MAX characters, in memory only (pinned by
    `tests/test_trigger_expansion.py::test_the_watcher_keeps_only_the_newest_characters`).
    Off unless 'snippet_triggers_enabled' is set.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        # XRecord blocks the display it records on, so keycodes are looked
        # up on a second one
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
        # Unblocks record_enable_context() in _run()
        try:
            if self._local_display is not None and self._ctx is not None:
                self._local_display.record_disable_context(self._ctx)
                self._local_display.flush()
        except Exception:
            pass

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

    def _forget_typed(self) -> None:
        with self._lock:
            self._buffer.clear()

    def _forget_last_char(self) -> None:
        with self._lock:
            if self._buffer:
                self._buffer.pop()

    def _on_keypress(self, event, X, XK) -> None:
        state = event.state
        chord = state & (X.ControlMask | X.Mod1Mask | X.Mod4Mask)
        if chord:
            self._forget_typed()
            return

        shift = bool(state & X.ShiftMask)
        caps = bool(state & X.LockMask)
        keysym = self._local_display.keycode_to_keysym(event.detail, 1 if shift else 0)
        if keysym == 0:
            return

        if keysym == XK.XK_BackSpace:
            self._forget_last_char()
            return
        if keysym in (XK.XK_Return, XK.XK_KP_Enter):
            self._handle_char("\n")
            return
        if keysym == XK.XK_Tab:
            self._handle_char("\t")
            return
        if keysym == XK.XK_Escape:
            self._forget_typed()
            return
        if keysym in _FUNCTION_KEYSYMS:
            return

        ch = self._keysym_to_char(keysym, caps, shift, XK)
        if not ch:
            return
        self._handle_char(ch)

    @staticmethod
    def _keysym_to_char(keysym, caps_lock: bool, shift: bool, XK) -> str | None:
        char = XK.keysym_to_string(keysym)
        if char is None or len(char) > 1:
            if keysym not in _LATIN1_KEYSYMS:
                return None
            char = chr(keysym)
        # Shift undoes Caps Lock. An approximation: non-Latin layouts vary
        if caps_lock and not shift and char.isalpha():
            return char.upper()
        if caps_lock and shift and char.isalpha():
            return char.lower()
        return char

    def _handle_char(self, ch: str) -> None:
        with self._lock:
            if ch not in _TRIGGER_CHARS:
                self._buffer.append(ch)
                del self._buffer[: max(0, len(self._buffer) - _TRIGGER_BUFFER_MAX)]
                return
            matched = _match_typed_trigger("".join(self._buffer))
            self._buffer.clear()
        if matched is not None:
            trigger, typed, snippet = matched
            GLib.idle_add(_fire_trigger_expansion, trigger, typed, ch, snippet)


def _match_typed_trigger(typed_text: str) -> tuple[str, str, Entry] | None:
    """(trigger, as typed, snippet) for the trigger `typed_text` ends with,
    or None. The longest wins and case is ignored; the typed form is
    returned so its case can carry over to the expansion (pinned by
    `tests/test_trigger_expansion.py::test_the_longest_trigger_the_text_ends_with_wins`,
    `tests/test_trigger_expansion.py::test_a_trigger_inside_a_word_is_not_matched`)."""
    typed_lower = typed_text.lower()
    with _trigger_index_lock:
        for trigger in sorted(_trigger_index.keys(), key=len, reverse=True):
            trigger_lower = trigger.lower()
            if not typed_lower.endswith(trigger_lower):
                continue
            if not _trigger_word_boundary_ok(typed_text, trigger_lower):
                continue
            return trigger, typed_text[-len(trigger_lower) :], _trigger_index[trigger]
    return None


def _trigger_word_boundary_ok(buf: str, trig_lc: str) -> bool:
    """Whether `trig_lc`, already a suffix of `buf`, starts on a word
    boundary: at the start of `buf`, after a non-word character, or itself
    starting with one (";mvh") (pinned by
    `tests/test_trigger_expansion.py::test_word_boundary`)."""
    if len(buf) == len(trig_lc):
        return True
    before = buf[-(len(trig_lc) + 1)]
    return not _is_word_char(before) or not _is_word_char(trig_lc[0])


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _detect_case_style(typed: str) -> str:
    """'lower', 'title', 'upper' or 'mixed' for how a trigger was typed;
    no letters is 'lower', one capital alone 'title' (pinned by
    `tests/test_trigger_expansion.py::test_detect_case_style`)."""
    letters = [c for c in typed if c.isalpha()]
    if not letters:
        return "lower"
    if all(c.islower() for c in letters):
        return "lower"
    if all(c.isupper() for c in letters):
        return "upper" if len(letters) >= 2 else "title"
    if letters[0].isupper() and all(c.islower() for c in letters[1:]):
        return "title"
    return "mixed"


def _propagate_case(typed: str, output: str) -> str:
    """`output` in the case `typed` was typed in: title capitalises the
    first letter, upper all of it, lower and mixed leave it (pinned by
    `tests/test_trigger_expansion.py::test_propagate_case`)."""
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
    """Lower-cased title and WM_CLASS of the focused window, from the
    platform backend (pinned by
    `tests/test_clipboard_store.py::test_trigger_blocklist_asks_the_backend_for_the_active_window`)."""
    try:
        from platform_backend import get_backend

        return [h.lower() for h in get_backend().active_window_haystacks()]
    except Exception as exc:  # noqa: BLE001
        print(f"[triggers] active-window check failed: {exc}")
        return []


# On Wayland, XRecord only sees XWayland clients and XTEST can't reach native
# windows, so the keyboards are read from /dev/input (needs the 'input'
# group), mapped with libxkbcommon, and expansions injected with ydotool.

# X keysyms for modifiers, arrows and F-keys, and the Latin-1 range
_FUNCTION_KEYSYMS = range(0xFF00, 0xFFFF + 1)
_LATIN1_KEYSYMS = range(0x20, 0xFF + 1)

# linux/input-event-codes.h
_EV_KEY = 0x01
_KEY_RELEASED, _KEY_PRESSED = 0, 1  # 2 is autorepeat
_KEY_BACKSPACE = 14
_KEY_LEFTCTRL = 29
_KEY_LEFTSHIFT = 42
_KEY_LEFTALT = 56
_KEY_RIGHTCTRL = 97
_KEY_LEFT = 105
_KEY_INSERT = 110
_KEY_LEFTMETA = 125
_KEY_RIGHTMETA = 126
# struct input_event: sec, usec, type, code, value
_INPUT_EVENT_FORMAT = "llHHi"
# A chord, not typing, drops a partial trigger. Not right Alt: as AltGr it
# types @ { } [ ] on many layouts.
_ABORT_MODS = {_KEY_LEFTCTRL, _KEY_RIGHTCTRL, _KEY_LEFTALT, _KEY_LEFTMETA, _KEY_RIGHTMETA}
# ydotool's keystrokes come back through /dev/input; until this monotonic
# time the watcher ignores them
_kbd_suppress_until = 0.0


def _is_wayland() -> bool:
    """Picks the trigger watcher's input path (evdev + ydotool), not the
    clipboard tool: see _wl_clipboard."""
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def _wl_clipboard() -> bool:
    """Use wl-clipboard rather than xclip: only on the KDE Wayland backend.
    mutter has no data-control protocol, so wl-copy there steals focus or
    fails silently and wl-paste --primary can hang; elsewhere xclip goes
    through the compositor's X selection bridge (pinned by
    `tests/test_clipboard_store.py::test_history_uses_the_backends_clipboard_tool`)."""
    try:
        from platform_backend import get_backend

        return get_backend().name == "wayland_kde"
    except Exception:  # noqa: BLE001
        return False


def _x11():
    """platform_backend.x11 (python-xlib over XTEST), or None without it."""
    try:
        from platform_backend import x11

        return x11
    except Exception:  # noqa: BLE001 - no python-xlib
        return None


def _x_key(combo: str, repeat: int = 1, delay_s: float = 0.0, clear: bool = True) -> None:
    """Press `combo` `repeat` times, `delay_s` apart, lifting the held
    modifiers first when `clear`."""
    x11 = _x11()
    if x11 is None or not x11.send_keys(combo, repeat, delay_s, clear):
        print(f"[clipboard] cannot send {combo!r}: no XTEST")


def _x_keyup(*names: str) -> None:
    x11 = _x11()
    if x11 is not None:
        x11.release_keys(list(names))


def _x_activate(wid: str) -> bool:
    """Make `wid` the active window; True once it is."""
    x11 = _x11()
    return x11 is not None and x11.activate_window(wid)


def _x_can_inject() -> bool:
    x11 = _x11()
    return x11 is not None and x11.xtest_available()


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

        # by-path lists only physical keyboards, not ydotool's virtual one
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

        event_size = struct.calcsize(_INPUT_EVENT_FORMAT)
        try:
            while not self._stop.is_set():
                readable, _, _ = _select.select(self._fds, [], [], 0.5)
                for fd in readable:
                    try:
                        data = os.read(fd, event_size * 64)
                    except (BlockingIOError, OSError):
                        continue
                    for offset in range(0, len(data) - event_size + 1, event_size):
                        _sec, _usec, event_type, code, value = struct.unpack_from(
                            _INPUT_EVENT_FORMAT, data, offset
                        )
                        if event_type == _EV_KEY:
                            self._on_key(code, value)
        finally:
            for fd in self._fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._fds = []

    def _on_key(self, code: int, value: int) -> None:
        released, pressed = value == _KEY_RELEASED, value == _KEY_PRESSED
        if released:
            self._mapper.update(code, False)
            return
        is_chord = code in _ABORT_MODS
        is_backspace = code == _KEY_BACKSPACE
        ch = "" if (is_chord or is_backspace) else self._mapper.char_for_press(code)
        if pressed:
            self._mapper.update(code, True)
        injecting_our_own_keys = time.monotonic() < _kbd_suppress_until
        if injecting_our_own_keys:
            return
        if is_chord:
            self._forget_typed()
            return
        if is_backspace:
            self._forget_last_char()
            return
        if not ch:
            return
        if ch == "\r":
            ch = "\n"
        if ch.isprintable() or ch in _TRIGGER_CHARS:
            self._handle_char(ch)


def _trigger_blocked() -> bool:
    """Whether the active window matches trigger_blocklist_patterns (pinned
    by `tests/test_trigger_expansion.py::test_a_blocklisted_window_gets_nothing`)."""
    patterns = list(_cfg("trigger_blocklist_patterns", []) or [])
    if not patterns:
        return False
    return _matches_blocklist(patterns, _active_window_haystacks())


def _snapshot_clipboard() -> tuple[str, bytes] | None:
    """The clipboard as (type, data), for a trigger expansion to put back:
    the text, else the PNG. None when empty or unreadable, or a password
    manager's copy, whose restore would undo its auto-clear (pinned by
    `tests/test_clipboard_store.py::test_a_password_copy_is_not_snapshotted`)."""
    targets = _read_clipboard_targets()
    if not targets or _is_password_copy(targets):
        return None
    if any(t in _TEXT_TARGETS for t in targets):
        text = _read_clipboard_text()
        return ("text", text.encode("utf-8")) if text else None
    if "image/png" in targets:
        data = _read_clipboard_image()
        return ("image/png", data) if data else None
    return None


def _write_clipboard(kind: str, data: bytes, primary: bool = False) -> None:
    """Put `data` on CLIPBOARD (or PRIMARY). `kind` is "text" or a MIME type."""
    if _wl_clipboard():
        args = ["wl-copy", "--primary"] if primary else ["wl-copy"]
    else:
        args = ["xclip", "-selection", "primary" if primary else "clipboard"]
    if kind != "text":
        args += ["-t", kind]
    subprocess.run(args, input=data, check=False, timeout=2.0)


def _restore_clipboard(saved: tuple[str, bytes] | None, staged: str) -> None:
    """Put back what was on the clipboard before an expansion staged
    `staged` there, unless something else was copied since, marked as our
    own copy so history doesn't record it (pinned by
    `tests/test_clipboard_store.py::test_expansion_restores_the_clipboard`,
    `tests/test_trigger_expansion.py::test_a_newer_copy_is_not_overwritten_by_the_restore`,
    `tests/test_clipboard_store.py::test_expansion_text_and_the_restored_copy_are_not_recorded`)."""
    if saved is None or _read_clipboard_text() != staged:
        return
    kind, data = saved
    if kind == "text":
        _mark_own_copy(data.decode("utf-8"))
    else:
        import hashlib

        _mark_own_copy(hashlib.sha1(data, usedforsecurity=False).digest())
    try:
        _write_clipboard(kind, data)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[triggers] could not restore the clipboard: {exc}")


# The waits between an expansion's steps, in seconds. Each one covers a race
# seen on a real desktop: docs/how-it-works.md, "Snippet trigger timing".
_BOUNDARY_KEY_RELEASE_WAIT = 0.06
_BACKSPACE_INTERVAL = 0.016
_AFTER_DELETE_WAIT = 0.08
_AFTER_PASTE_WAIT = 0.05
_BEFORE_MODIFIER_RELEASE_WAIT = 0.02
# The target app reads the clipboard after the paste key arrives: too short
# and it pastes what the user had there instead
_RESTORE_DELAY = 0.3
_YDOTOOL_AFTER_DELETE_WAIT = 0.03
_YDOTOOL_AFTER_STAGE_WAIT = 0.05
# How long the Wayland watcher ignores keys while ydotool injects: a base,
# plus a margin per injected key, then a tail after the last key
_YDOTOOL_SUPPRESS_BASE = 1.2
_YDOTOOL_SUPPRESS_PER_KEY = 0.02
_YDOTOOL_SUPPRESS_TAIL = 0.25


def _fire_trigger_expansion(
    trigger: str,
    typed: str,
    boundary_char: str,
    snippet: Entry,
) -> bool:
    """GLib idle callback: delete the typed trigger and boundary, then
    paste the rendered snippet in the case `typed` was typed in. An image
    or an {ask:} snippet is left to the picker (pinned by
    `tests/test_trigger_expansion.py::test_an_image_entry_is_never_typed`,
    `tests/test_trigger_expansion.py::test_an_ask_snippet_is_left_to_the_picker`)."""
    if snippet.kind != "text":
        return GLib.SOURCE_REMOVE
    if not snippet.literal and "{ask:" in snippet.text:
        print(f"[triggers] '{trigger}' has {{ask:}} - skipping (use picker)")
        return GLib.SOURCE_REMOVE
    if _trigger_blocked():
        print(f"[triggers] '{trigger}' skipped - active window is in blocklist")
        return GLib.SOURCE_REMOVE
    if snippet.literal:
        rendered, cursor_left = snippet.text, 0
    else:
        rendered, cursor_left, _ = render_placeholders(
            snippet.text,
            lambda _label: None,
        )
    rendered = _propagate_case(typed, rendered)
    _bump_usage(snippet.id)

    expand = _expand_with_ydotool if _is_wayland() else _expand_over_xtest
    typed_length = len(trigger) + len(boundary_char)
    threading.Thread(
        target=lambda: expand(typed_length, rendered, cursor_left),
        daemon=True,
        name="trigger-expansion",
    ).start()
    return GLib.SOURCE_REMOVE


def _expand_over_xtest(typed_length: int, rendered: str, cursor_left: int) -> None:
    """Checks its tools before deleting anything, so a failure leaves the
    trigger typed (pinned by `tests/test_trigger_expansion.py::test_without_xtest_nothing_is_deleted`,
    `tests/test_trigger_expansion.py::test_without_xclip_nothing_is_deleted`)."""
    if not _x_can_inject() or not shutil.which("xclip"):
        print("[triggers] no XTEST or no xclip - cannot expand")
        return
    time.sleep(_BOUNDARY_KEY_RELEASE_WAIT)
    # clear=False: lifting modifiers for a BackSpace sometimes left Ctrl stuck
    _x_key("BackSpace", repeat=typed_length, delay_s=_BACKSPACE_INTERVAL, clear=False)
    time.sleep(_AFTER_DELETE_WAIT)
    saved = _snapshot_clipboard()
    _mark_own_copy(rendered)
    try:
        _write_clipboard("text", rendered.encode("utf-8"))
    except (OSError, subprocess.SubprocessError):
        return
    _x_key("ctrl+v")
    if cursor_left > 0:
        time.sleep(_AFTER_PASTE_WAIT)
        _x_key("Left", repeat=cursor_left)
    # Restoring held modifiers races the user's key-up and can leave Ctrl
    # down, so they are released outright
    time.sleep(_BEFORE_MODIFIER_RELEASE_WAIT)
    _x_keyup("ctrl", "shift", "alt", "super", "Control_L", "Control_R")
    time.sleep(_RESTORE_DELAY)
    _restore_clipboard(saved, rendered)


def _ydotool_taps(key_code: int, count: int) -> list[str]:
    """`ydotool key` arguments for `count` presses and releases of one key."""
    return [f"{key_code}:1", f"{key_code}:0"] * count


def _expand_with_ydotool(typed_length: int, rendered: str, cursor_left: int) -> None:
    """The Wayland expansion (pinned by
    `tests/test_trigger_expansion.py::test_wayland_deletes_with_ydotool_and_pastes_with_shift_insert`,
    `tests/test_trigger_expansion.py::test_wayland_stages_clipboard_and_primary_and_puts_both_back`,
    `tests/test_trigger_expansion.py::test_wayland_without_ydotool_deletes_nothing`)."""
    clip_tool = "wl-copy" if _wl_clipboard() else "xclip"
    if not shutil.which("ydotool") or not shutil.which(clip_tool):
        print(f"[triggers] ydotool/{clip_tool} not found - cannot expand on Wayland")
        return
    global _kbd_suppress_until
    env = _ydotool_env()
    _kbd_suppress_until = (
        time.monotonic()
        + _YDOTOOL_SUPPRESS_BASE
        + _YDOTOOL_SUPPRESS_PER_KEY * (typed_length + cursor_left)
    )
    time.sleep(_BOUNDARY_KEY_RELEASE_WAIT)
    subprocess.run(
        ["ydotool", "key", *_ydotool_taps(_KEY_BACKSPACE, typed_length)], env=env, check=False
    )
    time.sleep(_YDOTOOL_AFTER_DELETE_WAIT)
    # Staged on the clipboard and on PRIMARY rather than typed: `ydotool
    # type` maps through a US keycode table and drops æ, ø, å
    saved = _snapshot_clipboard()
    saved_primary = _read_primary_selection()
    _mark_own_copy(rendered)
    try:
        _write_clipboard("text", rendered.encode("utf-8"))
        _write_clipboard("text", rendered.encode("utf-8"), primary=True)
    except (OSError, subprocess.SubprocessError):
        return
    time.sleep(_YDOTOOL_AFTER_STAGE_WAIT)
    # Shift+Insert: ydotool's Ctrl chords don't register on KWin
    shift_insert = [
        f"{_KEY_LEFTSHIFT}:1",
        f"{_KEY_INSERT}:1",
        f"{_KEY_INSERT}:0",
        f"{_KEY_LEFTSHIFT}:0",
    ]
    subprocess.run(["ydotool", "key", *shift_insert], env=env, check=False)
    if cursor_left > 0:
        time.sleep(_AFTER_PASTE_WAIT)
        subprocess.run(
            ["ydotool", "key", *_ydotool_taps(_KEY_LEFT, cursor_left)], env=env, check=False
        )
    _kbd_suppress_until = time.monotonic() + _YDOTOOL_SUPPRESS_TAIL
    time.sleep(_RESTORE_DELAY)
    _restore_clipboard(saved, rendered)
    if saved_primary and _read_primary_selection() == rendered:
        try:
            _write_clipboard("text", saved_primary.encode("utf-8"), primary=True)
        except (OSError, subprocess.SubprocessError):
            pass


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


def _read_clipboard_targets() -> list[str]:
    if _wl_clipboard():
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
    if _wl_clipboard():
        args = ["wl-paste", "-n", "-t", target]
    else:
        args = ["xclip", "-selection", "clipboard", "-t", target, "-o"]
    try:
        out = subprocess.run(args, capture_output=True, timeout=0.5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


# How password managers mark a copy for clipboard managers to leave alone.
# Lower-cased: X11 echoes an atom name in the case the app wrote it.
_PASSWORD_HINT_TARGET = "x-kde-passwordmanagerhint"  # nosec B105
_CONCEALED_TARGETS = ("application/x-nspasteboard-concealed-type",)


def _is_password_copy(targets: list[str]) -> bool:
    """A concealed copy, or a hint whose value is "secret" or can't be read:
    keeping a password by mistake is worse than missing one copy (pinned by
    `tests/test_clipboard_store.py::test_password_manager_copies_are_not_captured`,
    `tests/test_clipboard_store.py::test_a_non_secret_hint_value_is_captured`)."""
    lowered = {t.strip().lower(): t.strip() for t in targets}
    if any(t in lowered for t in _CONCEALED_TARGETS):
        return True
    if _PASSWORD_HINT_TARGET not in lowered:
        return False
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
    """Whether the focused window, normally the app the copy came from,
    matches the popup's blocklist_patterns (pinned by
    `tests/test_clipboard_store.py::test_copies_from_a_blocklisted_window_are_not_captured`)."""
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
    """Call once per new copy, after the dedup check, so polling doesn't
    query the active window every interval. Logs nothing on purpose: not
    even the log should note that a password went by."""
    return _is_password_copy(targets) or _capture_blocked()


def _read_clipboard_text() -> str:
    if _wl_clipboard():
        # -n: wl-paste appends a newline that xclip doesn't
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
    if _wl_clipboard():
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

# What vinoPop itself just put on the clipboard (text or image sha1) -> the
# monotonic time until which the watcher doesn't record it
_OWN_COPY_TTL = 5.0
_own_copies: dict[str | bytes, float] = {}
_own_copies_lock = threading.Lock()


def _mark_own_copy(content: str | bytes) -> None:
    now = time.monotonic()
    with _own_copies_lock:
        for k in [k for k, t in _own_copies.items() if t < now]:
            del _own_copies[k]
        _own_copies[content] = now + _OWN_COPY_TTL


def _is_own_copy(content: str | bytes) -> bool:
    with _own_copies_lock:
        return _own_copies.get(content, 0.0) >= time.monotonic()


# Kept on disk so a restart with the same image on the clipboard doesn't
# save it again
_DEDUP_STATE_FILE = CACHE_DIR / "last_image.sha1"


def _load_dedup_state() -> None:
    try:
        _state["last_image_hash"] = _DEDUP_STATE_FILE.read_bytes()
    except OSError:
        pass


def _save_dedup_state(h: bytes) -> None:
    try:
        private_dir(CACHE_DIR)
        with open_private(_DEDUP_STATE_FILE, "wb") as f:
            f.write(h)
    except OSError:
        pass


def _sweep_orphan_images() -> None:
    """Delete cached image files that no history or snippet entry points
    at, left behind by entries that aged out during an earlier run."""
    if not IMAGES_DIR.is_dir():
        return
    with _history_lock:
        entries = list(_history)
    with _snippets_lock:
        entries += _snippets
    referenced = {
        Path(entry.image_path).name
        for entry in entries
        if entry.kind == "image" and entry.image_path
    }
    removed = 0
    for image in IMAGES_DIR.glob("*.png"):
        if image.name not in referenced:
            try:
                image.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"[clipboard] swept {removed} orphan image(s) from cache")


def _tighten_permissions() -> None:
    """Make files older versions wrote with the default umask private
    (pinned by `tests/test_clipboard_store.py::test_files_from_older_versions_are_made_private`)."""
    for d in (_LP_CACHE, CACHE_DIR, IMAGES_DIR, _LP_CONFIG, SNIPPET_IMAGES_DIR):
        make_private(d)
    for f in (HISTORY_FILE, SNIPPETS_FILE, _DEDUP_STATE_FILE):
        make_private(f)
        make_private(f.with_suffix(f.suffix + ".unreadable"))
    if IMAGES_DIR.is_dir():
        for p in IMAGES_DIR.glob("*.png"):
            make_private(p)


def _capture_current_clipboard() -> None:
    """Record what is on the clipboard now. Text wins over an image, since
    a LibreOffice cell comes with a bitmap of itself; an SVG-only copy is
    kept as text (pinned by `tests/test_clipboard_store.py::test_text_wins_when_an_image_is_offered_too`,
    `tests/test_clipboard_store.py::test_an_svg_only_copy_is_kept_as_text`,
    `tests/test_clipboard_store.py::test_a_stopped_watcher_captures_nothing`)."""
    if _watcher_stop.is_set():
        return
    try:
        targets = _read_clipboard_targets()
        if any(t in _TEXT_TARGETS for t in targets):
            _capture_text(_read_clipboard_text(), targets)
            return
        if not _capture_images() or not any(t.startswith("image/") for t in targets):
            return
        data = _read_clipboard_image()
        if data:
            _capture_image(data, targets)
        elif _SVG_TARGET in targets:
            svg = _read_clipboard_target(_SVG_TARGET)
            if svg:
                _capture_text(svg.decode("utf-8", errors="replace"), targets)
    except Exception as exc:  # noqa: BLE001
        print(f"[clipboard] capture error: {exc}")


_TEXT_TARGETS = ("UTF8_STRING", "TEXT", "STRING", "text/plain", "text/plain;charset=utf-8")
_SVG_TARGET = "image/svg+xml"


def _capture_image(data: bytes, targets: list[str]) -> None:
    import hashlib

    h = hashlib.sha1(data, usedforsecurity=False).digest()
    if h == _state["last_image_hash"]:
        return
    _state["last_image_hash"] = h
    _state["last_text"] = ""
    _save_dedup_state(h)
    if _is_own_copy(h) or _skip_capture(targets):
        return
    if len(data) > _max_image_bytes():
        print(f"[clipboard] not keeping a {len(data) // 1024} KB image (too big)")
        return
    eid = uuid.uuid4().hex[:12]
    path = IMAGES_DIR / f"{eid}.png"
    entry = Entry(
        id=eid, timestamp=time.time(), kind="image", image_path=str(path), image_hash=h.hex()
    )
    if _promote_existing(entry):
        return
    private_dir(CACHE_DIR)
    private_dir(IMAGES_DIR)
    try:
        with open_private(path, "wb") as f:
            f.write(data)
        _add_entry(entry)
    except OSError as exc:
        print(f"[clipboard] could not save image: {exc}")


def _capture_text(text: str, targets: list[str]) -> None:
    """Too big a copy is skipped, not cut: half a log pasted back is worse
    than none (pinned by `tests/test_clipboard_store.py::test_oversized_text_is_not_kept`)."""
    if not text or text == _state["last_text"] or not text.strip():
        return
    _state["last_text"] = text
    _state["last_image_hash"] = b""
    if _is_own_copy(text) or _skip_capture(targets):
        return
    size = len(text.encode("utf-8", errors="replace"))
    if size > _max_text_bytes():
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
                readable, _, _ = _select.select([fd], [], [], _WATCHER_WAKE_S)
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


# How often the XFixes loop checks for shutdown(), which joins it
_WATCHER_WAKE_S = 0.5


def _poll_only_loop() -> None:
    while not _watcher_stop.is_set():
        _capture_current_clipboard()
        _watcher_stop.wait(_poll_interval())


def shutdown() -> None:
    """Stop this copy's background threads, waiting for a capture in
    progress. plugin_loader calls it before it re-executes the file as a
    fresh module; otherwise the old watchers keep recording history and
    expanding snippets beside the new ones (pinned by
    `tests/test_clipboard_store.py::test_shutdown_stops_the_watchers`,
    `tests/test_clipboard_store.py::test_shutdown_waits_for_the_watcher`,
    `tests/test_clipboard_store.py::test_plugin_loader_shuts_down_the_previous_copy_before_reloading`)."""
    global _watcher_thread, _trigger_watcher
    _watcher_stop.set()
    if _watcher_thread is not None and _watcher_thread is not threading.current_thread():
        _watcher_thread.join(timeout=1.0)
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
    _load_dedup_state()
    every_reference_loaded = history_ok and snippets_ok
    if every_reference_loaded:
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


def _get_active_window() -> str | None:
    """The active X window's id as a decimal string, or None."""
    x11 = _x11()
    if x11 is None:
        return None
    wid = x11.active_window_id()
    return str(wid) if wid else None


# Substrings of WM_CLASS that paste with Ctrl+Shift+V (pinned by
# `tests/test_clipboard_store.py::test_picker_paste_key_follows_the_target_window_class`)
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
    if not wid:
        return "ctrl+v"
    try:
        from platform_backend.x11 import window_wm_class

        wm_class = window_wm_class(wid)
    except Exception:  # noqa: BLE001 - no python-xlib
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
    """Put the entry on the clipboard, focus the target window and type or
    paste it there, then send `cursor_left` Left arrows so the caret lands
    where a {cursor} placeholder was."""
    if not _copy_entry_to_clipboard(entry):
        return
    _log.info(
        "[clipboard.paste] target_window=%s kind=%s cursor_left=%d",
        target_window,
        entry.kind,
        cursor_left,
    )
    threading.Thread(
        target=lambda: _paste_into_window(entry, target_window, cursor_left),
        daemon=True,
        name="clipboard-paste",
    ).start()


def _copy_entry_to_clipboard(entry: Entry) -> bool:
    """False when an image entry's file can't be read, so there is nothing
    to paste."""
    if entry.kind == "text":
        _write_clipboard("text", entry.text.encode("utf-8"))
    elif entry.kind == "image":
        if not Path(entry.image_path).is_file():
            return False
        if _wl_clipboard():
            try:
                with open(entry.image_path, "rb") as image:
                    subprocess.run(
                        ["wl-copy", "-t", "image/png"],
                        stdin=image,
                        check=False,
                        timeout=2.0,
                    )
            except OSError:
                return False
        else:
            subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-i", entry.image_path],
                check=False,
            )
    return True


# In seconds; docs/how-it-works.md, "Pasting from the clipboard picker" has
# what each wait is for
_HOTKEY_RELEASE_WAIT = 0.06
_FOCUS_WAIT = 0.18
_TERMINAL_FOCUS_WAIT = 0.30
_REACTIVATE_WAIT = 0.12
_TYPING_INTERVAL = 0.001
_MAX_TYPED_CHARS = 2048
_CTRL_AND_SUPER_KEYS = (
    "ctrl",
    "shift",
    "alt",
    "super",
    "Control_L",
    "Control_R",
    "Super_L",
    "Super_R",
)
_EVERY_MODIFIER = (*_CTRL_AND_SUPER_KEYS, "Shift_L", "Shift_R", "Alt_L", "Alt_R")


def _activate_target_window(target_window: str) -> None:
    """Twice, by two routes: on muffin the first can return before focus
    has moved, and the picker (unlike the popup) took focus itself."""
    if not _x_activate(target_window):
        _log.warning("[clipboard.paste] activating %s timed out", target_window)
    if shutil.which("wmctrl"):
        try:
            hex_id = f"0x{int(target_window):08x}"
            subprocess.run(["wmctrl", "-ia", hex_id], check=False, capture_output=True, timeout=1.0)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass


def _paste_into_window(entry: Entry, target_window: str | None, cursor_left: int) -> None:
    """Focus `target_window` and type or paste `entry` there.

    A short single line is typed, since Electron editors drop a synthetic
    Ctrl+V and a terminal's bracketed paste gets out of step; anything
    longer or with a newline is pasted, as typed lines would each run in a
    shell (pinned by `tests/test_clipboard_paste.py::test_a_short_line_is_typed_not_pasted`,
    `tests/test_clipboard_paste.py::test_text_at_the_typing_limit_is_typed_and_one_over_is_pasted`,
    `tests/test_clipboard_paste.py::test_several_lines_are_pasted_so_a_shell_does_not_run_each`)."""
    if not (target_window and _x_can_inject()):
        _log.warning("[clipboard.paste] skipped - no target, or no XTEST")
        return
    paste_key = _paste_keystroke_for_window(target_window)
    is_terminal = paste_key == "ctrl+shift+v"
    # The hotkey's modifiers are still logically held until the user lets go
    time.sleep(_HOTKEY_RELEASE_WAIT)
    _x_keyup(*_EVERY_MODIFIER)
    _activate_target_window(target_window)
    time.sleep(_TERMINAL_FOCUS_WAIT if is_terminal else _FOCUS_WAIT)
    active = _get_active_window() or ""
    _log.info(
        "[clipboard.paste] active=%s target=%s sending=%s",
        active or "?",
        target_window,
        paste_key,
    )
    if active and active != target_window:
        _x_activate(target_window)
        time.sleep(_REACTIVATE_WAIT)
    x11 = _x11()
    types_it = (
        entry.kind == "text"
        and "\n" not in entry.text
        and len(entry.text) <= _MAX_TYPED_CHARS
        and x11 is not None
    )
    if types_it and x11 is not None:
        _log.info("[clipboard.paste] typing %d chars over XTEST", len(entry.text))
        if not x11.type_text(entry.text, delay_s=_TYPING_INTERVAL):
            _log.warning("[clipboard.paste] typing failed, pasting instead")
            _x_key(paste_key)
    else:
        _x_key(paste_key)
    if cursor_left > 0:
        time.sleep(_AFTER_PASTE_WAIT)
        _x_key("Left", repeat=cursor_left)
    # The window manager may have re-asserted one while activating (pinned by
    # `tests/test_clipboard_paste.py::test_held_modifiers_are_released_before_and_after`)
    _x_keyup(*_CTRL_AND_SUPER_KEYS)


_PLACEHOLDER_RE = re.compile(
    r"\{("
    r"date(?::[^}]+)?|time|datetime|weekday|clipboard|selection|cursor|name"
    r"|ask:[^}]+"
    r"|shell:[^}]+"
    r"|var:[A-Za-z0-9_-]+"
    r")\}"
)


def _resolve_var(name: str) -> str:
    """A variable from the snippet_variables setting, "" when undefined (pinned
    by `tests/test_snippet_placeholders.py::test_var_comes_from_the_snippet_variables_setting`)."""
    if _settings is None:
        return ""
    vars_dict = _settings.get("snippet_variables") or {}
    if not isinstance(vars_dict, dict):
        return ""
    return str(vars_dict.get(name, ""))


def _read_primary_selection() -> str:
    """The highlighted text (PRIMARY), "" when there is none. The backend
    reads it, since xclip can't see PRIMARY on Wayland."""
    try:
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
    """`cmd`'s stdout through bash, without the trailing newline. With
    snippet_shell_enabled off, the token comes back as written so the user
    sees it didn't run (pinned by `tests/test_snippet_shell.py::test_off_returns_the_token_and_runs_nothing`,
    `tests/test_snippet_shell.py::test_on_runs_bash_with_a_timeout`,
    `tests/test_snippet_shell.py::test_a_timeout_is_reported_inline`)."""
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
            timeout=_SHELL_TOKEN_TIMEOUT_S,
        )
        return out.stdout.rstrip("\n")
    except subprocess.TimeoutExpired:
        return f"[shell timeout: {cmd}]"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"[shell error: {exc}]"


# So a runaway {shell:} snippet can't freeze the paste
_SHELL_TOKEN_TIMEOUT_S = 5.0

# Private-use characters never appear in real text, and unlike NUL nothing
# drops them
_CURSOR_SENTINEL = "\ue000CURSOR\ue000"


_DATE_MATH_RE = re.compile(r"^([+-]\d+)([dwmy])(?::(.+))?$")
# Months and years are not calendar-correct, on purpose
_DAYS_PER_UNIT = {"d": 1, "w": 7, "m": 30, "y": 365}


def _render_date_token(spec: str, now_struct) -> str:
    """What follows "date:" in a token, rendered against `now_struct`: a
    strftime format (`%A`), an offset (`+7d`, `-1w`, `+3m`, `-2y`) or both
    (`+7d:%A`)."""
    offset = _DATE_MATH_RE.match(spec)
    if offset is None:
        return time.strftime(spec, now_struct)
    count, unit, date_format = int(offset.group(1)), offset.group(2), offset.group(3)
    import datetime as _dt

    now = _dt.datetime.fromtimestamp(time.mktime(now_struct))
    shifted = now + _dt.timedelta(days=count * _DAYS_PER_UNIT[unit])
    return shifted.strftime(date_format or "%Y-%m-%d")


def _full_user_name() -> str:
    """The account's full name (the first GECOS field), else $USER, else
    "User"."""
    try:
        import pwd

        gecos = pwd.getpwuid(os.getuid()).pw_gecos or ""
        full_name = gecos.split(",", 1)[0].strip()
        if full_name:
            return full_name
    except (KeyError, OSError, ImportError):
        pass
    return os.environ.get("USER") or "User"


def render_placeholders(
    text: str,
    ask_callback: Callable[[list[tuple[str, list[str] | None]]], dict | None],
) -> tuple[str, int, bool]:
    """Expand the placeholder tokens in a snippet body. {clipboard} is read
    once, before the paste replaces it (pinned by
    `tests/test_snippet_placeholders.py::test_the_clipboard_is_read_once_however_often_it_is_used`).

    `ask_callback` is called once with every {ask:} field in the body and
    returns {label: answer}, or None when the user cancels.

    Returns (rendered text, Left arrows to send after the paste so the
    caret lands where {cursor} was, cancelled).
    """
    if "{" not in text:
        return text, 0, False

    ask_answers: dict | None = None
    ask_fields = _ask_fields(text)
    if ask_fields:
        ask_answers = ask_callback(ask_fields)
        if ask_answers is None:
            return text, 0, True

    clipboard_value: str | None = None

    def expand(match: re.Match[str]) -> str:
        nonlocal clipboard_value
        placeholder = match.group(1)
        now = time.localtime()
        if placeholder in _STRFTIME_TOKENS:
            return time.strftime(_STRFTIME_TOKENS[placeholder], now)
        kind, _, argument = placeholder.partition(":")
        if kind == "date":
            try:
                return _render_date_token(argument, now)
            except (ValueError, TypeError):
                return match.group(0)
        if placeholder == "name":
            return _full_user_name()
        if placeholder == "clipboard":
            if clipboard_value is None:
                clipboard_value = _read_clipboard_text() or ""
            return clipboard_value
        if placeholder == "selection":
            return _read_primary_selection()
        if placeholder == "cursor":
            return _CURSOR_SENTINEL
        if kind == "ask":
            label, _options = _parse_ask_field(argument)
            if ask_answers is not None and label in ask_answers:
                return ask_answers[label]
            return ""
        if kind == "shell":
            return _render_shell_token(argument)
        if kind == "var":
            return _resolve_var(argument)
        return match.group(0)

    rendered, cursor_left = _take_out_cursor(_PLACEHOLDER_RE.sub(expand, text))
    return rendered, cursor_left, False


_STRFTIME_TOKENS = {
    "date": "%Y-%m-%d",
    "time": "%H:%M",
    "datetime": "%Y-%m-%d %H:%M",
    "weekday": "%A",
}
_UNLABELLED_ASK = "Value"

AskField = tuple[str, list[str] | None]


def _parse_ask_field(spec: str) -> AskField:
    """(label, choices) of what follows "ask:". {ask:Status|Open|Closed}
    is a dropdown; without choices (None) it is free text."""
    label, *choices = spec.split("|")
    options = [choice.strip() for choice in choices if choice.strip()]
    return label.strip() or _UNLABELLED_ASK, options or None


def _ask_fields(text: str) -> list[AskField]:
    """Each {ask:} label once, in order of first appearance, so one dialog
    gathers every answer."""
    fields: dict[str, list[str] | None] = {}
    for match in _PLACEHOLDER_RE.finditer(text):
        kind, _, argument = match.group(1).partition(":")
        if kind == "ask":
            label, options = _parse_ask_field(argument)
            fields.setdefault(label, options)
    return list(fields.items())


def _take_out_cursor(rendered: str) -> tuple[str, int]:
    """(text without the cursor marks, characters after the first mark)."""
    before, mark, after = rendered.partition(_CURSOR_SENTINEL)
    if not mark:
        return rendered, 0
    after = after.replace(_CURSOR_SENTINEL, "")
    return before + after, len(after)


_PICKER_W = 580
_PICKER_H = 540
# Away from the pointer, so the next click doesn't land on a row
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
    """Place the picker near the pointer, clamped to its monitor. Not on
    Wayland, where a client can't position its own toplevel."""
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
        x, y = max(0, px - _PICKER_W // 2), max(0, py - _PICKER_H // 2)
    else:
        x = px + _PICKER_OFFSET_X
        y = py + _PICKER_OFFSET_Y
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
    """Block in a nested main loop until a dialog is dismissed, so the
    callers that need an answer (render_placeholders' ask callback, the
    confirm prompts) stay synchronous. `connect_done` receives the function
    to call once the dialog is gone."""
    loop = GLib.MainLoop()

    def _done(*_a):
        if loop.is_running():
            loop.quit()
        return False

    connect_done(_done)
    present()
    loop.run()


class _PickerTabs:
    """The picker's Recent / Snippets views by index (0 = Recent), over
    the Adw.ViewStack that shows them."""

    _NAMES = ("recent", "snippets")

    def __init__(self, stack: Adw.ViewStack) -> None:
        self.stack = stack

    def get_n_pages(self) -> int:
        return len(self._NAMES)

    def get_current_page(self) -> int:
        name = self.stack.get_visible_child_name()
        return self._NAMES.index(name) if name in self._NAMES else 0

    def set_current_page(self, index: int) -> None:
        self.stack.set_visible_child_name(self._NAMES[index % len(self._NAMES)])


_GUIDE_INTRO = (
    "<span fgalpha='55%'>A <b>snippet</b> is a "
    "piece of text you save once and reuse forever. Your "
    "email signature, a phone number, a reply to a "
    "frequently asked question, a bug-report template - "
    "anything you find yourself typing more than twice.</span>"
)
# (number, heading, body markup)
_GUIDE_WAYS_TO_USE = [
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
]
# (token, description markup)
_GUIDE_PLACEHOLDERS = [
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
# (title, snippet body, note markup)
_GUIDE_RECIPES = [
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
]
_GUIDE_CASE_TRICK = (
    "<span fgalpha='55%'>"
    "Triggers match without caring about capital letters, "
    "but they copy your capitalisation to the output. "
    "If your trigger is <tt>;name</tt> and the snippet "
    "is <tt>alex morgan</tt>:"
    "</span>\n\n"
    "  <tt>;name </tt> -> alex morgan\n"
    "  <tt>;Name </tt> -> Alex morgan\n"
    "  <tt>;NAME </tt> -> ALEX MORGAN"
)
_GUIDE_LITERAL_TRIGGER = (
    "<span fgalpha='55%'>"
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
    "<span fgalpha='55%'>"
    "Tip: a trigger that begins with a non-letter "
    "character (<tt>;</tt>, <tt>:</tt>, <tt>!</tt>, "
    "<tt>@</tt>, <tt>(</tt>, etc.) practically never "
    "fires by accident, because real words don't start "
    "that way."
    "</span>"
)
# (button label, token inserted, tooltip, offset and length of the part of
# the token left selected so typing replaces it; 0, 0 selects nothing)
_PLACEHOLDER_CHIPS = [
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


def _guide_heading(text: str) -> Gtk.Label:
    heading = Gtk.Label(xalign=0)
    heading.set_markup(f"<span size='large' weight='bold'>{text}</span>")
    return heading


def _guide_paragraph(markup: str) -> Gtk.Label:
    paragraph = Gtk.Label(xalign=0)
    paragraph.set_wrap(True)
    paragraph.set_markup(markup)
    return paragraph


def _guide_separator() -> Gtk.Separator:
    separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
    separator.set_margin_top(4)
    separator.set_margin_bottom(4)
    return separator


def _guide_way_to_use(number: str, heading: str, body: str) -> Gtk.Box:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    number_label = Gtk.Label(xalign=0, yalign=0)
    number_label.set_markup(f"<span weight='bold' size='large'>{number}</span>")
    number_label.add_css_class("lp-accent")
    row.append(number_label)
    text = Gtk.Label(xalign=0, yalign=0)
    text.set_markup(f"<b>{heading}</b>\n<span fgalpha='55%'>{body}</span>")
    text.set_wrap(True)
    text.set_hexpand(True)
    row.append(text)
    return row


def _guide_placeholder_table() -> Gtk.Grid:
    grid = Gtk.Grid(column_spacing=14, row_spacing=8)
    grid.set_margin_top(4)
    for row, (token, description) in enumerate(_GUIDE_PLACEHOLDERS):
        token_label = Gtk.Label(xalign=0, yalign=0)
        token_label.set_markup(f"<tt>{GLib.markup_escape_text(token)}</tt>")
        token_label.add_css_class("lp-accent")
        token_label.set_selectable(True)
        grid.attach(token_label, 0, row, 1, 1)
        description_label = Gtk.Label(xalign=0, yalign=0)
        description_label.set_markup(f"<span fgalpha='55%'>{description}</span>")
        description_label.set_wrap(True)
        description_label.set_hexpand(True)
        grid.attach(description_label, 1, row, 1, 1)
    return grid


def _guide_recipe(snippet_body: str, note: str) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.set_margin_start(8)
    # .lp-card gives the code block a surface that follows the theme
    code_frame = Gtk.Frame()
    code_frame.add_css_class("lp-card")
    code = Gtk.Label(xalign=0, selectable=True)
    code.set_markup(f"<tt>{GLib.markup_escape_text(snippet_body)}</tt>")
    code.add_css_class("lp-accent")
    code.set_wrap(True)
    code.set_margin_top(4)
    code.set_margin_bottom(4)
    code.set_margin_start(8)
    code.set_margin_end(8)
    code_frame.set_child(code)
    box.append(code_frame)
    note_label = Gtk.Label(xalign=0)
    note_label.set_markup(f"<small><span fgalpha='55%'>{note}</span></small>")
    note_label.set_wrap(True)
    box.append(note_label)
    return box


def _build_snippet_guide() -> Gtk.Box:
    """The Snippet guide's page: what snippets are, the three ways to use
    one, every placeholder, and a few examples. Written for someone who
    doesn't program."""
    page = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=14,
        margin_top=16,
        margin_bottom=16,
        margin_start=22,
        margin_end=22,
    )
    title = Gtk.Label(xalign=0)
    title.set_markup("<span size='xx-large' weight='bold'>Snippets, in plain words</span>")
    page.append(title)
    page.append(_guide_paragraph(_GUIDE_INTRO))
    page.append(_guide_separator())

    page.append(_guide_heading("Three ways to use one"))
    for number, heading, body in _GUIDE_WAYS_TO_USE:
        page.append(_guide_way_to_use(number, heading, body))
    page.append(_guide_separator())

    page.append(_guide_heading("Placeholders you can use"))
    page.append(_guide_placeholder_table())
    page.append(_guide_separator())

    page.append(_guide_heading("A few real-world recipes"))
    for recipe_title, snippet_body, note in _GUIDE_RECIPES:
        recipe_heading = Gtk.Label(xalign=0)
        recipe_heading.set_markup(f"<b>{GLib.markup_escape_text(recipe_title)}</b>")
        page.append(recipe_heading)
        page.append(_guide_recipe(snippet_body, note))
    page.append(_guide_separator())

    page.append(_guide_heading("One small case-trick"))
    page.append(_guide_paragraph(_GUIDE_CASE_TRICK))
    page.append(_guide_separator())

    page.append(_guide_heading("Typing a trigger without expanding it"))
    page.append(_guide_paragraph(_GUIDE_LITERAL_TRIGGER))
    return page


def _form_label(markup: str, margin_top: int | None = None) -> Gtk.Label:
    label = Gtk.Label(xalign=0)
    if margin_top is not None:
        label.set_margin_top(margin_top)
    label.set_markup(markup)
    return label


def _complete_from_existing_categories(category_entry: Gtk.Entry) -> None:
    """Offer the categories already in use, so "Email" and "email" don't
    end up as two groups."""
    try:
        with _snippets_lock:
            categories = sorted({(s.category or "").strip() for s in _snippets if s.category})
        if categories:
            store = Gtk.ListStore(str)
            for category in categories:
                store.append([category])
            completion = Gtk.EntryCompletion()
            completion.set_model(store)
            completion.set_text_column(0)
            completion.set_inline_completion(True)
            category_entry.set_completion(completion)
    except Exception:
        pass


def _snippet_body_editor(min_height: int) -> tuple[Gtk.ScrolledWindow, Gtk.TextView]:
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.set_min_content_height(min_height)
    # Without the frame the text view doesn't read as an input field
    scroll.set_has_frame(True)
    scroll.set_vexpand(True)
    view = Gtk.TextView()
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    view.set_accepts_tab(False)
    view.add_css_class("lp-cmd-edit")
    scroll.set_child(view)
    return scroll, view


def _text_of(view: Gtk.TextView) -> str:
    buffer = view.get_buffer()
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)


def _insert_token(view: Gtk.TextView, token: str, select_offset: int, select_len: int) -> None:
    """Insert `token` at the caret (over any selection) and leave the part
    of it at `select_offset` selected, so typing replaces it."""
    buffer = view.get_buffer()
    if buffer.get_has_selection():
        buffer.delete_selection(False, True)
    inserted_at = buffer.get_iter_at_mark(buffer.get_insert()).get_offset()
    buffer.insert_at_cursor(token)
    if select_len > 0:
        selection_start = buffer.get_iter_at_offset(inserted_at + select_offset)
        selection_end = buffer.get_iter_at_offset(inserted_at + select_offset + select_len)
        buffer.select_range(selection_start, selection_end)
    view.grab_focus()


def _placeholder_chips(body_view: Gtk.TextView) -> Gtk.FlowBox:
    """A button per placeholder that inserts it into `body_view`."""
    chips = Gtk.FlowBox()
    chips.set_selection_mode(Gtk.SelectionMode.NONE)
    chips.set_max_children_per_line(6)
    # GTK 4 sizes a FlowBox's minimum for one child per line, which made
    # the dialog about 1100 px tall
    chips.set_min_children_per_line(3)
    chips.set_row_spacing(4)
    chips.set_column_spacing(4)
    for label, token, tooltip, select_offset, select_len in _PLACEHOLDER_CHIPS:
        chip = Gtk.Button(label=label)
        chip.set_tooltip_text(tooltip)
        chip_label = chip.get_child()
        if isinstance(chip_label, Gtk.Label):
            chip_label.set_use_markup(False)
            chip_label.add_css_class("monospace")
        chip.connect(
            "clicked",
            lambda _b, token=token, offset=select_offset, length=select_len: _insert_token(
                body_view, token, offset, length
            ),
        )
        chips.append(chip)
    return chips


def _ok_cancel_alert(heading: str) -> Adw.AlertDialog:
    alert = Adw.AlertDialog(heading=heading)
    alert.add_response("cancel", "Cancel")
    alert.add_response("ok", "OK")
    alert.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    alert.set_default_response("ok")
    alert.set_close_response("cancel")
    return alert


def _flat_icon_button(icon_name: str, tooltip: str, on_clicked, entry: Entry) -> Gtk.Button:
    button = Gtk.Button.new_from_icon_name(icon_name)
    button.set_valign(Gtk.Align.CENTER)
    button.add_css_class("flat")
    button.set_tooltip_text(tooltip)
    button.connect("clicked", on_clicked, entry)
    return button


# (pinned by `tests/test_clipboard_store.py::test_a_snippet_row_says_how_often_it_was_used_from_the_third_time`,
# `tests/test_clipboard_store.py::test_a_snippet_used_twice_shows_only_its_body`)
_USAGE_SHOWN_FROM = 3


def _snippet_subtitle(entry: Entry) -> str:
    """The line under a named snippet: the start of its body and, once it
    has been used a few times, how often."""
    body_start = entry.text[:80] if entry.kind == "text" else entry.image_path
    body_start = body_start.replace("\n", " ↵ ")
    times_used = int(entry.usage_count or 0)
    if times_used < _USAGE_SHOWN_FROM:
        return body_start
    return f"{body_start}  ·  used {times_used} times"


def _category_sort_key(entry: Entry) -> tuple[str, str]:
    """By category, then name. No category sorts last, under General."""
    category = (entry.category or "").strip()
    return (category.lower() or "￿", (entry.name or "").lower())


_FOCUS_LOSS_CONFIRM_MS = 120
_DRAG_FLAG_CLEAR_MS = 200
_RECENT_PAGE, _SNIPPETS_PAGE = 0, 1


class _PickerDialog:
    def __init__(self) -> None:
        self.dialog: Gtk.Window | None = None
        self.target_window: str | None = None
        self.search_entry: Gtk.SearchEntry | None = None
        self.tabs: _PickerTabs | None = None
        self.recent_listbox: Gtk.ListBox | None = None
        self.snippets_listbox: Gtk.ListBox | None = None
        self._filter_text = ""
        # While a prompt is up, or the window is being dragged, losing focus
        # must not close the picker
        self._modal_child_open = False
        self._dragging = False

    def show(self, target_window: str | None = None) -> None:
        started = time.monotonic()

        def log_phase(label):
            _log.info("[clipboard.show] %s: %.0f ms", label, (time.monotonic() - started) * 1000)

        log_phase("entered show()")
        if self.dialog is not None and self.dialog.get_visible():
            _position_at_pointer(self.dialog)
            _force_to_front(self.dialog)
            log_phase("reused existing")
            return
        self.target_window = target_window

        win = self._build_window()
        win.set_content(self._build_content())
        self.dialog = win
        log_phase("built widgets")
        self._populate_recent()
        log_phase("populated recent")
        self._populate_snippets()
        log_phase("populated snippets")
        _position_at_pointer(win)
        win.present()
        log_phase("present")
        # Again now it is mapped: some window managers ignore a move before that
        _position_at_pointer(win)
        _force_to_front(win)
        log_phase("force_to_front")
        # On idle: a grab right after present() races the FocusIn and is lost
        if self.search_entry is not None:
            GLib.idle_add(self._grab_search_focus)

    def _build_window(self) -> Adw.Window:
        # An Adw.Window, so the Adw.AlertDialog prompts open as sheets inside it
        win = Adw.Window()
        win.add_css_class("lp-picker")  # the theme clears GTK's dark list background by it
        win.set_title("Clipboard")
        win.set_default_size(_PICKER_W, _PICKER_H)
        win.set_icon_name("vinopop")
        # A non-resizable AdwWindow floats its alert dialogs as separate windows
        win.set_resizable(True)
        if _is_wayland_kde():
            # The compositor places a Wayland toplevel, so it keeps its decorations
            win.set_decorated(True)
        else:
            win.set_decorated(False)
            try:
                from platform_backend import get_backend

                get_backend().prepare_popup_window(win)
            except Exception as exc:
                _log.info("[clipboard.show] prepare_popup_window failed: %s", exc)
        # "destroy" can fire late in GTK 4, so _dismiss() clears the state
        # itself; these cover the window manager closing the window
        win.connect("destroy", self._on_destroy)
        win.connect("close-request", lambda w: (self._on_destroy(w), False)[1])
        win.connect("map", lambda w: _apply_x11_hints(w))
        # Capture phase: the window sees a key before the focused child
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        win.add_controller(key_ctrl)
        # GTK 4's stand-in for focus-out on a toplevel
        win.connect("notify::is-active", self._on_active_changed)
        return win

    def _build_content(self) -> Gtk.Box:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_margin_top(8)
        outer.set_margin_bottom(8)
        outer.set_margin_start(8)
        outer.set_margin_end(8)
        outer.append(self._build_title_row())
        outer.append(self._build_search_entry())

        stack = self._build_views()
        switcher = Adw.ViewSwitcher(stack=stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        switcher.set_halign(Gtk.Align.CENTER)
        switcher.set_margin_top(8)
        switcher.set_margin_bottom(4)
        outer.append(switcher)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("card")
        card.set_overflow(Gtk.Overflow.HIDDEN)
        card.append(stack)
        outer.append(card)

        keyboard_hint = Gtk.Label(
            label="Enter to paste · Tab to switch · Ctrl+P to pin · Ctrl+R to rename · "
            "Delete to remove · Esc to close",
            xalign=0,
            margin_top=8,
            margin_start=4,
            margin_end=4,
        )
        keyboard_hint.add_css_class("caption")
        keyboard_hint.add_css_class("dim-label")
        keyboard_hint.set_wrap(True)
        outer.append(keyboard_hint)
        return outer

    def _build_title_row(self) -> Gtk.Box:
        """The picker's name and, with no window decoration, its drag
        handle. Not an Adw.HeaderBar: its own drag would let the focus-out
        dismiss fire mid-move (_on_header_pressed flags ours)."""
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_row.set_margin_start(4)
        title_row.set_margin_end(4)
        title_row.set_margin_bottom(8)
        title_row.set_cursor_from_name("grab")
        drag_click = Gtk.GestureClick()
        drag_click.set_button(Gdk.BUTTON_PRIMARY)
        drag_click.connect("pressed", self._on_header_pressed)
        title_row.add_controller(drag_click)
        # The full-colour logo reads on the dark and the light theme
        logo = Gtk.Image.new_from_icon_name("vinopop")
        logo.set_pixel_size(24)
        title_row.append(logo)
        title = Gtk.Label(label="Clipboard", xalign=0, hexpand=True)
        title.add_css_class("heading")
        title_row.append(title)
        return title_row

    def _build_search_entry(self) -> Gtk.SearchEntry:
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search clipboard and snippets")
        # A placeholder isn't a name: without this it reads as a blank field
        self.search_entry.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Search clipboard history and snippets"]
        )
        self.search_entry.connect("search-changed", self._on_search_changed)
        return self.search_entry

    def _build_views(self) -> Adw.ViewStack:
        stack = Adw.ViewStack()
        stack.set_vexpand(True)
        self.tabs = _PickerTabs(stack)

        recent_scroll, self.recent_listbox = self._build_list()
        stack.add_titled_with_icon(recent_scroll, "recent", "Recent", "edit-paste-symbolic")

        snippets_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        snippets_scroll, self.snippets_listbox = self._build_list()
        snippets_box.append(snippets_scroll)
        new_snippet_btn = Gtk.Button(
            child=Adw.ButtonContent(
                icon_name="list-add-symbolic", label="_New Snippet", use_underline=True
            )
        )
        new_snippet_btn.set_halign(Gtk.Align.START)
        new_snippet_btn.set_margin_start(4)
        new_snippet_btn.set_margin_bottom(4)
        new_snippet_btn.add_css_class("flat")
        new_snippet_btn.connect("clicked", self._on_new_snippet_clicked)
        snippets_box.append(new_snippet_btn)
        stack.add_titled_with_icon(snippets_box, "snippets", "Snippets", "document-edit-symbolic")
        return stack

    def _build_list(self) -> tuple[Gtk.ScrolledWindow, Gtk.ListBox]:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        listbox.set_filter_func(self._row_filter)
        listbox.connect("row-activated", self._on_row_activated)
        scroll.set_child(listbox)
        return scroll, listbox

    def _grab_search_focus(self) -> bool:
        if self.search_entry is not None and self.dialog is not None:
            self.search_entry.grab_focus()
        return GLib.SOURCE_REMOVE

    def _on_header_pressed(self, gesture: Gtk.GestureClick, _n_press, x, y) -> None:
        """Start a window move. The window manager takes focus for the
        drag, so it is flagged for the focus-out handler."""
        win = self.dialog
        if win is None:
            return
        surface = win.get_surface()
        if surface is None:
            return
        # begin_move takes surface coordinates, which include any shadow margin
        ok, point = gesture.get_widget().compute_point(win, Graphene.Point().init(x, y))
        if not ok:
            return
        shadow_x, shadow_y = win.get_surface_transform()
        self._dragging = True
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        try:
            surface.begin_move(
                gesture.get_device(),
                gesture.get_current_button(),
                point.x + shadow_x,
                point.y + shadow_y,
                gesture.get_current_event_time(),
            )
        except Exception:
            pass
        # GDK has no "move ended" signal, and a focus-out can arrive late
        GLib.timeout_add(_DRAG_FLAG_CLEAR_MS, self._clear_dragging)

    def _clear_dragging(self) -> bool:
        self._dragging = False
        return GLib.SOURCE_REMOVE

    def _on_active_changed(self, _win, _pspec) -> None:
        if self.dialog is None or self.dialog.is_active():
            return
        if self._modal_child_open or self._dragging:
            return

        def close_if_focus_stayed_away() -> bool:
            if self.dialog is None or self._modal_child_open:
                return GLib.SOURCE_REMOVE
            try:
                still_active = self.dialog.is_active()
            except Exception:
                still_active = False
            if not still_active:
                self._dismiss()
            return GLib.SOURCE_REMOVE

        # IBus and Cinnamon take focus for a frame and give it back, so the
        # loss only counts if it lasts
        GLib.timeout_add(_FOCUS_LOSS_CONFIRM_MS, close_if_focus_stayed_away)

    def _dismiss(self) -> None:
        """Close the picker and clear its state right away."""
        win = self.dialog
        if win is None:
            return
        self._on_destroy(win)
        win.destroy()

    def _on_destroy(self, win=None, *_):
        # A stale window's late "destroy" must not wipe a newer picker
        if win is not None and win is not self.dialog:
            return
        _log.info("[clipboard.show] window destroyed -- state cleared")
        self.dialog = None
        self.search_entry = None
        self.tabs = None
        self.recent_listbox = None
        self.snippets_listbox = None
        self._filter_text = ""

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
                self._empty_row("History is empty", "Copy something and it shows up here.")
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
            for entry in sorted(entries, key=_category_sort_key):
                self.snippets_listbox.append(self._make_row(entry, is_snippet=True))
            self.snippets_listbox.set_header_func(self._snippet_header_func)

    @staticmethod
    def _snippet_header_func(row, before) -> None:
        """Put a category header above the first row of each category.
        `before` is the previous row, None for the first."""
        entry = getattr(row, "entry", None)
        if entry is None:
            row.set_header(None)
            return
        category = (entry.category or "").strip() or "General"
        previous_entry = getattr(before, "entry", None) if before else None
        previous_category = (previous_entry.category or "").strip() if previous_entry else None
        previous_category = previous_category or "General" if previous_entry else None
        if previous_category == category:
            row.set_header(None)
            return
        header = Gtk.Label(xalign=0, margin_top=6, margin_bottom=2, margin_start=10, margin_end=10)
        header.set_markup(f"<small><b>{GLib.markup_escape_text(category.upper())}</b></small>")
        header.add_css_class("dim-label")
        row.set_header(header)

    def _empty_row(self, title: str, subtitle: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        title_label = Gtk.Label(xalign=0.5)
        title_label.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
        subtitle_label = Gtk.Label(label=subtitle, xalign=0.5, wrap=True)
        subtitle_label.add_css_class("dim-label")
        box.append(title_label)
        box.append(subtitle_label)
        row.set_child(box)
        return row

    def _make_row(self, entry: Entry, is_snippet: bool) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.entry = entry  # type: ignore[attr-defined]
        row.is_snippet = is_snippet  # type: ignore[attr-defined]
        # GTK 4.14 doesn't name a list row from its text; a screen reader
        # would read every entry as a blank "list item"
        row.update_property([Gtk.AccessibleProperty.LABEL], [entry.preview()])

        hbox = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        hbox.append(self._row_thumbnail(entry, is_snippet))
        hbox.append(self._row_text(entry, is_snippet))
        for button in self._row_buttons(entry, is_snippet):
            hbox.append(button)
        row.set_child(hbox)
        return row

    @staticmethod
    def _row_thumbnail(entry: Entry, is_snippet: bool) -> Gtk.Image:
        if entry.kind == "image" and Path(entry.image_path).is_file():
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    entry.image_path,
                    48,
                    48,
                    True,
                )
                thumbnail = Gtk.Image.new_from_paintable(Gdk.Texture.new_for_pixbuf(pixbuf))
                thumbnail.set_pixel_size(48)
            except Exception:
                thumbnail = Gtk.Image.new_from_icon_name("image-x-generic-symbolic")
                thumbnail.set_pixel_size(24)
            return thumbnail
        icon = Gtk.Image.new_from_icon_name(
            "starred-symbolic" if is_snippet else "edit-paste-symbolic"
        )
        icon.set_pixel_size(24)
        return icon

    @staticmethod
    def _row_text(entry: Entry, is_snippet: bool) -> Gtk.Box:
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vbox.set_hexpand(True)
        title = Gtk.Label(xalign=0)
        title.set_markup(f"<b>{GLib.markup_escape_text(entry.preview())}</b>")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(60)
        if is_snippet and entry.trigger:
            title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            title_row.append(title)
            triggers = ", ".join(entry.trigger_list())
            trigger_label = Gtk.Label(xalign=0)
            trigger_label.set_markup(f"<small>⇥ {GLib.markup_escape_text(triggers)}</small>")
            trigger_label.add_css_class("lp-accent")
            title_row.append(trigger_label)
            vbox.append(title_row)
        else:
            vbox.append(title)

        subtitle = Gtk.Label(xalign=0)
        if is_snippet and entry.name:
            subtitle.set_markup(
                f"<small><span fgalpha='55%'>"
                f"{GLib.markup_escape_text(_snippet_subtitle(entry))}"
                f"</span></small>"
            )
            subtitle.set_ellipsize(Pango.EllipsizeMode.END)
            subtitle.set_max_width_chars(80)
        else:
            copied_at = time.strftime("%H:%M:%S", time.localtime(entry.timestamp))
            if entry.kind == "text":
                copied_at += f"  ·  {len(entry.text)} chars"
            subtitle.set_markup(f"<small><span fgalpha='55%'>{copied_at}</span></small>")
        vbox.append(subtitle)
        return vbox

    def _row_buttons(self, entry: Entry, is_snippet: bool) -> list[Gtk.Button]:
        if is_snippet:
            return [
                _flat_icon_button(
                    "user-trash-symbolic", "Delete this snippet", self._on_unpin_clicked, entry
                ),
                _flat_icon_button(
                    "document-edit-symbolic", "Rename", self._on_rename_clicked, entry
                ),
            ]
        # Removing from history asks nothing: it is how a password copied by
        # mistake goes, and the copy is still on the clipboard
        return [
            _flat_icon_button(
                "user-trash-symbolic",
                "Remove from history (Delete)",
                self._on_delete_history_clicked,
                entry,
            ),
            _flat_icon_button(
                "non-starred-symbolic",
                "Pin as snippet (also lets you name it)",
                self._on_pin_clicked,
                entry,
            ),
        ]

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

    def _on_row_activated(self, _listbox, row) -> None:
        entry = getattr(row, "entry", None)
        is_snippet = bool(getattr(row, "is_snippet", False))
        if entry is not None:
            self._paste_and_close(entry, is_snippet=is_snippet)

    def _paste_and_close(self, entry: Entry, is_snippet: bool = False) -> None:
        target = self.target_window
        if is_snippet:
            _bump_usage(entry.id)
        cursor_left = 0
        # History and literal snippets paste as copied (pinned by
        # `tests/test_clipboard_store.py::test_picker_pastes_a_literal_snippet_as_written`,
        # `tests/test_clipboard_store.py::test_picker_still_expands_a_user_snippet`)
        if is_snippet and entry.kind == "text" and not entry.literal and "{" in entry.text:
            rendered, cursor_left, cancelled = render_placeholders(
                entry.text,
                self._ask_placeholder_values,
            )
            if cancelled:
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

    @contextmanager
    def _showing_prompt(self):
        """Keep the picker open under a prompt that takes its focus."""
        self._modal_child_open = True
        try:
            yield
        finally:
            self._modal_child_open = False

    def _run_alert(
        self,
        dlg: Adw.AlertDialog,
        parent: Gtk.Widget | None = None,
        activate_widgets: tuple = (),
    ) -> str:
        """Present an Adw.AlertDialog over `parent` (the picker by default)
        and block until it closes. Returns the response id; the dialog's
        close-response when dismissed. Entries in `activate_widgets` submit
        the default response on Enter."""
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
            # nested loop spinning
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
        vertical content box. With ok_label=None it's a plain closable
        window."""
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
        keys = Gtk.EventControllerKey()

        def close_on_escape(_c, keyval, _code, _state):
            if keyval == Gdk.KEY_Escape:
                win.close()
                return True
            return False

        keys.connect("key-pressed", close_on_escape)
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

    def _ask_placeholder_values(self, fields: list[AskField]) -> dict | None:
        """render_placeholders' ask callback: one form with a field per
        {ask:} label, a dropdown where it has choices. Returns
        {label: value}, or None on Cancel."""
        if not fields:
            return {}
        with self._showing_prompt():
            dlg = _ok_cancel_alert("Snippet" if len(fields) > 1 else fields[0][0])
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            widgets: dict = {}
            text_entries: list = []
            for label, options in fields:
                field_label = Gtk.Label(xalign=0)
                field_label.set_markup(f"<b>{GLib.markup_escape_text(label)}</b>")
                content.append(field_label)
                if options:
                    field = Gtk.DropDown.new(Gtk.StringList.new(list(options)), None)
                    field.set_selected(0)
                else:
                    field = Gtk.Entry()
                    field.set_width_chars(40)
                    text_entries.append(field)
                field_label.set_mnemonic_widget(field)
                content.append(field)
                widgets[label] = field
            dlg.set_extra_child(content)
            first_field_is_text = not fields[0][1]
            if first_field_is_text:
                dlg.set_focus(text_entries[0])
            answers = {}

            def read_answers(*_a):
                for label, field in widgets.items():
                    if isinstance(field, Gtk.DropDown):
                        item = field.get_selected_item()
                        answers[label] = item.get_string() if item is not None else ""
                    else:
                        answers[label] = field.get_text()

            # Read as the response fires, before the widgets are torn down
            dlg.connect("response", read_answers)
            response = self._run_alert(dlg, activate_widgets=tuple(text_entries))
            return answers if response == "ok" else None

    def _trigger_conflicts_accepted(self, trigger_field: str, own_id: str = "") -> bool:
        """False when a trigger is already another snippet's and the user
        chose not to take it over."""
        conflicts = _find_trigger_conflicts(_parse_triggers(trigger_field), own_id=own_id)
        return not conflicts or self._confirm_trigger_conflicts(conflicts)

    def _on_pin_clicked(self, _btn, entry: Entry) -> None:
        result = self._ask_edit_snippet_meta(
            default_name=entry.preview(40),
            default_trigger="",
            default_category="",
            default_text=entry.text if entry.kind == "text" else "",
        )
        if result is None:
            return
        name, trigger, category, new_text = result
        if not self._trigger_conflicts_accepted(trigger):
            return
        if _pin_entry(entry, name, trigger=trigger, category=category, text=new_text) is None:
            return
        self._populate_snippets()
        if self.tabs is not None:
            self.tabs.set_current_page(_SNIPPETS_PAGE)

    def _on_unpin_clicked(self, _btn, entry: Entry) -> None:
        """Delete a snippet, after asking: there is no undo."""
        label = entry.name or entry.preview(40) or "this snippet"
        with self._showing_prompt():
            dlg = Adw.AlertDialog(
                heading=f'Delete "{label}"?',
                body=(
                    "The snippet, its triggers, and its category will "
                    "be removed. This can't be undone."
                ),
            )
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("delete", "Delete")
            dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.set_close_response("cancel")
            response = self._run_alert(dlg)
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

        # Keyboard users land on the entry that takes the deleted one's place
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
        if not self._trigger_conflicts_accepted(new_trigger, own_id=entry.id):
            return
        _rename_snippet(entry.id, new_name)
        _set_snippet_trigger(entry.id, new_trigger)
        _set_snippet_category(entry.id, new_category)
        if entry.kind == "text":
            _set_snippet_text(entry.id, new_text)
        self._populate_snippets()

    def _show_snippet_help_dialog(self, parent: Gtk.Window) -> None:
        with self._showing_prompt():
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
            scroll.set_child(_build_snippet_guide())
            content.append(scroll)
            self._run_form(dlg, None, lambda: None)

    def _ask_edit_snippet_meta(
        self,
        default_name: str = "",
        default_trigger: str = "",
        default_category: str = "",
        default_text: str = "",
    ) -> tuple[str, str, str, str] | None:
        """Edit name + trigger + category + body for an existing snippet.
        Returns (name, trigger, category, text) on OK, None on Cancel."""
        with self._showing_prompt():
            dlg, content, ok_btn = self._make_form_window(
                "Edit snippet", self.dialog, 520, 460, "Save"
            )
            content.set_spacing(4)

            name_label = _form_label("<b>Name</b>")
            content.append(name_label)
            name_entry = Gtk.Entry()
            name_label.set_mnemonic_widget(name_entry)
            name_entry.set_text(default_name)
            name_entry.set_activates_default(True)
            name_entry.set_width_chars(36)
            content.append(name_entry)

            trigger_label = _form_label(
                "<b>Trigger(s)</b>  <small>(optional, comma-separated)</small>", margin_top=6
            )
            content.append(trigger_label)
            trigger_entry = Gtk.Entry()
            trigger_label.set_mnemonic_widget(trigger_entry)
            trigger_entry.set_text(default_trigger)
            trigger_entry.set_activates_default(True)
            if bool(_cfg("snippet_triggers_enabled", False)):
                trigger_entry.set_placeholder_text("e.g. ;email, ;e - any of them auto-expands")
            else:
                trigger_entry.set_placeholder_text("Enable 'Snippet triggers' in Settings to use")
            content.append(trigger_entry)

            category_label = _form_label("<b>Category</b>  <small>(optional)</small>", margin_top=6)
            content.append(category_label)
            category_entry = Gtk.Entry()
            category_label.set_mnemonic_widget(category_entry)
            category_entry.set_text(default_category)
            category_entry.set_activates_default(True)
            category_entry.set_placeholder_text("e.g. Email, Code, Personal")
            _complete_from_existing_categories(category_entry)
            content.append(category_entry)

            body_label = _form_label(
                "<b>Text</b>  <small>(the snippet body - what gets "
                "pasted when this snippet fires)</small>",
                margin_top=6,
            )
            content.append(body_label)
            body_scroll, body_view = _snippet_body_editor(min_height=140)
            body_label.set_mnemonic_widget(body_view)
            body_view.get_buffer().set_text(default_text)
            content.append(body_scroll)

            guide_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, margin_top=10)
            spacer = Gtk.Label()
            spacer.set_hexpand(True)
            guide_row.append(spacer)
            guide_btn = Gtk.Button(label="📖 Snippet guide")
            guide_btn.connect("clicked", lambda _b: self._show_snippet_help_dialog(parent=dlg))
            guide_row.append(guide_btn)
            content.append(guide_row)

            def read_form():
                return (
                    name_entry.get_text().strip(),
                    trigger_entry.get_text().strip(),
                    category_entry.get_text().strip(),
                    _text_of(body_view),
                )

            dlg.set_focus(name_entry)
            return self._run_form(dlg, ok_btn, read_form)

    def _on_new_snippet_clicked(self, _btn) -> None:
        result = self._ask_new_snippet()
        if result is None:
            return
        name, body, trigger, category = result
        if not body.strip():
            return
        if not self._trigger_conflicts_accepted(trigger):
            return
        _create_snippet(name=name, text=body, trigger=trigger, category=category)
        self._populate_snippets()
        if self.tabs is not None:
            self.tabs.set_current_page(_SNIPPETS_PAGE)

    def _confirm_trigger_conflicts(
        self,
        conflicts: list[tuple],
    ) -> bool:
        """Say which triggers are another snippet's and ask whether to save
        anyway (the new snippet then takes them over)."""
        lines = [f"  '{trig}'  is already used by  '{owner}'" for trig, owner in conflicts]
        body = (
            "These triggers are already in use:\n\n"
            + "\n".join(lines)
            + "\n\nIf you save, the new snippet will take over those "
            "triggers. The other snippet keeps its other triggers, if any."
        )
        with self._showing_prompt():
            dlg = Adw.AlertDialog(heading="Trigger already in use", body=body)
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("ok", "Save anyway")
            dlg.set_close_response("cancel")
            return self._run_alert(dlg) == "ok"

    def _ask_new_snippet(self) -> tuple[str, str, str, str] | None:
        """Modal dialog: name + body + trigger + category.
        Returns (name, body, trigger, category) on OK, None on Cancel."""
        with self._showing_prompt():
            dlg, content, ok_btn = self._make_form_window(
                "New snippet", self.dialog, 480, 360, "Save"
            )
            content.set_spacing(6)

            name_label = _form_label("<b>Name</b>")
            content.append(name_label)
            name_entry = Gtk.Entry()
            name_label.set_mnemonic_widget(name_entry)
            name_entry.set_placeholder_text("Short label (optional)")
            content.append(name_entry)

            trigger_label = _form_label(
                "<b>Trigger(s)</b>  <small>(optional, comma-separated)</small>", margin_top=6
            )
            content.append(trigger_label)
            trigger_entry = Gtk.Entry()
            trigger_label.set_mnemonic_widget(trigger_entry)
            if bool(_cfg("snippet_triggers_enabled", False)):
                trigger_entry.set_placeholder_text(
                    "e.g. ;email, ;e - any of them + space/tab auto-expands"
                )
            else:
                trigger_entry.set_placeholder_text(
                    "Set, then enable 'Snippet triggers' in Settings to auto-expand"
                )
            content.append(trigger_entry)

            category_label = _form_label(
                "<b>Category</b>  <small>(optional - keeps the picker tidy)</small>", margin_top=6
            )
            content.append(category_label)
            category_entry = Gtk.Entry()
            category_label.set_mnemonic_widget(category_entry)
            category_entry.set_placeholder_text(
                "e.g. Email, Code, Personal. Leave blank for General."
            )
            _complete_from_existing_categories(category_entry)
            content.append(category_entry)

            body_label = _form_label(
                "<b>Text</b>  <small>(this is the snippet body - "
                "type or paste what you want to expand to)</small>",
                margin_top=6,
            )
            content.append(body_label)
            body_scroll, body_view = _snippet_body_editor(min_height=180)
            body_label.set_mnemonic_widget(body_view)
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
            content.append(_placeholder_chips(body_view))

            def read_form():
                return (
                    name_entry.get_text().strip(),
                    _text_of(body_view),
                    trigger_entry.get_text().strip(),
                    category_entry.get_text().strip(),
                )

            dlg.set_focus(name_entry)
            return self._run_form(dlg, ok_btn, read_form)

    def _ask_for_name(self, default: str = "") -> str | None:
        with self._showing_prompt():
            dlg = _ok_cancel_alert("Snippet name")
            entry = Gtk.Entry()
            entry.update_property([Gtk.AccessibleProperty.LABEL], ["Snippet name"])
            entry.set_text(default)
            dlg.set_extra_child(entry)
            dlg.set_focus(entry)
            value = [""]
            dlg.connect("response", lambda *_a: value.__setitem__(0, entry.get_text().strip()))
            response = self._run_alert(dlg, activate_widgets=(entry,))
            if response != "ok":
                return None
            return value[0]

    def _current_listbox(self) -> Gtk.ListBox | None:
        if self.tabs is None:
            return None
        on_recent = self.tabs.get_current_page() == _RECENT_PAGE
        return self.recent_listbox if on_recent else self.snippets_listbox

    def _first_visible_row(self, listbox: Gtk.ListBox):
        """The first row that survives the current filter, or None."""
        for row in self._rows(listbox):
            if row.get_child_visible() and (row.get_mapped() or self._filter_text == ""):
                # get_visible() lags invalidate_filter() by a tick
                if self._row_filter(row):
                    return row
        return None

    def _last_matching_row(self, listbox: Gtk.ListBox):
        matching = [row for row in self._rows(listbox) if self._row_filter(row)]
        return matching[-1] if matching else None

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
        """(pinned by `tests/test_clipboard_picker_keys.py::test_a_prompt_inside_the_picker_gets_its_own_keys`)"""
        if self._modal_child_open:
            return False
        return self._handle_key(keyval, state)

    def _handle_key(self, keyval: int, state) -> bool:
        """tests/test_clipboard_picker_keys.py pins what each key does."""
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if keyval == Gdk.KEY_Escape:
            if self.dialog is not None:
                self._dismiss()
            return True
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            return self._switch_view()
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            return self._arrow_into_list(downwards=keyval == Gdk.KEY_Down)
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return self._activate_selected_row()
        if ctrl and keyval == Gdk.KEY_p:
            self._action_on_selected("pin")
            return True
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            return self._delete_selected_history_row(from_anywhere=ctrl)
        if ctrl and keyval == Gdk.KEY_r:
            self._action_on_selected("rename")
            return True
        return self._type_into_search(keyval, state)

    def _switch_view(self) -> bool:
        """Tab flips Recent and Snippets instead of cycling focus."""
        if self.tabs is not None and self.tabs.get_n_pages() > 1:
            self.tabs.set_current_page(1 - self.tabs.get_current_page())
        return True

    def _arrow_into_list(self, downwards: bool) -> bool:
        """From outside the list, Down focuses its first row and Up its
        last. Inside it, GTK's own row navigation takes the key."""
        listbox = self._current_listbox()
        if listbox is None or self._focus_within(listbox):
            return False
        if downwards:
            target = self._first_visible_row(listbox)
        else:
            target = self._last_matching_row(listbox)
        if target is not None:
            listbox.select_row(target)
            target.grab_focus()
        else:
            listbox.grab_focus()
        return True

    def _activate_selected_row(self) -> bool:
        listbox = self._current_listbox()
        if listbox is not None:
            selected = listbox.get_selected_row()
            if selected is not None:
                listbox.emit("row-activated", selected)
                return True
        return False

    def _delete_selected_history_row(self, from_anywhere: bool) -> bool:
        """Plain Delete only while a row has focus (in the search box it
        edits the text); Ctrl+Delete from anywhere."""
        on_recent = self.tabs is not None and self.tabs.get_current_page() == _RECENT_PAGE
        if on_recent and (from_anywhere or self._focus_within(self.recent_listbox)):
            self._action_on_selected("delete")
            return True
        return False

    def _type_into_search(self, keyval: int, state) -> bool:
        """A printable key pressed outside the search box starts a search."""
        chord = (
            Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK
        )
        if state & chord:
            return False
        code_point = Gdk.keyval_to_unicode(keyval)
        if not (code_point and 0x20 <= code_point < 0x10FFFF):
            return False
        char = chr(code_point)
        if not char.isprintable() or self.search_entry is None:
            return False
        if self._focus_within(self.search_entry):
            return False
        self.search_entry.grab_focus()
        # The key was consumed here, so it never reaches the entry by itself
        self.search_entry.set_text(self.search_entry.get_text() + char)
        self.search_entry.set_position(-1)
        return True

    def _action_on_selected(self, action: str) -> None:
        if self.tabs is None:
            return
        page = self.tabs.get_current_page()
        listbox = self.recent_listbox if page == _RECENT_PAGE else self.snippets_listbox
        if listbox is None:
            return
        row = listbox.get_selected_row()
        if row is None:
            return
        entry = getattr(row, "entry", None)
        if entry is None:
            return
        if action == "pin" and page == _RECENT_PAGE:
            self._on_pin_clicked(None, entry)
        elif action == "delete" and page == _RECENT_PAGE:
            self._on_delete_history_clicked(None, entry)
        elif action == "rename" and page == _SNIPPETS_PAGE:
            self._on_rename_clicked(None, entry)


_picker = _PickerDialog()


def open_picker(target_window: str | None = None) -> None:
    """Open the picker from the popup button or main.py's clipboard hotkey."""
    GLib.idle_add(_picker.show, target_window)


def _open_from_popup(_text: str) -> None:
    window_to_paste_into = _get_active_window()  # read before the picker takes focus
    open_picker(window_to_paste_into)


def register(register_plugin) -> None:
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
            content_types=(),
            priority=5,
        )
    )
