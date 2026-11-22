"""Searchable icon-picker dialog over every icon in the current GTK theme."""

from __future__ import annotations

import shutil
import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from core.xdg_paths import HICOLOR_APPS, USER_ICONS_DIR  # noqa: E402
from ui import gtk4_app  # noqa: E402

_PAGE_SIZE = 1500
_PREVIEW_ICON_PX = 24
_DIALOG_SIZE = (640, 520)
_SCROLL_PAGE_AHEAD_PX = 100
_USER_ICONS_LINK = "vinopop://open-user-icons"

# Offered when a search matches nothing: standard themes ship no brand icons
_FALLBACK_SUGGESTIONS = [
    "applications-internet-symbolic",
    "web-browser-symbolic",
    "system-search-symbolic",
    "applications-utilities-symbolic",
    "help-about-symbolic",
    "starred-symbolic",
    "view-list-symbolic",
    "emblem-system-symbolic",
]


def _suggestions(theme) -> list[str]:
    """_FALLBACK_SUGGESTIONS the theme can draw, with names it lacks
    swapped for their icon_style fallback (Breeze has no
    web-browser-symbolic) and anything still missing left out."""
    from ui import icon_style

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
    """Mirror the user's icons in ~/.config/vinopop/icons/ into hicolor, so
    they resolve by basename in the picker and on plugin buttons."""
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
        # GTK 4 has no rescan_if_needed(); re-setting the search path makes
        # the theme reload its directories
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
    return sorted(set(names))


