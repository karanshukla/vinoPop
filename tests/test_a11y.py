"""Accessibility checks over the real GTK windows: every control a screen
reader can land on has a name, dialogs close on Escape, the onboarding tour
holds still when animations are off, and the custom colours keep WCAG AA
contrast in light and dark.

They build the windows (nothing waits on a compositor), so they need GTK 4,
libadwaita and a display: CI's test-gtk job runs them under xvfb-run. With
no display they skip. GTK 4.14 has no getter for accessible properties, so
the calls that set a name (update_property, Label.set_mnemonic_widget,
reset_relation) are recorded as the windows are built, and the name is
worked out the way GTK does it: labelled-by, then LABEL, then the widget's
own text, then its tooltip.

The last test reads the live AT-SPI tree, as a screen reader does; it needs
VINOPOP_ATSPI_TEST=1, at-spi2-core and a session bus (CI's test-gtk job has
them)."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("gi")


# ── GTK setup ────────────────────────────────────────────────────────────────


def _record_accessible_calls(Gtk):
    """Remember accessible LABEL / LABELLED_BY on the widget, since GTK can't
    be asked for them afterwards. Returns a function that undoes it."""
    update_property = Gtk.Accessible.update_property
    reset_relation = Gtk.Accessible.reset_relation
    set_mnemonic_widget = Gtk.Label.set_mnemonic_widget

    def _update_property(self, props, values):
        for prop, value in zip(props, values, strict=False):
            if prop == Gtk.AccessibleProperty.LABEL:
                self._a11y_label = value
        return update_property(self, props, values)

    # PyGObject can't pass widget lists to update_relation(), so labelling
    # goes through Gtk.Label.set_mnemonic_widget(), which sets LABELLED_BY.
    def _set_mnemonic_widget(self, widget):
        if widget is not None:
            widget._a11y_labelled_by = [self]
        return set_mnemonic_widget(self, widget)

    def _reset_relation(self, rel):
        if rel == Gtk.AccessibleRelation.LABELLED_BY:
            self._a11y_reset_labelled_by = True
        return reset_relation(self, rel)

    Gtk.Accessible.update_property = _update_property
    Gtk.Label.set_mnemonic_widget = _set_mnemonic_widget
    Gtk.Accessible.reset_relation = _reset_relation

    def undo():
        Gtk.Accessible.update_property = update_property
        Gtk.Label.set_mnemonic_widget = set_mnemonic_widget
        Gtk.Accessible.reset_relation = reset_relation

    return undo


@pytest.fixture(scope="module")
def gtk():
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gdk, GLib, Gtk

        from ui import gtk4_app
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 / libadwaita unavailable: {exc}")
    try:
        gtk4_app.init()
    except Exception as exc:  # Gtk.init() raises without a display
        pytest.skip(f"no display: {exc}")
    if Gdk.Display.get_default() is None:
        pytest.skip("no display")
    undo = _record_accessible_calls(Gtk)
    yield SimpleNamespace(Gtk=Gtk, Adw=Adw, Gdk=Gdk, GLib=GLib)
    undo()


@pytest.fixture
def stub_backend(monkeypatch):
    """A platform backend that places nothing, so the floating windows build
    the same on any display."""
    backend = SimpleNamespace(
        name="x11",
        popup_uses_xlib=False,
        pointer_is_logical=True,
        prepare_popup_window=lambda _w: None,
        move_popup_window=lambda _w, _x, _y: None,
        set_clipboard=lambda _t: None,
    )
    import platform_backend

    monkeypatch.setattr(platform_backend, "get_backend", lambda: backend)
    return backend


def _settle(GLib, rounds: int = 50) -> None:
    ctx = GLib.MainContext.default()
    for _ in range(rounds):
        if not ctx.iteration(False):
            break


# ── accessible names ─────────────────────────────────────────────────────────


def _children(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


def _walk(widget):
    yield widget
    for child in _children(widget):
        yield from _walk(child)


def _text_content(widget, Gtk) -> str:
    """The text GTK names a button-like widget from: its labels'."""
    return " ".join(
        w.get_text().strip()
        for w in _walk(widget)
        if isinstance(w, Gtk.Label) and w.get_text().strip()
    )


