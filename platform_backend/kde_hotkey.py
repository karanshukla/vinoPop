"""Global hotkeys on KDE Plasma Wayland through KGlobalAccel (KDE's global
shortcut daemon, part of KWin on Wayland). Best-effort and non-fatal: if
registration fails the app runs normally and auto-popup on selection is
unaffected.

No GTK import at module level, so the key encoding is testable without it.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# Qt::KeyboardModifier values
_QT_SHIFT = 0x02000000
_QT_CTRL = 0x04000000
_QT_ALT = 0x08000000
_QT_META = 0x10000000
_QT_KEYPAD = 0x20000000  # KWin adds it for every KP_* keysym
_QT_MODS = {
    "shift": _QT_SHIFT,
    "ctrl": _QT_CTRL,
    "control": _QT_CTRL,
    "alt": _QT_ALT,
    "super": _QT_META,
    "win": _QT_META,
    "meta": _QT_META,
}

# Qt::Key values
_KEY_ESCAPE = 0x01000000
_KEY_TAB = 0x01000001
_KEY_BACKSPACE = 0x01000003
_KEY_RETURN = 0x01000004
_KEY_ENTER = 0x01000005
_KEY_INSERT = 0x01000006
_KEY_DELETE = 0x01000007
_KEY_PAUSE = 0x01000008
_KEY_PRINT = 0x01000009
_KEY_CLEAR = 0x0100000B
_KEY_HOME = 0x01000010
_KEY_END = 0x01000011
_KEY_LEFT = 0x01000012
_KEY_UP = 0x01000013
_KEY_RIGHT = 0x01000014
_KEY_DOWN = 0x01000015
_KEY_PAGEUP = 0x01000016
_KEY_PAGEDOWN = 0x01000017
_KEY_SCROLLLOCK = 0x01000026
_KEY_F1 = 0x01000030
_KEY_MENU = 0x01000055
_KEY_VOLUMEDOWN = 0x01000070
_KEY_VOLUMEMUTE = 0x01000071
_KEY_VOLUMEUP = 0x01000072
_KEY_MEDIAPLAY = 0x01000080
_KEY_MEDIASTOP = 0x01000081
_KEY_MEDIAPREVIOUS = 0x01000082
_KEY_MEDIANEXT = 0x01000083
_KEY_SPACE = 0x20

# Our names and the GDK names the Settings recorder stores
_QT_NAMED_KEYS = {
    "escape": _KEY_ESCAPE,
    "tab": _KEY_TAB,
    "return": _KEY_RETURN,
    "enter": _KEY_ENTER,
    "space": _KEY_SPACE,
    "backspace": _KEY_BACKSPACE,
    "delete": _KEY_DELETE,
    "home": _KEY_HOME,
    "end": _KEY_END,
    "left": _KEY_LEFT,
    "up": _KEY_UP,
    "right": _KEY_RIGHT,
    "down": _KEY_DOWN,
    "pageup": _KEY_PAGEUP,
    "pagedown": _KEY_PAGEDOWN,
    "insert": _KEY_INSERT,
    "page_up": _KEY_PAGEUP,
    "page_down": _KEY_PAGEDOWN,
    "prior": _KEY_PAGEUP,
    "next": _KEY_PAGEDOWN,
    "pause": _KEY_PAUSE,
    "print": _KEY_PRINT,
    "scroll_lock": _KEY_SCROLLLOCK,
    "menu": _KEY_MENU,
    "iso_left_tab": _KEY_TAB,  # GDK's Shift+Tab; the Shift is already a modifier
    "xf86audioplay": _KEY_MEDIAPLAY,
    "xf86audiostop": _KEY_MEDIASTOP,
    "xf86audioprev": _KEY_MEDIAPREVIOUS,
    "xf86audionext": _KEY_MEDIANEXT,
    "xf86audiolowervolume": _KEY_VOLUMEDOWN,
    "xf86audiomute": _KEY_VOLUMEMUTE,
    "xf86audioraisevolume": _KEY_VOLUMEUP,
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
    "kp_enter": _KEY_ENTER,
    "kp_home": _KEY_HOME,
    "kp_end": _KEY_END,
    "kp_left": _KEY_LEFT,
    "kp_up": _KEY_UP,
    "kp_right": _KEY_RIGHT,
    "kp_down": _KEY_DOWN,
    "kp_prior": _KEY_PAGEUP,
    "kp_page_up": _KEY_PAGEUP,
    "kp_next": _KEY_PAGEDOWN,
    "kp_page_down": _KEY_PAGEDOWN,
    "kp_insert": _KEY_INSERT,
    "kp_delete": _KEY_DELETE,
    "kp_begin": _KEY_CLEAR,
}


def _qt_keycode(hotkey_str: str) -> int | None:
    """Encode 'super+shift+y' as the QKeySequence int (modifiers | Qt::Key)
    KGlobalAccel.setShortcut takes. KWin reports Ctrl+Shift+1 as Ctrl+!, so
    Shift is dropped for a non-letter (pinned by
    `tests/test_wayland_input.py::test_qt_keycode_drops_the_shift_that_made_a_symbol`,
    `tests/test_wayland_input.py::test_qt_keycode_keeps_shift_on_a_letter`)."""
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
        return mods | (_KEY_F1 + int(key[1:]) - 1)
    if key in _QT_PUNCTUATION:
        char = _QT_PUNCTUATION[key]
    elif len(key) == 1:
        char = key
    else:
        return None
    if not char.isalpha():
        mods &= ~_QT_SHIFT
    return mods | ord(char.upper())


COMPONENT = "vinopop"
COMPONENT_NAME = "vinoPop"
# One fixed action id per role (pinned by
# `tests/test_wayland_input.py::test_action_ids_are_stable_per_role`).
ROLES = {
    "popup": ("vinopop-popup", "Show the popup"),
    "clipboard": ("vinopop-clipboard", "Clipboard history"),
    "ocr": ("vinopop-ocr", "Screen OCR"),
}
_ROLE_BY_CALLBACK = {
    "show_popup_now": "popup",
    "_on_popup_hotkey": "popup",
    "_on_clipboard_hotkey": "clipboard",
    "_on_ocr_hotkey": "ocr",
}
_LEGACY_ACTION = re.compile(r"^vinopop-action-\d+$")
_KGA_SET_PRESENT = 2
_KGA_NO_AUTOLOADING = 4


def role_for(on_trigger: Callable[[], None], role: str | None = None) -> str:
    """`role`, else guessed from the callback's name."""
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
    """Unregister the numbered ids (vinopop-action-N) once per run; returns
    the names removed (pinned by
    `tests/test_wayland_input.py::test_legacy_numbered_actions_are_cleaned_up_once`)."""
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
    """A global shortcut registered with KGlobalAccel over D-Bus. Failures
    are logged, never raised."""

    def __init__(
        self, hotkey_str: str, on_trigger: Callable[[], None], role: str | None = None
    ) -> None:
        self._hotkey_str = hotkey_str
        self._on_trigger = on_trigger
        self.role = role_for(on_trigger, role)
        self._action, self._friendly = action_id_for_role(self.role)
        self._component = COMPONENT
        self._registered = False
        self._match = None  # dbus SignalMatch

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
        # setShortcut (keys `ai`), never setShortcutKeys: KWin demarshals its
        # `a(ai)` QKeySequence list with an operator that aborts kwin_wayland,
        # and the whole session, on any framing mismatch from dbus-python.
        # Without SetPresent the component stays inactive on Plasma 6 and KWin
        # never grabs the key; NoAutoloading keeps our keys over stored config.
        flags = dbus.UInt32(_KGA_SET_PRESENT | _KGA_NO_AUTOLOADING)
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
        # No bus_name/path: KWin may emit from another sender or path than
        # getComponent reports. _on_pressed filters by action id.
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
        """globalShortcutPressed for every shortcut on the desktop; acts only
        on ours, found anywhere in the args (pinned by
        `tests/test_wayland_input.py::test_other_global_shortcuts_are_ignored`)."""
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