class IconPicker:
    """Modal icon chooser: `IconPicker(parent, initial).run()` returns the
    chosen icon name, or None on cancel."""

    def __init__(self, parent=None, initial: str = "") -> None:
        self._parent = parent
        self._initial = initial
        self._all_icons: list[str] = []
        self._filtered: list[str] = []
        self._rendered: int = 0
        self._chosen: str | None = None
        self._symbolic_only = True
        self._loop: GLib.MainLoop | None = None

    def run(self) -> str | None:
        """Show the picker modally and block (nested main loop) until the
        user picks an icon or cancels. Returns the icon name or None."""
        self._all_icons = _list_all_icons()
        dlg = Adw.Dialog(title="Pick an Icon")
        dlg.set_content_width(_DIALOG_SIZE[0])
        dlg.set_content_height(_DIALOG_SIZE[1])
        self._dialog = dlg
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self._build_header(dlg))
        toolbar.set_content(self._build_results())
        dlg.set_child(toolbar)
        # Adw.Dialog closes itself (returning None) on Escape
        dlg.set_focus(self._search)

        self._loop = GLib.MainLoop()
        self._apply_filter()
        parent = self._parent if isinstance(self._parent, Gtk.Widget) else None
        gtk4_app.present_dialog(dlg, parent, self._on_closed)
        self._loop.run()
        self._loop = None
        return self._chosen

    def _build_header(self, dlg: Adw.Dialog) -> Adw.HeaderBar:
        """Cancel, the search entry as the title, and the Symbolic toggle.
        The entry is labelled: a placeholder isn't an accessible name (pinned
        by `tests/test_a11y.py::test_icon_picker_is_named`)."""
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: dlg.close())
        header.pack_start(cancel)

        self._search = Gtk.SearchEntry()
        self._search.set_placeholder_text("Search icons (for example, folder or mail)")
        self._search.update_property([Gtk.AccessibleProperty.LABEL], ["Search icons"])
        self._search.set_hexpand(True)
        self._search.set_max_width_chars(40)
        self._search.connect("search-changed", self._on_search)
        self._search.connect("stop-search", lambda _e: dlg.close())
        header.set_title_widget(self._search)

        self._symbolic_toggle = Gtk.CheckButton(label="Symbolic only")
        self._symbolic_toggle.set_active(True)
        self._symbolic_toggle.set_tooltip_text(
            "Show only monochrome icons that adapt to your theme."
        )
        self._symbolic_toggle.connect("toggled", self._on_symbolic_toggle)
        header.pack_end(self._symbolic_toggle)
        return header

    def _build_results(self) -> Gtk.Box:
        """The match count line (with the add-your-own link) over the grid."""
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(6)
        outer.set_margin_bottom(10)
        outer.set_margin_start(10)
        outer.set_margin_end(10)

        self._count_label = Gtk.Label(xalign=0)
        self._count_label.set_wrap(True)
        self._count_label.connect("activate-link", self._on_link_clicked)
        outer.append(self._count_label)

        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroll.set_hexpand(True)
        self._scroll.set_vexpand(True)
        self._scroll.get_vadjustment().connect("value-changed", self._on_scroll)

        self._flowbox = Gtk.FlowBox()
        # Without START GTK spreads a short result list over the whole height
        self._flowbox.set_valign(Gtk.Align.START)
        self._flowbox.set_halign(Gtk.Align.FILL)
        self._flowbox.set_hexpand(True)
        # The default is 7 per line, whatever the width
        self._flowbox.set_max_children_per_line(64)
        self._flowbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._flowbox.set_row_spacing(2)
        self._flowbox.set_column_spacing(2)
        self._scroll.set_child(self._flowbox)
        outer.append(self._scroll)
        return outer

    def _apply_filter(self) -> bool:
        query = self._search.get_text().strip().lower()
        symbolic_only = self._symbolic_toggle.get_active()

        if symbolic_only:
            pool = [n for n in self._all_icons if n.endswith("-symbolic")]
        else:
            pool = list(self._all_icons)

        if query:
            self._filtered = [n for n in pool if query in n.lower()]
        else:
            self._filtered = pool

        self._flowbox.remove_all()
        self._rendered = 0

        if not self._filtered and query:
            esc_query = GLib.markup_escape_text(query)
            symbolic_hint = (
                " <b>Tip:</b> if you added a colored brand icon, "
                "uncheck <i>Symbolic only</i> above to see it."
                if symbolic_only
                else ""
            )
            self._count_label.set_markup(
                f"<i>No icons match “{esc_query}”. Standard icon themes do not include "
                f"brand-specific icons (such as Wikipedia or GitHub).</i>\n"
                f"<small>Add custom <tt>.svg</tt> or <tt>.png</tt> files to "
                f'<a href="{_USER_ICONS_LINK}">'
                f"~/.config/vinopop/icons/</a>.{symbolic_hint} "
                f"Or choose a generic icon:</small>"
            )
            self._filtered = _suggestions(_icon_theme())
        else:
            self._count_label.set_markup(
                f"{len(self._filtered)} matching icons "
                f"({len(self._all_icons)} total in theme) · "
                f'<a href="{_USER_ICONS_LINK}">Add custom icons</a>'
            )

        self._render_next_page()
        return GLib.SOURCE_REMOVE

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
        img.set_pixel_size(_PREVIEW_ICON_PX)
        btn.set_child(img)
        btn.connect("clicked", self._on_icon_clicked, name)
        return btn

    def _on_search(self, _entry) -> None:
        GLib.idle_add(self._apply_filter)

    def _on_symbolic_toggle(self, _btn) -> None:
        self._apply_filter()

    def _on_scroll(self, adj) -> None:
        """Render the next page once the view nears the bottom."""
        if self._rendered >= len(self._filtered):
            return
        if adj.get_value() + adj.get_page_size() >= adj.get_upper() - _SCROLL_PAGE_AHEAD_PX:
            self._render_next_page()

    def _on_icon_clicked(self, _btn, name: str) -> None:
        self._chosen = name
        self._dialog.close()

    def _on_closed(self, _dialog) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.quit()

    def _on_link_clicked(self, _label, uri: str) -> bool:
        """Open the user icon folder for our link; True when handled."""
        if uri == _USER_ICONS_LINK:
            USER_ICONS_DIR.mkdir(parents=True, exist_ok=True)
            try:
                subprocess.Popen(["xdg-open", str(USER_ICONS_DIR)], start_new_session=True)
            except FileNotFoundError:
                pass
            return True
        return False
