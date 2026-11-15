"""What the popup and the AI bubble share for keyboard navigation: neither
takes focus, so both grab their keys and move a highlight of their own."""

from __future__ import annotations

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk  # noqa: E402

SELECTED_CLASS = "vinopop-selected"


def highlight_only(widgets, index: int) -> None:
    """Mark widgets[index] as the one Enter would run, and no other."""
    for i, widget in enumerate(widgets):
        if i == index:
            widget.add_css_class(SELECTED_CLASS)
        else:
            widget.remove_css_class(SELECTED_CLASS)


def clear_highlight(widgets) -> None:
    for widget in widgets:
        widget.remove_css_class(SELECTED_CLASS)


def key_from_event(keyval: int, state) -> tuple[str, bool]:
    """(key name, shift held) of a GTK key press, in the form the key grab
    reports: Shift+Tab arrives from GTK as ISO_Left_Tab."""
    name = Gdk.keyval_name(keyval) or ""
    shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
    if name == "ISO_Left_Tab":
        return "Tab", True
    return name, shift
