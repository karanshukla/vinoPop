"""Searchable icon-picker dialog backed by the current GTK icon theme.

Lists every icon name the running theme exposes (usually 1500-3000), with
a live search box and a grid of preview thumbnails. Click → returns the
chosen icon name; Esc → returns None.

Usage:
    chosen = IconPicker(parent=some_window, initial="folder-symbolic").run()
"""

from __future__ import annotations

import shutil
import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from xdg_paths import HICOLOR_APPS, USER_ICONS_DIR  # noqa: E402

# How many icons to render per "page". GTK FlowBox is fast enough that
# 1000+ per page is fine on modern hardware; lazy loading was solving a
# problem we didn't really have.
_PAGE_SIZE = 1500
_ICON_RENDER_SIZE = 24  # pixels in the preview grid

# When a search has zero matches, offer these generic icons. Brand-specific
# icons aren't in standard themes (Adwaita, hicolor) for trademark reasons.
_FALLBACK_SUGGESTIONS = [
    "applications-internet",
    "web-browser-symbolic",
    "system-search-symbolic",
    "applications-utilities-symbolic",
    "help-about-symbolic",
    "starred-symbolic",
    "view-list-symbolic",
    "applications-other",
]


def _suggestions(theme) -> list[str]:
    """_FALLBACK_SUGGESTIONS the theme can draw, with names it lacks
    swapped for their icon_style fallback (Breeze has no
    web-browser-symbolic) and anything still missing left out."""
    import icon_style

    out: list[str] = []
    for name in _FALLBACK_SUGGESTIONS:
        name = icon_style.available(name, theme)
        if name not in out and (theme is None or theme.has_icon(name)):
            out.append(name)
    return out


def _icon_theme() -> Gtk.IconTheme | None:
    display = Gdk.Display.get_default()
    if display is None:
        return None
    return Gtk.IconTheme.get_for_display(display)


def _sync_user_icons() -> None:
    """Make sure user-supplied icons in ~/.config/vinopop/icons/ are
    visible to the GTK icon theme. Mirror them into hicolor so they show
    up by basename in the picker and in plugin button references.
    """
    USER_ICONS_DIR.mkdir(parents=True, exist_ok=True)
    HICOLOR_APPS.mkdir(parents=True, exist_ok=True)
    copied = False
    for src in list(USER_ICONS_DIR.glob("*.svg")) + list(USER_ICONS_DIR.glob("*.png")):
        dst = HICOLOR_APPS / src.name
        if not dst.is_file() or src.stat().st_mtime > dst.stat().st_mtime:
            try:
                shutil.copy2(src, dst)
                copied = True
            except OSError as exc:
                print(f"[icon_picker] could not mirror {src.name}: {exc}")
    if copied:
        # Force GTK to rescan so the new icons appear without a restart.
        # GTK 4 has no rescan_if_needed(); re-setting the search path
        # invalidates the theme and makes it reload its directories.
        try:
            theme = _icon_theme()
            if theme is not None:
                theme.set_search_path(theme.get_search_path())
        except Exception:
            pass


def _list_all_icons() -> list[str]:
    _sync_user_icons()
    theme = _icon_theme()
    if theme is None:
        return []
    try:
        names = list(theme.get_icon_names())
    except Exception:
        return []
    # Stable order, deduplicated
    return sorted(set(names))


