"""GTK 4 + libadwaita preferences window for vinoPop.

Uses Adw.PreferencesWindow with grouped boxed-list rows (Adw.SwitchRow,
Adw.ComboRow, Adw.SpinRow, Adw.EntryRow) - the GNOME-Settings-style UI
that libhandy used to provide on GTK3.

Apply-on-change semantics: edits save immediately, no Save/Cancel buttons.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

import gtk4_app  # noqa: E402
from settings import get_settings  # noqa: E402

# libhandy needed _unwrap_subtitle_labels() here: HdyActionRow baked
# ellipsize=END / lines=1 into its subtitle label, so we walked the realised
# tree after show_all() to patch it. Adw.ActionRow wraps its subtitle by
# default (subtitle-lines=0 means unlimited), so the hack is gone.


def _plain(row: Gtk.Widget) -> Gtk.Widget:
    """Adw rows parse title/subtitle as Pango markup by default; ours are
    plain text (they contain '&', '<', quotes ...), so turn that off."""
    row.set_use_markup(False)
    return row


def _open_text_editor_modal(
    parent: Gtk.Window | None,
    title: str,
    subtitle: str,
    initial_text: str,
    placeholder_text: str,
    on_done: Callable[[str | None], None],
) -> None:
    """Pop a modal editor with a big multi-line TextView, Save / Cancel.
    Calls on_done(new_text) on Save, on_done(None) on Cancel. Used for
    blocklists, snippet variables - things that need a whole textarea but
    shouldn't occupy permanent space in the Settings page when empty.

    GTK 4 has no blocking Gtk.Dialog.run(), so the result is delivered
    through a callback instead of a return value."""
    dlg = Adw.Window()
    dlg.set_title(title)
    dlg.set_default_size(580, 420)
    dlg.set_icon_name("vinopop")
    dlg.set_modal(True)
    if parent is not None:
        dlg.set_transient_for(parent)

    header = Adw.HeaderBar()
    header.set_show_end_title_buttons(False)
    header.set_show_start_title_buttons(False)
    cancel_btn = Gtk.Button(label="Cancel")
    save_btn = Gtk.Button(label="Save")
    save_btn.add_css_class("suggested-action")
    header.pack_start(cancel_btn)
    header.pack_end(save_btn)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    content.set_margin_top(12)
    content.set_margin_bottom(12)
    content.set_margin_start(14)
    content.set_margin_end(14)

    sub_lbl = Gtk.Label(xalign=0)
    sub_lbl.set_wrap(True)
    sub_lbl.set_text(subtitle)
    sub_lbl.add_css_class("dim-label")
    sub_lbl.add_css_class("caption")
    content.append(sub_lbl)

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.set_has_frame(True)
    scroll.set_hexpand(True)
    scroll.set_vexpand(True)
    view = Gtk.TextView()
    view.set_wrap_mode(Gtk.WrapMode.NONE)
    view.set_monospace(True)
    view.add_css_class("lp-cmd-edit")
    if initial_text:
        view.get_buffer().set_text(initial_text)
    _attach_textarea_placeholder(view, placeholder_text)
    scroll.set_child(view)
    content.append(scroll)

    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(header)
    toolbar.set_content(content)
    dlg.set_content(toolbar)

    finished = {"done": False}

    def _finish(save: bool) -> None:
        if finished["done"]:
            return
        finished["done"] = True
        result: str | None = None
        if save:
            buf = view.get_buffer()
            s, e = buf.get_bounds()
            raw = buf.get_text(s, e, True)
            result = "" if getattr(view, "_placeholder_active", False) else raw
        dlg.destroy()
        on_done(result)

    cancel_btn.connect("clicked", lambda *_: _finish(False))
    save_btn.connect("clicked", lambda *_: _finish(True))
    # Window-manager close and Escape both mean Cancel, like Gtk.Dialog did.
    dlg.connect("close-request", lambda *_: (_finish(False), True)[1])
    esc = Gtk.ShortcutController()
    esc.add_shortcut(
        Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("Escape"),
            Gtk.CallbackAction.new(lambda *_: (_finish(False), True)[1]),
        )
    )
    dlg.add_controller(esc)

    dlg.present()
    view.grab_focus()


def _attach_textarea_placeholder(view: Gtk.TextView, placeholder_text: str) -> None:
    """Give a Gtk.TextView the placeholder-text behaviour that Gtk.Entry
    has built-in. When the buffer is empty the placeholder shows in
    grey italics; focus or any typing wipes it. Empty state is restored
    on focus-out so the user always sees the hint when the field is blank.

    Save handlers MUST check view._placeholder_active and treat the
    placeholder text as "no value" - otherwise the placeholder string
    would be persisted as data.
    """
    buf = view.get_buffer()
    # Per-view tag so callers can have multiple placeholders without
    # name collisions in the buffer's tag table.
    tag = buf.create_tag(
        f"lp-placeholder-{id(view)}",
        foreground="#7a8090",
        style=Pango.Style.ITALIC,
    )
    view._placeholder_active = False

    def _set_placeholder() -> None:
        # Suppress the 'changed' signal during the placeholder swap so
        # debounced save handlers don't fire on what is really UI chrome.
        view._setting_placeholder = True
        buf.set_text(placeholder_text)
        s, e = buf.get_bounds()
        buf.apply_tag(tag, s, e)
        view._placeholder_active = True
        view._setting_placeholder = False

    s, e = buf.get_bounds()
    if not buf.get_text(s, e, True):
        _set_placeholder()

    def _clear_placeholder() -> None:
        if view._placeholder_active:
            view._setting_placeholder = True
            buf.set_text("")
            view._placeholder_active = False
            view._setting_placeholder = False

    def _on_key_press(_ctrl, keyval, _keycode, _state):
        # Clear the hint the moment the user types something that produces
        # text (letters, digits, space, Ctrl+V...), but keep it for pure
        # navigation/modifier keys - and, crucially, while the field is
        # merely focused. Clearing on focus-in used to wipe the hint the
        # instant the dialog opened (the TextView grabs focus on show), so
        # the example only reappeared after a focus-out/in cycle such as a
        # right-click. keyval_to_unicode is non-zero exactly for the keys
        # that put a character in the buffer.
        if view._placeholder_active and Gdk.keyval_to_unicode(keyval):
            _clear_placeholder()
        return False

    def _on_focus_out(_ctrl):
        s, e = buf.get_bounds()
        if not buf.get_text(s, e, True).strip():
            _set_placeholder()

    # Capture phase: run before the TextView's own key handler inserts the
    # character, so it lands in an emptied buffer rather than the hint.
    keys = Gtk.EventControllerKey()
    keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    keys.connect("key-pressed", _on_key_press)
    view.add_controller(keys)
    focus = Gtk.EventControllerFocus()
    focus.connect("leave", _on_focus_out)
    view.add_controller(focus)


def _doctor_checks() -> list[dict]:
    """Run `main.py --doctor --json` and return its checks."""
    import json
    import sys
    from pathlib import Path

    main_py = Path(__file__).resolve().parent / "main.py"
    try:
        res = subprocess.run(
            [sys.executable, str(main_py), "--doctor", "--json"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return list(json.loads(res.stdout)["checks"])
    except Exception as exc:  # noqa: BLE001 - shown in the page
        return [{"status": "FAIL", "label": "doctor", "detail": f"couldn't run: {exc}"}]


def _session_info() -> list[tuple[str, str]]:
    import os
    import platform

    try:
        from version import __version__
    except Exception:  # noqa: BLE001
        __version__ = "unknown"
    env = os.environ.get
    display = Gdk.Display.get_default()
    try:
        system = platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
    except OSError:
        system = platform.system()
    return [
        ("vinoPop", __version__),
        (
            "Desktop",
            f"{env('XDG_CURRENT_DESKTOP') or 'unknown'} on {env('XDG_SESSION_TYPE') or 'unknown'}",
        ),
        ("GTK display", type(display).__name__ if display else "none"),
        ("System", system),
        ("Kernel", platform.release()),
    ]


def _open_path(path) -> None:
    from gi.repository import Gio

    try:
        Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
    except Exception as exc:  # noqa: BLE001
        print(f"[settings] couldn't open {path}: {exc}")


def _apply_theme(mode: str) -> None:
    """Load the app theme for `mode` and rebuild the popup's own CSS
    provider so the selection popup picks up the new palette too."""
    try:
        import theme as _theme

        _theme.install_premium_theme(mode)
    except Exception as exc:
        print(f"[settings] theme reload failed: {exc}")
    try:
        import popup as _popup

        _popup.reinstall_popup_css()
    except Exception as exc:
        print(f"[settings] popup theme reload failed: {exc}")


def _force_all_supported() -> bool:
    """Whether the popup can read the force-all-plugins modifier: it
    queries the pointer's modifier mask over Xlib, so X11 only."""
    try:
        from platform_backend import get_backend

        return bool(get_backend().popup_uses_xlib)
    except Exception:
        return True