def _ancestors(widget):
    parent = widget.get_parent()
    while parent is not None:
        yield parent
        parent = parent.get_parent()


def accessible_name(widget, Gtk, Adw) -> str:
    """The name a screen reader gets for `widget`, as GTK 4.14 computes it."""
    labelled_by = getattr(widget, "_a11y_labelled_by", None)
    if labelled_by:
        return " ".join(_text_content(w, Gtk) for w in labelled_by).strip()
    content = ""
    if isinstance(widget, (Gtk.Button, Gtk.CheckButton)):
        content = _text_content(widget, Gtk)
    label = getattr(widget, "_a11y_label", "") or ""
    if label:
        # A button made with a label is labelled-by that label, which wins
        # over an accessible LABEL unless the relation is reset.
        has_label_child = isinstance(widget, Gtk.Button) and widget.get_label()
        if not has_label_child or getattr(widget, "_a11y_reset_labelled_by", False):
            return label
    if isinstance(widget, Adw.PreferencesRow):
        return widget.get_title() or ""
    for parent in _ancestors(widget):
        # Adw.ActionRow labels its activatable widget with the row title
        if isinstance(parent, Adw.ActionRow) and parent.get_activatable_widget() is widget:
            return parent.get_title() or ""
    if content:
        return content
    return (widget.get_tooltip_text() or "").strip()


def _needs_name(widget, Gtk, Adw) -> bool:
    if isinstance(widget, Gtk.ListBoxRow):
        # A row the user can select or activate is a stop for the screen reader
        box = widget.get_parent()
        selectable = isinstance(box, Gtk.ListBox) and (
            box.get_selection_mode() != Gtk.SelectionMode.NONE
        )
        return selectable or widget.get_activatable()
    return isinstance(
        widget,
        (
            Gtk.Button,
            Gtk.CheckButton,
            Gtk.Switch,
            Gtk.Entry,
            Gtk.SearchEntry,
            Gtk.TextView,
            Gtk.SpinButton,
            Gtk.DropDown,
            Gtk.Scale,
        ),
    )


def _owned_by_libadwaita(widget, Gtk, Adw) -> bool:
    """Controls libadwaita builds and labels itself (the window buttons, a
    SpinRow's spin button, the header bar's back button)."""
    for parent in _ancestors(widget):
        if isinstance(parent, (Gtk.WindowControls, Adw.SpinRow, Adw.EntryRow, Adw.ComboRow)):
            return True
        if isinstance(parent, Gtk.Popover) and isinstance(parent.get_parent(), Adw.ComboRow):
            return True
        # The back button Adw.NavigationView / NavigationSplitView put in the
        # header bar (labelled "Back" by libadwaita)
        if (
            isinstance(parent, Adw.HeaderBar)
            and isinstance(widget, Gtk.Button)
            and widget.get_icon_name() == "go-previous-symbolic"
        ):
            return True
    return False


def unnamed_controls(root, Gtk, Adw) -> list[str]:
    problems = []
    for w in _walk(root):
        if not _needs_name(w, Gtk, Adw) or _owned_by_libadwaita(w, Gtk, Adw):
            continue
        if accessible_name(w, Gtk, Adw):
            continue
        where = next(
            (
                p.get_title()
                for p in _ancestors(w)
                if isinstance(p, Adw.PreferencesRow) and p.get_title()
            ),
            "",
        )
        icon = ""
        for d in _walk(w):
            if isinstance(d, Gtk.Image) and d.get_icon_name():
                icon = d.get_icon_name()
                break
        problems.append(f"{type(w).__name__} icon={icon!r} in row {where!r}")
    return problems


def _assert_named(root, Gtk, Adw) -> None:
    problems = unnamed_controls(root, Gtk, Adw)
    assert not problems, "controls with no accessible name:\n" + "\n".join(problems)


# ── the windows ──────────────────────────────────────────────────────────────


