"""vinoPop's preferences window: one Adw.PreferencesPage per topic in a
sidebar (ui/sidebar_window). Edits save as they are made; there is no Save
button."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from core.settings import get_settings  # noqa: E402
from ui import gtk4_app, settings_text, sidebar_window  # noqa: E402
from ui.sidebar_window import force_to_front as _force_to_front  # noqa: E402
from ui.sidebar_window import plain_text_row as _plain  # noqa: E402

if TYPE_CHECKING:
    from ui.plugin_manager import PluginManagerDialog


def _gb(size: int) -> str:
    if size >= 10**9:
        return f"{size / 1e9:.1f} GB"
    return f"{size / 1e6:.0f} MB"


def _open_text_editor_modal(
    parent: Gtk.Window | None,
    title: str,
    subtitle: str,
    initial_text: str,
    placeholder_text: str,
    on_done: Callable[[str | None], None],
) -> None:
    """A modal multi-line editor with Save / Cancel. Calls on_done(new_text)
    on Save and on_done(None) on Cancel, Escape or close."""
    dlg = Adw.Dialog()
    dlg.set_title(title)
    dlg.set_content_width(580)
    dlg.set_content_height(420)

    header = Adw.HeaderBar()
    header.set_show_end_title_buttons(False)
    header.set_show_start_title_buttons(False)
    cancel_btn = Gtk.Button(label="Cancel")
    save_btn = Gtk.Button(label="Save")
    save_btn.add_css_class("suggested-action")
    header.pack_start(cancel_btn)
    header.pack_end(save_btn)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    content.set_margin_top(6)
    content.set_margin_bottom(12)
    content.set_margin_start(12)
    content.set_margin_end(12)

    sub_lbl = Gtk.Label(xalign=0)
    sub_lbl.set_wrap(True)
    sub_lbl.set_text(subtitle)
    content.append(sub_lbl)

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.set_hexpand(True)
    scroll.set_vexpand(True)
    scroll.add_css_class("card")
    view = Gtk.TextView()
    view.set_wrap_mode(Gtk.WrapMode.NONE)
    view.set_monospace(True)
    view.add_css_class("lp-cmd-edit")
    view.update_property([Gtk.AccessibleProperty.LABEL], [title])
    view.update_property([Gtk.AccessibleProperty.DESCRIPTION], [subtitle])
    if initial_text:
        view.get_buffer().set_text(initial_text)
    _attach_textarea_placeholder(view, placeholder_text)
    scroll.set_child(view)
    content.append(scroll)

    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(header)
    toolbar.set_content(content)
    dlg.set_child(toolbar)
    dlg.set_default_widget(save_btn)
    dlg.set_focus(view)

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
        dlg.force_close()
        on_done(result)

    cancel_btn.connect("clicked", lambda *_: _finish(False))
    save_btn.connect("clicked", lambda *_: _finish(True))
    gtk4_app.present_dialog(dlg, parent, lambda *_: _finish(False))


def _attach_textarea_placeholder(view: Gtk.TextView, placeholder_text: str) -> None:
    """Gtk.Entry's placeholder text for a TextView: a dimmed hint while the
    buffer is blank. A key that types a character clears it, focus alone
    doesn't, and leaving the field blank brings it back (pinned by
    `tests/test_windows_settings_rows.py::test_a_typed_character_clears_the_placeholder`,
    `tests/test_windows_settings_rows.py::test_a_modifier_or_arrow_key_keeps_the_placeholder`,
    `tests/test_windows_settings_rows.py::test_leaving_the_field_blank_restores_the_placeholder`).
    While `view._placeholder_active`, the buffer holds no value: savers
    must not store the hint."""
    buf = view.get_buffer()
    tag = buf.create_tag(
        f"lp-placeholder-{id(view)}",
        style=Pango.Style.ITALIC,
    )
    view._placeholder_active = False

    def _set_placeholder() -> None:
        # The view's own colour, dimmed: a fixed grey fails contrast in light or dark
        from ui.theme import PLACEHOLDER_ALPHA

        color = view.get_color()
        color.alpha = PLACEHOLDER_ALPHA
        tag.set_property("foreground-rgba", color)
        buf.set_text(placeholder_text)
        s, e = buf.get_bounds()
        buf.apply_tag(tag, s, e)
        view._placeholder_active = True

    s, e = buf.get_bounds()
    if not buf.get_text(s, e, True):
        _set_placeholder()

    def _clear_placeholder() -> None:
        if view._placeholder_active:
            buf.set_text("")
            view._placeholder_active = False

    def _on_key_press(_ctrl, keyval, _keycode, _state):
        types_a_character = bool(Gdk.keyval_to_unicode(keyval))
        if view._placeholder_active and types_a_character:
            _clear_placeholder()
        return False

    def _on_focus_out(_ctrl):
        s, e = buf.get_bounds()
        if not buf.get_text(s, e, True).strip():
            _set_placeholder()

    # Capture phase, so the typed character lands in the emptied buffer
    keys = Gtk.EventControllerKey()
    keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    keys.connect("key-pressed", _on_key_press)
    view.add_controller(keys)
    focus = Gtk.EventControllerFocus()
    focus.connect("leave", _on_focus_out)
    view.add_controller(focus)


def _doctor_checks() -> list[dict]:
    """Run `vinopop --doctor --json` and return its checks."""
    import json
    import sys
    from pathlib import Path

    launcher = Path(__file__).resolve().parent.parent / "vinopop"
    try:
        res = subprocess.run(
            [sys.executable, str(launcher), "--doctor", "--json"],
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
        from core.version import __version__
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
        from ui import theme as _theme

        _theme.install_premium_theme(mode)
    except Exception as exc:
        print(f"[settings] theme reload failed: {exc}")
    try:
        from ui import popup as _popup

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


def _combo_to_accel(combo: str) -> str:
    """'super+shift+y' (the settings.json form) as a GTK accelerator
    ('<Super><Shift>y'), or '' if GTK can't parse it."""
    mods = {
        "ctrl": "<Control>",
        "control": "<Control>",
        "alt": "<Alt>",
        "shift": "<Shift>",
        "super": "<Super>",
        "meta": "<Meta>",
    }
    parts = [p for p in combo.lower().split("+") if p]
    if not parts or any(p not in mods for p in parts[:-1]):
        return ""
    key = parts[-1]
    keyval = _keyval_from_saved_name(key)
    if not keyval:
        return ""
    accel = "".join(mods[p] for p in parts[:-1]) + (Gdk.keyval_name(keyval) or key)
    ok, parsed, _mods = Gtk.accelerator_parse(accel)
    return accel if ok and parsed else ""


