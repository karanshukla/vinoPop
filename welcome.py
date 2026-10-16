"""First-run welcome dialog.

Shown exactly once, on the first time vinoPop starts. The goal is to:

  1. Explain in 10 seconds how to use the app (select text, get popup).
  2. Point at the tray icon so the user knows where to find settings.
  3. Quietly offer a way to support the project. The support row is a
     calm secondary CTA - no nag, no countdown, no modal-blocking guilt
     trip. Research is clear that nag screens kill goodwill and don't
     meaningfully lift conversion.

The dialog dismissal sets the same FIRST_RUN_MARKER that main.py was
using before, so the welcome flow can't trigger twice.
"""

from __future__ import annotations

import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

_SUPPORT_BUTTONS = [
    # (settings-key, icon, label, accent-class)
    # PayPal is the only donation channel exposed in the picker -
    # Ko-fi, Buy Me a Coffee and GitHub Sponsors were removed in
    # May 2026 because no real account existed behind any of them,
    # so the buttons led to 404s. (GitHub still shows its own
    # Sponsor button via .github/FUNDING.yml - that goes straight
    # to paypal.me/gaimsdev too.) To add another channel later,
    # add its key here AND the default URL in settings.py.
    ("support_paypal_url", "emblem-favorite-symbolic", "Support on PayPal", "suggested-action"),
]


def _dialog_window(title: str, parent: Gtk.Window | None, width: int) -> Adw.Window:
    """A small modal Adwaita window (header bar + content) standing in for
    the old Gtk.Dialog - Gtk.Dialog is deprecated in GTK 4.10."""
    dlg = Adw.Window(title=title, modal=True)
    if parent is not None:
        dlg.set_transient_for(parent)
    dlg.set_default_size(width, -1)
    dlg.set_icon_name("vinopop")
    return dlg


def _support_button(icon_name: str, label: str, accent: str, url: str) -> Gtk.Button:
    btn = Gtk.Button()
    btn.set_child(Adw.ButtonContent(icon_name=icon_name, label=label))
    btn.add_css_class("pill")
    if accent:
        btn.add_css_class(accent)
    btn.connect("clicked", lambda _b, u=url: _open_url(u))
    return btn


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
            "Top panel, right side -- find Settings, Plugin Manager, and your custom buttons.",
        ),
        (
            4,
            "Starter plugins are pre-installed",
            "Clipboard history, translate, word count, large type, transforms, and search shortcuts. "
            "Open Plugin Manager to add more (developer tools, code formatters, QR, etc.).",
        ),
    ]:
        row = Adw.ActionRow(title=head, subtitle=body)
        num = Gtk.Label(label=f"{n}.")
        num.add_css_class("accent")
        num.add_css_class("title-3")
        row.add_prefix(num)
        steps.append(row)
    outer.append(steps)

    # ---- support row (only if URLs are configured) ----
    support_urls = [
        (key, icon_name, label, accent)
        for key, icon_name, label, accent in _SUPPORT_BUTTONS
        if (settings.get(key) or "").strip()
    ]
    if support_urls:
        support_head = Gtk.Label(xalign=0, label="Like it?")
        support_head.add_css_class("heading")
        support_head.set_margin_top(8)
        outer.append(support_head)

        support_blurb = Gtk.Label(
            xalign=0,
            label="vinoPop is free and open source. If it saves you time, a "
            "small tip keeps the project moving. No pressure -- the link "
            "is also in the tray menu under About.",
        )
        support_blurb.add_css_class("dim-label")
        support_blurb.set_wrap(True)
        outer.append(support_blurb)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for key, icon_name, label, accent in support_urls:
            url = (settings.get(key) or "").strip()
            button_row.append(_support_button(icon_name, label, accent, url))
        outer.append(button_row)

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


def _open_url(url: str) -> None:
    try:
        subprocess.Popen(["xdg-open", url], start_new_session=True)
    except Exception:
        pass


# Reused by the tray "Support vinoPop..." entry and the About dialog.
def open_support_picker(settings, parent: Gtk.Window | None = None) -> Adw.Window:
    """Standalone dialog listing the configured support options. Same
    look as the welcome's support row, but reachable at any time."""
    urls = [(settings.get(key) or "").strip() for key, *_ in _SUPPORT_BUTTONS]
    configured = [(spec, url) for spec, url in zip(_SUPPORT_BUTTONS, urls, strict=True) if url]

    dlg = _dialog_window("Support vinoPop", parent, 440)

    box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=12,
        margin_top=8,
        margin_bottom=16,
        margin_start=24,
        margin_end=24,
    )

    if not configured:
        msg = Gtk.Label(xalign=0)
        msg.set_markup(
            "No donation links are configured. Set "
            "<tt>support_paypal_url</tt> in your settings.json to "
            "enable the PayPal button."
        )
        msg.add_css_class("dim-label")
        msg.set_wrap(True)
        box.append(msg)
    else:
        head = Gtk.Label(xalign=0, label="Thanks for considering it.")
        head.add_css_class("title-3")
        box.append(head)
        blurb = Gtk.Label(
            xalign=0, label="Tips are what let one-person Linux projects keep shipping."
        )
        blurb.add_css_class("dim-label")
        blurb.set_wrap(True)
        box.append(blurb)

        for (_key, icon_name, label, accent), url in configured:
            box.append(_support_button(icon_name, label, accent, url))

    close = Gtk.Button(label="Close")
    close.connect("clicked", lambda _b: dlg.close())
    bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, margin_top=6)
    bottom.append(Gtk.Box(hexpand=True))
    bottom.append(close)
    box.append(bottom)

    header = Adw.HeaderBar()
    header.add_css_class("flat")
    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(header)
    toolbar.set_content(box)
    dlg.set_content(toolbar)
    dlg.present()
    return dlg
