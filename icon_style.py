"""Colour vs glyph icon style.

Every concept ships two icons: a colour tile ``vinopop-<concept>`` and a
mono glyph ``vinopop-<concept>-symbolic`` (the same mark, GTK-recolourable).
The user picks between them in Settings via the ``icon_style`` setting:

  * ``color`` (default) - vibrant gradient tiles for branded/destination
    actions; plain text-edit actions (cut, bold, ...) stay as their system
    symbolic glyphs.
  * ``glyph`` - every concept renders as its mono glyph, matching the system
    symbolic icons so the whole popup is one uniform style.

`resolve()` maps whatever icon name a plugin declares to the right variant.
Unmapped names (edit-cut-symbolic etc.) pass through untouched.
"""

from __future__ import annotations

# plugin-declared icon name -> concept that has colour + mono variants
_CONCEPT_BY_ICON = {
    "vinopop-youtube": "youtube",
    "vinopop-wikipedia": "wikipedia",
    "mark-location-symbolic": "maps",
    "applications-development-symbolic": "github",
    "help-faq-symbolic": "stackoverflow",
    "system-search-symbolic": "google",
    "preferences-desktop-locale-symbolic": "translate",
    "accessories-dictionary-symbolic": "dictionary",
    "package-x-generic-symbolic": "package",
    "vinopop-calculator-symbolic": "calculator",
    "vinopop-clipboard-symbolic": "clipboard",
    "vinopop-json-symbolic": "json",
    "vinopop-qr-symbolic": "qr",
    "vinopop-base64-encode-symbolic": "base64",
    "vinopop-case-upper-symbolic": "case",
    "vinopop-url-encode-symbolic": "url",
    "vinopop-wordcount-symbolic": "wordcount",
}

# Concept ordering for the Settings preview (most recognisable first).
PREVIEW_CONCEPTS = [
    "google",
    "youtube",
    "wikipedia",
    "maps",
    "translate",
    "github",
    "package",
    "clipboard",
    "qr",
    "json",
]


# System icons Breeze and/or Adwaita don't ship -> the closest names both do.
# A missing icon renders as a blank/broken button, so `available()` swaps
# these in when the active theme lacks the declared name.
_FALLBACKS = {
    "web-browser-symbolic": ("network-workgroup-symbolic", "insert-link-symbolic"),
    "applications-internet-symbolic": ("network-workgroup-symbolic",),
    "utilities-terminal-symbolic": ("system-run-symbolic", "application-x-executable-symbolic"),
    "accessories-text-editor-symbolic": ("document-edit-symbolic",),
    "accessories-calculator-symbolic": ("insert-object-symbolic",),
    "applications-development-symbolic": ("emblem-system-symbolic",),
    "applications-engineering-symbolic": ("emblem-system-symbolic",),
    "applications-graphics-symbolic": ("color-select-symbolic",),
    "applications-utilities-symbolic": ("preferences-other-symbolic",),
    "help-faq-symbolic": ("help-about-symbolic", "dialog-question-symbolic"),
    "image-x-generic-symbolic": ("folder-pictures-symbolic", "camera-photo-symbolic"),
    "package-x-generic-symbolic": ("folder-symbolic",),
    "preferences-desktop-keyboard-shortcuts-symbolic": ("input-keyboard-symbolic",),
    "preferences-desktop-locale-symbolic": ("object-flip-horizontal-symbolic",),
    "preferences-system-time-symbolic": ("alarm-symbolic",),
    "system-software-install-symbolic": ("list-add-symbolic",),
    "text-html-symbolic": ("text-x-generic-symbolic",),
}


def _theme():
    """The GTK 4 icon theme, or None when GTK isn't loaded / has no display."""
    import sys

    if "gi.repository.Gtk" not in sys.modules:
        return None
    from gi.repository import Gdk, Gtk

    display = Gdk.Display.get_default()
    return Gtk.IconTheme.get_for_display(display) if display is not None else None


def available(icon_name: str, theme=None) -> str:
    """`icon_name`, or the first fallback the icon theme actually ships.

    Bundled ``vinopop-*`` icons and unknown names pass through untouched;
    with no usable theme (tests, --doctor) the name is returned as-is.
    """
    if not icon_name or icon_name.startswith("vinopop-"):
        return icon_name
    alternatives = _FALLBACKS.get(icon_name)
    if not alternatives:
        return icon_name
    if theme is None:
        theme = _theme()
    if theme is None or theme.has_icon(icon_name):
        return icon_name
    for alt in alternatives:
        if theme.has_icon(alt):
            return alt
    return icon_name


def current_style() -> str:
    try:
        from settings import get_settings

        s = (get_settings().get("icon_style") or "color").strip().lower()
    except Exception:
        s = "color"
    return s if s in ("color", "glyph") else "color"


def resolve(icon_name: str, style: str | None = None) -> str:
    """Return the icon name to actually load for the current icon_style."""
    if not icon_name:
        return icon_name
    concept = _CONCEPT_BY_ICON.get(icon_name)
    if concept is None:
        # Already a concept colour name? (e.g. a future plugin sets
        # icon="vinopop-maps" directly.)
        bare = icon_name.removeprefix("vinopop-")
        if bare != icon_name and bare in set(_CONCEPT_BY_ICON.values()):
            concept = bare
        else:
            return available(icon_name)
    if style is None:
        style = current_style()
    if style == "glyph":
        return f"vinopop-{concept}-symbolic"
    return f"vinopop-{concept}"
