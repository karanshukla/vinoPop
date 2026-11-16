"""A global X11 hotkey: "ctrl+alt+space" grabbed on the root window from
its own thread, firing a callback on the GTK main thread."""

from __future__ import annotations

import select
import threading
from collections.abc import Callable, Iterable

from gi.repository import GLib
from Xlib import XK, X, display, error

_MOD_NAMES = {
    "shift": X.ShiftMask,
    "ctrl": X.ControlMask,
    "control": X.ControlMask,
    "alt": X.Mod1Mask,
    "mod1": X.Mod1Mask,
    "super": X.Mod4Mask,
    "win": X.Mod4Mask,
    "meta": X.Mod4Mask,
    "mod4": X.Mod4Mask,
    "hyper": X.Mod3Mask,
    "mod3": X.Mod3Mask,
}

# Our key names -> XStringToKeysym names
_KEY_ALIASES = {
    "space": "space",
    "spacebar": "space",
    "tab": "Tab",
    "return": "Return",
    "enter": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "backspace": "BackSpace",
    "delete": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "Page_Up",
    "pagedown": "Page_Down",
    "page_up": "Page_Up",
    "page_down": "Page_Down",
    "iso_left_tab": "Tab",  # Shift+Tab; the Shift is already a modifier
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
}


# Without Caps and Num Lock (pinned by
# `tests/test_hotkey_listen.py::test_caps_lock_and_num_lock_do_not_stop_it_firing`,
# `tests/test_hotkey_listen.py::test_a_press_with_an_extra_modifier_is_ignored`).
_MODIFIER_MASK = X.ShiftMask | X.ControlMask | X.Mod1Mask | X.Mod3Mask | X.Mod4Mask
_LOCK_VARIANTS = (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask)

_STOP_CHECK_S = 1.0
_POLL_INTERVAL_S = 0.05
# Consecutive poll errors (~2 s) that mean the X connection is gone
_MAX_POLL_FAILURES = 40