def _keyval_from_saved_name(key: str) -> int:
    """The keyval for a lowercased saved key name: GDK's names are
    case-sensitive ("F9", "y"). 0 if there's none."""
    return next(
        (
            v
            for v in (Gdk.keyval_from_name(n) for n in (key, key.capitalize(), key.upper()))
            if v and v != Gdk.KEY_VoidSymbol
        ),
        0,
    )


def _pretty_combo(combo: str) -> str:
    """'super+shift+y' as 'Shift+Super+Y', the way GNOME writes shortcuts."""
    accel = _combo_to_accel(combo)
    if accel:
        ok, keyval, mods = Gtk.accelerator_parse(accel)
        if ok:
            return Gtk.accelerator_get_label(keyval, mods)
    return "+".join(p[:1].upper() + p[1:] for p in combo.split("+") if p)


# Under 16 px the symbolic icons go muddy; over 32 the popup spans the screen
_POPUP_BUTTON_PX = (16, 32)
# Fewer than 4 makes the popup feel broken; past 40 the +N chip does the job
_MAX_POPUP_BUTTONS = (4, 40)

_TRIGGER_BLOCKLIST_EDITOR = {
    "title": "Trigger blocklist",
    "subtitle": "One app or window title per line (case-insensitive substring match).",
    "placeholder_text": "KeePassXC\ngnome-terminal\nbank.example.com",
}
_SNIPPET_VARIABLES_EDITOR = {
    "title": "Snippet variables",
    "subtitle": "One name = value per line. Use {var:name} in any snippet.",
    "placeholder_text": "email = you@example.com\nsignature = Best,\\nAlex\nphone = +1 555 0100",
}
_POPUP_BLOCKLIST_EDITOR = {
    "title": "Popup blocklist",
    "subtitle": "One app or window title per line (case-insensitive substring match).",
    "placeholder_text": "KeePassXC\n1Password\nBitwarden",
}


def _enabled_while(switch_row: Adw.SwitchRow, dependent: Gtk.Widget) -> None:
    """Keep `dependent` usable only while `switch_row` is on."""
    dependent.set_sensitive(switch_row.get_active())
    switch_row.connect("notify::active", lambda s, _p: dependent.set_sensitive(s.get_active()))


def _ocr_support() -> tuple[bool, str]:
    """(usable, what is missing) for screen OCR."""
    try:
        from capture.screen_ocr import is_supported

        supported, reason = is_supported()
        return supported, reason
    except Exception:
        return False, "screen_ocr module not available"


def _ocr_manual_install_hint(reason: str) -> str:
    """What to run by hand where the OCR tools can't be installed in place
    (a read-only /usr, an unknown package manager)."""
    try:
        from vino_ai import install_hint

        hint = f"Install the OCR engine with: {install_hint()}"
    except Exception:
        hint = "Install the OCR engine with: pip install --user -r requirements-ai.txt"
    if "capture tool" in reason:
        try:
            from capture.screen_ocr import install_command

            hint = f"Install a screen-capture tool: {install_command()}"
        except Exception:
            pass
    return hint


def _run_ocr_install(argv: list[str]) -> tuple[bool, str]:
    """(exited 0, the end of stderr or the error) of the install command."""
    try:
        result = subprocess.run(argv, capture_output=True, timeout=600)
        if result.returncode == 0:
            return True, ""
        return False, (result.stderr or b"").decode("utf-8", "replace").strip()[-160:]
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# What the Updates button does on its next click
NEXT_CHECK = "check"
NEXT_INSTALL = "install"
NEXT_RESTART = "restart"
NEXT_PAGE = "page"


class UpdateOffer(NamedTuple):
    subtitle: str
    label: str | None
    next: str
    suggested: bool = False


def _update_offer(
    current: str, tag: str | None, error: str | None, can_upgrade: bool
) -> UpdateOffer:
    """What the Updates row shows after a check. A development build (a
    version past a tag, e.g. "1.3.0-5-gabc123") is never offered the release
    (pinned by `tests/test_windows_settings_update.py::test_a_development_build_is_not_offered_the_release`,
    `tests/test_windows_settings_update.py::test_an_older_release_is_offered_the_update`)."""
    from core import update

    if error is not None:
        return UpdateOffer(error, "Try again", NEXT_CHECK)
    if tag is not None and update.is_newer(current, tag):
        if can_upgrade:
            return UpdateOffer(f"{tag} is available.", f"Update to {tag}", NEXT_INSTALL, True)
        return UpdateOffer(
            f"{tag} is available. See the release page for update instructions.",
            "Open release page",
            NEXT_PAGE,
        )
    if tag is not None and update.version_parts(current) is None:
        return UpdateOffer(
            f"This is a development build. The latest release is {tag}.", "Check again", NEXT_CHECK
        )
    return UpdateOffer(f"vinoPop {current} is up to date.", "Check again", NEXT_CHECK)


