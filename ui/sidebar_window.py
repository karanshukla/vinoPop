"""The sidebar-and-pages layout shared by the Settings and Plugin Manager
windows: a page list on the left, the selected page on the right under a
header bar that names it, collapsing to one pane at a time when narrow."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402


def build(
    win: Adw.Window,
    sidebar_title: str,
    pages: list[tuple[str, str, str, Gtk.Widget]],
    initial: str,
    on_show: Callable[[str], None] | None = None,
) -> Callable[[str], None]:
    """Lay `pages` ((id, title, icon, widget) each) out in `win` and show
    `initial`. on_show is called with the page id whenever a page is
    shown. Returns a function that shows a page by id (unknown ids are
    ignored), moving the sidebar selection with it."""
    stack = Gtk.Stack()
    stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
    names: list[str] = []
    sidebar = Gtk.ListBox()
    sidebar.add_css_class("navigation-sidebar")
    for name, title, icon, widget in pages:
        names.append(name)
        stack.add_titled(widget, name, title).set_icon_name(icon)
        item = Gtk.Box(spacing=12)
        item.set_margin_top(6)
        item.set_margin_bottom(6)
        item.set_margin_start(6)
        # The icon only decorates the title next to it
        item.append(Gtk.Image(icon_name=icon, accessible_role=Gtk.AccessibleRole.PRESENTATION))
        item.append(Gtk.Label(label=title, xalign=0))
        row = Gtk.ListBoxRow(child=item)
        # GTK 4.14 doesn't name a list row from its text: without this a
        # screen reader reads every page in the list as a blank "list item".
        row.update_property([Gtk.AccessibleProperty.LABEL], [title])
        sidebar.append(row)

    content_page = Adw.NavigationPage()
    content_view = Adw.ToolbarView()
    content_view.add_top_bar(Adw.HeaderBar())
    content_view.set_content(stack)
    content_page.set_child(content_view)

    sidebar_page = Adw.NavigationPage(title=sidebar_title)
    sidebar_view = Adw.ToolbarView()
    sidebar_view.add_top_bar(Adw.HeaderBar())
    sidebar_scroll = Gtk.ScrolledWindow()
    sidebar_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    sidebar_scroll.set_child(sidebar)
    sidebar_view.set_content(sidebar_scroll)
    sidebar_page.set_child(sidebar_view)

    split = Adw.NavigationSplitView()
    split.set_sidebar(sidebar_page)
    split.set_content(content_page)
    split.set_min_sidebar_width(190)
    split.set_max_sidebar_width(230)

    def _show_page(name: str) -> None:
        stack.set_visible_child_name(name)
        child = stack.get_child_by_name(name)
        content_page.set_title(stack.get_page(child).get_title() or "")
        if on_show is not None:
            on_show(name)

    def _on_row(_box, row) -> None:
        if row is None:
            return
        _show_page(names[row.get_index()])
        split.set_show_content(True)

    sidebar.connect("row-activated", _on_row)
    sidebar.connect("row-selected", _on_row)

    def select(name: str) -> None:
        if name not in names:
            return
        row = sidebar.get_row_at_index(names.index(name))
        if sidebar.get_selected_row() is row:
            # row-selected doesn't fire again; still bring the page forward
            # when the window is collapsed to one pane.
            split.set_show_content(True)
        else:
            sidebar.select_row(row)

    current = initial if initial in names else names[0]
    _show_page(current)
    sidebar.select_row(sidebar.get_row_at_index(names.index(current)))

    # Narrow windows show one pane at a time: the page list, then the
    # page with a back button.
    narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 600sp"))
    narrow.add_setter(split, "collapsed", True)
    win.add_breakpoint(narrow)
    win.set_content(split)
    return select
