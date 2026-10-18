"""Theme for vinoPop on GTK 4 + libadwaita -- one CSS provider for every window.

libadwaita's own stylesheet is the base, so windows look native on GNOME and
follow its light/dark handling everywhere else. install_premium_theme() maps
the user's "dark" / "light" / "system" choice onto Adw.StyleManager's colour
scheme (FORCE_DARK / FORCE_LIGHT / DEFAULT) and layers a small brand layer on
top: the accent colour (cobalt blue -> royal violet -> magenta pink, from the
app icon), the gradient on suggested-action buttons, and vinoPop's own helper
classes (.lp-*). Everything else - header bars, rows, entries, switches,
popovers - is left to Adwaita.

The floating popup bar is styled by popup.py. It keeps its fixed dark brand
palette and uses _LIGHT_REMAP / _apply_remap() below to swap it to a light
variant when the resolved mode is light.

The installer is idempotent and hot-reloadable: calling it again with a
different mode swaps the colour scheme without restarting.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

# Brand layer. Uses libadwaita's named colours (@window_bg_color, @card_bg_color,
# @accent_bg_color, ...) so one stylesheet serves light and dark.
_BRAND_CSS = """
/* ============================================================
   vinoPop brand layer over libadwaita
     accent-1   #5B7DF5  (cobalt blue)
     accent-2   #7C3AED  (royal violet)
     accent-3   #EC4899  (magenta pink)
   ============================================================ */

@define-color accent_bg_color #5B7DF5;
@define-color accent_fg_color #ffffff;
/* Accent used as text/foreground: a shade darker in light mode so it keeps
   AA contrast on white (overridden for dark below). */
@define-color accent_color #3f5fd8;