def _hotkey_bindable(combo: str) -> bool:
    try:
        from platform_backend import get_backend

        return get_backend().can_bind_hotkey(combo)
    except Exception:
        return True


def _force_to_front(window: Gtk.Window) -> None:
    """Bring the window to the foreground. GTK 4 dropped keep_above and
    GdkWindow.raise_(), so present() (with an unminimize first) is the
    only portable request; the compositor arbitrates the rest."""
    try:
        window.unminimize()
    except Exception:
        pass
    window.present()


_MODIFIER_KEYVALS = {
    Gdk.KEY_Control_L,
    Gdk.KEY_Control_R,
    Gdk.KEY_Shift_L,
    Gdk.KEY_Shift_R,
    Gdk.KEY_Alt_L,
    Gdk.KEY_Alt_R,
    Gdk.KEY_Super_L,
    Gdk.KEY_Super_R,
    Gdk.KEY_Meta_L,
    Gdk.KEY_Meta_R,
    Gdk.KEY_Hyper_L,
    Gdk.KEY_Hyper_R,
}


def _format_combo(keyval: int, state: int) -> str:
    parts = []
    if state & Gdk.ModifierType.CONTROL_MASK:
        parts.append("ctrl")
    if state & Gdk.ModifierType.ALT_MASK:
        parts.append("alt")
    if state & Gdk.ModifierType.SHIFT_MASK:
        parts.append("shift")
    if state & Gdk.ModifierType.SUPER_MASK:
        parts.append("super")
    name = Gdk.keyval_name(keyval) or ""
    parts.append(name.lower())
    return "+".join(parts)


class HotkeyRecorder(Gtk.Button):
    """Compact button that captures a keypress combo when clicked."""

    def __init__(self, initial: str = "", on_changed: Callable[[str], None] | None = None) -> None:
        super().__init__()
        self._value = initial
        self._recording = False
        self._rejected = ""
        self._on_changed = on_changed
        self.set_valign(Gtk.Align.CENTER)
        self._update_label()
        self.connect("clicked", self._on_clicked)
        keys = Gtk.EventControllerKey()
        # Capture phase so the window's own shortcuts (Escape, Ctrl+W ...)
        # don't get the combo before we can record it.
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_press)
        self.add_controller(keys)
        self.set_focusable(True)

    def get_value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        if self._value == value:
            return
        self._value = value
        self._recording = False
        self._update_label()
        if self._on_changed:
            self._on_changed(value)

    def _update_label(self) -> None:
        if self._recording and self._rejected:
            self.set_label(f"Can't bind {self._rejected} - try another  (Esc to cancel)")
        elif self._recording:
            self.set_label("⏺  Press a hotkey  (Esc to cancel)")
        elif self._value:
            self.set_label(self._value)
        else:
            self.set_label("Click to record…")

    def _on_clicked(self, *_):
        self._recording = True
        self._rejected = ""
        self._update_label()
        self.grab_focus()

    def _on_key_press(self, _ctrl, keyval, _keycode, state):
        if not self._recording:
            return False
        plain_esc = keyval == Gdk.KEY_Escape and not (
            state
            & (
                Gdk.ModifierType.CONTROL_MASK
                | Gdk.ModifierType.ALT_MASK
                | Gdk.ModifierType.SUPER_MASK
            )
        )
        if plain_esc:
            self._recording = False
            self._update_label()
            return True
        if keyval in _MODIFIER_KEYVALS:
            return True
        combo = _format_combo(keyval, state)
        if not _hotkey_bindable(combo):
            self._rejected = combo
            self._update_label()
            return True
        self._rejected = ""
        self.set_value(combo)
        return True


#: Per-row leading icon for the Settings rows, so each gets a coloured tile in
#: the same style as the Plugin Manager. Titles must match set_title() exactly;
#: anything unmapped falls back to a generic gear (never a broken glyph).
_SETTING_ICONS = {
    "Theme": "applications-graphics-symbolic",
    "Popup button size": "view-fullscreen-symbolic",
    "Maximum buttons in the popup": "view-grid-symbolic",
    "When actions overflow": "view-more-symbolic",
    "Popup hotkey": "preferences-desktop-keyboard-shortcuts-symbolic",
    "Screen OCR hotkey": "camera-photo-symbolic",
    "Auto-popup on selection": "edit-select-all-symbolic",
    "Modifier+double-click for the edit menu": "input-mouse-symbolic",
    "Modifier key": "input-keyboard-symbolic",
    "Force all plugins modifier": "view-grid-symbolic",
    "Start at login": "system-run-symbolic",
    "Clipboard history": "edit-copy-symbolic",
    "Optional clipboard shortcut": "edit-paste-symbolic",
    "Snippet triggers (text expansion)": "document-edit-symbolic",
    "Don't expand triggers in these apps or sites": "security-high-symbolic",
    "Snippet variables": "view-list-symbolic",
    "Shell expansion in snippets": "utilities-terminal-symbolic",
    "Skip short auto-popup selections": "format-justify-left-symbolic",
    "Don't show in these apps or pages": "security-medium-symbolic",
    "Search engine": "edit-find-symbolic",
    "Custom search URL ({q} = the selection)": "insert-link-symbolic",
    "Share anonymous OpenVINO usage stats with Intel": "network-transmit-symbolic",
    "Trigger hotkey on first press": "media-playback-start-symbolic",
    "Reset settings to defaults": "view-refresh-symbolic",
    "Keep terminal open after running": "utilities-terminal-symbolic",
}