class HotkeyRecorder(Gtk.Button):
    """Compact button that captures a keypress combo when clicked."""

    def __init__(
        self,
        initial: str = "",
        on_changed: Callable[[str], None] | None = None,
        name: str = "",
    ) -> None:
        super().__init__()
        # Read out with the combo: the row title doesn't name a suffix widget
        self._name = name
        self._value = initial
        self._recording = False
        self._rejected = ""
        self._on_changed = on_changed
        self.set_valign(Gtk.Align.CENTER)
        self.add_css_class("flat")
        self._update_label()
        self.connect("clicked", self._on_clicked)
        keys = Gtk.EventControllerKey()
        # Capture phase, before the window's own shortcuts (Escape, Ctrl+W)
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
            text = f"Cannot bind {_pretty_combo(self._rejected)}; try another"
            self.set_tooltip_text("Press Esc to cancel")
            self.set_child(Gtk.Label(label=text))
        elif self._recording:
            text = "Press a shortcut…"
            self.set_tooltip_text("Press Esc to cancel")
            self.set_child(Gtk.Label(label=text))
        elif self._value:
            text = _pretty_combo(self._value)
            self.set_tooltip_text("Click to change")
            accel = _combo_to_accel(self._value)
            self.set_child(Gtk.ShortcutLabel(accelerator=accel) if accel else Gtk.Label(label=text))
        else:
            text = "Disabled"
            self.set_tooltip_text("Click to set a shortcut")
            self.set_child(Gtk.Label(label=text))
        if self._name:
            # The label child would otherwise name the button by itself
            self.reset_relation(Gtk.AccessibleRelation.LABELLED_BY)
            self.update_property([Gtk.AccessibleProperty.LABEL], [f"{self._name}: {text}"])

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


