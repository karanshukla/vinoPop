"""libxkbcommon over ctypes: the user's keyboard layout, keysym names, and
keysym -> evdev keycode lookups.

No GTK import, so the doctor and the tests can use it. libxkbcommon.so.0 is
a hard dependency of GTK 4's Wayland backend (and of KWin), so it is on every
desktop vinoPop runs on; callers still treat a missing library as "no
layout information" and fall back to US QWERTY keycodes.
"""

from __future__ import annotations

import ctypes as C
import os
import subprocess

_XKB_KEYSYM_CASE_INSENSITIVE = 1


def detect_kbd_rmlvo() -> tuple[str, str, str, str]:
    """(model, layout, variant, options) for the active keyboard.

    KDE keeps the live layout and options (Ctrl/Alt swap, caps remaps, ...)
    in kxkbrc and is authoritative on Plasma Wayland, so that wins over
    localectl, which only knows the system default and omits the options.
    Only the first layout of a multi-layout setup is returned: which one is
    active right now isn't visible from outside the compositor."""
    model, layout, variant, options = "pc105", "us", "", ""
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
            elif s.startswith("X11 Options:"):
                options = s.split(":", 1)[1].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        kx = _read_kxkbrc(os.path.expanduser("~/.config/kxkbrc"))
    except OSError:
        kx = {}
    if "Options" in kx:
        options = kx["Options"]
    # Use=false means Plasma leaves the layout to the system default.
    if kx.get("Use", "").lower() == "true" and kx.get("LayoutList"):
        layout = kx["LayoutList"]
        variant = kx.get("VariantList", "")
        model = kx.get("Model") or model
    return (model, layout.split(",")[0], variant.split(",")[0], options)