def test_popup_buttons_are_named(gtk, stub_backend, monkeypatch):
    from core.classifier import ContentType
    from plugin_system.plugin_base import Plugin
    from ui import popup

    monkeypatch.setattr(popup, "get_backend", lambda: stub_backend)
    plugins = [
        Plugin(name="copy", icon="edit-copy-symbolic", tooltip="Copy", handler=print),
        Plugin(name="paste", icon="edit-paste-symbolic", tooltip="Paste", handler=print),
        # A recipe saved without a label: icon-only, so it needs a fallback name
        Plugin(name="shout-it", icon="edit-cut-symbolic", tooltip="", handler=print),
    ]
    monkeypatch.setattr(popup.plugin_loader, "for_content_type", lambda *_a: list(plugins))
    p = popup.PopupWindow(initial_grace_ms=0)
    monkeypatch.setattr(p, "_present_near", lambda *_a, **_k: None)
    monkeypatch.setattr(p, "_refit", lambda: None)
    monkeypatch.setattr(p, "_max_per_row", lambda *_a: 2)
    monkeypatch.setattr(p, "_overflow_mode", lambda: "expand")
    p.show_for("hello", 0, 0, ContentType.PLAIN_TEXT)
    try:
        buttons = [b for b in _walk(p.win) if isinstance(b, gtk.Gtk.Button)]
        # The expand chevron says what it does
        assert accessible_name(buttons[-1], gtk.Gtk, gtk.Adw) == "Show 1 more action"
        _assert_named(p.win, gtk.Gtk, gtk.Adw)
        buttons[-1].emit("clicked")  # reveals the unlabelled one
        _assert_named(p.win, gtk.Gtk, gtk.Adw)
        names = [
            accessible_name(b, gtk.Gtk, gtk.Adw)
            for b in _walk(p.win)
            if isinstance(b, gtk.Gtk.Button)
        ]
        assert names == ["Copy", "Paste", "shout-it"]
    finally:
        p.win.destroy()


def test_popup_bar_is_a_toolbar(gtk, stub_backend, monkeypatch):
    from ui import popup

    monkeypatch.setattr(popup, "get_backend", lambda: stub_backend)
    p = popup.PopupWindow(initial_grace_ms=0)
    try:
        assert p._bar.get_accessible_role() == gtk.Gtk.AccessibleRole.TOOLBAR
    finally:
        p.win.destroy()


def test_ai_bubble_controls_are_named(gtk, stub_backend):
    from ui import ai_bubble

    b = ai_bubble.ResultBubble("Summarize", 0, 0, on_replace=lambda _t: None)
    try:
        _assert_named(b.win, gtk.Gtk, gtk.Adw)
        names = {
            accessible_name(w, gtk.Gtk, gtk.Adw)
            for w in _walk(b.win)
            if isinstance(w, gtk.Gtk.Button)
        }
        # The ✕ chip is read as "Close", not as the glyph
        assert "Close" in names and "✕" not in names
    finally:
        b.dismiss()


def test_settings_window_is_named_and_labelled(gtk, stub_backend):
    from ui.settings_gui import SettingsDialog

    dlg = SettingsDialog()
    dlg.show()
    win = dlg._window
    try:
        _assert_named(win, gtk.Gtk, gtk.Adw)
        # Two hotkey rows each have a "Disable" button: they must not read the same
        clears = [
            accessible_name(w, gtk.Gtk, gtk.Adw)
            for w in _walk(win)
            if isinstance(w, gtk.Gtk.Button) and (w.get_icon_name() or "") == "edit-clear-symbolic"
        ]
        assert len(clears) == len(set(clears)), clears
    finally:
        win.destroy()
        _settle(gtk.GLib)


def test_plugin_manager_is_named(gtk, stub_backend):
    from ui.plugin_manager import PluginManagerDialog

    dlg = PluginManagerDialog()
    dlg.show()
    win = dlg._window
    try:
        _assert_named(win, gtk.Gtk, gtk.Adw)
    finally:
        win.destroy()
        _settle(gtk.GLib)


def test_clipboard_picker_is_named(gtk, stub_backend, monkeypatch):
    import clipboard_history as ch

    monkeypatch.setattr(
        ch, "_history", [ch.Entry(id="h1", timestamp=1.0, kind="text", text="hello")]
    )
    monkeypatch.setattr(
        ch,
        "_snippets",
        [ch.Entry(id="s1", timestamp=1.0, kind="text", text="Regards", name="Sign-off")],
    )
    picker = ch._PickerDialog()
    picker.show()
    try:
        assert picker.dialog is not None
        _assert_named(picker.dialog, gtk.Gtk, gtk.Adw)
    finally:
        if picker.dialog is not None:
            picker.dialog.destroy()
        _settle(gtk.GLib)