def _parse(hotkey: str) -> tuple[int, str]:
    """Return (modifier_mask, keysym_name) for a hotkey string."""
    parts = [p.strip().lower() for p in hotkey.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty hotkey: {hotkey!r}")
    mods = 0
    key_token = parts[-1]
    for token in parts[:-1]:
        if token not in _MOD_NAMES:
            raise ValueError(f"unknown modifier {token!r} in {hotkey!r}")
        mods |= _MOD_NAMES[token]
    if key_token in _KEY_ALIASES:
        key_name = _KEY_ALIASES[key_token]
    elif len(key_token) > 1 and key_token.startswith("f") and key_token[1:].isdigit():
        key_name = key_token.upper()
    else:
        key_name = key_token
    return mods, key_name


def _resolve_keysym(name: str) -> int:
    """The keysym for a key name in any case ('return', 'page_up',
    'xf86audioplay'), 0 if none (pinned by
    `tests/test_hotkey.py::test_recorded_key_names_resolve`,
    `tests/test_hotkey.py::test_unknown_key_names_still_fail_cleanly`).
    X keysym names mix cases ('Return', 'space', 'BackSpace')."""
    for cand in dict.fromkeys((name, name.lower(), name[:1].upper() + name[1:], name.upper())):
        ks = XK.string_to_keysym(cand)
        if ks:
            return ks
    real = _keysym_names().get(name.lower())
    return XK.string_to_keysym(real) if real else 0


_KEYSYM_NAMES: dict[str, str] | None = None


def _keysym_names() -> dict[str, str]:
    """lower-case keysym name -> real name, for every keysym python-xlib knows."""
    global _KEYSYM_NAMES
    if _KEYSYM_NAMES is None:
        for group in ("xkb", "xf86", "latin2", "latin3", "latin4", "greek", "cyrillic", "hebrew"):
            try:
                XK.load_keysym_group(group)
            except Exception:  # noqa: BLE001 - a missing group only narrows the table
                pass
        _KEYSYM_NAMES = {n[3:].lower(): n[3:] for n in dir(XK) if n.startswith("XK_")}
        # python-xlib spells "XF86_AudioPlay" what GDK calls "XF86AudioPlay".
        for real in list(_KEYSYM_NAMES.values()):
            if real.startswith("XF86_"):
                _KEYSYM_NAMES.setdefault(real.replace("_", "").lower(), real)
    return _KEYSYM_NAMES


def _lock_variants(dpy: display.Display) -> Iterable[int]:
    """Each Caps/Num Lock mask to grab alongside the hotkey's own (pinned by
    `tests/test_hotkey_listen.py::test_grabs_the_key_with_every_lock_modifier_variant`)."""
    return list(_LOCK_VARIANTS)


def _is_autorepeat(press_time: int, last_release_time: int | None) -> bool:
    """X11 autorepeat arrives as KeyRelease + KeyPress with the same server
    time; a real second press can't (pinned by
    `tests/test_hotkey.py::test_x11_autorepeat_is_not_a_new_press`,
    `tests/test_hotkey_listen.py::test_a_held_key_fires_once`,
    `tests/test_hotkey_listen.py::test_a_second_press_after_a_release_fires_again`)."""
    return last_release_time is not None and press_time == last_release_time


class Hotkey:
    def __init__(
        self,
        hotkey_str: str,
        on_trigger: Callable[[], None],
        use_polling: bool = False,
    ) -> None:
        self._hotkey_str = hotkey_str
        self._on_trigger = on_trigger
        self._use_polling = use_polling
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mods: int = 0
        self._keycode: int = 0
        self._grabbed_combos: list[tuple[int, int]] = []  # (keycode, modmask)
        self._dpy: display.Display | None = None
        self._root = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="vinopop-hotkey")
        self._thread.start()

    def stop(self, wait: bool = True, timeout: float = 1.5) -> None:
        """Stop the thread. `wait` blocks until the grab is released, so on a
        rebind the old ungrab can't race the new grab of the same key."""
        self._stop.set()
        if wait and self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        try:
            self._dpy = display.Display()
        except Exception as exc:  # noqa: BLE001
            print(f"[hotkey] cannot open display: {exc}")
            return
        # The X server caps its clients: never leak a connection on exit.
        try:
            self._listen(self._dpy)
        finally:
            self._release()

    def _release(self) -> None:
        dpy, self._dpy = self._dpy, None
        if dpy is None:
            return
        if self._root is not None:
            for keycode, mask in self._grabbed_combos:
                try:
                    self._root.ungrab_key(keycode, mask)
                except Exception:
                    pass
        self._grabbed_combos.clear()
        try:
            dpy.close()
        except Exception:
            pass

    def _listen(self, dpy: display.Display) -> None:
        binding = self._resolve_binding(dpy)
        if binding is None:
            return
        mods, keysym, keycode = binding
        self._mods = mods
        self._keycode = keycode
        self._root = dpy.screen().root

        if self._use_polling:
            print(
                f"[hotkey] '{self._hotkey_str}' polling mode (keycode={keycode}, mods=0x{mods:x})"
            )
            self._poll_loop()
            return

        refused = self._grab_or_report(dpy, keycode)
        if not self._grabbed_combos:
            self._notify_conflict()
            return
        if refused:
            print(f"[hotkey] {refused} lock-variant grab(s) refused (non-fatal)")

        print(f"[hotkey] listening for {self._hotkey_str} (keycode={keycode}, mods=0x{mods:x})")
        self._event_loop(dpy, keysym, keycode)

    def _resolve_binding(self, dpy: display.Display) -> tuple[int, int, int] | None:
        """(modifier mask, keysym, keycode), or None with the reason printed."""
        try:
            mods, keysym_name = _parse(self._hotkey_str)
        except ValueError as exc:
            print(f"[hotkey] {exc}")
            return None
        keysym = _resolve_keysym(keysym_name)
        if keysym == 0:
            print(f"[hotkey] unknown key name: {keysym_name!r}")
            return None
        keycode = dpy.keysym_to_keycode(keysym)
        if keycode == 0:
            print(f"[hotkey] no keycode for {keysym_name!r}")
            return None
        return mods, keysym, keycode

    def _grab(self, dpy: display.Display, keycode: int, mask: int) -> bool:
        """False when another client already owns the combination."""
        catcher = error.CatchError(error.BadAccess)
        self._root.grab_key(keycode, mask, 1, X.GrabModeAsync, X.GrabModeAsync, onerror=catcher)
        dpy.sync()
        if catcher.get_error():
            return False
        self._grabbed_combos.append((keycode, mask))
        return True

    def _grab_or_report(self, dpy: display.Display, keycode: int) -> int:
        """Grab every lock-modifier variant; returns how many were refused."""
        refused = 0
        for extra in _lock_variants(dpy):
            mask = self._mods | extra
            try:
                if not self._grab(dpy, keycode, mask):
                    refused += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[hotkey] grab failed for mask 0x{mask:x}: {exc}")
                refused += 1
        return refused

    def _notify_conflict(self) -> None:
        print(f"[hotkey] '{self._hotkey_str}' could not be grabbed - already in use")
        from core import actions

        actions.notify(
            "Hotkey conflict",
            f"'{self._hotkey_str}' is already bound by another app. "
            "Pick a different combination in Settings.",
            "dialog-warning",
            urgency="critical",
        )

    def _event_loop(self, dpy: display.Display, keysym: int, keycode: int) -> None:
        last_release: int | None = None
        while not self._stop.is_set():
            if dpy.pending_events() == 0:
                try:
                    select.select([dpy.fileno()], [], [], _STOP_CHECK_S)
                except OSError:
                    break
                continue
            event = dpy.next_event()
            if event.type == X.MappingNotify:
                keycode = self._follow_remap(dpy, event, keysym, keycode)
            elif event.type == X.KeyRelease and event.detail == keycode:
                last_release = event.time
            elif event.type == X.KeyPress and event.detail == keycode:
                if not _is_autorepeat(event.time, last_release):
                    self._on_press(event.state)

    def _follow_remap(self, dpy: display.Display, event, keysym: int, keycode: int) -> int:
        """The keycode the hotkey's keysym is on now, re-grabbed if it moved."""
        try:
            event.refresh_keyboard_mapping()
        except Exception:
            pass
        new_keycode = dpy.keysym_to_keycode(keysym)
        if not new_keycode or new_keycode == keycode:
            return keycode
        print(
            f"[hotkey] '{self._hotkey_str}' keycode shifted "
            f"{keycode} -> {new_keycode} after MappingNotify; "
            f"re-grabbing"
        )
        for old_keycode, old_mask in list(self._grabbed_combos):
            try:
                self._root.ungrab_key(old_keycode, old_mask)
            except Exception:
                pass
        self._grabbed_combos.clear()
        for extra in _lock_variants(dpy):
            try:
                self._grab(dpy, new_keycode, self._mods | extra)
            except Exception:
                pass
        return new_keycode

    def _on_press(self, state: int) -> None:
        effective = state & _MODIFIER_MASK
        if effective == self._mods:
            print(f"[hotkey] '{self._hotkey_str}' fired -- scheduling trigger on GTK main thread")
            GLib.idle_add(self._safe_trigger)
        else:
            print(
                f"[hotkey] '{self._hotkey_str}' press ignored: "
                f"expected mods=0x{self._mods:x} got effective=0x{effective:x} "
                f"(full state=0x{state:x})"
            )

    def _poll_loop(self) -> None:
        """Fire when the key goes down with the modifiers held, sampling
        without XGrabKey, for when another client (Cinnamon's Super
        detector) eats the grabbed press. A key already held at start
        doesn't count. query_pointer().mask is X's only live modifier state."""
        import time as _time

        from platform_backend.x11 import _key_is_down

        keycode = self._keycode
        mods = self._mods
        prev_pressed = False
        failures = 0
        try:
            prev_pressed = _key_is_down(self._dpy.query_keymap(), keycode)
        except Exception:
            pass
        while not self._stop.is_set():
            try:
                pressed = _key_is_down(self._dpy.query_keymap(), keycode)
                if pressed and not prev_pressed:
                    data = self._root.query_pointer()
                    effective = data.mask & _MODIFIER_MASK
                    if effective == mods:
                        print(
                            f"[hotkey] '{self._hotkey_str}' fired (polling) -- scheduling trigger"
                        )
                        GLib.idle_add(self._safe_trigger)
                prev_pressed = pressed
                failures = 0
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"[hotkey] poll error: {exc}")
                if failures >= _MAX_POLL_FAILURES:
                    print(f"[hotkey] '{self._hotkey_str}' polling stopped")
                    return
            _time.sleep(_POLL_INTERVAL_S)

    def _safe_trigger(self) -> bool:
        try:
            self._on_trigger()
        except Exception as exc:  # noqa: BLE001
            print(f"[hotkey] trigger handler failed: {exc}")
        return False