def _read_kxkbrc(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    in_layout = False
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("["):
                in_layout = line == "[Layout]"
            elif in_layout and "=" in line:
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
    return values


class _RuleNames(C.Structure):
    _fields_ = [
        ("rules", C.c_char_p),
        ("model", C.c_char_p),
        ("layout", C.c_char_p),
        ("variant", C.c_char_p),
        ("options", C.c_char_p),
    ]


_KEY_ITER = C.CFUNCTYPE(None, C.c_void_p, C.c_uint32, C.c_void_p)
_LIB: C.CDLL | None = None
_LIB_FAILED = False


def lib() -> C.CDLL | None:
    """libxkbcommon with the prototypes we use, or None if it can't load."""
    global _LIB, _LIB_FAILED
    if _LIB is not None or _LIB_FAILED:
        return _LIB
    try:
        x = C.CDLL("libxkbcommon.so.0")
    except OSError:
        _LIB_FAILED = True
        return None
    x.xkb_context_new.restype = C.c_void_p
    x.xkb_context_new.argtypes = [C.c_int]
    x.xkb_context_unref.argtypes = [C.c_void_p]
    x.xkb_keymap_new_from_names.restype = C.c_void_p
    x.xkb_keymap_new_from_names.argtypes = [C.c_void_p, C.POINTER(_RuleNames), C.c_int]
    x.xkb_keymap_unref.argtypes = [C.c_void_p]
    x.xkb_keymap_key_for_each.argtypes = [C.c_void_p, _KEY_ITER, C.c_void_p]
    x.xkb_keymap_num_levels_for_key.restype = C.c_uint32
    x.xkb_keymap_num_levels_for_key.argtypes = [C.c_void_p, C.c_uint32, C.c_uint32]
    x.xkb_keymap_key_get_syms_by_level.restype = C.c_int
    x.xkb_keymap_key_get_syms_by_level.argtypes = [
        C.c_void_p,
        C.c_uint32,
        C.c_uint32,
        C.c_uint32,
        C.POINTER(C.POINTER(C.c_uint32)),
    ]
    x.xkb_keysym_from_name.restype = C.c_uint32
    x.xkb_keysym_from_name.argtypes = [C.c_char_p, C.c_int]
    x.xkb_utf32_to_keysym.restype = C.c_uint32
    x.xkb_utf32_to_keysym.argtypes = [C.c_uint32]
    _LIB = x
    return x


# Enough to send every chord the app uses when libxkbcommon is missing.
_FALLBACK_KEYSYMS = {
    "return": 0xFF0D,
    "enter": 0xFF0D,
    "kp_enter": 0xFF8D,
    "backspace": 0xFF08,
    "tab": 0xFF09,
    "escape": 0xFF1B,
    "esc": 0xFF1B,
    "space": 0x20,
    "delete": 0xFFFF,
    "home": 0xFF50,
    "left": 0xFF51,
    "up": 0xFF52,
    "right": 0xFF53,
    "down": 0xFF54,
    "prior": 0xFF55,
    "next": 0xFF56,
    "end": 0xFF57,
    "minus": 0x2D,
    "equal": 0x3D,
}

# xdotool modifier spellings -> the left-hand modifier keysym
MODIFIER_KEYSYMS = {
    "ctrl": 0xFFE3,  # Control_L
    "control": 0xFFE3,
    "shift": 0xFFE1,  # Shift_L
    "alt": 0xFFE9,  # Alt_L
    "super": 0xFFEB,  # Super_L
    "win": 0xFFEB,
    "meta": 0xFFEB,
}


def keysym_from_name(name: str) -> int:
    """Keysym for an xdotool/X key name ('BackSpace', 'v', 'Return'), or 0."""
    if not name:
        return 0
    if len(name) == 1 and name.isascii() and name.isprintable():
        # Latin-1 keysyms equal the code point; 'V' -> 'v' so a chord never
        # asks for an implicit Shift.
        return ord(name.lower())
    x = lib()
    if x is not None:
        ks = x.xkb_keysym_from_name(name.encode(), 0)
        if not ks:
            ks = x.xkb_keysym_from_name(name.encode(), _XKB_KEYSYM_CASE_INSENSITIVE)
        if ks:
            return int(ks)
    return _FALLBACK_KEYSYMS.get(name.lower(), 0)


def keysym_for_char(ch: str) -> int:
    """Keysym that types one character."""
    cp = ord(ch)
    if ch == "\n":
        return 0xFF0D
    if ch == "\t":
        return 0xFF09
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp
    x = lib()
    if x is not None:
        ks = int(x.xkb_utf32_to_keysym(cp))
        if ks:
            return ks
    return 0x01000000 | cp  # the Unicode keysym range


class Keymap:
    """keysym -> (evdev keycode, needs Shift) for one xkb layout.

    ydotool writes raw evdev keycodes, which the compositor then interprets
    through the user's layout; to get 'a' on AZERTY we have to send the
    physical Q key. This walks the compiled keymap once and records, for
    every keysym on level 1 (plain) or level 2 (Shift) of the first layout
    group, the lowest keycode that produces it."""

    def __init__(self, model: str, layout: str, variant: str = "", options: str = "") -> None:
        self.layout = layout or "us"
        self._map: dict[int, tuple[int, bool]] = {}
        x = lib()
        if x is None:
            raise OSError("libxkbcommon.so.0 not loadable")
        ctx = x.xkb_context_new(0)
        if not ctx:
            raise OSError("xkb_context_new failed")
        try:
            names = _RuleNames(
                b"evdev",
                (model or "pc105").encode(),
                self.layout.encode(),
                variant.encode() if variant else None,
                options.encode() if options else None,
            )
            km = x.xkb_keymap_new_from_names(ctx, C.byref(names), 0)
            if not km and options:
                names.options = None
                km = x.xkb_keymap_new_from_names(ctx, C.byref(names), 0)
            if not km:
                raise OSError(f"no xkb keymap for layout {self.layout!r}")
            try:
                self._walk(x, km)
            finally:
                x.xkb_keymap_unref(km)
        finally:
            x.xkb_context_unref(ctx)

    def _walk(self, x: C.CDLL, km: int) -> None:
        found: dict[int, tuple[int, bool]] = {}

        def visit(_km, key, _data):
            levels = min(2, int(x.xkb_keymap_num_levels_for_key(km, key, 0)))
            for level in range(levels):
                syms = C.POINTER(C.c_uint32)()
                n = x.xkb_keymap_key_get_syms_by_level(km, key, 0, level, C.byref(syms))
                for i in range(n):
                    sym = int(syms[i])
                    prev = found.get(sym)
                    # plain beats shifted; then the lower keycode wins
                    if prev is None or (prev[1] and level == 0):
                        found[sym] = (int(key) - 8, level == 1)

        cb = _KEY_ITER(visit)
        x.xkb_keymap_key_for_each(km, cb, None)
        self._map = found

    def keycode_for(self, keysym: int) -> tuple[int, bool] | None:
        """(evdev keycode, needs Shift) that produces `keysym`, or None."""
        return self._map.get(keysym)


_KEYMAP_CACHE: dict[tuple[str, str, str, str], Keymap | None] = {}


def keymap_for(rmlvo: tuple[str, str, str, str]) -> Keymap | None:
    """Cached Keymap for an RMLVO tuple; None when xkb can't build it."""
    if rmlvo not in _KEYMAP_CACHE:
        try:
            _KEYMAP_CACHE[rmlvo] = Keymap(*rmlvo)
        except OSError as exc:
            print(f"[xkb] no keymap for {rmlvo}: {exc}")
            _KEYMAP_CACHE[rmlvo] = None
    return _KEYMAP_CACHE[rmlvo]