def _run_modal_and_check(gtk, start, get_window, check):
    """Start a nested-loop modal (RecipeWizard / IconPicker), run `check` on
    its window once it is up, then close it."""
    results: dict = {}

    def _poke():
        win = get_window()
        if win is None:
            return True  # not built yet
        try:
            results["value"] = check(win)
        except Exception as exc:  # surfaced after the loop exits
            results["error"] = exc
        win.close()
        return False

    gtk.GLib.timeout_add(50, _poke)
    start()
    if "error" in results:
        raise results["error"]
    return results.get("value")


def test_recipe_wizard_is_named_and_labelled(gtk, stub_backend):
    from ui.recipe_wizard import _STEPS, RecipeWizard

    wiz = RecipeWizard()

    def check(_win):
        # Every step, not just the one on screen: the rest wait in the
        # navigation view
        problems = []
        for tag, _title in _STEPS:
            problems += unnamed_controls(wiz._nav.find_page(tag), gtk.Gtk, gtk.Adw)
        return problems

    problems = _run_modal_and_check(gtk, wiz.run_and_get, lambda: wiz._window, check)
    assert not problems, "\n".join(problems)


def test_icon_picker_is_named(gtk, stub_backend):
    from ui.icon_picker import IconPicker

    picker = IconPicker()
    problems = _run_modal_and_check(
        gtk,
        picker.run,
        lambda: getattr(picker, "_dialog", None),
        lambda win: unnamed_controls(win, gtk.Gtk, gtk.Adw),
    )
    assert not problems, "\n".join(problems)


# Text a user has to read to get through a step. libadwaita's dim-label puts
# it at ~3.2:1 on the light theme, under AA's 4.5:1 (#126). Recaps and
# previews can stay dim; these can't.
_INSTRUCTIONS = (
    "Custom buttons turn any text you select",
    "Click Next when you're ready",
    "Pick one. You can change it later",
    "Example: ",
    "The icon is what shows up in the popup",
    "Or insert a dynamic value",
    "Try one of these",
    "You can leave this as 'always'",
    "Press Apply to save",
    "Shows on: ",
)


def _dimmed_instructions(root, Gtk) -> list[str]:
    return [
        w.get_text()
        for w in _walk(root)
        if isinstance(w, Gtk.Label)
        and w.get_text().startswith(_INSTRUCTIONS)
        and w.has_css_class("dim-label")
    ]


def test_recipe_wizard_instructions_are_not_dimmed(gtk, stub_backend):
    from ui.recipe_wizard import _STEPS, RecipeWizard

    wiz = RecipeWizard()

    def check(_win):
        # The confirm page fills in only once it's reached
        wiz._refresh_confirm_page()
        found, dimmed = set(), []
        for tag, _title in _STEPS:
            page = wiz._nav.find_page(tag)
            for w in _walk(page):
                if isinstance(w, gtk.Gtk.Label) and w.get_text().startswith(_INSTRUCTIONS):
                    found.add(next(p for p in _INSTRUCTIONS if w.get_text().startswith(p)))
            dimmed += _dimmed_instructions(page, gtk.Gtk)
        return found, dimmed

    found, dimmed = _run_modal_and_check(gtk, wiz.run_and_get, lambda: wiz._window, check)
    # Guards the list itself: a reworded label would otherwise pass silently
    assert found == set(_INSTRUCTIONS), set(_INSTRUCTIONS) - found
    assert not dimmed, dimmed


def test_icon_picker_count_is_not_dimmed(gtk, stub_backend):
    from ui.icon_picker import IconPicker

    picker = IconPicker()
    dimmed = _run_modal_and_check(
        gtk,
        picker.run,
        lambda: getattr(picker, "_dialog", None),
        lambda _win: picker._count_label.has_css_class("dim-label"),
    )
    assert not dimmed


def test_welcome_dialog_is_named_and_closes_on_escape(gtk):
    from ui.welcome import show_welcome_dialog

    dlg = show_welcome_dialog({"show_welcome_dialog": True}, on_open_plugins=lambda: None)
    assert dlg is not None
    try:
        _assert_named(dlg, gtk.Gtk, gtk.Adw)
        assert _escape_closes(gtk, dlg)
    finally:
        dlg.destroy()