#: The Plugins page: (title, subtitle, Plugin Manager tab) for each row.
_PLUGIN_LINKS = [
    (
        "Plugin Manager",
        "Browse the catalog and install actions for the popup.",
        "available",
    ),
    (
        "Installed plugins",
        "Update, remove, or toggle individual actions.",
        "installed",
    ),
    (
        "Custom actions",
        "Create your own popup buttons without code.",
        "custom",
    ),
    (
        "Popup button order",
        "Reorder how buttons appear in the popup.",
        "order",
    ),
]


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
        self._plugin_dialog: PluginManagerDialog | None = None
        self._debug_refresh: Callable[[], None] | None = None
        self._settings = get_settings()
        self._window: Adw.Window | None = None
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
        # GTK 4 can keep a destroyed window alive while Python holds a ref
        win.connect("close-request", lambda w: (self._on_destroy(w), False)[1])
        # Before the groups: their modal editors use it as parent
        self._window = win

        pages: list[tuple[str, str, str, Gtk.Widget]] = [
            (name, title, icon, self._build_page(title, builders))
            for name, title, icon, builders in self._pages()
        ]

        def _on_show(name: str) -> None:
            self._page_name = name
            if name == "debug" and self._debug_refresh is not None:
                refresh, self._debug_refresh = self._debug_refresh, None
                refresh()

        sidebar_window.build(win, "Settings", pages, self._page_name, _on_show)
        _force_to_front(win)

    @staticmethod
    def _build_page(
        title: str, builders: list[Callable[[], Adw.PreferencesGroup]]
    ) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        for build in builders:
            group = build()
            if group.get_title() == title:
                group.set_title("")
            page.add(group)
        return page

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
                "plugins",
                "Plugins",
                "application-x-addon-symbolic",
                [self._build_plugins_group],
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
                [self._build_ai_group, self._build_advanced_group, self._build_updates_group],
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
        if win is not None and self._window is not None and win is not self._window:
            return
        self._window = None

    def _switch_row(self, title: str, subtitle: str, active: bool) -> Adw.SwitchRow:
        row = _plain(Adw.SwitchRow())
        row.set_title(title)
        if subtitle:
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
        """An Adw.ComboRow over (id, label) pairs; on_change gets the selected
        id. An unknown `current` shows the first option and saves nothing
        until the user picks (pinned by
        `tests/test_windows_settings_rows.py::test_an_unknown_combo_id_shows_the_first_option_unsaved`,
        `tests/test_windows_settings_rows.py::test_a_known_combo_id_is_selected`)."""
        row = _plain(Adw.ComboRow())
        row.set_title(title)
        if subtitle:
            row.set_subtitle(subtitle)
        ids = [key for key, _label in options]
        row.set_model(Gtk.StringList.new([label for _key, label in options]))
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

    def _build_appearance_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Appearance")
        group.add(self._theme_row())
        group.add(self._popup_button_size_row())
        icon_style_row, icon_preview = self._icon_style_row_and_preview()
        group.add(icon_style_row)
        group.add(icon_preview)
        group.add(self._text_labels_row())
        group.add(self._tray_icon_row())
        group.add(
            self._spin_row(
                "Hide tray icon after (minutes)",
                "Reappears when the popup opens. 0 keeps it visible.",
                "tray_idle_minutes",
                0,
                1440,
                1,
            )
        )
        group.add(self._max_popup_buttons_row())
        group.add(self._overflow_mode_row())
        return group

    def _theme_row(self) -> Adw.ComboRow:
        def on_theme_changed(mode: str) -> None:
            mode = mode or "dark"
            self._save_key("theme", mode)
            _apply_theme(mode)

        return self._combo_row(
            "Theme",
            "",
            [
                ("dark", "Dark"),
                ("light", "Light"),
                ("system", "Follow system"),
            ],
            self._settings.get("theme", "dark") or "dark",
            on_theme_changed,
        )

    def _bounded_spin_row(
        self,
        title: str,
        subtitle: str,
        key: str,
        bounds: tuple[int, int],
        on_changed: Callable[[Adw.SpinRow, object], None],
    ) -> Adw.SpinRow:
        lower, upper = bounds
        adjustment = Gtk.Adjustment(
            value=self._settings.get_int(key),
            lower=lower,
            upper=upper,
            step_increment=1,
            page_increment=4,
        )
        row = _plain(Adw.SpinRow.new(adjustment, 1, 0))
        row.set_title(title)
        if subtitle:
            row.set_subtitle(subtitle)
        row.set_numeric(True)
        row.connect("notify::value", on_changed)
        return row

    def _popup_button_size_row(self) -> Adw.SpinRow:
        def on_size_changed(spin: Adw.SpinRow, _pspec) -> None:
            self._save_key("popup_button_size", int(spin.get_value()))
            try:
                from ui import popup as _popup

                _popup.reinstall_popup_css()
            except Exception as exc:
                print(f"[settings] popup css reload failed: {exc}")

        return self._bounded_spin_row(
            "Popup button size",
            "Size in pixels (16–32).",
            "popup_button_size",
            _POPUP_BUTTON_PX,
            on_size_changed,
        )

    def _icon_style_row_and_preview(self) -> tuple[Adw.ComboRow, Gtk.Box]:
        """The icon style picker and, under it, sample icons in that style."""
        current = self._settings.get("icon_style") or "color"
        if current not in ("color", "glyph"):
            current = "color"

        preview = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        preview.set_halign(Gtk.Align.CENTER)
        preview.set_margin_top(8)
        preview.set_margin_bottom(12)

        def show_samples(style: str) -> None:
            while (sample := preview.get_first_child()) is not None:
                preview.remove(sample)
            try:
                from ui import icon_style as _ist

                concepts = _ist.PREVIEW_CONCEPTS
            except Exception:
                concepts = []
            for concept in concepts:
                icon_name = (
                    f"vinopop-{concept}-symbolic" if style == "glyph" else f"vinopop-{concept}"
                )
                sample = Gtk.Image.new_from_icon_name(icon_name)
                sample.set_pixel_size(30)
                preview.append(sample)

        def on_style_changed(style: str) -> None:
            style = style or "color"
            self._save_key("icon_style", style)
            show_samples(style)

        row = self._combo_row(
            "Plugin icon style",
            "",
            [("color", "Color tiles"), ("glyph", "Monochrome glyphs")],
            current,
            on_style_changed,
        )
        show_samples(current)
        return row, preview

    def _text_labels_row(self) -> Adw.SwitchRow:
        row = self._switch_row(
            "Text labels",
            "Show action names instead of icons.",
            bool(self._settings.get("popup_text_labels", False)),
        )
        row.connect(
            "notify::active",
            lambda r, _p: self._save_key("popup_text_labels", r.get_active()),
        )
        return row

    def _tray_icon_row(self) -> Adw.ComboRow:
        current = self._settings.get("tray_icon_style") or "color"
        if current not in ("color", "light", "dark"):
            current = "color"
        # Saving runs on_changed, and main.py has the tray redraw its icon
        return self._combo_row(
            "Tray icon",
            "Override if your panel doesn't recolor symbolic icons.",
            [
                ("color", "Auto (follows panel)"),
                ("light", "Light mono (dark panels)"),
                ("dark", "Dark mono (light panels)"),
            ],
            current,
            lambda style: self._save_key("tray_icon_style", style or "color"),
        )

    def _max_popup_buttons_row(self) -> Adw.SpinRow:
        return self._bounded_spin_row(
            "Maximum buttons in the popup",
            "Extra actions move to a +N button.",
            "max_popup_buttons",
            _MAX_POPUP_BUTTONS,
            lambda spin, _p: self._save_key("max_popup_buttons", int(spin.get_value())),
        )

    def _overflow_mode_row(self) -> Adw.ComboRow:
        return self._combo_row(
            "When actions overflow",
            "",
            [
                ("expand", "One row + expand arrow"),
                ("wrap", "Wrap to two rows"),
                ("cap", "One row only (+N button)"),
            ],
            self._settings.get("popup_overflow_mode") or "expand",
            lambda v: self._save_key("popup_overflow_mode", v or "expand"),
        )

    def _build_activation_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Activation")
        group.add(
            self._hotkey_row(
                "Popup hotkey",
                "Shows actions for selected text, or a paste menu if nothing is selected.",
                key="hotkey",
                recorder_name="Popup hotkey",
                clear_tooltip="Disable the popup hotkey",
            )
        )
        group.add(self._ocr_hotkey_row())

        on_selection_row = self._switch_row(
            "Auto-popup on selection",
            "Open the popup when you highlight text.",
            self._settings.get("show_on_selection"),
        )
        on_selection_row.connect("notify::active", self._on_switch, "show_on_selection")
        group.add(on_selection_row)

        double_click_row = self._switch_row(
            "Double-click edit menu",
            "Hold the modifier below and double-click in a text field for "
            "Paste, Select All, and Backspace.",
            self._settings.get("double_click_popup_enabled", False),
        )
        double_click_row.connect("notify::active", self._on_switch, "double_click_popup_enabled")
        group.add(double_click_row)
        modifier_row = self._combo_row(
            "Modifier key",
            "",
            [
                ("ctrl", "Ctrl"),
                ("shift", "Shift"),
                ("alt", "Alt"),
                ("super", "Super (Windows key)"),
            ],
            (self._settings.get("double_click_modifier") or "ctrl").lower(),
            lambda v: self._save_key("double_click_modifier", v or "ctrl"),
        )
        _enabled_while(double_click_row, modifier_row)
        group.add(modifier_row)

        group.add(self._force_all_modifier_row())
        group.add(self._autostart_row())
        return group

    def _add_hotkey_recorder(
        self, row: Adw.ActionRow, key: str, recorder_name: str, clear_tooltip: str
    ) -> None:
        """A recorder for the hotkey stored under `key`, and a button that
        disables it, as the row's suffixes."""
        recorder = HotkeyRecorder(
            self._settings.get(key) or "",
            on_changed=lambda combo: self._save_key(key, combo),
            name=recorder_name,
        )
        row.add_suffix(recorder)
        clear = self._suffix_icon_button("edit-clear-symbolic", clear_tooltip)
        clear.connect("clicked", lambda *_: recorder.set_value(""))
        row.add_suffix(clear)

    def _hotkey_row(
        self, title: str, subtitle: str, *, key: str, recorder_name: str, clear_tooltip: str
    ) -> Adw.ActionRow:
        row = _plain(Adw.ActionRow())
        row.set_title(title)
        if subtitle:
            row.set_subtitle(subtitle)
        self._add_hotkey_recorder(row, key, recorder_name, clear_tooltip)
        return row

    def _ocr_hotkey_row(self) -> Adw.ActionRow:
        """The subtitle says whether the hotkey can do anything yet: OCR
        needs a capture tool and the OpenVINO OCR engine."""
        row = _plain(Adw.ActionRow())
        row.set_title("Screen OCR hotkey")
        supported, reason = _ocr_support()
        if supported:
            row.set_subtitle("Copy text from a screen region.")
        else:
            row.set_subtitle(f"Setup required: {reason}.")
            row.add_suffix(self._ocr_install_button(row, reason))
        self._add_hotkey_recorder(row, "ocr_hotkey", "Screen OCR hotkey", "Disable the OCR hotkey")
        return row

    def _ocr_install_button(self, row: Adw.ActionRow, reason: str) -> Gtk.Button:
        """Installs the OCR tools through pkexec and updates `row` when done.
        Where nothing can install them in place, its tooltip says what to run."""
        try:
            from capture.screen_ocr import install_argv

            argv = install_argv()
        except Exception:
            argv = None
        button = Gtk.Button(label="Install")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text("Install OCR tools (requires administrator password).")
        if argv is None:
            button.set_sensitive(False)
            button.set_tooltip_text(_ocr_manual_install_hint(reason))
        button.connect("clicked", self._on_ocr_install_clicked, argv, row)
        return button

    def _on_ocr_install_clicked(self, button: Gtk.Button, argv, row: Adw.ActionRow) -> None:
        if not argv:
            return
        button.set_sensitive(False)
        button.set_label("Installing…")
        row.set_subtitle("Approve the password prompt to continue…")

        def install() -> None:
            ok, error = _run_ocr_install(argv)
            GLib.idle_add(self._on_ocr_install_finished, button, row, ok, error)

        threading.Thread(target=install, daemon=True, name="ocr-install").start()

    def _on_ocr_install_finished(
        self, button: Gtk.Button, row: Adw.ActionRow, exited_ok: bool, error: str
    ) -> bool:
        try:
            from capture.screen_ocr import is_supported

            supported, reason = is_supported()
        except Exception:
            supported, reason = exited_ok, ""
        if supported:
            row.set_subtitle("Copy text from a screen region.")
            button.set_label("Installed")
            button.set_sensitive(False)
            # on_changed binds the hotkey now that it can do something
            if self._on_changed is not None:
                try:
                    self._on_changed()
                except Exception:
                    pass
        else:
            row.set_subtitle(f"Installation didn't complete: {reason or error}. Try again.")
            button.set_label("Install")
            button.set_sensitive(True)
        return False

    def _force_all_modifier_row(self) -> Adw.ComboRow:
        # The option labels stay short: a wide combo squeezes the wrapping
        # subtitle into a tall ribbon
        row = self._combo_row(
            "Force all plugins modifier",
            "Hold while selecting text to show all matching actions.",
            [
                ("", "None (off)"),
                ("ctrl", "Ctrl"),
                ("alt", "Alt"),
                ("super", "Super"),
                ("shift", "Shift"),
            ],
            (self._settings.get("popup_force_all_modifier") or "").lower(),
            lambda v: self._save_key("popup_force_all_modifier", v or ""),
        )
        if not _force_all_supported():
            row.set_visible(False)
        return row

    def _autostart_row(self) -> Adw.SwitchRow:
        """Start at login is the ~/.config/autostart entry itself, not a
        settings.json key: the desktop reads the file."""
        row = self._switch_row("Start at login", "", False)
        try:
            from core import autostart

            row.set_active(autostart.is_enabled())

            def on_toggled(switch, _p):
                from core import autostart as _as

                written = _as.set_enabled(switch.get_active())
                if not written:
                    switch.set_active(not switch.get_active())

            row.connect("notify::active", on_toggled)
        except Exception:
            row.set_sensitive(False)
        return row

    def _build_snippets_clipboard_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Snippets &amp; Clipboard")

        history_row = self._switch_row(
            "Clipboard history",
            "Save recently copied text and images.",
            self._settings.get("clipboard_history_enabled", True),
        )
        history_row.connect("notify::active", self._on_switch, "clipboard_history_enabled")
        group.add(history_row)

        shortcut_row = self._hotkey_row(
            "Clipboard shortcut",
            "Open the clipboard picker anywhere.",
            key="clipboard_hotkey",
            recorder_name="Clipboard shortcut",
            clear_tooltip="Disable the clipboard shortcut",
        )
        _enabled_while(history_row, shortcut_row)
        group.add(shortcut_row)

        triggers_row = self._switch_row(
            "Snippet triggers",
            "Expand triggers like ;email when followed by Space or Tab.",
            self._settings.get("snippet_triggers_enabled", False),
        )
        triggers_row.connect("notify::active", self._on_switch, "snippet_triggers_enabled")
        triggers_row.connect("notify::active", lambda *_: self._apply_trigger_toggle())
        _enabled_while(history_row, triggers_row)
        group.add(triggers_row)

        trigger_blocklist_row = self._pattern_list_row(
            "trigger_blocklist_patterns",
            "Trigger blocklist",
            "None — e.g. password managers or terminals",
            _TRIGGER_BLOCKLIST_EDITOR,
        )

        def _sync_trigger_blocklist(*_args) -> None:
            trigger_blocklist_row.set_sensitive(
                history_row.get_active() and triggers_row.get_active()
            )

        _sync_trigger_blocklist()
        history_row.connect("notify::active", _sync_trigger_blocklist)
        triggers_row.connect("notify::active", _sync_trigger_blocklist)
        group.add(trigger_blocklist_row)

        variables_row = self._snippet_variables_row()
        _enabled_while(history_row, variables_row)
        group.add(variables_row)

        shell_row = self._switch_row(
            "Shell expansion in snippets",
            "Allow {shell:CMD} placeholders to run bash commands. "
            "Only enable for snippets you trust.",
            self._settings.get("snippet_shell_enabled", False),
        )
        shell_row.connect("notify::active", self._on_switch, "snippet_shell_enabled")
        _enabled_while(history_row, shell_row)
        group.add(shell_row)
        return group

    def _modal_editor_row(
        self,
        title: str,
        *,
        subtitle: Callable[[], str],
        as_text: Callable[[], str],
        save_text: Callable[[str], None],
        editor: dict[str, str],
    ) -> Adw.ActionRow:
        """A row that opens a text editor on click, so a free-form list
        takes no permanent room on the page. `editor` is the modal's title,
        subtitle and placeholder_text."""
        row = _plain(Adw.ActionRow())
        row.set_activatable(True)

        def refresh() -> None:
            row.set_title(title)
            row.set_subtitle(subtitle())

        def on_done(new_text: str | None) -> None:
            if new_text is None:
                return
            save_text(new_text)
            refresh()

        def on_activated(_row) -> None:
            _open_text_editor_modal(
                parent=self._window, initial_text=as_text(), on_done=on_done, **editor
            )

        refresh()
        row.connect("activated", on_activated)
        row.add_suffix(self._edit_suffix())
        return row

    def _pattern_list_row(
        self, key: str, title: str, when_empty: str, editor: dict[str, str]
    ) -> Adw.ActionRow:
        """A blocklist stored under `key`: one substring per line, matched
        against the focused window's title and class."""

        def patterns() -> list[str]:
            return list(self._settings.get(key) or [])

        return self._modal_editor_row(
            title,
            subtitle=lambda: settings_text.blocked_patterns_subtitle(patterns(), when_empty),
            as_text=lambda: "\n".join(patterns()),
            save_text=lambda text: self._save_key(key, settings_text.patterns_from_text(text)),
            editor=editor,
        )

    def _snippet_variables_row(self) -> Adw.ActionRow:
        def variables() -> dict:
            stored = self._settings.get("snippet_variables") or {}
            return stored if isinstance(stored, dict) else {}

        return self._modal_editor_row(
            "Snippet variables",
            subtitle=lambda: settings_text.variables_subtitle(
                variables(),
                "Reusable {var:name} values for snippets.",
            ),
            as_text=lambda: settings_text.variables_as_text(variables()),
            save_text=lambda text: self._save_key(
                "snippet_variables", settings_text.variables_from_text(text)
            ),
            editor=_SNIPPET_VARIABLES_EDITOR,
        )

    def _build_filter_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Filters")

        skip_short_row = self._switch_row(
            "Ignore short selections",
            "Applies to auto-popup only, not the hotkey.",
            self._settings.get("min_selection_length_enabled"),
        )
        skip_short_row.connect("notify::active", self._on_switch, "min_selection_length_enabled")
        group.add(skip_short_row)

        min_length_row = self._spin_row(
            "Minimum characters",
            "",
            "min_selection_length",
            1,
            100,
            1,
        )
        _enabled_while(skip_short_row, min_length_row)
        group.add(min_length_row)

        group.add(
            self._pattern_list_row(
                "blocklist_patterns",
                "Blocked apps and sites",
                "None — e.g. password managers or banking sites",
                _POPUP_BLOCKLIST_EDITOR,
            )
        )
        return group

    def _build_timing_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Timing")
        group.set_description("Delays in seconds.")

        debounce_row = self._seconds_spin_row(
            "Delay before popup appears",
            "Prevents flicker while dragging.",
            "selection_debounce_ms",
            0.0,
            2.0,
            0.05,
        )
        initial_row = self._seconds_spin_row(
            "Auto-hide before mouse arrives",
            "Hide if the pointer never reaches the popup.",
            "auto_hide_initial_ms",
            0.5,
            30.0,
            0.5,
        )
        leave_row = self._seconds_spin_row(
            "Auto-hide after mouse leaves",
            "",
            "auto_hide_leave_ms",
            0.2,
            20.0,
            0.2,
        )
        group.add(debounce_row)
        group.add(initial_row)
        group.add(leave_row)
        return group

    def _build_search_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Web search")

        try:
            from core.actions import SEARCH_ENGINES
        except Exception:
            SEARCH_ENGINES = {"google": ("Google", "https://www.google.com/search?q={q}")}

        current = (self._settings.get("search_engine") or "google").strip().lower()
        if current not in SEARCH_ENGINES and current != "custom":
            current = "google"
        options = [(key, label) for key, (label, _tmpl) in SEARCH_ENGINES.items()]
        options.append(("custom", "Custom URL…"))

        # Adw.EntryRow has no subtitle or placeholder, so the {q} rule is in
        # the title and the example in the tooltip
        custom_row = _plain(Adw.EntryRow())
        custom_row.set_title("Custom search URL ({q} = the selection)")
        custom_row.set_tooltip_text(
            "Must include {q}, which is replaced by the selected text.\n"
            "Example: https://searx.example.com/search?q={q}"
        )
        custom_row.set_text(self._settings.get("search_engine_custom_url") or "")

        def _sync_custom_visibility(engine_id: str | None) -> None:
            custom_row.set_visible(engine_id == "custom")

        def _on_engine_changed(new_id: str) -> None:
            self._save_key("search_engine", new_id or "google")
            _sync_custom_visibility(new_id)

        engine_row = self._combo_row(
            "Search engine",
            "",
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
            size = f"{model.label} ({model.size})"
        except Exception:
            size = "about 4 GB"
        prefetch_row = self._switch_row(
            "Pre-download AI model",
            f"Download {size} at startup instead of on your first AI action.",
            self._settings.get("ai_prefetch_model", False),
        )
        prefetch_row.connect("notify::active", self._on_prefetch_switch)
        group.add(prefetch_row)
        describe_row = self._switch_row(
            "Describe images after OCR",
            "Offer an AI description for OCR captures, or run automatically if "
            "no text is found (~10 s).",
            self._settings.get("ocr_describe_images", False),
        )
        describe_row.connect(
            "notify::active",
            lambda sw, _p: self._save_key("ocr_describe_images", bool(sw.get_active())),
        )
        group.add(describe_row)
        group.add(self._build_model_row(prefetch_row))
        return group

    def _build_model_row(self, prefetch_row: Adw.SwitchRow) -> Adw.ActionRow:
        """What's in the models folder, with Delete…: nothing else ever
        removes a model (switching keeps the old one)."""
        row = _plain(Adw.ActionRow())
        row.set_title("Downloaded models")
        btn = Gtk.Button(label="Delete…")
        btn.set_valign(Gtk.Align.CENTER)
        btn.add_css_class("destructive-action")
        row.add_suffix(btn)

        def _refresh() -> None:
            try:
                from vino_ai import models

                items = models.on_disk()
            except Exception:
                items = []
            if items:
                row.set_subtitle("\n".join(f"{i.label}: {_gb(i.size)}" for i in items))
            else:
                row.set_subtitle("None downloaded (downloads on first AI action)")
            btn.set_sensitive(bool(items))

        def _delete() -> None:
            def _work() -> None:
                try:
                    from vino_ai import llm

                    freed, err = llm.delete_models(), None
                except Exception as exc:  # noqa: BLE001 - shown in the row
                    freed, err = 0, str(exc)
                GLib.idle_add(_done, freed, err)

            btn.set_sensitive(False)
            row.set_subtitle("Deleting…")
            threading.Thread(target=_work, name="vinopop-model-delete", daemon=True).start()

        def _done(freed: int, err: str | None) -> bool:
            _refresh()
            if err is not None:
                alert = Adw.AlertDialog.new("Could not delete model", err)
                alert.add_response("close", "Close")
                alert.present(self._window)
            elif prefetch_row.get_active():
                prefetch_row.set_active(False)
            return False

        def _on_click(_btn: Gtk.Button) -> None:
            try:
                from vino_ai import models

                items = models.on_disk()
                if models.downloading():
                    alert = Adw.AlertDialog.new(
                        "Model download in progress",
                        "Wait for the download to finish before deleting models.",
                    )
                    alert.add_response("close", "Close")
                    alert.present(self._window)
                    return
            except Exception:
                items = []
            total = sum(i.size for i in items)
            body = (
                f"Removes {_gb(total)} of AI models and cache. "
                "They'll download again next time you use an AI action."
            )
            if prefetch_row.get_active():
                body += "\n\nBackground download will be turned off."
            confirm = Adw.AlertDialog.new("Delete AI models?", body)
            confirm.add_response("cancel", "Cancel")
            confirm.add_response("delete", "Delete")
            confirm.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
            confirm.set_default_response("cancel")
            confirm.set_close_response("cancel")
            confirm.connect("response", lambda _d, r: _delete() if r == "delete" else None)
            confirm.present(self._window)

        btn.connect("clicked", _on_click)
        _refresh()
        return row

    def _on_prefetch_switch(self, switch: Adw.SwitchRow, _param) -> None:
        """Flipping it is an answer, so a stored True counts as consent (pinned by
        `tests/test_prefetch_consent.py::test_yes_from_onboarding_or_settings_downloads`,
        `tests/test_prefetch_consent.py::test_old_default_true_is_not_consent`)."""
        self._settings.set("ai_prefetch_asked", True)
        self._save_key("ai_prefetch_model", bool(switch.get_active()))

    def _build_updates_group(self) -> Adw.PreferencesGroup:
        """Check for updates, then Update to vN. Online only when clicked (core/update.py)."""
        from core import update
        from core.version import __version__

        group = Adw.PreferencesGroup()
        group.set_title("Updates")
        row = _plain(Adw.ActionRow())
        row.set_title(f"vinoPop {__version__}")
        btn = Gtk.Button(label="Check for updates")
        btn.set_valign(Gtk.Align.CENTER)
        row.add_suffix(btn)
        row.set_activatable_widget(btn)
        group.add(row)
        state: dict[str, object] = {"next": NEXT_CHECK, "tag": None}

        def _show(subtitle: str, label: str | None, nxt: str, *, suggested: bool = False) -> None:
            row.set_subtitle(subtitle)
            btn.set_visible(label is not None)
            btn.set_sensitive(True)
            if label is not None:
                btn.set_label(label)
            if suggested:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")
            state["next"] = nxt

        def _busy(subtitle: str) -> None:
            row.set_subtitle(subtitle)
            btn.set_sensitive(False)

        def _checked(tag: str | None, error: str | None) -> bool:
            offer = _update_offer(__version__, tag, error, update.upgrade_command() is not None)
            if offer.next in (NEXT_INSTALL, NEXT_PAGE):
                state["tag"] = tag
            _show(offer.subtitle, offer.label, offer.next, suggested=offer.suggested)
            return False

        def _installed(error: str | None) -> bool:
            tag = state["tag"]
            if error is not None:
                _show(error, "Try again", NEXT_INSTALL)
            elif self._on_restart is not None:
                _show(f"Restart vinoPop to use {tag}.", "Restart", NEXT_RESTART, suggested=True)
            else:
                _show(f"Restart vinoPop to use {tag}.", None, NEXT_CHECK)
            return False

        def _clicked(*_) -> None:
            nxt = state["next"]
            if nxt == NEXT_CHECK:
                _busy("Checking GitHub for a newer release…")

                def _worker() -> None:
                    try:
                        GLib.idle_add(_checked, update.fetch_latest(), None)
                    except RuntimeError as exc:
                        GLib.idle_add(_checked, None, str(exc))

                threading.Thread(target=_worker, daemon=True, name="update-check").start()
            elif nxt == NEXT_INSTALL:
                tag, upgrade = state["tag"], update.upgrade_command()
                if not isinstance(tag, str) or upgrade is None:
                    return
                _busy(f"Installing {tag}. This may take a few minutes…")

                def _install() -> None:
                    try:
                        update.install(tag, upgrade)
                        GLib.idle_add(_installed, None)
                    except RuntimeError as exc:
                        GLib.idle_add(_installed, str(exc))

                threading.Thread(target=_install, daemon=True, name="update-install").start()
            elif nxt == NEXT_RESTART and self._on_restart is not None:
                self._on_restart()
            elif nxt == NEXT_PAGE:
                Gtk.UriLauncher.new(f"{update.RELEASES_URL}/tag/{state['tag']}").launch(
                    self._window, None, None
                )

        btn.connect("clicked", _clicked)
        _show(
            "Checks GitHub for a newer release.",
            "Check for updates",
            NEXT_CHECK,
        )
        return group

    def _build_advanced_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Advanced")

        poll_row = self._switch_row(
            "Trigger hotkey on first press",
            "Workaround if the hotkey needs 2–3 presses to open (common on Cinnamon).",
            self._settings.get("hotkey_use_polling", False),
        )
        poll_row.connect("notify::active", self._on_switch, "hotkey_use_polling")
        group.add(poll_row)

        # editable_atspi_listener_enabled has no switch on purpose
        # (docs/how-it-works.md, "Why the defaults are what they are")
        reset_row = _plain(Adw.ActionRow())
        reset_row.set_title("Reset settings to defaults")
        reset_row.set_subtitle(
            "Restore preferences to defaults. Snippets, clipboard history, "
            "plugins, and custom actions are kept."
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
            "Reset settings to defaults?",
            "Hotkeys, timing, appearance, AI, blocklists, search, and terminal "
            "settings will reset. Your snippets, clipboard history, plugins, "
            "and custom actions are kept.",
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
        try:
            self._settings.reset_to_defaults()
        except Exception as exc:
            err = Adw.AlertDialog.new("Couldn't reset settings", f"{exc}")
            err.add_response("close", "Close")
            err.present(self._window)
            return
        # Applied here, not by the daemon, as for a change by hand
        _apply_theme(self._settings.get("theme") or "dark")
        if self._on_changed is not None:
            try:
                self._on_changed()
            except Exception:
                pass

        # From idle: the alert is still emitting its response inside this window
        def _rebuild() -> bool:
            old = self._window
            if old is not None:
                self._window = None
                old.destroy()
            self.show()
            return False

        GLib.idle_add(_rebuild)

    def _build_diagnostics_group(self) -> Adw.PreferencesGroup:
        """`vinopop --doctor`, run in a child process: it imports OpenVINO
        and probes devices, which would stall this window."""
        group = Adw.PreferencesGroup()
        group.set_title("Diagnostics")
        group.set_description("Checks local AI, OpenVINO devices, and desktop setup.")
        buttons = Gtk.Box(spacing=6)
        rerun = self._suffix_icon_button("view-refresh-symbolic", "Run diagnostic checks again")
        copy = self._suffix_icon_button("edit-copy-symbolic", "Copy diagnostics for a bug report")
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
                row.set_subtitle(settings_text.doctor_check_detail(check))
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
            report["text"] = settings_text.doctor_report(checks)
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
        from core.xdg_paths import LOG_FILE

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
            if subtitle:
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
            "Try this first if the popup, hotkeys, or tray icon stop responding.",
            "Restart",
            lambda *_: self._on_restart() if self._on_restart else None,
        )
        restart.set_sensitive(self._on_restart is not None)

        def _setup(*_) -> None:
            from ui.onboarding import OnboardingWindow

            win = OnboardingWindow(self._settings, on_open_plugins=self._on_open_plugins)
            if self._window is not None:
                win.set_transient_for(self._window)
            win.present()

        _button_row(
            "Run onboarding tour",
            "Replay the welcome tour.",
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
                btn.set_label("Cleared")

            devs = ", ".join(skipped)
            _button_row(
                "Retry failed AI devices",
                f"Skipped after an earlier error: {devs}. Clear to try them again.",
                "Retry",
                _forget,
            )
        return group

    def _build_plugins_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Plugins")
        group.set_description("Manage popup buttons in the Plugin Manager.")
        for title, subtitle, tab in _PLUGIN_LINKS:
            row = _plain(Adw.ActionRow())
            row.set_title(title)
            row.set_subtitle(subtitle)
            row.set_activatable(True)
            chevron = Gtk.Image.new_from_icon_name("go-next-symbolic")
            chevron.set_valign(Gtk.Align.CENTER)
            row.add_suffix(chevron)
            row.connect("activated", self._on_plugins_row, tab)
            group.add(row)
        return group

    def _on_plugins_row(self, _row: Adw.ActionRow, tab: str) -> None:
        if self._on_open_plugins is not None:
            self._on_open_plugins(tab=tab)
            return
        # Standalone (python3 -m ui.settings_gui): no daemon to ask.
        if self._plugin_dialog is None:
            from ui.plugin_manager import PluginManagerDialog

            self._plugin_dialog = PluginManagerDialog()
        self._plugin_dialog.show(tab=tab)

    def _build_terminal_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Terminal commands")

        term_row = self._switch_row(
            "Keep terminal open after running",
            "Leave the terminal open after a command finishes to read its output.",
            self._settings.get("terminal_keep_open", True),
        )
        term_row.connect("notify::active", self._on_switch, "terminal_keep_open")
        group.add(term_row)
        return group

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
        if subtitle:
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
        """A spinner that shows seconds and saves milliseconds, as the
        *_ms keys in settings.json expect."""
        row = _plain(Adw.SpinRow.new_with_range(lo_s, hi_s, step_s))
        row.set_title(title)
        if subtitle:
            row.set_subtitle(subtitle)
        row.set_digits(2 if step_s < 0.1 else 1)
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
        """Start or stop the snippet-trigger watcher now, without a restart.
        It lives in the loaded clipboard_history plugin."""
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
