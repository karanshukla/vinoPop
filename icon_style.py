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
        if icon_name.startswith("vinopop-") and icon_name[9:] in set(_CONCEPT_BY_ICON.values()):
            concept = icon_name[9:]
        else:
            return icon_name
    if style is None:
        style = current_style()
    if style == "glyph":
        return f"vinopop-{concept}-symbolic"
    return f"vinopop-{concept}"