def test_onboarding_is_named_and_closes_on_escape(gtk):
    from ui.onboarding import OnboardingWindow

    win = OnboardingWindow({"show_welcome_dialog": True})
    win.present()
    try:
        _assert_named(win, gtk.Gtk, gtk.Adw)
        assert _escape_closes(gtk, win)
    finally:
        win.destroy()


def _escape_closes(gtk, win) -> bool:
    """Send Escape through the window's own key handling and report whether
    it asked to close. GTK 4 can't synthesize key events, so this activates
    the window's shortcut / key controllers the way a press would."""
    closed = {"yes": False}
    handler = win.connect("close-request", lambda *_: closed.update(yes=True) or False)
    Gtk, Gdk = gtk.Gtk, gtk.Gdk
    try:
        for ctrl in list(win.observe_controllers()):
            if isinstance(ctrl, Gtk.EventControllerKey):
                ctrl.emit("key-pressed", Gdk.KEY_Escape, 9, Gdk.ModifierType(0))
            elif isinstance(ctrl, Gtk.ShortcutController):
                for shortcut in ctrl:
                    trigger = shortcut.get_trigger()
                    if trigger.to_string() == "Escape":
                        shortcut.get_action().activate(Gtk.ShortcutActionFlags(0), win, None)
            if closed["yes"]:
                break
        _settle(gtk.GLib)
    finally:
        try:
            win.disconnect(handler)
        except Exception:
            pass
    return closed["yes"]


# ── focus and keyboard ───────────────────────────────────────────────────────


def test_onboarding_focus_starts_on_next(gtk):
    """Keyboard focus starts on Next, not on the "Don't show this again"
    checkbox that comes first in the bar."""
    from ui.onboarding import OnboardingWindow

    win = OnboardingWindow({"show_welcome_dialog": True})
    win.present()
    try:
        _settle(gtk.GLib)
        assert win.get_focus() is win._next
    finally:
        win.destroy()


def test_onboarding_holds_still_without_animations(gtk, monkeypatch):
    """With animations off (reduced motion) the tour draws one still frame
    per page instead of redrawing every frame."""
    from ui import onboarding

    settings = gtk.Gtk.Settings.get_default()
    old = settings.get_property("gtk-enable-animations")
    settings.set_property("gtk-enable-animations", False)
    try:
        win = onboarding.OnboardingWindow({"show_welcome_dialog": True})
        try:
            assert win._timer is None
        finally:
            win.destroy()
    finally:
        settings.set_property("gtk-enable-animations", old)
    win = onboarding.OnboardingWindow({"show_welcome_dialog": True})
    try:
        assert win._timer is not None
    finally:
        win.destroy()


def test_onboarding_scene_is_decorative(gtk):
    """The Cairo scene illustrates the text under it; it isn't read out."""
    from ui.onboarding import OnboardingWindow

    win = OnboardingWindow({"show_welcome_dialog": True})
    try:
        assert win._area.get_accessible_role() == gtk.Gtk.AccessibleRole.PRESENTATION
    finally:
        win.destroy()


# ── colour contrast ──────────────────────────────────────────────────────────
#
# libadwaita 1.5's named colours (Ubuntu 24.04, what CI and the supported
# distros ship), light / dark. Foregrounds with alpha are flattened onto the
# background they sit on.
_ADW = {
    "light": {
        "window_bg": "#fafafa",
        "window_fg": ("#000000", 0.8),
        "view_bg": "#ffffff",
        "view_fg": ("#000000", 0.8),
        "card_bg": "#ffffff",
        "popover_bg": "#ffffff",
        "popover_fg": ("#000000", 0.8),
        "warning": "#9c6e03",
        "error": "#c01c28",
    },
    "dark": {
        "window_bg": "#242424",
        "window_fg": ("#ffffff", 1.0),
        "view_bg": "#1e1e1e",
        "view_fg": ("#ffffff", 1.0),
        "card_bg": "#363636",  # rgba(255,255,255,0.08) over the window
        "popover_bg": "#383838",
        "popover_fg": ("#ffffff", 1.0),
        "warning": "#f8e45c",
        "error": "#ff7b63",
    },
}


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _flatten(fg: str, alpha: float, bg: str) -> str:
    f, b = _rgb(fg), _rgb(bg)
    return "#" + "".join(
        f"{round(alpha * x + (1 - alpha) * y):02x}" for x, y in zip(f, b, strict=True)
    )