/* Suggested action: the headline blue->violet gradient */
button.suggested-action,
button.suggested-action.pill {
    background-image: linear-gradient(to bottom right, #5B7DF5, #7C3AED);
    color: #ffffff;
    box-shadow: 0 2px 6px rgba(91, 125, 245, 0.25);
}

button.suggested-action:hover {
    background-image: linear-gradient(to bottom right, #6B8AF7, #8B4CF0);
    box-shadow: 0 4px 12px rgba(91, 125, 245, 0.35);
}

button.suggested-action:active {
    background-image: linear-gradient(to bottom right, #4A6CE3, #6929DB);
}

button.suggested-action:disabled {
    background-image: none;
    box-shadow: none;
}

progressbar progress {
    background-image: linear-gradient(to right, #5B7DF5, #7C3AED);
}

/* ----- colored plugin badges (the onboarding-store look) ----- */
.lp-badge {
    border-radius: 50%;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.22),
                0 1px 0 rgba(255, 255, 255, 0.20) inset;
}
.lp-badge-0 { background-image: linear-gradient(135deg, #5B7DF5, #4A6CE3); }
.lp-badge-1 { background-image: linear-gradient(135deg, #7C3AED, #6929DB); }
.lp-badge-2 { background-image: linear-gradient(135deg, #EC4899, #DB2777); }
.lp-badge-3 { background-image: linear-gradient(135deg, #34C759, #16A34A); }
.lp-badge-plain {
    background-color: #ffffff;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.18),
                0 0 0 1px rgba(0, 0, 0, 0.06) inset;
}
.lp-badge-glyph { color: #ffffff; }
.lp-badge-letter {
    color: #ffffff;
    font-weight: bold;
    font-size: 15px;
    text-shadow: 0 1px 1px rgba(0, 0, 0, 0.28);
}

/* Terminal confirm dialog: read-only "preview" state. Flat, no box,
   sits on the dialog surface the way a label would. */
textview.lp-cmd-preview,
textview.lp-cmd-preview text {
    background-color: transparent;
    padding: 4px 6px;
}

/* Terminal confirm dialog: editable state after Edit was pressed.
   Picks up an entry-field look so it's obvious you can type now. */
textview.lp-cmd-edit,
textview.lp-cmd-edit text {
    background-color: @view_bg_color;
    padding: 6px 10px;
}
textview.lp-cmd-edit {
    border-radius: 8px;
    box-shadow: inset 0 0 0 1px alpha(currentColor, 0.15);
}

/* ----- Snippet / clipboard picker -----
   The list area inherits the window surface; the selected row keeps the
   brand tint so it reads on either palette. */
.lp-picker list,
.lp-picker list > row {
    background-color: transparent;
}

.lp-picker list > row:hover {
    background-color: alpha(@accent_bg_color, 0.10);
}

.lp-picker list > row:selected {
    background-image: linear-gradient(to right,
        alpha(#5B7DF5, 0.22),
        alpha(#7C3AED, 0.22));
}

/* ----- vinoPop-specific helper classes -----
   Any widget that adds these style classes via add_css_class()
   gets brand accents on top of the Adwaita widget styling. */
.lp-accent {
    color: @accent_color;
}

.lp-card {
    background-color: @card_bg_color;
    box-shadow: 0 0 0 1px @card_shade_color, 0 2px 6px @card_shade_color;
    border-radius: 12px;
    padding: 12px;
}

.lp-hero {
    background-image: linear-gradient(135deg, #5B7DF5 0%, #7C3AED 55%, #EC4899 100%);
    color: #ffffff;
    border-radius: 12px;
    padding: 14px 18px;
}

.lp-muted {
    opacity: 0.7;
}

.lp-title {
    font-size: 1.15em;
    font-weight: 700;
    letter-spacing: -0.005em;
}
"""

# Dark-mode overrides appended after the brand layer.
_BRAND_DARK_CSS = """
@define-color accent_color #8ea6ff;
"""


# Dark -> light hex remap. Keeps the accent gradient (cobalt blue ->
# royal violet -> magenta pink) and the destructive reds untouched -
# those read well on either background. Only swaps the surfaces, text,
# borders, and the muted button/sidebar tints.
_LIGHT_REMAP = {
    # core palette
    "#0e1118": "#f6f7fb",  # window bg
    "#1c2231": "#ffffff",  # surface (cards / list rows)
    "#262d3f": "#eef0f6",  # elevated (hover / selected)
    "#3a4258": "#d4d8e2",  # border
    "#f0f3fa": "#1c2231",  # primary text
    "#b8c0d4": "#5a6378",  # muted / dim labels
    # secondary tints (buttons, sidebar, inputs)
    "#181d2a": "#ffffff",  # entry / textview body
    "#1a1f2c": "#e8ecf3",  # button gradient bottom
    "#1f2433": "#f4f6fa",  # button gradient top
    "#262c3e": "#dde1ea",  # button hover top
    "#232a3c": "#eef0f6",  # header gradient top
    "#1a1f2e": "#f4f6fa",  # header gradient bottom
    "#2c3346": "#e0e4ee",  # subtle border / row bottom
    "#161a24": "#f0f2f7",  # sidebar bg
    "#141823": "#e0e4ee",  # notebook header
    "#14171f": "#ebeef4",  # check/radio bg
    "#3d4456": "#d4d8e2",  # switch track (off) - light grey in light mode
    "#4a5266": "#a8b0c0",  # disabled text
    "#5a6378": "#7a8090",  # placeholder text
    # Pango-markup greys that several dialogs hard-code for dim/muted
    # secondary text. Inverted into a darker grey for light mode so the
    # contrast ratio stays readable (the original tones were tuned
    # against a dark surface).
    "#8a92a8": "#5a6378",
    "#9aa3b8": "#5a6378",
    "#9ba3b8": "#3a4258",
    "#c4cad8": "#5a6378",  # popup overflow chip
    # White-tinted highlights used for the "inset top edge" lift on
    # cards and the header bar. Invisible on a light surface (white on
    # white) - swap to a subtle dark shadow so the dimension survives.
    "rgba(255, 255, 255, 0.06)": "rgba(0, 0, 0, 0.06)",
}


def _apply_remap(css: bytes, remap: dict[str, str]) -> bytes:
    """Substitute one hex palette into the CSS in a single pass.
    Sequential .replace() would let later replacements re-hit earlier
    outputs (e.g. swapping #1c2231 to #ffffff, then #5a6378 to #7a8090
    would corrupt the first output if it shared a substring). Build a
    regex and replace by full match instead."""
    import re

    if not remap:
        return css
    text = css.decode("ascii")
    # Order keys longest-first so #abcdef wins over #abcde when both
    # appear in the map. Wrap each hex in word-boundary so partial
    # hexes inside another hex never match.
    keys = sorted(remap.keys(), key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(k) for k in keys))
    out = pattern.sub(lambda m: remap[m.group(0)], text)
    return out.encode("ascii")


def _system_prefers_dark() -> bool:
    """Best-effort: does the user's system prefer dark? With the colour
    scheme at DEFAULT, Adw.StyleManager.get_dark() follows the desktop's
    preference (Settings portal on GNOME/KDE). Defaults to True when we have
    nothing to go on - keeps the long-standing dark look."""
    try:
        # Only asked for "system", i.e. while the scheme is DEFAULT - then
        # get_dark() is the desktop's own preference.
        return bool(Adw.StyleManager.get_default().get_dark())
    except Exception:
        return True


def _resolve_mode(mode: str) -> str:
    if mode == "system":
        return "dark" if _system_prefers_dark() else "light"
    if mode in ("dark", "light"):
        return mode
    return "dark"


_active_provider: Gtk.CssProvider | None = None
_active_mode: str | None = None
_requested_mode: str | None = None
_dark_handler: int | None = None

_SCHEMES = {
    "dark": Adw.ColorScheme.FORCE_DARK,
    "light": Adw.ColorScheme.FORCE_LIGHT,
    "system": Adw.ColorScheme.DEFAULT,
}


def _load_css(provider: Gtk.CssProvider, css: str) -> None:
    if hasattr(provider, "load_from_string"):  # GTK 4.12+
        provider.load_from_string(css)
    else:
        provider.load_from_data(css.encode("utf-8"), -1)


def _on_system_dark_changed(sm, _pspec) -> None:
    """The desktop flipped light/dark while we follow it ("system"): swap the
    brand layer and rebuild the popup bar's palette to match."""
    if _requested_mode != "system":
        return
    _apply_brand("dark" if sm.get_dark() else "light")
    import sys

    popup = sys.modules.get("popup")
    if popup is not None:
        try:
            popup.reinstall_popup_css()
        except Exception:
            pass


def _apply_brand(effective: str) -> None:
    global _active_provider, _active_mode
    display = Gdk.Display.get_default()
    if display is None:
        return
    if _active_mode == effective and _active_provider is not None:
        return
    css = _BRAND_CSS + (_BRAND_DARK_CSS if effective == "dark" else "")
    provider = Gtk.CssProvider()
    try:
        _load_css(provider, css)
    except Exception:
        import logging

        logging.getLogger("vinopop").exception("brand theme (%s) failed to load", effective)
        return
    if _active_provider is not None:
        try:
            Gtk.StyleContext.remove_provider_for_display(display, _active_provider)
        except Exception:
            pass
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _active_provider = provider
    _active_mode = effective


def install_premium_theme(mode: str = "dark") -> None:
    """Apply the colour scheme and the brand layer on the default display.

    `mode` is "dark", "light", or "system". Calling again with the same
    mode is a no-op; calling with a different one switches the
    Adw.StyleManager scheme and swaps the brand provider - so the
    settings-side toggle can just call this without restarting the daemon.
    """
    global _requested_mode, _dark_handler

    import gtk4_app

    gtk4_app.init()
    if Gdk.Display.get_default() is None:
        return
    mode = mode if mode in _SCHEMES else "dark"
    sm = Adw.StyleManager.get_default()
    sm.set_color_scheme(_SCHEMES[mode])
    _requested_mode = mode
    if _dark_handler is None:
        _dark_handler = sm.connect("notify::dark", _on_system_dark_changed)

    # On KDE/Wayland the app's button/action icons are GNOME/Adwaita symbolic
    # names; Breeze names many differently, so they'd render as
    # "image-missing". Force the Adwaita icon theme so they resolve (dark
    # mode only, as before). libadwaita ignores gtk-theme-name and owns
    # light/dark itself, so neither is touched any more.
    if mode == "dark":
        try:
            from platform_backend import get_backend

            if get_backend().name == "wayland_kde":
                s = Gtk.Settings.get_default()
                if s is not None:
                    s.set_property("gtk-icon-theme-name", "Adwaita")
        except Exception:
            pass

    _apply_brand("dark" if mode == "dark" else "light" if mode == "light" else _resolve_mode(mode))
