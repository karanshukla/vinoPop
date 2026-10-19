"""First-run welcome dialog.

Shown exactly once, on the first time vinoPop starts. The goal is to:

  1. Explain in 10 seconds how to use the app (select text, get popup).
  2. Point at the tray icon so the user knows where to find settings.

The dialog dismissal sets the same FIRST_RUN_MARKER that main.py was
using before, so the welcome flow can't trigger twice.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402


def _dialog_window(title: str, parent: Gtk.Window | None, width: int) -> Adw.Window:
    """A small modal Adwaita window (header bar + content) standing in for
    the old Gtk.Dialog - Gtk.Dialog is deprecated in GTK 4.10."""
    dlg = Adw.Window(title=title, modal=True)
    if parent is not None:
        dlg.set_transient_for(parent)
    dlg.set_default_size(width, -1)
    dlg.set_icon_name("vinopop")
    return dlg


def show_welcome_dialog(
    settings,
    on_open_plugins=None,
    parent: Gtk.Window | None = None,
) -> Adw.Window | None:
    """Render the welcome dialog. Non-modal so the user can still
    interact with the popup if they trigger it accidentally during the
    first run. Returns the window (or None when disabled)."""
    if not bool(settings.get("show_welcome_dialog")):
        return None

    dlg = _dialog_window("Welcome to vinoPop", parent, 560)
    dlg.set_default_size(560, 640)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)

    # ---- header: Adwaita hero page with the app icon ----
    # StatusPage gives the big icon + title + description treatment; the
    # rest of the dialog lives in its child.
    hero = Adw.StatusPage(
        icon_name="vinopop",
        title="Welcome to vinoPop",
        description="A floating popup of context-aware actions for any text you select.",
    )
    hero.add_css_class("compact")

    # ---- 4-step usage explainer ----
    steps_lbl = Gtk.Label(xalign=0, label="How to use it")
    steps_lbl.add_css_class("heading")
    outer.append(steps_lbl)

    steps = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    steps.add_css_class("boxed-list")
    for n, head, body in [
        (
            1,
            "Highlight text",
            "Select anything in any app -- a URL, an error message, a paragraph.",
        ),
        (
            2,
            "Click the popup button",
            "vinoPop shows a small floating bar of actions tailored to what you selected.",
        ),
        (
            3,
            "Open the tray icon for more",
            "It sits in your panel's system tray -- find Settings, Plugin Manager, "
            "and your custom buttons there.",
        ),
        (
            4,
            "Starter plugins are pre-installed",
            "Clipboard history, translate, word count, transforms, offline AI actions, and "
            "search shortcuts. Open Plugin Manager to add more (developer tools, code "
            "formatters, QR, large type, etc.).",
        ),
    ]:
        row = Adw.ActionRow(title=head, subtitle=body)
        num = Gtk.Label(label=f"{n}.")
        num.add_css_class("accent")
        num.add_css_class("title-3")
        row.add_prefix(num)
        steps.append(row)
    outer.append(steps)

    # ---- bottom action row ----
    bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_top=10)
    if on_open_plugins is not None:
        plug_btn = Gtk.Button(label="Open Plugin Manager...")
        plug_btn.add_css_class("pill")
        plug_btn.connect("clicked", lambda _b: (dlg.close(), on_open_plugins()))
        bottom.append(plug_btn)

    spacer = Gtk.Box(hexpand=True)
    bottom.append(spacer)

    close_btn = Gtk.Button(label="Got it -- let's go")
    close_btn.add_css_class("suggested-action")
    close_btn.add_css_class("pill")
    close_btn.connect("clicked", lambda _b: dlg.close())
    bottom.append(close_btn)
    outer.append(bottom)

    hero.set_child(outer)

    header = Adw.HeaderBar()
    header.add_css_class("flat")
    header.set_show_title(False)
    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(header)
    toolbar.set_content(hero)
    dlg.set_content(toolbar)
    dlg.present()
    return dlg
