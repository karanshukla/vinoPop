"""Global hotkeys on KDE Plasma Wayland through KGlobalAccel (KDE's global
shortcut daemon, part of KWin on Wayland). Best-effort and non-fatal: if
registration fails the app runs normally and auto-popup on selection is
unaffected.

No GTK import at module level, so the key encoding is testable without it.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_QT_MODS = {
    "shift": 0x02000000,
    "ctrl": 0x04000000,
    "control": 0x04000000,
    "alt": 0x08000000,
    "super": 0x10000000,
    "win": 0x10000000,
    "meta": 0x10000000,
}
_QT_SHIFT = 0x02000000
_QT_KEYPAD = 0x20000000  # Qt::KeypadModifier; KWin adds it for every KP_* keysym
_QT_NAMED_KEYS = {
    "escape": 0x01000000,
    "tab": 0x01000001,
    "return": 0x01000004,
    "enter": 0x01000005,
    "space": 0x20,
    "backspace": 0x01000003,
    "delete": 0x01000007,
    "home": 0x01000010,
    "end": 0x01000011,
    "left": 0x01000012,
    "up": 0x01000013,
    "right": 0x01000014,
    "down": 0x01000015,
    "pageup": 0x01000016,
    "pagedown": 0x01000017,
    "insert": 0x01000006,
    # The Settings recorder stores GDK key names ("page_up", "print", ...)
    "page_up": 0x01000016,
    "page_down": 0x01000017,
    "prior": 0x01000016,
    "next": 0x01000017,
    "pause": 0x01000008,
    "print": 0x01000009,
    "scroll_lock": 0x01000026,
    "menu": 0x01000055,
    # Shift+Tab is reported as ISO_Left_Tab; shift is already a modifier
    "iso_left_tab": 0x01000001,
    # media keys (GDK: XF86AudioPlay, ...)
    "xf86audioplay": 0x01000080,
    "xf86audiostop": 0x01000081,
    "xf86audioprev": 0x01000082,
    "xf86audionext": 0x01000083,
    "xf86audiolowervolume": 0x01000070,
    "xf86audiomute": 0x01000071,
    "xf86audioraisevolume": 0x01000072,
}
# GDK names of printable punctuation -> the character, which is also the
# Qt::Key value (Qt::Key_Comma == 0x2c == ord(",")).
_QT_PUNCTUATION = {
    "comma": ",",
    "period": ".",
    "minus": "-",
    "equal": "=",
    "plus": "+",
    "slash": "/",
    "backslash": "\\",
    "semicolon": ";",
    "apostrophe": "'",
    "grave": "`",
    "bracketleft": "[",
    "bracketright": "]",
    "asterisk": "*",
    # symbols that need Shift on US QWERTY
    "exclam": "!",
    "at": "@",
    "numbersign": "#",
    "dollar": "$",
    "percent": "%",
    "asciicircum": "^",
    "ampersand": "&",
    "parenleft": "(",
    "parenright": ")",
    "underscore": "_",
    "colon": ":",
    "quotedbl": '"',
    "less": "<",
    "greater": ">",
    "question": "?",
    "bar": "|",
    "braceleft": "{",
    "braceright": "}",
    "asciitilde": "~",
}
# GDK keypad names -> Qt key, sent with Qt::KeypadModifier
_QT_KEYPAD_KEYS = {
    **{f"kp_{d}": ord(str(d)) for d in range(10)},
    "kp_add": ord("+"),
    "kp_subtract": ord("-"),
    "kp_multiply": ord("*"),
    "kp_divide": ord("/"),
    "kp_decimal": ord("."),
    "kp_separator": ord(","),
    "kp_equal": ord("="),
    "kp_enter": 0x01000005,
    # NumLock off
    "kp_home": 0x01000010,
    "kp_end": 0x01000011,
    "kp_left": 0x01000012,
    "kp_up": 0x01000013,
    "kp_right": 0x01000014,
    "kp_down": 0x01000015,
    "kp_prior": 0x01000016,
    "kp_page_up": 0x01000016,
    "kp_next": 0x01000017,
    "kp_page_down": 0x01000017,
    "kp_insert": 0x01000006,
    "kp_delete": 0x01000007,
    "kp_begin": 0x0100000B,  # Qt::Key_Clear
}


def _qt_keycode(hotkey_str: str) -> int | None:
    """Encode 'super+shift+y' as a Qt QKeySequence int (modifiers OR'd with
    the Qt::Key value), which is what KGlobalAccel.setShortcut expects."""
    parts = [p.strip().lower() for p in hotkey_str.split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    for tok in parts[:-1]:
        if tok not in _QT_MODS:
            return None
        mods |= _QT_MODS[tok]
    key = parts[-1]
    if key in _QT_NAMED_KEYS:
        return mods | _QT_NAMED_KEYS[key]
    if key in _QT_KEYPAD_KEYS:
        return mods | _QT_KEYPAD | _QT_KEYPAD_KEYS[key]
    if key.startswith("f") and key[1:].isdigit():
        return mods | (0x01000030 + (int(key[1:]) - 1))  # Qt::Key_F1 = 0x01000030
    if key in _QT_PUNCTUATION:
        char = _QT_PUNCTUATION[key]
    elif len(key) == 1:
        char = key
    else:
        return None
    if not char.isalpha():
        # GDK recorded the shifted symbol ("exclam", not "1"), so Shift was
        # what produced it. KWin counts Shift as consumed for non-letters and
        # reports Ctrl+Shift+1 as Ctrl+!, so the shortcut must not include it.
        mods &= ~_QT_SHIFT
    return mods | ord(char.upper())


COMPONENT = "vinopop"
COMPONENT_NAME = "vinoPop"
# Stable per-role action ids, so a crash or a rebind can't leave numbered
# leftovers in kglobalshortcutsrc or collide with them.
ROLES = {
    "popup": ("vinopop-popup", "Show the popup"),
    "clipboard": ("vinopop-clipboard", "Clipboard history"),
    "ocr": ("vinopop-ocr", "Screen OCR"),
}
# Fallback by callback name, for callers that don't pass role=
_ROLE_BY_CALLBACK = {
    "show_popup_now": "popup",
    "_on_clipboard_hotkey": "clipboard",
    "_on_ocr_hotkey": "ocr",
}
_LEGACY_ACTION = re.compile(r"^vinopop-action-\d+$")


def role_for(on_trigger: Callable[[], None], role: str | None = None) -> str:
    if role:
        return role
    return _ROLE_BY_CALLBACK.get(getattr(on_trigger, "__name__", ""), "popup")


def action_id_for_role(role: str) -> tuple[str, str]:
    """(action unique name, friendly name) for a hotkey role."""
    if role in ROLES:
        return ROLES[role]
    safe = re.sub(r"[^a-z0-9-]+", "-", role.lower()).strip("-") or "hotkey"
    return (f"vinopop-{safe}", role)


_legacy_cleaned = False


def cleanup_legacy_actions(kga) -> list[str]:
    """Unregister the old creation-order ids (vinopop-action-N) once per run.
    Returns the names removed."""
    global _legacy_cleaned
    if _legacy_cleaned:
        return []
    _legacy_cleaned = True
    removed = []
    try:
        actions = kga.allActionsForComponent([COMPONENT, "", COMPONENT_NAME, ""])
    except Exception:  # noqa: BLE001 - no component yet, or an older KGlobalAccel
        return []
    for action in actions:
        names = [str(a) for a in action]
        if len(names) >= 2 and _LEGACY_ACTION.match(names[1]):
            try:
                kga.unRegister(names)
                removed.append(names[1])
            except Exception:  # noqa: BLE001
                pass
    if removed:
        print(f"[wayland] removed stale KGlobalAccel actions: {', '.join(removed)}")
    return removed


class WaylandKdeHotkey:
    """Registers a global shortcut with KGlobalAccel over DBus.

    All failures are swallowed and logged; the app keeps running either way.
    """

    def __init__(
        self, hotkey_str: str, on_trigger: Callable[[], None], role: str | None = None
    ) -> None:
        self._hotkey_str = hotkey_str
        self._on_trigger = on_trigger
        self.role = role_for(on_trigger, role)
        self._action, self._friendly = action_id_for_role(self.role)
        self._component = COMPONENT
        self._registered = False
        self._match = None  # dbus SignalMatch, removed in stop()

    def _action_id(self, dbus):
        return dbus.Array(
            [self._component, self._action, COMPONENT_NAME, self._friendly], signature="s"
        )

    def start(self) -> None:
        try:
            self._register()
        except Exception as exc:  # noqa: BLE001
            print(
                f"[wayland] KGlobalAccel registration failed for "
                f"{self._hotkey_str!r}: {exc} (hotkey inactive; auto-popup "
                f"on selection still works)"
            )

    def _register(self) -> None:
        import dbus

        keycode = _qt_keycode(self._hotkey_str)
        if keycode is None:
            print(f"[wayland] cannot encode hotkey {self._hotkey_str!r}")
            return
        bus = dbus.SessionBus()
        kga = dbus.Interface(
            bus.get_object("org.kde.kglobalaccel", "/kglobalaccel"),
            "org.kde.KGlobalAccel",
        )
        cleanup_legacy_actions(kga)
        action_id = self._action_id(dbus)
        kga.doRegister(action_id)
        # Register with setShortcut(actionId: as, keys: ai, flags: u). The keys
        # argument is a PLAIN array of Qt key ints (each modifiers|key); the
        # call returns the keycodes actually assigned. Verified against the live
        # KF6 interface: it returns the requested keycode, i.e. the grab IS
        # activated - so this single call is sufficient on Plasma 6.
        #
        # Do NOT use setShortcutKeys here. Its keys argument is QList<QKeySequence>
        # (wire type `a(ai)`), which KGlobalAccel/KWin demarshals with a custom
        # QKeySequence operator that ABORTS the whole process on the slightest
        # framing mismatch. Hand-marshalling that nested type from dbus-python
        # made kwin_wayland SIGABRT inside libdbus ("type invalid 0 not a basic
        # type") on every vinoPop startup, taking the entire Plasma session down
        # with it. Plain `ai` is type-trivial and cannot trigger that abort.
        # flags = SetPresent(2) | NoAutoloading(4) = 6. SetPresent is ESSENTIAL
        # on Plasma 6 Wayland: without it the key is recorded but the component
        # stays INACTIVE (isActive()==False), so KWin never installs the physical
        # key grab and presses do nothing (only DBus invokeShortcut fires). With
        # SetPresent the component goes active and KWin grabs the key. Verified
        # live: flag 4 -> isActive False, key not grabbed; flag 6 -> isActive
        # True, key grabbed. NoAutoloading keeps the keys we pass (not stored config).
        flags = dbus.UInt32(6)
        assigned = kga.setShortcut(
            action_id, dbus.Array([dbus.Int32(keycode)], signature="i"), flags
        )
        if not list(assigned):
            print(
                f"[wayland] KGlobalAccel did not assign {self._hotkey_str!r} "
                f"- it is likely already bound to another action (e.g. a KDE "
                f"system shortcut); the hotkey will be inactive"
            )
        kga.getComponent(self._component)
        # Subscribe broadly (interface + signal only). Pinning bus_name/path
        # can silently miss the signal if KWin emits it from a different sender
        # name or object path than getComponent reports; the handler filters by
        # our action id anyway.
        self._match = bus.add_signal_receiver(
            self._on_pressed,
            signal_name="globalShortcutPressed",
            dbus_interface="org.kde.kglobalaccel.Component",
        )
        self._registered = True
        print(
            f"[wayland] registered global shortcut {self._hotkey_str!r} "
            f"via KGlobalAccel (action={self._action})"
        )

    def _on_pressed(self, *args) -> None:
        # globalShortcutPressed(componentUnique, shortcutUnique, timestamp).
        # This fires for EVERY global shortcut on the desktop, so stay quiet
        # unless it's ours. Matched anywhere in the args, as before, in case
        # KWin's argument order differs.
        if self._action not in {str(a) for a in args}:
            return
        from gi.repository import GLib

        GLib.idle_add(self._safe_trigger)

    def _safe_trigger(self) -> bool:
        try:
            self._on_trigger()
        except Exception as exc:  # noqa: BLE001
            print(f"[wayland] hotkey trigger failed: {exc}")
        return False

    def stop(self, wait: bool = True, timeout: float = 1.5) -> None:
        if self._match is not None:
            try:
                self._match.remove()
            except Exception:  # noqa: BLE001
                pass
            self._match = None
        if not self._registered:
            return
        try:
            import dbus

            bus = dbus.SessionBus()
            kga = dbus.Interface(
                bus.get_object("org.kde.kglobalaccel", "/kglobalaccel"),
                "org.kde.KGlobalAccel",
            )
            kga.unRegister(self._action_id(dbus))
        except Exception:
            pass
        self._registered = False