class SettingsDialog:
    def __init__(
        self,
        on_changed: Callable[[], None] | None = None,
        on_restart: Callable[[], None] | None = None,
        on_open_plugins: Callable[..., None] | None = None,
    ) -> None:
        gtk4_app.init()
        self._on_changed = on_changed
        self._on_restart = on_restart
        self._on_open_plugins = on_open_plugins
        # Runs the doctor the first time the Debug page is opened.
        self._debug_refresh: Callable[[], None] | None = None
        self._settings = get_settings()
        self._window: Adw.Window | None = None
        # Kept across a rebuild (Reset to defaults) so the user stays put.
        self._page_name = "activation"

    def show(self) -> None:
        if self._window is not None and self._window.get_visible():
            _force_to_front(self._window)
            return

        win = Adw.Window()
        win.set_title("vinoPop Settings")
        win.set_default_size(860, 640)
        win.set_size_request(360, 360)
        win.set_icon_name("vinopop")
        win.set_modal(False)
        win.connect("destroy", self._on_destroy)
        # GTK 4 may keep the destroyed window alive while Python holds a
        # ref, so drop our references on close-request as well.
        win.connect("close-request", lambda w: (self._on_destroy(w), False)[1])
        # Set before the groups are built: their modal editors use it as
        # their parent.
        self._window = win

        # One page per topic in a sidebar (like vinoWhisper's settings),
        # instead of every group stacked in one long scroll.
        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        for name, title, icon, builders in self._pages():
            page = Adw.PreferencesPage()
            for build in builders:
                group = build()
                # The header already names the page.
                if group.get_title() == title:
                    group.set_title("")
                page.add(group)
            # The Debug page's rows carry status icons instead.
            if name != "debug":
                self._decorate_badges(page)
            stack.add_titled(page, name, title).set_icon_name(icon)

        content_page = Adw.NavigationPage()
        content_header = Adw.HeaderBar()
        content_view = Adw.ToolbarView()
        content_view.add_top_bar(content_header)
        content_view.set_content(stack)
        content_page.set_child(content_view)

        sidebar = Gtk.ListBox()
        sidebar.add_css_class("navigation-sidebar")
        names: list[str] = []
        for name, title, icon, _builders in self._pages():
            names.append(name)
            item = Gtk.Box(spacing=12)
            item.set_margin_top(6)
            item.set_margin_bottom(6)
            item.set_margin_start(6)
            item.append(Gtk.Image.new_from_icon_name(icon))
            item.append(Gtk.Label(label=title, xalign=0))
            sidebar.append(item)
        sidebar_page = Adw.NavigationPage(title="Settings")
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
            self._page_name = name
            if name == "debug" and self._debug_refresh is not None:
                refresh, self._debug_refresh = self._debug_refresh, None
                refresh()

        def _on_row(_box, row) -> None:
            if row is None:
                return
            _show_page(names[row.get_index()])
            split.set_show_content(True)

        sidebar.connect("row-activated", _on_row)
        sidebar.connect("row-selected", _on_row)
        current = self._page_name if self._page_name in names else names[0]
        _show_page(current)
        sidebar.select_row(sidebar.get_row_at_index(names.index(current)))

        # Narrow windows show one pane at a time: the page list, then the
        # page with a back button.
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 600sp"))
        narrow.add_setter(split, "collapsed", True)
        win.add_breakpoint(narrow)
        win.set_content(split)
        _force_to_front(win)

    def _pages(self) -> list[tuple[str, str, str, list[Callable[[], Adw.PreferencesGroup]]]]:
        """(id, sidebar title, icon, group builders) for each Settings page."""
        return [
            (
                "activation",
                "Activation",
                "input-keyboard-symbolic",
                [self._build_activation_group, self._build_timing_group],
            ),
            (
                "appearance",
                "Appearance",
                "applications-graphics-symbolic",
                [self._build_appearance_group],
            ),
            (
                "clipboard",
                "Clipboard & Snippets",
                "edit-paste-symbolic",
                [self._build_snippets_clipboard_group],
            ),
            (
                "filters",
                "Filters",
                "security-medium-symbolic",
                [self._build_filter_group],
            ),
            (
                "actions",
                "Search & Terminal",
                "edit-find-symbolic",
                [self._build_search_group, self._build_terminal_group],
            ),
            (
                "advanced",
                "Advanced",
                "emblem-system-symbolic",
                [self._build_ai_group, self._build_privacy_group, self._build_advanced_group],
            ),
            (
                "debug",
                "Debug",
                "dialog-information-symbolic",
                [
                    self._build_troubleshoot_group,
                    self._build_session_group,
                    self._build_diagnostics_group,
                ],
            ),
        ]

    def _on_destroy(self, win=None, *_):
        # Ignore a late signal from a window we already replaced.
        if win is not None and self._window is not None and win is not self._window:
            return
        self._window = None

    # ---- coloured icon badges (matches the Plugin Manager) -------------------
    def _badge(self, icon_name: str | None, color_index: int) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box.set_size_request(32, 32)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        box.set_margin_end(8)
        box.add_css_class("lp-badge")
        box.add_css_class(f"lp-badge-{color_index % 4}")
        it = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        import icon_style

        icon_name = icon_style.available(icon_name or "", it)
        name = icon_name if (icon_name and it.has_icon(icon_name)) else "emblem-system-symbolic"
        img = Gtk.Image.new_from_icon_name(name)
        img.set_pixel_size(17)
        img.add_css_class("lp-badge-glyph")
        img.set_hexpand(True)
        box.set_hexpand(False)  # stop the glyph's hexpand propagating to the row prefix
        img.set_halign(Gtk.Align.CENTER)
        box.append(img)
        return box

    def _decorate_badges(self, root: Gtk.Widget) -> None:
        """Give each settings row a coloured icon tile so Settings reads as the
        same family as the Plugin Manager. Walks the row tree, skipping the
        inside of each row (so expander sub-rows aren't decorated)."""
        rows: list[Gtk.Widget] = []

        def walk(w):
            # ComboRow / SwitchRow / SpinRow are ActionRow subclasses.
            if isinstance(w, (Adw.ActionRow, Adw.ExpanderRow, Adw.EntryRow)):
                rows.append(w)
                return
            child = w.get_first_child()
            while child is not None:
                walk(child)
                child = child.get_next_sibling()

        walk(root)
        for i, r in enumerate(rows):
            icon = _SETTING_ICONS.get(r.get_title() or "")
            try:
                r.add_prefix(self._badge(icon, i))
            except Exception:
                pass

    # ---- row factories -------------------------------------------------------

    def _switch_row(self, title: str, subtitle: str, active: bool) -> Adw.SwitchRow:
        row = _plain(Adw.SwitchRow())
        row.set_title(title)
        row.set_subtitle(subtitle)
        row.set_active(bool(active))
        return row

    def _combo_row(
        self,
        title: str,
        subtitle: str,
        options: list[tuple[str, str]],
        current: str | None,
        on_change: Callable[[str], None],
    ) -> Adw.ComboRow:
        """Adw.ComboRow over (id, label) pairs - the GTK 4 stand-in for
        the old ComboBoxText-with-ids. on_change gets the selected id."""
        row = _plain(Adw.ComboRow())
        row.set_title(title)
        row.set_subtitle(subtitle)
        ids = [key for key, _label in options]
        row.set_model(Gtk.StringList.new([label for _key, label in options]))
        # Unknown ids fall back to the first option (ComboBoxText showed
        # an empty combo instead; nothing is saved until the user picks).
        row.set_selected(ids.index(current) if current in ids else 0)

        def _on_selected(r, _pspec) -> None:
            idx = r.get_selected()
            if 0 <= idx < len(ids):
                on_change(ids[idx])

        row.connect("notify::selected", _on_selected)
        row._lp_ids = ids
        return row

    @staticmethod
    def _combo_id(row: Adw.ComboRow) -> str | None:
        ids = getattr(row, "_lp_ids", [])
        idx = row.get_selected()
        return ids[idx] if 0 <= idx < len(ids) else None

    def _suffix_icon_button(self, icon_name: str, tooltip: str) -> Gtk.Button:
        btn = Gtk.Button.new_from_icon_name(icon_name)
        btn.set_valign(Gtk.Align.CENTER)
        btn.set_tooltip_text(tooltip)
        btn.add_css_class("flat")
        return btn

    def _edit_suffix(self) -> Gtk.Image:
        img = Gtk.Image.new_from_icon_name("document-edit-symbolic")
        img.set_valign(Gtk.Align.CENTER)
        return img

    # ---- groups --------------------------------------------------------------

    def _build_appearance_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Appearance")

        def _on_theme_changed(mode: str) -> None:
            mode = mode or "dark"
            self._save_key("theme", mode)
            _apply_theme(mode)

        theme_row = self._combo_row(
            "Theme",
            "'Follow system' matches your desktop's light or dark setting.",
            [
                ("dark", "Dark"),
                ("light", "Light"),
                ("system", "Follow system"),
            ],
            self._settings.get("theme", "dark") or "dark",
            _on_theme_changed,
        )
        group.add(theme_row)

        # Popup button size: how big each action chip in the floating
        # popup is. Clamped to [16, 32] pixels - under 16 the symbolic
        # icons go muddy at every common Linux resolution; over 32 is
        # touch-target territory mouse users don't benefit from, and it
        # turns the popup into a screen-spanning bar.
        size_adj = Gtk.Adjustment(
            value=self._settings.get_int("popup_button_size"),
            lower=16,
            upper=32,
            step_increment=1,
            page_increment=4,
        )
        size_row = _plain(Adw.SpinRow.new(size_adj, 1, 0))
        size_row.set_title("Popup button size")
        size_row.set_subtitle(
            "Size of each popup button in pixels, from 16 (dense) to 32 "
            "(roomy). Smaller buttons fit more actions on one row."
        )
        size_row.set_numeric(True)

        def _on_size_changed(spin: Adw.SpinRow, _pspec) -> None:
            self._save_key("popup_button_size", int(spin.get_value()))
            # Live-apply: rebuild the popup's CSS provider so the next
            # show_for() renders at the new size.
            try:
                import popup as _popup

                _popup.reinstall_popup_css()
            except Exception as exc:
                print(f"[settings] popup css reload failed: {exc}")

        size_row.connect("notify::value", _on_size_changed)
        group.add(size_row)

        # ----- Plugin icon style: colourful tiles vs uniform mono glyphs -----
        _cur_style = self._settings.get("icon_style") or "color"
        if _cur_style not in ("color", "glyph"):
            _cur_style = "color"

        # Live preview strip: sample icons rendered in the chosen style.
        icon_preview = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon_preview.set_halign(Gtk.Align.CENTER)
        icon_preview.set_margin_top(8)
        icon_preview.set_margin_bottom(12)

        def _rebuild_icon_preview(style: str) -> None:
            child = icon_preview.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                icon_preview.remove(child)
                child = nxt
            try:
                import icon_style as _ist

                concepts = _ist.PREVIEW_CONCEPTS
            except Exception:
                concepts = []
            for concept in concepts:
                nm = f"vinopop-{concept}-symbolic" if style == "glyph" else f"vinopop-{concept}"
                im = Gtk.Image.new_from_icon_name(nm)
                im.set_pixel_size(30)
                icon_preview.append(im)

        def _on_icon_style_changed(style: str) -> None:
            style = style or "color"
            self._save_key("icon_style", style)
            _rebuild_icon_preview(style)

        icon_row = self._combo_row(
            "Plugin icon style",
            "Colourful tiles, or uniform mono glyphs that match the plain "
            "edit icons. Applies to the popup and Plugin Manager.",
            [("color", "Colourful tiles"), ("glyph", "Simple glyphs")],
            _cur_style,
            _on_icon_style_changed,
        )
        group.add(icon_row)

        _rebuild_icon_preview(_cur_style)
        group.add(icon_preview)

        # ----- Tray icon: panel-recoloured glyph vs fixed light/dark mono -----
        _cur_tray = self._settings.get("tray_icon_style") or "color"
        if _cur_tray not in ("color", "light", "dark"):
            _cur_tray = "color"

        def _on_tray_icon_changed(style: str) -> None:
            # _save_key fires the on_changed chain -> main.py tells the tray
            # subprocess to re-render its icon live (no restart needed).
            self._save_key("tray_icon_style", style or "color")

        tray_row = self._combo_row(
            "Tray icon",
            "Auto follows your panel colour. If it doesn't on your desktop, "
            "pick Light for a dark panel or Dark for a light panel.",
            [
                ("color", "Auto (follows panel)"),
                ("light", "Light mono (dark panels)"),
                ("dark", "Dark mono (light panels)"),
            ],
            _cur_tray,
            _on_tray_icon_changed,
        )
        group.add(tray_row)

        # Max buttons per popup: how many actions can show before the
        # popup wraps to a second row and (past 2 rows) drops to a
        # "+N" overflow chip. Pairs with popup_button_size to govern
        # popup density end-to-end. Range starts at 4 (anything less
        # makes the popup feel broken) and tops out at 40 (point of
        # diminishing returns; the chip handles the rest).
        count_adj = Gtk.Adjustment(
            value=self._settings.get_int("max_popup_buttons"),
            lower=4,
            upper=40,
            step_increment=1,
            page_increment=4,
        )
        count_row = _plain(Adw.SpinRow.new(count_adj, 1, 0))
        count_row.set_title("Maximum buttons in the popup")
        count_row.set_subtitle(
            "Actions past this are hidden behind a '+N' chip that opens the Plugin Manager."
        )
        count_row.set_numeric(True)
        count_row.connect(
            "notify::value",
            lambda spin, _p: self._save_key("max_popup_buttons", int(spin.get_value())),
        )
        group.add(count_row)

        # Overflow mode: what the popup does when more actions match than fit
        # on a single line. Bounded by the cap above.
        ov_row = self._combo_row(
            "When actions overflow",
            "How the popup handles more actions than fit on one line. "
            "‘One row + expand arrow’ stays compact and reveals the rest on "
            "click; ‘Wrap’ uses two rows; ‘One row only’ hides extras behind "
            "a +N chip.",
            [
                ("expand", "One row + expand arrow"),
                ("wrap", "Wrap to two rows"),
                ("cap", "One row only (+N chip)"),
            ],
            self._settings.get("popup_overflow_mode") or "expand",
            lambda v: self._save_key("popup_overflow_mode", v or "expand"),
        )
        group.add(ov_row)

        return group

    def _build_activation_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Activation")
        group.set_description("How vinoPop appears.")

        # Popup hotkey row. Lives here rather than in a dedicated Hotkeys
        # group because, with the clipboard shortcut moved to Snippets &
        # Clipboard, there's only one keyboard shortcut left and it
        # belongs alongside the other "how do I summon the popup" toggles.
        hk_row = _plain(Adw.ActionRow())
        hk_row.set_title("Popup hotkey")
        hk_row.set_subtitle(
            "Press this anywhere to open the popup. If you have text "
            "highlighted, it shows actions for that text. If not, it "
            "shows a paste menu instead."
        )
        recorder = HotkeyRecorder(
            self._settings.get("hotkey") or "",
            on_changed=lambda v: self._save_key("hotkey", v),
        )
        hk_row.add_suffix(recorder)
        clear = self._suffix_icon_button("edit-clear-symbolic", "Disable hotkey")
        clear.connect("clicked", lambda *_: recorder.set_value(""))
        hk_row.add_suffix(clear)
        group.add(hk_row)

        # OCR hotkey row - sits next to the popup hotkey so the two
        # "summon something with a key chord" rows cluster together.
        # The status subtitle changes based on whether the OCR backend
        # is reachable (a capture tool + the OpenVINO OCR engine), so the user knows whether
        # the hotkey will actually do anything when pressed.
        ocr_row = _plain(Adw.ActionRow())
        ocr_row.set_title("Screen OCR hotkey")
        try:
            from screen_ocr import is_supported as _ocr_supported

            ocr_ok, ocr_reason = _ocr_supported()
        except Exception:
            ocr_ok, ocr_reason = False, "screen_ocr module not available"
        if ocr_ok:
            ocr_row.set_subtitle("Draw a box anywhere; the text inside is copied as text.")
        else:
            ocr_row.set_subtitle(f"Setup needed: {ocr_reason}.")
        ocr_recorder = HotkeyRecorder(
            self._settings.get("ocr_hotkey") or "",
            on_changed=lambda v: self._save_key("ocr_hotkey", v),
        )
        # When OCR backends aren't available, offer a one-click Install
        # button that runs the install in the background via pkexec (a
        # graphical password prompt), then re-probes and updates the row
        # live. Replaces the old "copy command to clipboard" dance, which
        # also relied on xclip - absent on a Wayland box.
        if not ocr_ok:
            try:
                from screen_ocr import install_argv as _ocr_install_argv

                argv = _ocr_install_argv()
            except Exception:
                argv = None
            ocr_install_btn = Gtk.Button(label="Install")
            ocr_install_btn.set_valign(Gtk.Align.CENTER)
            ocr_install_btn.add_css_class("suggested-action")
            ocr_install_btn.set_tooltip_text(
                "Installs the OCR tools in the background (asks for your password)."
            )
            if argv is None:
                ocr_install_btn.set_sensitive(False)
                ocr_install_btn.set_tooltip_text(
                    "Install the OCR engine with: pip install --user -r requirements-ai.txt"
                )

            def _on_ocr_install(btn, argv=argv, row=ocr_row):
                if not argv:
                    return
                btn.set_sensitive(False)
                btn.set_label("Installing...")
                row.set_subtitle("Installing - authorise the password prompt...")

                def _done(ok, err):
                    try:
                        from screen_ocr import is_supported as _sup

                        now_ok, reason = _sup()
                    except Exception:
                        now_ok, reason = ok, ""
                    if now_ok:
                        row.set_subtitle("Ready. Draw a box anywhere to copy its text.")
                        btn.set_label("Installed")
                        btn.set_sensitive(False)
                        btn.remove_css_class("suggested-action")
                        # Bind the hotkey now that the backend works.
                        if self._on_changed is not None:
                            try:
                                self._on_changed()
                            except Exception:
                                pass
                    else:
                        row.set_subtitle(f"Install didn't complete: {reason or err}. Try again.")
                        btn.set_label("Install")
                        btn.set_sensitive(True)
                    return False

                def _worker():
                    ok, err = False, ""
                    try:
                        res = subprocess.run(argv, capture_output=True, timeout=600)
                        ok = res.returncode == 0
                        if not ok:
                            err = (res.stderr or b"").decode("utf-8", "replace").strip()[-160:]
                    except Exception as exc:  # noqa: BLE001
                        err = str(exc)
                    GLib.idle_add(_done, ok, err)

                threading.Thread(target=_worker, daemon=True, name="ocr-install").start()

            ocr_install_btn.connect("clicked", _on_ocr_install)
            ocr_row.add_suffix(ocr_install_btn)
        ocr_row.add_suffix(ocr_recorder)
        ocr_clear = self._suffix_icon_button("edit-clear-symbolic", "Disable hotkey")
        ocr_clear.connect("clicked", lambda *_: ocr_recorder.set_value(""))
        ocr_row.add_suffix(ocr_clear)
        group.add(ocr_row)

        # Auto-popup on selection (switch row)
        sel_row = self._switch_row(
            "Auto-popup on selection",
            "Show the popup automatically whenever you highlight text in any app.",
            self._settings.get("show_on_selection"),
        )
        sel_row.connect("notify::active", self._on_switch, "show_on_selection")
        group.add(sel_row)

        # Double-click in an empty editable field shows the edit menu
        # (Paste / Select all / Backspace). PopClip's text-field gesture.
        dbl_row = self._switch_row(
            "Modifier+double-click for the edit menu",
            "Hold the modifier below and double-click in a text field for "
            "Paste / Select all / Backspace. Watches mouse clicks locally; "
            "nothing is logged. On Wayland it reads /dev/input, so your user "
            "needs to be in the 'input' group.",
            self._settings.get("double_click_popup_enabled", False),
        )
        # Live-apply happens through the settings on_changed callback in
        # main.py - no separate plumbing needed here.
        dbl_row.connect("notify::active", self._on_switch, "double_click_popup_enabled")
        group.add(dbl_row)

        mod_row = self._combo_row(
            "Modifier key",
            "The key to hold while double-clicking.",
            [
                ("ctrl", "Ctrl"),
                ("shift", "Shift"),
                ("alt", "Alt"),
                ("super", "Super (Windows key)"),
            ],
            (self._settings.get("double_click_modifier") or "ctrl").lower(),
            lambda v: self._save_key("double_click_modifier", v or "ctrl"),
        )
        # Sensitive only when the toggle above is on, so it's obvious
        # the picker is doing nothing while the feature itself is off.
        mod_row.set_sensitive(dbl_row.get_active())
        dbl_row.connect("notify::active", lambda s, _p: mod_row.set_sensitive(s.get_active()))
        group.add(mod_row)

        # Force-all-plugins modifier. Held while making a selection,
        # the popup skips the classifier's content-type filter and
        # shows every installed plugin. Useful when a selection is
        # misclassified (e.g. "Channel | Title" tagged as command,
        # hiding the Send-to-AI buttons).
        # Keep the dropdown labels SHORT - the per-key caveats live in the
        # subtitle. Long option labels blow up the combo's natural width,
        # which steals horizontal space from the wrapping subtitle and
        # squeezes it into a tall narrow ribbon.
        force_row = self._combo_row(
            "Force all plugins modifier",
            "Hold this key while selecting to show every action, not just "
            "the ones that fit the text. Each key clashes with something: "
            "Alt/Super often move windows, Ctrl and Shift change selection.",
            [
                ("", "None (off)"),
                ("ctrl", "Ctrl"),
                ("alt", "Alt"),
                ("super", "Super"),
                ("shift", "Shift"),
            ],
            # "None" is the empty-string id; map None/empty back to it.
            (self._settings.get("popup_force_all_modifier") or "").lower(),
            lambda v: self._save_key("popup_force_all_modifier", v or ""),
        )
        # The popup reads held modifiers through Xlib, which the Wayland
        # backend never opens, so the key is never seen there.
        if not _force_all_supported():
            force_row.set_sensitive(False)
            force_row.set_subtitle(
                "Only works in an X11 session for now: on Wayland vinoPop "
                "can't see which keys are held when the popup opens."
            )
        group.add(force_row)

        # Autostart at login (switch row) - driven by ~/.config/autostart
        # rather than a settings.json key, since the .desktop file is what
        # the DE actually reads. We just toggle the file from here.
        auto_row = self._switch_row(
            "Start at login",
            "Launch vinoPop automatically when you sign in.",
            False,
        )
        try:
            import autostart

            auto_row.set_active(autostart.is_enabled())

            def _on_auto_toggle(sw, _p):
                import autostart as _as

                ok = _as.set_enabled(sw.get_active())
                if not ok:
                    # Revert the visual if writing failed
                    sw.set_active(not sw.get_active())

            auto_row.connect("notify::active", _on_auto_toggle)
        except Exception:
            auto_row.set_sensitive(False)
        group.add(auto_row)

        return group

    def _build_snippets_clipboard_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Snippets &amp; Clipboard")
        group.set_description(
            "The picker that remembers what you've copied and the snippets "
            "you save for reuse. Most of vinoPop lives here."
        )

        # Clipboard plugin master toggle. When off, the background
        # selection-watcher thread is not started, the picker hotkey
        # doesn't bind, and the popup button is hidden.
        clip_master_row = self._switch_row(
            "Clipboard history",
            "Remember things you've recently copied so you can paste them "
            "later. Turn off to stop vinoPop from watching the clipboard.",
            self._settings.get("clipboard_history_enabled", True),
        )
        clip_master_row.connect("notify::active", self._on_switch, "clipboard_history_enabled")
        group.add(clip_master_row)
        clip_on = clip_master_row.get_active()

        # Clipboard-picker hotkey row -- only meaningful if the master
        # switch above is on, but we keep it visible so the binding can
        # be configured ahead of enabling.
        clip_row = _plain(Adw.ActionRow())
        clip_row.set_title("Optional clipboard shortcut")
        clip_row.set_subtitle(
            "Opens the clipboard picker at the cursor. Optional: the popup "
            "hotkey (Activation page) opens it too when nothing is highlighted."
        )
        clip_recorder = HotkeyRecorder(
            self._settings.get("clipboard_hotkey") or "",
            on_changed=lambda v: self._save_key("clipboard_hotkey", v),
        )
        clip_row.add_suffix(clip_recorder)
        clip_clear = self._suffix_icon_button("edit-clear-symbolic", "Disable")
        clip_clear.connect("clicked", lambda *_: clip_recorder.set_value(""))
        clip_row.add_suffix(clip_clear)
        clip_row.set_sensitive(clip_on)
        # Re-flow sensitivity when the master toggles
        clip_master_row.connect(
            "notify::active", lambda s, _p: clip_row.set_sensitive(s.get_active())
        )
        group.add(clip_row)

        # Snippet triggers (text expansion). Off by default - turning it
        # on means vinoPop attaches to the X11 RECORD extension and sees
        # every keystroke on the desktop. Be honest about that in the
        # subtitle so the user can decide.
        trigger_row = self._switch_row(
            "Snippet triggers (text expansion)",
            "When ON, typing a snippet's trigger code (e.g. ';email') followed "
            "by space or tab auto-expands it. Requires vinoPop to watch "
            "global keystrokes locally - keys are matched against your "
            "snippet triggers only, never logged or sent anywhere.",
            self._settings.get("snippet_triggers_enabled", False),
        )
        trigger_row.connect("notify::active", self._on_switch, "snippet_triggers_enabled")
        trigger_row.connect("notify::active", lambda *_: self._apply_trigger_toggle())
        trigger_row.set_sensitive(clip_on)
        clip_master_row.connect(
            "notify::active", lambda s, _p: trigger_row.set_sensitive(s.get_active())
        )
        group.add(trigger_row)

        # Per-app/site blocklist for trigger expansion - opens a modal
        # editor instead of taking permanent room in the page.
        tblock_row = _plain(Adw.ActionRow())
        tblock_row.set_activatable(True)

        def _refresh_tblock_row() -> None:
            patterns = list(self._settings.get("trigger_blocklist_patterns") or [])
            tblock_row.set_title("Don't expand triggers in these apps or sites")
            count = len(patterns)
            if count == 0:
                tblock_row.set_subtitle(
                    "Useful for password fields, terminals, and "
                    "security-sensitive sites. Click to add patterns."
                )
            else:
                examples = ", ".join(patterns[:3])
                if count > 3:
                    examples += f", +{count - 3} more"
                tblock_row.set_subtitle(
                    f"{count} pattern{'s' if count != 1 else ''} blocked: "
                    f"{examples}. Click to edit."
                )

        _refresh_tblock_row()

        def _on_tblock_saved(new_text: str | None) -> None:
            if new_text is None:
                return
            new_patterns = [line.strip() for line in new_text.splitlines() if line.strip()]
            self._save_key("trigger_blocklist_patterns", new_patterns)
            _refresh_tblock_row()

        def _on_tblock_edit(_row) -> None:
            patterns = list(self._settings.get("trigger_blocklist_patterns") or [])
            initial = "\n".join(patterns)
            _open_text_editor_modal(
                parent=self._window,
                title="Edit trigger blocklist",
                subtitle=(
                    "Apps and sites where snippet triggers should NEVER "
                    "auto-expand. Useful for password managers, terminals, "
                    "and banking sites - places where typing 'rraak' is "
                    "supposed to stay 'rraak'."
                ),
                initial_text=initial,
                placeholder_text=(
                    "Type one app or site per line. Each line is matched "
                    "(case-insensitive) against the focused window's "
                    "title and class - if any line is a substring of "
                    "either, expansion is skipped for that window.\n\n"
                    "Try things like:\n"
                    "  KeePassXC        - the password manager\n"
                    "  gnome-terminal   - any terminal you use\n"
                    "  bank.no          - matches whatever your bank's\n"
                    "                     site name shows in the title\n"
                    "  1Password\n"
                    "  Bitwarden"
                ),
                on_done=_on_tblock_saved,
            )

        tblock_row.connect("activated", _on_tblock_edit)
        tblock_row.add_suffix(self._edit_suffix())
        group.add(tblock_row)

        # Shared snippet variables: a key=value editor that backs the
        # {var:NAME} placeholder.
        vars_row = _plain(Adw.ActionRow())
        vars_row.set_activatable(True)

        def _parse_vars_text(raw: str) -> dict:
            out: dict[str, str] = {}
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                name, _, value = line.partition("=")
                name = name.strip()
                if not name:
                    continue
                out[name] = value.strip()
            return out

        def _refresh_vars_row() -> None:
            existing = self._settings.get("snippet_variables") or {}
            if not isinstance(existing, dict):
                existing = {}
            vars_row.set_title("Snippet variables")
            count = len(existing)
            if count == 0:
                vars_row.set_subtitle(
                    "Define your email, signature, phone, etc. once and "
                    "use {var:name} across snippets. Click to add."
                )
            else:
                names = sorted(existing.keys())
                preview = ", ".join(f"{{var:{n}}}" for n in names[:3])
                if count > 3:
                    preview += f", +{count - 3} more"
                vars_row.set_subtitle(
                    f"{count} variable{'s' if count != 1 else ''} defined: "
                    f"{preview}. Click to edit."
                )

        _refresh_vars_row()

        def _on_vars_saved(new_text: str | None) -> None:
            if new_text is None:
                return
            self._save_key("snippet_variables", _parse_vars_text(new_text))
            _refresh_vars_row()

        def _on_vars_edit(_row) -> None:
            existing = self._settings.get("snippet_variables") or {}
            if isinstance(existing, dict):
                initial = "\n".join(f"{k} = {v}" for k, v in sorted(existing.items()))
            else:
                initial = ""
            _open_text_editor_modal(
                parent=self._window,
                title="Edit snippet variables",
                subtitle=(
                    "Reusable values that any snippet can pull in. "
                    "Define a value once here, then write {var:name} "
                    "in any snippet to drop it in. Change the value "
                    "later and every snippet that uses it updates "
                    "automatically."
                ),
                initial_text=initial,
                placeholder_text=(
                    "Type one variable per line in the form\n"
                    "    name = value\n\n"
                    "Then write {var:name} in your snippets to pull the "
                    "value in. Common ones to start with:\n\n"
                    "  email     = you@example.com\n"
                    "  signature = Best,\\nAlex\n"
                    "  phone     = +47 555 1234\n"
                    "  company   = Acme AS\n"
                    "  address   = Karl Johans gate 1, 0154 Oslo\n\n"
                    "Names can be anything you'd recognise later -\n"
                    "letters, digits, underscores, hyphens."
                ),
                on_done=_on_vars_saved,
            )

        vars_row.connect("activated", _on_vars_edit)
        vars_row.add_suffix(self._edit_suffix())
        group.add(vars_row)

        # Shell extension {shell:CMD} in snippets. Off by default - same
        # threat model as enabling macros: a hostile imported snippet
        # with a {shell:rm -rf ~} runs immediately when expanded.
        shell_row = self._switch_row(
            "Shell expansion in snippets",
            "When ON, snippets containing {shell:CMD} run that command "
            "in bash and paste the output. Useful for {shell:git branch} "
            "or {shell:date -u}. Off by default - an imported snippet "
            "with a hostile command would execute immediately.",
            self._settings.get("snippet_shell_enabled", False),
        )
        shell_row.connect("notify::active", self._on_switch, "snippet_shell_enabled")
        shell_row.set_sensitive(clip_on)
        clip_master_row.connect(
            "notify::active", lambda s, _p: shell_row.set_sensitive(s.get_active())
        )
        group.add(shell_row)

        return group

    def _build_timing_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Timing")
        group.set_description("Seconds for popup show / hide behaviour.")

        debounce_row = self._seconds_spin_row(
            "Delay before popup appears",
            "Seconds to wait after you finish selecting - keeps the "
            "popup from flashing while you're still dragging.",
            "selection_debounce_ms",
            0.0,
            2.0,
            0.05,
        )
        initial_row = self._seconds_spin_row(
            "Auto-hide before mouse arrives",
            "Seconds the popup waits if you never move the mouse to it.",
            "auto_hide_initial_ms",
            0.5,
            30.0,
            0.5,
        )
        leave_row = self._seconds_spin_row(
            "Auto-hide after mouse leaves",
            "Seconds the popup stays visible after you move the cursor away from it.",
            "auto_hide_leave_ms",
            0.2,
            20.0,
            0.2,
        )
        group.add(debounce_row)
        group.add(initial_row)
        group.add(leave_row)
        return group

    def _build_filter_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Filters")
        group.set_description("When the popup should stay hidden.")

        # Minimum-length filter is a two-part control: a toggle (default
        # off, matches PopClip) gates whether the auto-popup ignores
        # short selections at all, and a spinner under it picks the
        # threshold. The spinner only goes 'live' when the toggle is on.
        min_toggle_row = self._switch_row(
            "Skip short auto-popup selections",
            "The auto-popup ignores selections shorter than the number "
            "below. The hotkey always works.",
            self._settings.get("min_selection_length_enabled"),
        )
        min_toggle_row.connect("notify::active", self._on_switch, "min_selection_length_enabled")
        group.add(min_toggle_row)

        min_row = self._spin_row(
            "Minimum characters",
            "Selections shorter than this are skipped by the "
            "auto-popup (only when the switch above is on).",
            "min_selection_length",
            1,
            100,
            1,
        )
        min_row.set_sensitive(min_toggle_row.get_active())
        min_toggle_row.connect(
            "notify::active", lambda s, _p: min_row.set_sensitive(s.get_active())
        )
        group.add(min_row)

        # Blocklist: one substring per line. Matches against the active
        # window's title and WM_CLASS (case-insensitive) at popup time.
        # Stored in settings.json as a list of strings; the UI gives a
        # plain multi-line text area because per-row Adw.ActionRows would
        # be overkill for a free-form list.
        block_row = _plain(Adw.ActionRow())
        block_row.set_activatable(True)

        def _refresh_block_row() -> None:
            patterns = list(self._settings.get("blocklist_patterns") or [])
            block_row.set_title("Don't show in these apps or pages")
            count = len(patterns)
            if count == 0:
                block_row.set_subtitle(
                    "Hide the popup in password managers, banking sites, "
                    "and anywhere else you'd rather not see it. Click to add."
                )
            else:
                examples = ", ".join(patterns[:3])
                if count > 3:
                    examples += f", +{count - 3} more"
                block_row.set_subtitle(
                    f"{count} pattern{'s' if count != 1 else ''} blocked: "
                    f"{examples}. Click to edit."
                )

        _refresh_block_row()

        def _on_block_saved(new_text: str | None) -> None:
            if new_text is None:
                return
            new_patterns = [line.strip() for line in new_text.splitlines() if line.strip()]
            self._save_key("blocklist_patterns", new_patterns)
            _refresh_block_row()

        def _on_block_edit(_row) -> None:
            patterns = list(self._settings.get("blocklist_patterns") or [])
            initial = "\n".join(patterns)
            _open_text_editor_modal(
                parent=self._window,
                title="Edit popup blocklist",
                subtitle=(
                    "Apps and pages where the popup should never appear. "
                    "Useful for password managers, banking sites, and any "
                    "other context where a floating menu would be in "
                    "the way."
                ),
                initial_text=initial,
                placeholder_text=(
                    "Type one app or site per line. Each line is matched "
                    "(case-insensitive) against the focused window's "
                    "title and class - if any line is a substring of "
                    "either, the popup stays hidden for that window.\n\n"
                    "Try things like:\n"
                    "  KeePassXC\n"
                    "  1Password\n"
                    "  Bitwarden\n"
                    "  Mozilla Firefox - DNB     - just the bank tab\n"
                    "  - kjøp                    - any browser tab whose\n"
                    "                              title contains this"
                ),
                on_done=_on_block_saved,
            )

        block_row.connect("activated", _on_block_edit)
        block_row.add_suffix(self._edit_suffix())
        group.add(block_row)
        return group

    def _build_search_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Web search")
        group.set_description(
            GLib.markup_escape_text(
                'Which engine the popup\'s "Search the web" button uses. '
                "Google also surfaces Gemini answers; DuckDuckGo and Brave "
                "are private; Kagi requires an account."
            )
        )

        # Pull catalog from actions.py so the two stay in sync.
        try:
            from actions import SEARCH_ENGINES
        except Exception:
            SEARCH_ENGINES = {"google": ("Google", "https://www.google.com/search?q={q}")}

        current = (self._settings.get("search_engine") or "google").strip().lower()
        if current not in SEARCH_ENGINES and current != "custom":
            current = "google"
        options = [(key, label) for key, (label, _tmpl) in SEARCH_ENGINES.items()]
        options.append(("custom", "Custom URL…"))

        # Custom-URL row (sensitive only when "Custom URL…" is picked).
        # Adw.EntryRow has no subtitle or placeholder, so the "{q}" rule
        # lives in the title and the example URL in the tooltip.
        custom_row = _plain(Adw.EntryRow())
        custom_row.set_title("Custom search URL ({q} = the selection)")
        custom_row.set_tooltip_text(
            "Must contain {q} - replaced by the selection.\n"
            "Example: https://searx.example.com/search?q={q}"
        )
        custom_row.set_text(self._settings.get("search_engine_custom_url") or "")

        def _sync_custom_visibility(engine_id: str | None) -> None:
            custom_row.set_sensitive(engine_id == "custom")

        def _on_engine_changed(new_id: str) -> None:
            self._save_key("search_engine", new_id or "google")
            _sync_custom_visibility(new_id)

        # Picker row
        engine_row = self._combo_row(
            "Search engine",
            "The button on the popup will open this site.",
            options,
            current,
            _on_engine_changed,
        )
        group.add(engine_row)
        group.add(custom_row)
        _sync_custom_visibility(current)

        def _on_custom_changed(entry: Adw.EntryRow) -> None:
            self._save_key("search_engine_custom_url", entry.get_text().strip())

        custom_row.connect("changed", _on_custom_changed)
        return group

    def _build_ai_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Offline AI")
        try:
            from vino_ai import models

            model = models.llm_model()
            size = f"{model.label}, {model.size}"
        except Exception:
            size = "about 4 GB"
        prefetch_row = self._switch_row(
            "Download the AI model in the background",
            f"One-time download ({size}) when vinoPop starts, so the first AI "
            "action doesn't wait for it. Off: it downloads on the first AI action.",
            self._settings.get("ai_prefetch_model", False),
        )
        prefetch_row.connect("notify::active", self._on_prefetch_switch)
        group.add(prefetch_row)
        return group

    def _on_prefetch_switch(self, switch: Adw.SwitchRow, _param) -> None:
        # Flipping it is an answer, so a stored True now counts as a yes
        # (see vino_ai.models.prefetch_consented).
        self._settings.set("ai_prefetch_asked", True)
        self._save_key("ai_prefetch_model", bool(switch.get_active()))

    def _build_privacy_group(self) -> Adw.PreferencesGroup:
        from vino_ai import telemetry

        group = Adw.PreferencesGroup()
        group.set_title("Privacy")
        # Shows the consent file, not just the setting: a file written by
        # Intel's opt_in_out wins until the user flips this.
        row = self._switch_row(
            "Share anonymous OpenVINO usage stats with Intel",
            "OpenVINO's own telemetry, sent to Intel through Google Analytics "
            "when the AI actions load. Usage counts only, never your selected "
            "text. Saved in ~/intel/openvino_telemetry.",
            telemetry.enabled(),
        )

        def _on_toggled(sw: Adw.SwitchRow, _pspec) -> None:
            if telemetry.set_enabled(bool(sw.get_active())) and self._on_changed:
                self._on_changed()

        row.connect("notify::active", _on_toggled)
        group.add(row)
        return group

    def _build_advanced_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Advanced")
        group.set_description(
            "Experimental switches. Leave alone unless you know what you're doing."
        )

        poll_row = self._switch_row(
            "Trigger hotkey on first press",
            "Turn on if the popup only appears after pressing the hotkey "
            "2-3 times (common on Cinnamon).",
            self._settings.get("hotkey_use_polling", False),
        )
        poll_row.connect("notify::active", self._on_switch, "hotkey_use_polling")
        group.add(poll_row)

        # The "Smarter editable detection (experimental)" AT-SPI toggle
        # used to live here. It was removed 2026-05-29 because the
        # modifier+double-click path now covers its main use case
        # (explicit popup-in-text-field) and AT-SPI was correlated with
        # a Cinnamon desktop-panel crash. The `editable_atspi_listener_
        # enabled` setting still exists in settings.py DEFAULTS for the
        # rare power user who wants to opt back in via settings.json.

        # ---- Reset settings -------------------------------------------
        # Destructive: clears every key in settings.json that has a
        # built-in default. Plugins, recipes, snippets, and clipboard
        # history are kept (those are user data, not preferences). A
        # confirmation dialog stands in the way so this can't be
        # triggered by an accidental Enter.
        reset_row = _plain(Adw.ActionRow())
        reset_row.set_title("Reset settings to defaults")
        reset_row.set_subtitle(
            "Restores every preference (hotkeys, timing, popup look, "
            "AI services, etc.) to its factory default. Your snippets, "
            "clipboard history, installed plugins, and custom buttons "
            "are kept - this only resets preferences."
        )
        reset_btn = Gtk.Button(label="Reset…")
        reset_btn.set_valign(Gtk.Align.CENTER)
        reset_btn.add_css_class("destructive-action")
        reset_btn.connect("clicked", self._on_reset_to_defaults)
        reset_row.add_suffix(reset_btn)
        group.add(reset_row)
        return group

    def _on_reset_to_defaults(self, _btn: Gtk.Button) -> None:
        confirm = Adw.AlertDialog.new(
            "Reset all preferences to defaults?",
            "Every setting in this window will go back to how "
            "vinoPop ships out of the box.\n\n"
            "Kept: your snippets, clipboard history, installed "
            "plugins, custom buttons.\n"
            "Reset: hotkeys, timing, popup look, AI services, "
            "blocklists, search engine, terminal behaviour.\n\n"
            "There's no undo.",
        )
        confirm.add_response("cancel", "Cancel")
        confirm.add_response("reset", "Reset")
        confirm.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        confirm.set_default_response("cancel")
        confirm.set_close_response("cancel")
        confirm.connect("response", self._on_reset_response)
        confirm.present(self._window)

    def _on_reset_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response != "reset":
            return
        from settings import DEFAULTS
        from vino_ai import telemetry

        # A consent file vinoPop wrote goes back to the default (off) too;
        # one from Intel's opt_in_out stays the user's call.
        owned = bool(self._settings.get(telemetry.OWNED_SETTING, False))
        try:
            self._settings.reset_to_defaults()
        except Exception as exc:
            err = Adw.AlertDialog.new("Couldn't reset settings", f"{exc}")
            err.add_response("close", "Close")
            err.present(self._window)
            return
        # Theme, button size and icon style are applied here rather than
        # by the daemon, same as when they're changed one at a time.
        _apply_theme(self._settings.get("theme") or "dark")
        if owned:
            telemetry.set_enabled(bool(DEFAULTS[telemetry.SETTING]))
        # Trigger the daemon-side reload so hotkeys / watchers / etc.
        # pick up the new state without restart, then rebuild this
        # window so its widgets reflect what's on disk.
        if self._on_changed is not None:
            try:
                self._on_changed()
            except Exception:
                pass

        # Rebuild from idle: the alert dialog is still finishing its
        # response emission inside the window we're about to destroy.
        def _rebuild() -> bool:
            old = self._window
            if old is not None:
                self._window = None
                old.destroy()
            self.show()
            return False

        GLib.idle_add(_rebuild)

    # ---- debug page ----------------------------------------------------------

    def _build_diagnostics_group(self) -> Adw.PreferencesGroup:
        """`vinopop --doctor`, run in a child process: it imports OpenVINO
        and probes devices, which would stall this window."""
        group = Adw.PreferencesGroup()
        group.set_title("Diagnostics")
        group.set_description("What the on-device AI needs from this machine (vinopop --doctor).")
        buttons = Gtk.Box(spacing=6)
        rerun = self._suffix_icon_button("view-refresh-symbolic", "Run the checks again")
        copy = self._suffix_icon_button("edit-copy-symbolic", "Copy a report for a bug report")
        buttons.append(rerun)
        buttons.append(copy)
        group.set_header_suffix(buttons)

        status_icons = {
            "ok": ("emblem-ok-symbolic", "success"),
            "warn": ("dialog-warning-symbolic", "warning"),
            "FAIL": ("dialog-error-symbolic", "error"),
        }
        rows: list[Gtk.Widget] = []
        report = {"text": ""}

        def _set_rows(checks: list[dict]) -> None:
            for row in rows:
                group.remove(row)
            rows.clear()
            for check in checks:
                row = _plain(Adw.ActionRow())
                row.set_title(check.get("label", ""))
                row.set_subtitle(check.get("detail", ""))
                row.set_subtitle_selectable(True)
                icon, css = status_icons.get(
                    check.get("status", ""), ("dialog-question-symbolic", "dim-label")
                )
                img = Gtk.Image.new_from_icon_name(icon)
                img.add_css_class(css)
                row.add_prefix(img)
                group.add(row)
                rows.append(row)

        def _done(checks: list[dict]) -> bool:
            _set_rows(checks)
            width = max((len(c.get("label", "")) for c in checks), default=0)
            report["text"] = "\n".join(
                f"[{c.get('status', ''):>4}] {c.get('label', ''):<{width}}  {c.get('detail', '')}"
                for c in checks
            )
            rerun.set_sensitive(True)
            return False

        def _run() -> None:
            rerun.set_sensitive(False)
            _set_rows([{"status": "", "label": "Running checks…"}])

            def _worker() -> None:
                checks = _doctor_checks()
                GLib.idle_add(_done, checks)

            threading.Thread(target=_worker, daemon=True, name="doctor").start()

        def _copy(*_) -> None:
            text = "\n".join(
                [f"{label}: {value}" for label, value in _session_info()] + ["", report["text"]]
            )
            Gdk.Display.get_default().get_clipboard().set(text.strip() + "\n")
            copy.set_tooltip_text("Copied")

        rerun.connect("clicked", lambda *_: _run())
        copy.connect("clicked", _copy)
        _set_rows([{"status": "", "label": "Checks run when this page opens"}])
        self._debug_refresh = _run
        return group

    def _build_session_group(self) -> Adw.PreferencesGroup:
        from xdg_paths import LOG_FILE

        group = Adw.PreferencesGroup()
        group.set_title("This session")
        for label, value in _session_info():
            row = _plain(Adw.ActionRow())
            row.set_title(label)
            row.set_subtitle(value)
            row.set_subtitle_selectable(True)
            row.add_css_class("property")
            group.add(row)

        log_row = _plain(Adw.ActionRow())
        log_row.set_title("Log file")
        log_row.set_subtitle(str(LOG_FILE))
        log_row.set_subtitle_selectable(True)
        open_log = Gtk.Button(label="Open")
        open_log.set_valign(Gtk.Align.CENTER)
        open_log.connect("clicked", lambda *_: _open_path(LOG_FILE))
        folder = self._suffix_icon_button("folder-open-symbolic", "Open the log folder")
        folder.connect("clicked", lambda *_: _open_path(LOG_FILE.parent))
        log_row.add_suffix(open_log)
        log_row.add_suffix(folder)
        group.add(log_row)
        return group

    def _build_troubleshoot_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Troubleshooting")

        def _button_row(title: str, subtitle: str, label: str, on_click) -> Adw.ActionRow:
            row = _plain(Adw.ActionRow())
            row.set_title(title)
            row.set_subtitle(subtitle)
            btn = Gtk.Button(label=label)
            btn.set_valign(Gtk.Align.CENTER)
            btn.connect("clicked", on_click)
            row.add_suffix(btn)
            row.set_activatable_widget(btn)
            group.add(row)
            return row

        restart = _button_row(
            "Restart vinoPop",
            "Try this first if the popup, hotkeys or tray stop responding.",
            "Restart",
            lambda *_: self._on_restart() if self._on_restart else None,
        )
        restart.set_sensitive(self._on_restart is not None)

        def _setup(*_) -> None:
            from onboarding import OnboardingWindow

            win = OnboardingWindow(self._settings, on_open_plugins=self._on_open_plugins)
            if self._window is not None:
                win.set_transient_for(self._window)
            win.present()

        _button_row(
            "Run the setup again",
            "Walk through the first-run setup: hotkeys, plugins and permissions.",
            "Open",
            _setup,
        )

        from vino_ai import failures

        skipped = sorted(failures.all_records())
        if skipped:

            def _forget(btn: Gtk.Button) -> None:
                for dev in skipped:
                    failures.forget(dev)
                btn.set_sensitive(False)
                btn.set_label("Forgotten")

            _button_row(
                "Retry AI devices that failed",
                f"{', '.join(skipped)} failed to load the model before and are skipped. "
                "Clear that so the next AI action tries them again.",
                "Retry",
                _forget,
            )
        return group

    def _build_terminal_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Terminal commands")

        term_row = self._switch_row(
            "Keep terminal open after running",
            "After running a command, leaves the terminal window open "
            "so you can read the output. Otherwise it closes right away.",
            self._settings.get("terminal_keep_open", True),
        )
        term_row.connect("notify::active", self._on_switch, "terminal_keep_open")
        group.add(term_row)
        return group

    # ---- helpers -------------------------------------------------------------

    def _spin_row(
        self,
        title: str,
        subtitle: str,
        key: str,
        lo: int,
        hi: int,
        step: int,
    ) -> Adw.SpinRow:
        row = _plain(Adw.SpinRow.new_with_range(lo, hi, step))
        row.set_title(title)
        row.set_subtitle(subtitle)
        row.set_value(self._settings.get_int(key))
        row.connect("notify::value", self._on_spin, key)
        return row

    def _seconds_spin_row(
        self,
        title: str,
        subtitle: str,
        key: str,
        lo_s: float,
        hi_s: float,
        step_s: float,
    ) -> Adw.SpinRow:
        """Spinner that DISPLAYS seconds but PERSISTS milliseconds, so
        the JSON schema (everything in *_ms) stays unchanged while users
        see and edit numbers that are actually meaningful at a glance."""
        row = _plain(Adw.SpinRow.new_with_range(lo_s, hi_s, step_s))
        row.set_title(title)
        row.set_subtitle(subtitle)
        row.set_digits(2 if step_s < 0.1 else 1)
        # Unit hint via tooltip - the row already shows the title and a
        # subtitle, no room for an inline suffix.
        row.set_tooltip_text("Value in seconds")
        row.set_value(self._settings.get_int(key) / 1000.0)

        def _on_seconds_changed(sb: Adw.SpinRow, _pspec) -> None:
            self._save_key(key, int(round(sb.get_value() * 1000)))

        row.connect("notify::value", _on_seconds_changed)
        return row

    def _on_switch(self, switch: Adw.SwitchRow, _param, key: str) -> None:
        self._save_key(key, bool(switch.get_active()))

    def _on_spin(self, spin: Adw.SpinRow, _param, key: str) -> None:
        self._save_key(key, int(spin.get_value()))

    def _save_key(self, key: str, value) -> None:
        self._settings.set(key, value)
        self._settings.save()
        if self._on_changed:
            self._on_changed()

    def _apply_trigger_toggle(self) -> None:
        """Start or stop the snippet-trigger XRecord watcher live, so the
        setting takes effect without a daemon restart. Looks up the
        already-loaded clipboard_history user module - that's where the
        watcher lives."""
        try:
            import sys

            mod = sys.modules.get("vinopop_user_clipboard_history") or sys.modules.get(
                "clipboard_history"
            )
            if mod is not None and hasattr(mod, "_maybe_start_trigger_watcher"):
                mod._maybe_start_trigger_watcher()
        except Exception as exc:
            print(f"[settings] could not apply trigger toggle: {exc}")


if __name__ == "__main__":
    _dlg = SettingsDialog()
    _dlg.show()
    gtk4_app.run_window(_dlg._window)