def _luminance(hex_colour: str) -> float:
    def lin(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = _rgb(hex_colour)
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _css_block(css: str, selector: str) -> dict[str, str]:
    """The declarations of the first rule whose selector list is exactly
    `selector` (whitespace-insensitive)."""
    import re

    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        sel = " ".join(m.group(1).split())
        if sel == selector:
            props = {}
            for decl in m.group(2).split(";"):
                if ":" in decl:
                    k, v = decl.split(":", 1)
                    props[k.strip()] = " ".join(v.split())
            return props
    raise AssertionError(f"no CSS rule for {selector!r}")


def _define(css: str, name: str) -> str:
    """A @define-color's value; the last definition wins, as in GTK."""
    import re

    found = re.findall(rf"@define-color {name} (#[0-9a-fA-F]{{6}});", css)
    assert found, name
    return found[-1]


TEXT = 4.5  # WCAG 1.4.3, normal text
UI = 3.0  # WCAG 1.4.11, component boundaries and state


@pytest.fixture(scope="module")
def theme_css():
    try:
        from ui import popup, recipe_wizard, theme
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 unavailable: {exc}")
    return SimpleNamespace(
        theme=theme,
        brand=theme._BRAND_CSS,
        brand_dark=theme._BRAND_CSS + theme._BRAND_DARK_CSS,
        popup_dark=popup._CSS.decode(),
        popup_light=theme._apply_remap(popup._CSS, theme._LIGHT_REMAP).decode(),
        wizard=recipe_wizard._MOCK_CSS,
    )


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_popup_bar_contrast(theme_css, mode):
    css = theme_css.popup_dark if mode == "dark" else theme_css.popup_light
    bar = _css_block(css, "box.vinopop-bar")["background-color"]
    action = _css_block(css, "button.vinopop-action")["color"]
    assert contrast(action, bar) >= TEXT
    icon = _css_block(css, "button.vinopop-action image")["color"]
    assert contrast(icon, bar) >= UI
    overflow = _css_block(css, "button.vinopop-overflow")["color"]
    assert contrast(overflow, bar) >= TEXT
    for state in ("button.vinopop-action:hover", "button.vinopop-action.vinopop-selected"):
        block = _css_block(css, state)
        assert contrast(block["color"], block["background-color"]) >= TEXT, state
        # The keyboard highlight has to stand out from the bar itself
        assert contrast(block["background-color"], bar) >= UI, state
    active = _css_block(css, "button.vinopop-action:active")
    assert contrast(active["color"], active["background-color"]) >= TEXT


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_brand_accent_text_contrast(theme_css, mode):
    css = theme_css.brand_dark if mode == "dark" else theme_css.brand
    adw = _ADW[mode]
    accent = _define(css, "accent_color")
    for bg in ("window_bg", "card_bg", "view_bg", "popover_bg"):
        assert contrast(accent, adw[bg]) >= TEXT, bg
    accent_bg = _define(css, "accent_bg_color")
    accent_fg = _define(css, "accent_fg_color")
    assert contrast(accent_fg, accent_bg) >= TEXT


def test_badges_and_hero_contrast(theme_css):
    css = theme_css.brand
    glyph = _css_block(css, ".lp-badge-letter")["color"]
    for i in range(4):
        bg = _css_block(css, f".lp-badge-{i}")["background-color"]
        assert contrast(glyph, bg) >= TEXT, f".lp-badge-{i}"
    hero = _css_block(css, ".lp-hero")
    assert contrast(hero["color"], hero["background-color"]) >= TEXT


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_muted_text_contrast(theme_css, mode):
    adw = _ADW[mode]
    opacity = float(_css_block(theme_css.brand, ".lp-muted")["opacity"])
    fg, alpha = adw["window_fg"]
    for bg in ("window_bg", "card_bg"):
        muted = _flatten(fg, alpha * opacity, adw[bg])
        assert contrast(muted, adw[bg]) >= TEXT, bg


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_ai_bubble_text_contrast(gtk, mode):
    import re

    from ui import ai_bubble

    adw = _ADW[mode]
    title = _css_block(ai_bubble._CSS, "label.vinopop-ai-title")["color"]
    m = re.fullmatch(r"alpha\(@popover_fg_color, ([0-9.]+)\)", title)
    assert m, title
    fg, alpha = adw["popover_fg"]
    flat = _flatten(fg, alpha * float(m.group(1)), adw["popover_bg"])
    assert contrast(flat, adw["popover_bg"]) >= TEXT
    assert contrast(adw["warning"], adw["popover_bg"]) >= TEXT


def test_recipe_wizard_mock_and_tiles_contrast(theme_css):
    css = theme_css.wizard
    bar = _css_block(css, ".lp-mock-popup")["background-color"]
    assert contrast(_css_block(css, ".lp-mock-popup button")["color"], bar) >= TEXT
    new = _css_block(css, ".lp-mock-popup button.lp-mock-new")
    assert contrast(new["color"], new["background-color"]) >= TEXT
    # The chosen action tile's ring: brand accent, against the card in both modes
    ring = _css_block(css, "button.card.lp-action-tile:checked")["box-shadow"]
    assert "@accent_color" in ring, ring
    for mode, css_all in (("light", theme_css.brand), ("dark", theme_css.brand_dark)):
        accent = _define(css_all, "accent_color")
        assert contrast(accent, _ADW[mode]["card_bg"]) >= UI, mode


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_text_view_hint_contrast(theme_css, mode):
    """Sample text in Settings' text areas and the wizard's template box: the
    view's own colour at PLACEHOLDER_ALPHA."""
    adw = _ADW[mode]
    fg, _alpha = adw["view_fg"]
    hint = _flatten(fg, theme_css.theme.PLACEHOLDER_ALPHA, adw["view_bg"])
    assert contrast(hint, adw["view_bg"]) >= TEXT


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_recipe_warning_contrast(theme_css, mode):
    adw = _ADW[mode]
    block = _css_block(theme_css.wizard, ".lp-warning")
    assert block["color"] == "@error_color"
    tint = _flatten(adw["error"], 0.12, adw["window_bg"])
    assert contrast(adw["error"], tint) >= TEXT


# ── live tree (AT-SPI) ───────────────────────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("VINOPOP_ATSPI_TEST"),
    reason="set VINOPOP_ATSPI_TEST=1 to read the live AT-SPI tree (needs at-spi2-core and a session bus)",
)
def test_atspi_settings_window_has_no_unnamed_controls():
    """Open the Settings window in a child process and read what a screen
    reader would: no focusable control without a name."""
    import subprocess
    import sys
    import time

    pyatspi = pytest.importorskip("pyatspi")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from ui.settings_gui import SettingsDialog; from ui import gtk4_app;"
            "d = SettingsDialog(); d.show(); gtk4_app.run_window(d._window)",
        ],
        cwd=root,
        env={**os.environ, "GTK_A11Y": "atspi", "PYTHONPATH": root},
    )
    try:
        roles = {
            "push button",
            "toggle button",
            "check box",
            "switch",
            "entry",
            "text",
            "combo box",
        }
        found = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and found is None:
            time.sleep(0.5)
            desktop = pyatspi.Registry.getDesktop(0)
            for i in range(desktop.childCount):
                app = desktop.getChildAtIndex(i)
                if app is None:
                    continue
                for j in range(app.childCount):
                    frame = app.getChildAtIndex(j)
                    if frame is not None and frame.name == "vinoPop Settings":
                        found = frame
        assert found is not None, "Settings window never showed up on the AT-SPI bus"
        unnamed = []

        def walk(acc, path):
            try:
                role, name = acc.getRoleName(), acc.name
            except Exception:
                return
            if (role in roles or role == "list item") and not name.strip():
                unnamed.append(" > ".join([*path, role]))
            # A combo row's own drop-down list is libadwaita's to name
            if role == "combo box":
                return
            for k in range(acc.childCount):
                walk(acc.getChildAtIndex(k), [*path, f"{role} {name!r}" if name else role])

        walk(found, [])
        assert not unnamed, "\n".join(unnamed)
    finally:
        child.terminate()
        child.wait(timeout=10)
