"""Step-by-step assistant for building a custom vinoPop button (recipe).

Uses an Adw.NavigationView so the user gets one decision per page (with
the usual Adwaita back button / swipe), with a live popup-mockup updating
as they go. Each page hides the underlying "recipe JSON" jargon and uses
plain-language buttons (e.g. "Use selected text" instead of "{text_url}").

Same external interface as before - plugin_manager calls run_and_get()
and gets a recipe dict back (or None if cancelled).
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from plugin_system import recipe_loader  # noqa: E402
from ui.icon_picker import IconPicker  # noqa: E402

# --------------------------------------------------------------------------
# Action catalog -- the four building blocks of any custom button.
# Each entry carries everything the wizard needs to render its tile, its
# detail page, and its preview line.
# --------------------------------------------------------------------------

_ACTIONS = [
    dict(
        key="open_url",
        title="Open a web page",
        icon="web-browser-symbolic",
        blurb="Search a site, look up a word, open a map.",
        example="Search Wikipedia for the selected word.",
        # Detail page wording
        detail_prompt="What's the address of the page to open?",
        # Placeholder is a complete working URL with {text_url} visible
        # at the end - instantly shows where the selection will land.
        detail_placeholder="https://www.google.com/search?q={text_url}",
        insert_label="Insert the selected text here",
        insert_text="{text_url}",  # URL-safe encoded
        preview_label='If you select "hello world" and click your button:',
        preview_verb="Opens this address in your browser:",
        examples=[
            ("Google", "https://www.google.com/search?q={text_url}"),
            ("Wikipedia", "https://en.wikipedia.org/wiki/Special:Search?search={text_url}"),
            ("YouTube", "https://www.youtube.com/results?search_query={text_url}"),
            ("Maps", "https://www.google.com/maps/search/{text_url}"),
        ],
    ),
    dict(
        key="run_command",
        title="Run a command",
        icon="utilities-terminal-symbolic",
        blurb="Open a folder, run a script, anything you'd type in a terminal.",
        example="Open the selected file path in VS Code.",
        detail_prompt="What command should run?",
        # notify-send is a safe, instantly observable demo - runs without
        # side-effects and shows the {text} token in context so the user
        # can see how to use it. No quoting needed: a command gets every
        # placeholder as an argument, never as shell source.
        detail_placeholder="notify-send 'You selected:' {text}",
        insert_label="Insert the selected text here",
        insert_text="{text}",
        preview_label='If you select "hello world" and click your button:',
        preview_verb="Runs this command in the background:",
        warning="Commands can change or delete files. Test with something harmless first.",
        examples=[
            ("Open in default app", "xdg-open {text}"),
            ("Show notification", "notify-send 'You selected' {text}"),
            ("Open in VS Code", "code {text}"),
            ("Open folder in Files", 'xdg-open "$(dirname {text})"'),
            (
                "Count characters",
                "echo -n {text} | wc -c | xargs -I N notify-send 'Length' 'N characters'",
            ),
        ],
    ),
    dict(
        key="notify",
        title="Show a notification",
        icon="dialog-information-symbolic",
        blurb="Pop a short message on your desktop. Handy for confirmations.",
        example="Show how many characters you've selected.",
        detail_prompt="What should the notification say?",
        # Show the {text} token inline so the placement is obvious.
        detail_placeholder="Selected: {text}",
        insert_label="Insert the selected text",
        insert_text="{text}",
        preview_label='If you select "hello world" and click your button:',
        preview_verb="Shows this notification:",
        examples=[
            ("Plain", "Selected: {text}"),
            ("Trimmed", "Trimmed: {text_strip}"),
            ("Uppercase", "{text_upper}"),
            ("Lowercase", "{text_lower}"),
            ("URL-encoded", "URL-safe form: {text_url}"),
        ],
    ),
    dict(
        key="copy_transformed",
        title="Transform and copy",
        icon="edit-copy-symbolic",
        blurb="Make a tweaked copy of the text - wrap it, change its case, encode it.",
        example="Wrap the selection in markdown highlight: ==text==",
        detail_prompt="How should the copied text look?",
        # Markdown highlight is a useful starter that students/note-takers
        # will recognise. Demonstrates the wrap pattern without being
        # esoteric.
        detail_placeholder="=={text}==",
        insert_label="Insert the selected text",
        insert_text="{text}",
        preview_label='If you select "hello world" and click your button:',
        preview_verb="Copies this to your clipboard:",
        examples=[
            ("Markdown highlight", "=={text}=="),
            ("Markdown bold", "**{text}**"),
            ("Markdown quote", "> {text}"),
            ("Markdown code", "`{text}`"),
            ("Markdown link", "[{text}]()"),
            ("UPPERCASE", "{text_upper}"),
            ("lowercase", "{text_lower}"),
            ("In quotes", '"{text}"'),
            ("mOcKiNg cAsE", "{text_mock}"),
        ],
    ),
]

_ACTION_BY_KEY = {a["key"]: a for a in _ACTIONS}

# Extra inserter buttons offered for copy_transformed (this is the one
# action where a casual user might genuinely want a transform pipeline).
_TRANSFORM_INSERTERS = [
    ("Selected text", "{text}"),
    ("UPPERCASE", "{text_upper}"),
    ("lowercase", "{text_lower}"),
    ("URL-safe", "{text_url}"),
    ("Trimmed", "{text_strip}"),
    ("mOcKiNg cAsE", "{text_mock}"),
]

_CONTENT_TYPES = [
    ("url", "Web addresses"),
    ("email", "Email addresses"),
    ("path", "File paths"),
    ("command", "Shell commands"),
    ("plain_text", "Plain text"),
]

_COMMON_ICONS = [
    "applications-internet",
    "web-browser-symbolic",
    "system-search-symbolic",
    "mail-send-symbolic",
    "utilities-terminal-symbolic",
    "folder-open-symbolic",
    "accessories-text-editor-symbolic",
    "accessories-calculator-symbolic",
    "starred-symbolic",
    "emblem-favorite-symbolic",
    "emoji-people-symbolic",
    "view-list-symbolic",
    "format-text-bold-symbolic",
    "edit-copy-symbolic",
    "document-edit-symbolic",
]

_SAMPLE_SELECTION = "hello world"


# Wizard-local CSS. The live popup mockup keeps the dark premium-popup look
# on purpose (it imitates the real popup); everything else sits on stock
# Adwaita cards and colors so it follows the light/dark style. Loaded once
# per process.
_MOCK_CSS = """
.lp-mock-popup {
    background-color: #0e0e10;
    border: 1px solid #2a2c30;
    border-radius: 9px;
    padding: 6px 8px;
    box-shadow: 0 8px 20px rgba(0, 0, 0, 0.45);
}
.lp-mock-popup button {
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 5px 7px;
    min-width: 28px;
    min-height: 28px;
    color: #fafafa;
}
.lp-mock-popup button.lp-mock-new {
    background-color: #1f7d42;
    color: #ffffff;
    box-shadow: 0 0 0 2px rgba(86, 204, 120, 0.35);
}
.lp-step-tile {
    padding: 16px;
}
button.card.lp-action-tile {
    padding: 14px 16px;
}
button.card.lp-action-tile:checked {
    background-color: alpha(@accent_bg_color, 0.12);
    box-shadow: inset 0 0 0 2px @accent_bg_color;
}
.lp-step-number {
    background-color: @accent_bg_color;
    color: @accent_fg_color;
    border-radius: 50%;
    min-width: 28px;
    min-height: 28px;
    font-weight: 700;
}
.lp-warning {
    background-color: alpha(@error_color, 0.12);
    border: 1px solid alpha(@error_color, 0.4);
    border-radius: 8px;
    padding: 8px 12px;
    color: @error_color;
}
.lp-preview-card {
    padding: 12px;
}
"""
_css_installed = False


def _install_mock_css() -> None:
    global _css_installed
    if _css_installed:
        return
    try:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        if hasattr(provider, "load_from_string"):  # GTK 4.12+
            provider.load_from_string(_MOCK_CSS)
        else:
            provider.load_from_data(_MOCK_CSS, -1)
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        _css_installed = True
    except Exception:
        pass


def _icon(name: str, size: int) -> Gtk.Image:
    from ui import icon_style

    img = Gtk.Image.new_from_icon_name(icon_style.available(name))
    img.set_pixel_size(size)
    return img


def _label(text: str = "", *classes: str, markup: bool = False, wrap: bool = False) -> Gtk.Label:
    lbl = Gtk.Label(xalign=0)
    if markup:
        lbl.set_markup(text)
    else:
        lbl.set_text(text)
    for c in classes:
        lbl.add_css_class(c)
    if wrap:
        lbl.set_wrap(True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    return lbl


def _clear(box: Gtk.Box) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


# --------------------------------------------------------------------------
# RecipeWizard
# --------------------------------------------------------------------------

# (tag, page title) in navigation order
_STEPS = [
    ("intro", "Welcome"),
    ("action", "Pick action"),
    ("label", "Name + icon"),
    ("detail", "Details"),
    ("filter", "When to show"),
    ("confirm", "Done"),
]


class RecipeWizard:
    """Multi-step assistant. Same public API as the legacy wizard."""

    def __init__(self, parent=None, recipe: dict | None = None, source_path=None) -> None:
        self._parent = parent
        self._source_path = source_path
        existing = recipe or {}
        action = existing.get("action") or {}
        # All decisions live in this dict so each page is a pure view of state.
        self._state = dict(
            name=existing.get("name", ""),
            tooltip=existing.get("tooltip", ""),
            icon=existing.get("icon", ""),
            action_type=action.get("type", "open_url"),
            template=action.get("template", ""),
            content_types=list(existing.get("content_types") or []),
        )
        self._editing = bool(recipe)
        self._result: dict | None = None
        self._window: Adw.Window | None = None
        self._nav: Adw.NavigationView | None = None
        self._loop: GLib.MainLoop | None = None
        self._next_buttons: dict[str, Gtk.Button] = {}
        self._mock_button: Gtk.Button | None = None
        self._mock_label: Gtk.Label | None = None

    # ---- public entry point -------------------------------------------------

    def run_and_get(self) -> dict | None:
        """Show the wizard modally and block (nested main loop) until it is
        applied or cancelled. Returns the recipe dict or None."""
        _install_mock_css()
        win = Adw.Window(modal=True)
        win.set_title("Edit custom button" if self._editing else "New custom button")
        win.set_default_size(720, 560)
        if isinstance(self._parent, Gtk.Window):
            win.set_transient_for(self._parent)
        win.set_icon_name("vinopop")
        self._window = win

        self._nav = Adw.NavigationView()
        builders = {
            "intro": self._add_intro_page,
            "action": self._add_action_page,
            "label": self._add_label_page,
            "detail": self._add_detail_page,
            "filter": self._add_filter_page,
            "confirm": self._add_confirm_page,
        }
        # Build pages in order; each returns (content, initially-complete).
        for i, (tag, title) in enumerate(_STEPS):
            content, complete = builders[tag]()
            self._nav.add(self._wrap_page(i, tag, title, content))
            self._set_page_complete(tag, complete)
        win.set_content(self._nav)

        # Esc cancels, like the old assistant
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        win.add_controller(keys)

        self._loop = GLib.MainLoop()
        win.connect("close-request", self._on_close_request)
        win.present()
        self._loop.run()
        self._loop = None
        return self._result

    def _wrap_page(self, i: int, tag: str, title: str, content: Gtk.Widget) -> Adw.NavigationPage:
        """One navigation step: header (step counter + auto back button),
        scrollable content, and a bottom bar with Cancel / Next (or Apply)."""
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(
                title=self._window.get_title(),
                subtitle=f"Step {i + 1} of {len(_STEPS)} · {title}",
            )
        )

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(content)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bottom.set_margin_top(8)
        bottom.set_margin_bottom(10)
        bottom.set_margin_start(12)
        bottom.set_margin_end(12)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self._window.close())
        bottom.append(cancel)
        bottom.append(Gtk.Box(hexpand=True))
        last = i == len(_STEPS) - 1
        nxt = Gtk.Button(label="Apply" if last else "Next")
        nxt.add_css_class("suggested-action")
        nxt.add_css_class("pill")
        if last:
            nxt.connect("clicked", self._on_apply)
        else:
            nxt.connect("clicked", lambda _b, t=_STEPS[i + 1][0]: self._nav.push_by_tag(t))
        bottom.append(nxt)
        self._next_buttons[tag] = nxt

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(scroll)
        toolbar.add_bottom_bar(bottom)
        page = Adw.NavigationPage(title=title, tag=tag)
        page.set_child(toolbar)
        return page

    def _set_page_complete(self, tag: str, complete: bool) -> None:
        btn = self._next_buttons.get(tag)
        if btn is not None:
            btn.set_sensitive(bool(complete))

    def _on_key(self, _ctrl, keyval, _code, _state) -> bool:
        if keyval == Gdk.KEY_Escape and self._window is not None:
            self._window.close()
            return True
        return False

    def _on_close_request(self, _win) -> bool:
        if self._loop is not None and self._loop.is_running():
            self._loop.quit()
        return False  # let the window close

    @staticmethod
    def _page_box(spacing: int, orientation=Gtk.Orientation.VERTICAL, hmargin: int = 28) -> Gtk.Box:
        return Gtk.Box(
            orientation=orientation,
            spacing=spacing,
            margin_top=20,
            margin_bottom=20,
            margin_start=hmargin,
            margin_end=hmargin,
        )

    # ---- page 1: intro ------------------------------------------------------

    def _add_intro_page(self):
        page = self._page_box(18, hmargin=32)
        page.set_margin_top(24)
        page.set_margin_bottom(24)

        page.append(_label("Make your own vinoPop button", "title-1", wrap=True))
        page.append(
            _label(
                "Custom buttons turn any text you select into an action. We'll "
                "set yours up in a few short steps -- no coding needed.",
                "dim-label",
                wrap=True,
            )
        )

        # 3-step visual flow
        flow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14, margin_top=18)
        flow.set_homogeneous(True)
        for n, icon, head, body in [
            (1, "edit-select-all-symbolic", "Select", "Highlight any text\nin any app."),
            (
                2,
                "input-mouse-symbolic",
                "Click",
                "The vinoPop popup\nshows up. Pick your\nnew button.",
            ),
            (
                3,
                "starred-symbolic",
                "Done",
                "Whatever you set up\nhappens. Open a URL,\nrun a command -- you choose.",
            ),
        ]:
            flow.append(self._build_step_tile(n, icon, head, body))
        page.append(flow)

        hint = _label(
            "Click <b>Next</b> when you're ready. You can go back at any point.",
            "dim-label",
            "caption",
            markup=True,
        )
        hint.set_margin_top(10)
        page.append(hint)
        return page, True

    def _build_step_tile(self, n: int, icon_name: str, head: str, body: str) -> Gtk.Widget:
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        tile.add_css_class("card")
        tile.add_css_class("lp-step-tile")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        num = Gtk.Label(label=str(n))
        num.add_css_class("lp-step-number")
        num.set_xalign(0.5)
        num.set_yalign(0.5)
        num.set_valign(Gtk.Align.CENTER)
        top.append(num)
        top.append(_icon(icon_name, 48))
        tile.append(top)

        tile.append(_label(head, "heading"))
        tile.append(_label(body, "dim-label", wrap=True))
        return tile

    # ---- page 2: choose action type -----------------------------------------

    def _add_action_page(self):
        page = self._page_box(14)

        page.append(_label("What should your button do?", "title-2", wrap=True))
        page.append(
            _label(
                "Pick one. You can change it later if you want to try something else.",
                "dim-label",
                wrap=True,
            )
        )

        grid = Gtk.Grid(column_spacing=12, row_spacing=12, margin_top=8)
        grid.set_column_homogeneous(True)
        grid.set_row_homogeneous(True)

        # Toggle-button cards in one group: clicking (or Space/Enter on a
        # focused card) selects it, exactly one is selected at a time.
        self._action_tiles: dict[str, Gtk.ToggleButton] = {}
        group = None
        for i, spec in enumerate(_ACTIONS):
            tile = self._build_action_tile(spec)
            if group is None:
                group = tile
            else:
                tile.set_group(group)
            grid.attach(tile, i % 2, i // 2, 1, 1)
            self._action_tiles[spec["key"]] = tile

        page.append(grid)
        self._reflect_action_selection()
        return page, bool(self._state["action_type"])

    def _build_action_tile(self, spec: dict) -> Gtk.ToggleButton:
        tile = Gtk.ToggleButton()
        tile.add_css_class("card")
        tile.add_css_class("lp-action-tile")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.append(_icon(spec["icon"], 48))
        title = _label(spec["title"], "title-4", wrap=True)
        title.set_hexpand(True)
        row.append(title)
        box.append(row)

        box.append(_label(spec["blurb"], wrap=True))
        box.append(_label(f"Example: {spec['example']}", "dim-label", "caption", wrap=True))
        tile.set_child(box)

        def on_toggled(btn):
            if not btn.get_active():
                return
            if self._state["action_type"] == spec["key"]:
                return
            self._state["action_type"] = spec["key"]
            # Refresh detail page since contents depend on chosen action
            self._refresh_detail_page()
            self._refresh_confirm_page()
            self._refresh_mock_label()
            self._set_page_complete("action", True)

        tile.connect("toggled", on_toggled)
        return tile

    def _reflect_action_selection(self) -> None:
        chosen = self._state["action_type"]
        tile = getattr(self, "_action_tiles", {}).get(chosen)
        if tile is not None:
            tile.set_active(True)

    # ---- page 3: name & icon ------------------------------------------------

    def _add_label_page(self):
        page = self._page_box(16, Gtk.Orientation.HORIZONTAL, hmargin=24)

        # ---- LEFT column: inputs ----
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        left.set_hexpand(True)

        left.append(_label("Name it and pick an icon", "title-2", wrap=True))
        left.append(
            _label(
                "The icon is what shows up in the popup. The label is the tooltip when you hover.",
                "dim-label",
                wrap=True,
            )
        )

        # Tooltip / button label
        left.append(_label("What's the button called?", "heading"))

        self._tooltip_entry = Gtk.Entry()
        self._tooltip_entry.set_text(self._state.get("tooltip") or "")
        self._tooltip_entry.set_placeholder_text("e.g. Search Wikipedia")
        self._tooltip_entry.connect("changed", self._on_label_input)
        left.append(self._tooltip_entry)

        # Icon picker chips + browse button
        ilbl = _label("Pick an icon", "heading")
        ilbl.set_margin_top(6)
        left.append(ilbl)

        chips_scroll = Gtk.ScrolledWindow()
        chips_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        chips_scroll.set_min_content_height(110)
        chips_flow = Gtk.FlowBox()
        chips_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        chips_flow.set_max_children_per_line(7)
        chips_flow.set_homogeneous(True)
        chips_flow.set_valign(Gtk.Align.START)
        for name in _COMMON_ICONS:
            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.set_child(_icon(name, 32))
            btn.set_tooltip_text(name)
            btn.connect("clicked", self._on_icon_chip, name)
            chips_flow.append(btn)
        chips_scroll.set_child(chips_flow)
        left.append(chips_scroll)

        browse_btn = Gtk.Button(label="Browse all icons…")
        browse_btn.connect("clicked", self._on_browse_icons)
        left.append(browse_btn)

        page.append(left)

        # ---- RIGHT column: live preview ----
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        right.set_size_request(280, -1)

        right.append(_label("Live preview", "heading"))
        right.append(
            _label(
                "This is roughly how your button will appear in the vinoPop popup, "
                "next to the built-in ones.",
                "dim-label",
                "caption",
                wrap=True,
            )
        )

        # Mock popup bar
        mock = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        mock.set_halign(Gtk.Align.START)
        mock.add_css_class("lp-mock-popup")
        # A couple of dummy buttons + the new one (highlighted)
        for icon_name, tip in [
            ("edit-copy-symbolic", "Copy"),
            ("system-search-symbolic", "Search"),
        ]:
            b = Gtk.Button()
            b.set_child(_icon(icon_name, 24))
            b.set_tooltip_text(tip)
            b.set_can_focus(False)
            mock.append(b)
        # The user's button (highlighted)
        self._mock_button = Gtk.Button()
        self._mock_button.add_css_class("lp-mock-new")
        self._mock_button.set_can_focus(False)
        self._refresh_mock_button()
        mock.append(self._mock_button)

        right.append(mock)

        self._mock_label = _label("", "dim-label", wrap=True)
        self._refresh_mock_label()
        right.append(self._mock_label)

        page.append(right)
        return page, self._label_page_complete()

    def _label_page_complete(self) -> bool:
        return bool((self._state.get("tooltip") or "").strip())

    def _on_label_input(self, _entry) -> None:
        self._state["tooltip"] = self._tooltip_entry.get_text()
        self._set_page_complete("label", self._label_page_complete())
        self._refresh_mock_button()
        self._refresh_mock_label()
        self._refresh_confirm_page()

    def _on_icon_chip(self, _btn, name: str) -> None:
        self._state["icon"] = name
        self._refresh_mock_button()
        self._refresh_confirm_page()

    def _on_browse_icons(self, _btn) -> None:
        picker = IconPicker(
            parent=self._window, initial=self._state.get("icon") or "applications-other"
        )
        chosen = picker.run()
        if chosen:
            self._state["icon"] = chosen
            self._refresh_mock_button()
            self._refresh_confirm_page()

    def _refresh_mock_button(self) -> None:
        if self._mock_button is None:
            return
        icon_name = self._state.get("icon") or "applications-other"
        self._mock_button.set_child(_icon(icon_name, 24))
        self._mock_button.set_tooltip_text(self._state.get("tooltip") or "Your button")

    def _refresh_mock_label(self) -> None:
        if self._mock_label is None:
            return
        name = (self._state.get("tooltip") or "").strip() or "Your button"
        spec = _ACTION_BY_KEY[self._state["action_type"]]
        self._mock_label.set_markup(
            f"Hovering shows: <b>{GLib.markup_escape_text(name)}</b>\n"
            f"<span size='small'>Clicking will: {GLib.markup_escape_text(spec['blurb'].lower())}</span>"
        )

    # ---- page 4: action details --------------------------------------------

    def _add_detail_page(self):
        # We hold a reference to the outer container so we can wipe and
        # rebuild it when the user changes action type on page 2.
        self._detail_container = self._page_box(12)
        self._refresh_detail_page()
        return self._detail_container, self._detail_page_complete()

    def _detail_page_complete(self) -> bool:
        # Pre-filled placeholder text doesn't count - only actual user
        # input (typed, pasted, or picked from a chip). _placeholder_active
        # is True only while the dimmed sample is on screen.
        if getattr(self, "_placeholder_active", False):
            return False
        return bool((self._state.get("template") or "").strip())

    def _refresh_detail_page(self) -> None:
        if not hasattr(self, "_detail_container"):
            return
        # Wipe existing children
        _clear(self._detail_container)

        spec = _ACTION_BY_KEY[self._state["action_type"]]

        self._detail_container.append(_label(spec["title"], "title-2", wrap=True))
        self._detail_container.append(_label(spec["detail_prompt"], "heading", wrap=True))

        # Template editor
        tmpl_scroll = Gtk.ScrolledWindow()
        tmpl_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        tmpl_scroll.set_min_content_height(70)
        tmpl_scroll.set_has_frame(True)
        self._template_view = Gtk.TextView()
        self._template_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        # Monospace via Adwaita's style class (override_font is gone in GTK 4)
        self._template_view.add_css_class("monospace")
        self._template_view.set_top_margin(8)
        self._template_view.set_bottom_margin(8)
        self._template_view.set_left_margin(8)
        self._template_view.set_right_margin(8)
        self._template_buffer = self._template_view.get_buffer()

        # Style tag for the dimmed sample text. Has to live on the
        # buffer's tag table - recreated per page render because we
        # rebuild the whole detail container when action type changes.
        self._placeholder_tag = self._template_buffer.create_tag(
            "lp-placeholder",
            foreground="#6b7080",  # muted grey, readable on light and dark
            style=Pango.Style.ITALIC,
        )

        # If the user already typed something on a previous visit, show
        # that and treat as real input. Otherwise drop the sample text
        # in with the dim-italic tag, and remember it's "just a hint".
        existing = self._state.get("template") or ""
        if existing:
            self._template_buffer.set_text(existing)
            self._placeholder_active = False
        else:
            placeholder_text = spec.get("detail_placeholder") or ""
            if placeholder_text:
                self._template_buffer.set_text(placeholder_text)
                start, end = self._template_buffer.get_bounds()
                self._template_buffer.apply_tag(self._placeholder_tag, start, end)
                self._placeholder_active = True
            else:
                self._placeholder_active = False

        # Connect AFTER the initial set_text so the seed itself doesn't
        # trip the change handler and clobber _placeholder_active.
        self._template_buffer.connect("changed", self._on_template_changed)
        # Wipe the dim sample as soon as the user clicks into the field or
        # starts typing - avoids the weird state where their typing lands
        # next to greyed placeholder content. (Not on plain focus-in: the
        # navigation view focuses the field when the page opens, which
        # would wipe the sample before the user ever saw it.)
        click = Gtk.GestureClick()
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", lambda *_: self._on_template_focus_in())
        self._template_view.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_template_key)
        self._template_view.add_controller(keys)
        tmpl_scroll.set_child(self._template_view)
        self._detail_container.append(tmpl_scroll)

        # Inserter row -- plain-language buttons that drop the right token in
        if spec["key"] == "copy_transformed":
            # Several flavor options (wrap onto more lines when narrow)
            inserter_row = Gtk.FlowBox()
            inserter_row.set_selection_mode(Gtk.SelectionMode.NONE)
            inserter_row.set_max_children_per_line(6)
            inserter_row.set_column_spacing(8)
            inserter_row.set_row_spacing(8)
            for label, token in _TRANSFORM_INSERTERS:
                btn = Gtk.Button(label=f"Insert {label}")
                btn.set_tooltip_text("Click to drop this into your template at the cursor.")
                btn.connect("clicked", self._on_insert, token)
                inserter_row.append(btn)
        else:
            inserter_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            btn = Gtk.Button(label=spec["insert_label"])
            btn.add_css_class("suggested-action")
            btn.set_tooltip_text(
                "Click to drop the selected text into your "
                + (
                    "URL"
                    if spec["key"] == "open_url"
                    else "command"
                    if spec["key"] == "run_command"
                    else "message"
                )
                + " at the cursor."
            )
            btn.connect("clicked", self._on_insert, spec["insert_text"])
            inserter_row.append(btn)
        self._detail_container.append(inserter_row)

        # More-placeholders chip row: dynamic tokens recipes pick up
        # from the snippet engine. Inserting one of these drops a tag
        # like {date} or {clipboard} into the template at the cursor;
        # it gets resolved when the button is actually clicked, not now.
        # In a command these are as safe as {text}: {clipboard} and
        # {selection} reach bash as arguments, never as shell source.
        extra_label = _label(
            "Or insert a dynamic value (filled in when the button runs):",
            "dim-label",
            "caption",
            wrap=True,
        )
        extra_label.set_margin_top(6)
        self._detail_container.append(extra_label)
        extra_flow = Gtk.FlowBox()
        extra_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        extra_flow.set_max_children_per_line(7)
        extra_flow.set_column_spacing(4)
        extra_flow.set_row_spacing(4)
        for chip_label, chip_token, chip_tip in [
            ("{date}", "{date}", "Today's date, e.g. 2026-05-27"),
            ("{time}", "{time}", "Current time, e.g. 14:30"),
            ("{weekday}", "{weekday}", "Name of the day"),
            ("{date:+7d}", "{date:+7d}", "Date math: +7d, -1w, +3m, +2y"),
            ("{name}", "{name}", "Your full name (from your user account)"),
            ("{clipboard}", "{clipboard}", "Whatever's on your clipboard when the button runs"),
            ("{selection}", "{selection}", "Whatever text is highlighted on screen"),
        ]:
            chip = Gtk.Button(label=chip_label)
            chip.set_tooltip_text(chip_tip)
            chip.connect("clicked", lambda _b, t=chip_token: self._on_insert(None, t))
            extra_flow.append(chip)
        self._detail_container.append(extra_flow)

        # "Try also" chip row - replaces the template with a known-good
        # example. Saves the user from having to invent valid patterns
        # by hand the first time they meet an action type.
        examples = spec.get("examples") or []
        if examples:
            ex_label = _label(
                "Try one of these - click to fill the box above:", "dim-label", "caption", wrap=True
            )
            ex_label.set_margin_top(4)
            self._detail_container.append(ex_label)

            ex_flow = Gtk.FlowBox()
            ex_flow.set_selection_mode(Gtk.SelectionMode.NONE)
            ex_flow.set_homogeneous(False)
            ex_flow.set_max_children_per_line(8)
            ex_flow.set_row_spacing(4)
            ex_flow.set_column_spacing(4)
            for label, template in examples:
                btn = Gtk.Button(label=label)
                btn.set_tooltip_text(template)
                btn.connect("clicked", self._on_example_pick, template)
                ex_flow.append(btn)
            self._detail_container.append(ex_flow)

        # Warning (run_command)
        if spec.get("warning"):
            warn = _label(
                "<b>Heads up:</b> " + GLib.markup_escape_text(spec["warning"]),
                "lp-warning",
                markup=True,
                wrap=True,
            )
            self._detail_container.append(warn)

        # Preview card
        prev_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        prev_card.add_css_class("card")
        prev_card.add_css_class("lp-preview-card")
        prev_card.append(_label(spec["preview_label"], "heading", wrap=True))
        prev_card.append(_label(spec["preview_verb"], "dim-label", "caption", wrap=True))
        self._preview_result = _label("", "monospace", "accent", wrap=True)
        self._preview_result.set_selectable(True)
        self._refresh_preview_result()
        prev_card.append(self._preview_result)
        self._detail_container.append(prev_card)

        self._set_page_complete("detail", self._detail_page_complete())

    def _on_template_changed(self, buf: Gtk.TextBuffer) -> None:
        # Any actual buffer mutation means the sample text is no longer
        # what's there - clear the "this is just a hint" flag so the
        # Next button enables and the new content is real input.
        if getattr(self, "_placeholder_active", False):
            self._placeholder_active = False
            start, end = buf.get_bounds()
            buf.remove_tag(self._placeholder_tag, start, end)
        start, end = buf.get_start_iter(), buf.get_end_iter()
        self._state["template"] = buf.get_text(start, end, True)
        self._set_page_complete("detail", self._detail_page_complete())
        self._refresh_preview_result()
        self._refresh_confirm_page()

    def _on_template_focus_in(self) -> None:
        """Clear the dim placeholder on first click/keypress so the user
        types into an empty field, not into the middle of the sample."""
        if getattr(self, "_placeholder_active", False):
            self._placeholder_active = False
            self._template_buffer.set_text("")

    _NON_EDIT_KEYS = {
        Gdk.KEY_Tab,
        Gdk.KEY_ISO_Left_Tab,
        Gdk.KEY_Escape,
        Gdk.KEY_Shift_L,
        Gdk.KEY_Shift_R,
        Gdk.KEY_Control_L,
        Gdk.KEY_Control_R,
        Gdk.KEY_Alt_L,
        Gdk.KEY_Alt_R,
        Gdk.KEY_Super_L,
        Gdk.KEY_Super_R,
    }

    def _on_template_key(self, _ctrl, keyval, _code, _state) -> bool:
        if keyval not in self._NON_EDIT_KEYS:
            self._on_template_focus_in()
        return False  # let the text view handle the key as usual

    def _on_insert(self, _btn, token: str) -> None:
        # If the sample text is still on screen, replace it wholesale
        # rather than appending the token to it - otherwise the user
        # ends up with "=={text}==<their token>" which is rarely what
        # they want.
        if getattr(self, "_placeholder_active", False):
            self._placeholder_active = False
            self._template_buffer.set_text(token)
        else:
            if self._template_buffer.get_has_selection():
                self._template_buffer.delete_selection(True, True)
            self._template_buffer.insert_at_cursor(token)
        self._template_view.grab_focus()

    def _on_example_pick(self, _btn, template: str) -> None:
        """Replace the template buffer with the chosen example. The user
        can still hand-edit afterwards - chips are starting points, not
        final answers."""
        self._template_buffer.set_text(template)
        self._template_view.grab_focus()

    def _command_preview(self) -> str:
        # The command as it effectively runs, with the sample selection
        # shell-quoted, rather than the raw text pasted into the template.
        return recipe_loader.command_preview(self._state.get("template") or "", _SAMPLE_SELECTION)

    def _refresh_preview_result(self) -> None:
        if not hasattr(self, "_preview_result"):
            return
        rendered = recipe_loader._render(self._state.get("template") or "", _SAMPLE_SELECTION)
        if not rendered.strip():
            # When the placeholder is showing, the box isn't empty but
            # state.template is - explain that explicitly so the user
            # doesn't wonder why the preview is blank.
            hint = (
                "(the grey text above is just an example - type "
                "your own or click one of the chips to set it)"
                if getattr(self, "_placeholder_active", False)
                else "(nothing yet - pick a template above)"
            )
            self._preview_result.set_text(hint)
            return
        spec = _ACTION_BY_KEY[self._state["action_type"]]
        if spec["key"] == "open_url":
            self._preview_result.set_text(rendered.strip())
        elif spec["key"] == "run_command":
            self._preview_result.set_text("$ " + self._command_preview().strip())
        elif spec["key"] == "notify":
            self._preview_result.set_text(rendered.strip())
        else:
            self._preview_result.set_text(rendered.strip())

    # ---- page 5: when to show -----------------------------------------------

    def _add_filter_page(self):
        page = self._page_box(12)

        page.append(_label("When should it show up?", "title-2", wrap=True))
        page.append(
            _label("You can leave this as 'always' -- most people do.", "dim-label", wrap=True)
        )

        # Radio-style check buttons (Gtk.RadioButton is gone in GTK 4)
        self._always_radio = Gtk.CheckButton(label="Always -- show on any selected text")
        page.append(self._always_radio)

        self._only_radio = Gtk.CheckButton(label="Only when I've selected one of these:")
        self._only_radio.set_group(self._always_radio)
        page.append(self._only_radio)
        if self._state["content_types"]:
            self._only_radio.set_active(True)
        else:
            self._always_radio.set_active(True)

        # Indented checkboxes
        cb_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=28)
        self._type_checks: dict[str, Gtk.CheckButton] = {}
        for key, label in _CONTENT_TYPES:
            cb = Gtk.CheckButton(label=label)
            cb.set_active(key in self._state["content_types"])
            cb.connect("toggled", self._on_filter_changed)
            cb_box.append(cb)
            self._type_checks[key] = cb
        page.append(cb_box)

        # Wire radios so checkboxes only matter when "Only" is active
        def sync(*_):
            only = self._only_radio.get_active()
            cb_box.set_sensitive(only)
            self._on_filter_changed()

        self._always_radio.connect("toggled", sync)
        self._only_radio.connect("toggled", sync)
        sync()
        return page, True

    def _on_filter_changed(self, *_):
        if self._only_radio.get_active():
            self._state["content_types"] = [
                k for k, cb in self._type_checks.items() if cb.get_active()
            ]
        else:
            self._state["content_types"] = []
        self._refresh_confirm_page()

    # ---- page 6: confirm ----------------------------------------------------

    def _add_confirm_page(self):
        self._confirm_container = self._page_box(14)
        self._refresh_confirm_page()
        return self._confirm_container, True

    def _refresh_confirm_page(self) -> None:
        if not hasattr(self, "_confirm_container"):
            return
        _clear(self._confirm_container)

        spec = _ACTION_BY_KEY[self._state["action_type"]]

        self._confirm_container.append(_label("Looks good?", "title-2"))
        self._confirm_container.append(
            _label(
                "Press <b>Apply</b> to save. You can edit or delete this "
                "button later from the Plugin Manager.",
                "dim-label",
                markup=True,
                wrap=True,
            )
        )

        # Summary card
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        card.add_css_class("card")
        card.add_css_class("lp-preview-card")

        icon_img = _icon(self._state.get("icon") or "applications-other", 48)
        icon_img.set_valign(Gtk.Align.START)
        card.append(icon_img)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        info.set_hexpand(True)
        name = (self._state.get("tooltip") or "").strip() or "(unnamed)"
        info.append(_label(name, "title-4", wrap=True))
        info.append(_label(spec["blurb"], "dim-label", wrap=True))

        # Render the actual outcome for the sample selection
        rendered = recipe_loader._render(self._state.get("template") or "", _SAMPLE_SELECTION)
        info.append(
            _label(
                f'When you select <b>"{_SAMPLE_SELECTION}"</b> and click it:',
                "caption",
                markup=True,
            )
        )

        result_lbl = _label("", "monospace", "accent", wrap=True)
        result_lbl.set_selectable(True)
        if spec["key"] == "run_command":
            result_lbl.set_text("$ " + self._command_preview().strip())
        else:
            result_lbl.set_text(rendered.strip() or "(empty)")
        info.append(result_lbl)

        # When to show
        when = (
            ", ".join(label for k, label in _CONTENT_TYPES if k in self._state["content_types"])
            or "any selected text"
        )
        info.append(_label(f"Shows on: {when}", "dim-label", "caption", wrap=True))

        card.append(info)
        self._confirm_container.append(card)

        # Validation hint if something's missing
        provisional = self._build_recipe_dict()
        errors = recipe_loader.validate(provisional) + recipe_loader.safety_errors(provisional)
        if errors:
            warn = _label(
                "<b>One more thing:</b> " + GLib.markup_escape_text(errors[0]),
                "lp-warning",
                markup=True,
                wrap=True,
            )
            self._confirm_container.append(warn)

    # ---- save ---------------------------------------------------------------

    def _build_recipe_dict(self) -> dict:
        # Derive an internal name from the user's button label if they're
        # creating fresh -- they shouldn't have to invent a filename.
        name = self._state.get("name") or _slugify(self._state.get("tooltip") or "custom-button")
        return dict(
            name=name,
            tooltip=(self._state.get("tooltip") or "").strip(),
            icon=self._state.get("icon") or "applications-other",
            content_types=list(self._state["content_types"]),
            action=dict(
                type=self._state["action_type"],
                template=(self._state.get("template") or "").strip(),
            ),
        )

    def _on_apply(self, _btn) -> None:
        recipe = self._build_recipe_dict()
        errors = recipe_loader.validate(recipe) + recipe_loader.safety_errors(recipe)
        if errors:
            msg = Adw.AlertDialog(heading="Couldn't save just yet", body="\n".join(errors))
            msg.add_response("ok", "OK")
            msg.set_default_response("ok")
            msg.present(self._window)
            return
        self._result = recipe
        # Closing ends the nested main loop in run_and_get().
        self._window.close()


def _slugify(name: str) -> str:
    out = []
    for ch in name.lower().strip():
        if ch.isalnum():
            out.append(ch)
        elif ch in " -_":
            out.append("-")
    slug = "".join(out).strip("-")
    return slug or "custom-button"