class IconPicker:
    def __init__(self, parent=None, initial: str = "") -> None:
        self._parent = parent
        self._initial = initial
        self._all_icons: list[str] = []
        self._filtered: list[str] = []
        self._rendered: int = 0
        self._chosen: str | None = None
        self._symbolic_only = True
        self._loop: GLib.MainLoop | None = None

    # ---- public ----------------------------------------------------------

    def run(self) -> str | None:
        """Show the picker modally and block (nested main loop) until the
        user picks an icon or cancels. Returns the icon name or None."""
        self._all_icons = _list_all_icons()

        dlg = Adw.Window(title="Pick an icon", modal=True)
        if isinstance(self._parent, Gtk.Window):
            dlg.set_transient_for(self._parent)
        dlg.set_default_size(640, 520)
        dlg.set_icon_name("vinopop")
        self._dialog = dlg

        # Header: Cancel on the left, the search entry as the title widget
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: dlg.close())
        header.pack_start(cancel)

        self._search = Gtk.SearchEntry()
        self._search.set_placeholder_text("Search icons… (e.g. folder, mail, save)")
        self._search.set_hexpand(True)
        self._search.set_max_width_chars(40)
        self._search.connect("search-changed", self._on_search)
        # Esc inside the search box cancels the whole picker
        self._search.connect("stop-search", lambda _e: dlg.close())
        header.set_title_widget(self._search)

        self._symbolic_toggle = Gtk.CheckButton(label="Symbolic only")
        self._symbolic_toggle.set_active(True)
        self._symbolic_toggle.set_tooltip_text(
            "Show only monochrome icons (the ones that tint with your theme)."
        )
        self._symbolic_toggle.connect("toggled", self._on_symbolic_toggle)
        header.pack_end(self._symbolic_toggle)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(6)
        outer.set_margin_bottom(10)
        outer.set_margin_start(10)
        outer.set_margin_end(10)

        # Result count line - supports clickable links in empty-state
        self._count_label = Gtk.Label(xalign=0)
        self._count_label.set_wrap(True)
        self._count_label.connect("activate-link", self._on_link_clicked)
        self._count_label.add_css_class("dim-label")
        outer.append(self._count_label)

        # Scrollable grid
        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroll.set_hexpand(True)
        self._scroll.set_vexpand(True)
        self._scroll.get_vadjustment().connect("value-changed", self._on_scroll)

        self._flowbox = Gtk.FlowBox()
        # valign=START keeps rows packed at the top of the scroller instead
        # of GTK spreading a short result list over the whole height.
        self._flowbox.set_valign(Gtk.Align.START)
        self._flowbox.set_halign(Gtk.Align.FILL)
        self._flowbox.set_hexpand(True)
        # A generous max (the default is 7) lets GTK lay out as many
        # children per line as the current width allows.
        self._flowbox.set_max_children_per_line(64)
        self._flowbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._flowbox.set_row_spacing(2)
        self._flowbox.set_column_spacing(2)
        self._scroll.set_child(self._flowbox)
        outer.append(self._scroll)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(outer)
        dlg.set_content(toolbar)

        # Esc anywhere in the dialog → cancel (returns None)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        dlg.add_controller(keys)

        self._loop = GLib.MainLoop()
        dlg.connect("close-request", self._on_close_request)

        # Initial population
        self._apply_filter()

        dlg.present()
        self._search.grab_focus()
        self._loop.run()
        self._loop = None
        return self._chosen

    # ---- filtering -------------------------------------------------------

    def _apply_filter(self) -> bool:
        query = self._search.get_text().strip().lower()
        symbolic_only = self._symbolic_toggle.get_active()

        if symbolic_only:
            pool = [n for n in self._all_icons if n.endswith("-symbolic")]
        else:
            pool = list(self._all_icons)

        if query:
            # Substring match across icon names
            self._filtered = [n for n in pool if query in n.lower()]
        else:
            self._filtered = pool

        # Wipe current grid
        self._flowbox.remove_all()
        self._rendered = 0

        if not self._filtered and query:
            # Zero matches - show suggestions + hint about Symbolic toggle + user icons
            esc_query = GLib.markup_escape_text(query)
            symbolic_hint = (
                " <b>Tip:</b> if you added a colored brand icon, "
                "uncheck <i>Symbolic only</i> above to see it."
                if symbolic_only
                else ""
            )
            self._count_label.set_markup(
                f"<i>No icons match “{esc_query}”. Standard themes don't ship "
                f"brand-specific icons (Wikipedia, GitHub etc.).</i>\n"
                f"<small>Drop your own <tt>.svg</tt> or <tt>.png</tt> files in "
                f'<a href="vinopop://open-user-icons">'
                f"~/.config/vinopop/icons/</a>.{symbolic_hint} "
                f"Or pick a generic one:</small>"
            )
            self._filtered = _suggestions(_icon_theme())
        else:
            self._count_label.set_markup(
                f"{len(self._filtered)} matching icons "
                f"({len(self._all_icons)} total in your theme) - "
                f'<a href="vinopop://open-user-icons">add your own</a>'
            )

        self._render_next_page()
        return False  # one-shot when scheduled via idle_add

    def _render_next_page(self) -> None:
        end = min(self._rendered + _PAGE_SIZE, len(self._filtered))
        for name in self._filtered[self._rendered : end]:
            child = self._make_icon_tile(name)
            if child is not None:
                self._flowbox.append(child)
        self._rendered = end

    def _make_icon_tile(self, name: str) -> Gtk.Widget | None:
        btn = Gtk.Button()
        btn.add_css_class("flat")
        btn.set_tooltip_text(name)
        img = Gtk.Image.new_from_icon_name(name)
        img.set_pixel_size(_ICON_RENDER_SIZE)
        btn.set_child(img)
        btn.connect("clicked", self._on_icon_clicked, name)
        return btn

    # ---- events ----------------------------------------------------------

    def _on_search(self, _entry) -> None:
        # Debounce slightly: tiny GLib timeout
        GLib.idle_add(self._apply_filter)

    def _on_symbolic_toggle(self, _btn) -> None:
        self._apply_filter()

    def _on_scroll(self, adj) -> None:
        # When near the bottom, render the next page
        if self._rendered >= len(self._filtered):
            return
        if adj.get_value() + adj.get_page_size() >= adj.get_upper() - 100:
            self._render_next_page()

    def _on_key(self, _ctrl, keyval, _code, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self._dialog.close()
            return True
        return False

    def _on_icon_clicked(self, _btn, name: str) -> None:
        self._chosen = name
        self._dialog.close()

    def _on_close_request(self, _win) -> bool:
        if self._loop is not None and self._loop.is_running():
            self._loop.quit()
        return False  # let the window close

    def _on_link_clicked(self, _label, uri: str) -> bool:
        """Handle the custom vinopop:// link in the empty-state message."""
        if uri == "vinopop://open-user-icons":
            USER_ICONS_DIR.mkdir(parents=True, exist_ok=True)
            try:
                subprocess.Popen(["xdg-open", str(USER_ICONS_DIR)], start_new_session=True)
            except FileNotFoundError:
                pass
            return True  # consumed
        return False  # let GTK handle real URIs
