"""Plugin manager window using libadwaita boxed-list rows."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import gi

from xdg_paths import CONFIG_DIR

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

import gtk4_app  # noqa: E402

# libhandy needed two tree-walking hacks here (_unwrap_subtitle_labels and
# _unellipsize_tab_labels) because HdyActionRow ellipsised subtitles and
# HdyViewSwitcherButton cropped tab labels in C. Adw.ActionRow wraps its
# subtitle by default (subtitle-lines=0) and Adw.ViewSwitcher sizes its
# labels properly, so both are gone.


def _force_to_front(window: Gtk.Window) -> None:
    """Bring the window to the foreground. GTK 4 has no keep_above /
    GdkWindow.raise_() any more; present() is the only portable request
    and the compositor decides the rest."""
    try:
        window.unminimize()
    except Exception:
        pass
    window.present()


def _plain(row: Gtk.Widget) -> Gtk.Widget:
    """Adw rows parse title/subtitle as Pango markup by default; plugin
    titles and descriptions are plain text (they contain '&', '<' ...)."""
    row.set_use_markup(False)
    return row


def _icon_button(icon_name: str, tooltip: str | None = None) -> Gtk.Button:
    btn = Gtk.Button.new_from_icon_name(icon_name)
    btn.set_valign(Gtk.Align.CENTER)
    if tooltip:
        btn.set_tooltip_text(tooltip)
    return btn


REPO_DIR = Path(__file__).resolve().parent / "plugins_repo"
USER_PLUGIN_DIR = CONFIG_DIR / "plugins"
USER_RECIPE_DIR = CONFIG_DIR / "recipes"


def _target_dir(entry: dict) -> Path:
    """Where this catalogue entry installs to, based on its kind."""
    return USER_RECIPE_DIR if entry.get("kind") == "recipe" else USER_PLUGIN_DIR


def _source_path(entry: dict) -> Path:
    """Where the bundled source lives inside plugins_repo."""
    if entry.get("kind") == "recipe":
        return REPO_DIR / "recipes" / entry["file"]
    return REPO_DIR / entry["file"]


def _load_manifest() -> list[dict]:
    manifest_path = REPO_DIR / "manifest.json"
    if not manifest_path.is_file():
        return []
    try:
        with manifest_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[plugin_manager] could not read manifest: {exc}")
        return []


def _status(entry: dict) -> str:
    """The installed copy of a catalogue entry against the bundled one:
    "missing", "current", "outdated" or "changed" (see plugin_loader)."""
    import plugin_loader

    return plugin_loader.bundled_status(
        _source_path(entry), _target_dir(entry) / entry["file"], int(entry.get("version") or 0)
    )


def _is_installed(entry_or_filename) -> bool:
    """Check whether a catalogue entry (dict) or filename (str) is installed."""
    if isinstance(entry_or_filename, dict):
        return (_target_dir(entry_or_filename) / entry_or_filename["file"]).is_file()
    # Backwards-compat: string means a plugin .py file
    return (USER_PLUGIN_DIR / entry_or_filename).is_file()


def _recipe_row_text(
    path: Path, recipe: dict, atype_labels: dict[str, str], retired: dict[str, str] | None = None
) -> tuple[str, str]:
    """Title and subtitle for a recipe's row in the Custom tab. The JSON is
    hand-editable and may not validate (that recipe just doesn't load), so
    every field is coerced to a string here rather than trusted."""
    action = recipe.get("action")
    if not isinstance(action, dict):
        action = {}
    title = str(recipe.get("tooltip") or recipe.get("name") or path.stem)
    atype = str(action.get("type") or "")
    template = str(action.get("template") or "")
    short = template if len(template) <= 70 else template[:67] + "…"
    action_label = atype_labels.get(atype, atype or "Custom action")
    subtitle = f"{action_label}  ·  {short}"
    if retired and path.name in retired:
        subtitle += f"\nNo longer bundled with vinoPop. {retired[path.name]}"
    return title, subtitle


class PluginManagerDialog:
    def __init__(self, on_changed: Callable[[], None] | None = None) -> None:
        gtk4_app.init()
        self._on_changed = on_changed
        self._window: Adw.PreferencesWindow | None = None
        self._catalog_group: Adw.PreferencesGroup | None = None
        self._catalog_groups: list[Adw.PreferencesGroup] = []
        self._catalog_page: Adw.PreferencesPage | None = None
        self._installed_group: Adw.PreferencesGroup | None = None
        self._order_group: Adw.PreferencesGroup | None = None
        self._custom_group: Adw.PreferencesGroup | None = None
        # Adw.PreferencesGroup has no public "list my rows" API, so every
        # row we add to a refreshable group is tracked here for removal.
        self._group_rows: dict[Adw.PreferencesGroup, list[Gtk.Widget]] = {}

    def _add_row(self, group: Adw.PreferencesGroup, row: Gtk.Widget) -> None:
        group.add(row)
        self._group_rows.setdefault(group, []).append(row)

    def _clear_group(self, group: Adw.PreferencesGroup) -> None:
        for row in self._group_rows.pop(group, []):
            group.remove(row)

    def show(self, tab: str | None = None) -> None:
        """Open the Plugin Manager. tab may be one of 'available',
        'installed', 'custom', 'order' to land on a specific page;
        defaults to 'available'."""
        if self._window is not None and self._window.get_visible():
            if tab:
                self._switch_to_tab(tab)
            _force_to_front(self._window)
            return

        win = Adw.PreferencesWindow()
        win.set_title("vinoPop - Plugins")
        win.set_search_enabled(False)
        win.set_default_size(780, 620)
        win.set_icon_name("vinopop")
        win.set_modal(False)
        win.connect("destroy", self._on_destroy)
        # GTK 4 may keep the destroyed window alive while Python holds a
        # ref, so drop our references on close-request as well.
        win.connect("close-request", lambda w: (self._on_destroy(w), False)[1])

        # Tab 1: catalog. Built as one PreferencesGroup per category so
        # the user sees Productivity / Web / AI / Developer separately
        # rather than one undifferentiated flat list. _catalog_group is
        # tracked as a reference to the *first* group purely so the
        # parent-page lookup in _rebuild_catalog_buttons still works.
        import icon_style

        catalog_page = Adw.PreferencesPage()
        catalog_page.set_title("Available")
        catalog_page.set_icon_name(icon_style.available("system-software-install-symbolic"))
        self._catalog_groups = []
        for group in self._build_catalog_groups():
            catalog_page.add(group)
            self._catalog_groups.append(group)
        self._catalog_group = self._catalog_groups[0] if self._catalog_groups else None
        self._catalog_page = catalog_page
        win.add(catalog_page)

        # Tab 2: installed
        installed_page = Adw.PreferencesPage()
        installed_page.set_title("Installed")
        installed_page.set_icon_name(icon_style.available("emblem-default-symbolic"))
        self._installed_group = self._build_installed_group()
        installed_page.add(self._installed_group)
        win.add(installed_page)

        # Tab 3: custom actions (recipes)
        custom_page = Adw.PreferencesPage()
        custom_page.set_title("Custom")
        custom_page.set_icon_name("document-new-symbolic")
        self._custom_group = self._build_custom_group()
        custom_page.add(self._custom_group)
        win.add(custom_page)

        # Tab 4: order
        order_page = Adw.PreferencesPage()
        order_page.set_title("Order")
        order_page.set_icon_name("view-sort-ascending-symbolic")
        self._order_group = self._build_order_group()
        order_page.add(self._order_group)
        win.add(order_page)

        self._window = win
        # Remember the pages we created so we can switch tabs later.
        self._pages_by_tab = {
            "available": catalog_page,
            "installed": installed_page,
            "custom": custom_page,
            "order": order_page,
        }
        if tab:
            self._switch_to_tab(tab)
        _force_to_front(win)

    def _switch_to_tab(self, tab: str) -> None:
        """Activate one of the PreferencesWindow's tabs by name. Names
        match the keys of _pages_by_tab. No-op for unknown tabs."""
        if not hasattr(self, "_pages_by_tab"):
            return
        page = self._pages_by_tab.get(tab)
        if page is None or self._window is None:
            return
        self._window.set_visible_page(page)

    def _on_destroy(self, win=None, *_):
        # Ignore a late signal from a window we already replaced.
        if win is not None and self._window is not None and win is not self._window:
            return
        self._window = None
        self._catalog_group = None
        self._catalog_groups = []
        self._catalog_page = None
        self._installed_group = None
        self._order_group = None
        self._custom_group = None
        self._group_rows = {}

    # ---- catalog tab ---------------------------------------------------------

    # Display order for category sections. Categories present in the
    # manifest but missing here are appended in first-seen order.
    _CATEGORY_ORDER = [
        "Snippets & Clipboard",
        "Productivity",
        "Web shortcuts",
        "AI",
        "Developer",
    ]
    _CATEGORY_DESCRIPTIONS = {
        "Snippets & Clipboard": "The picker that remembers what you've copied and the snippets "
        "you save for reuse. The core of what vinoPop is for.",
        "Productivity": "Everyday actions - math, counts, speech, formatting.",
        "Web shortcuts": "Open the selection in a search engine or web service.",
        "AI": "Summarize, rewrite, explain and translate offline, on your NPU, GPU or CPU.",
        "Developer": "Encoders, hashes and converters for technical text.",
    }

    def _build_catalog_groups(self) -> list[Adw.PreferencesGroup]:
        manifest = _load_manifest()
        if not manifest:
            group = Adw.PreferencesGroup()
            group.set_title("Plugin catalogue")
            row = _plain(Adw.ActionRow())
            row.set_title("Catalogue is empty")
            row.set_subtitle("plugins_repo/manifest.json is missing or unreadable")
            group.add(row)
            return [group]

        # Bucket by category, preserving manifest order within each bucket.
        buckets: dict[str, list[dict]] = {}
        for entry in manifest:
            cat = entry.get("category") or "Other"
            buckets.setdefault(cat, []).append(entry)

        # Stable, opinionated ordering: known categories first in the order
        # defined above, then anything else by first appearance.
        seen = set()
        ordered_cats: list[str] = []
        for cat in self._CATEGORY_ORDER:
            if cat in buckets:
                ordered_cats.append(cat)
                seen.add(cat)
        for cat in buckets:
            if cat not in seen:
                ordered_cats.append(cat)

        groups: list[Adw.PreferencesGroup] = []
        for cat in ordered_cats:
            group = Adw.PreferencesGroup()
            # Group title/description are markup too; escape the '&'.
            group.set_title(GLib.markup_escape_text(cat))
            desc = self._CATEGORY_DESCRIPTIONS.get(cat)
            if desc:
                group.set_description(GLib.markup_escape_text(desc))
            for entry in buckets[cat]:
                group.add(self._make_catalog_row(entry))
            groups.append(group)
        return groups

    def _build_catalog_group(self) -> Adw.PreferencesGroup:
        # Kept for backwards-compat / single-group callers. Returns the
        # first category group; in practice _build_catalog_groups() is
        # what the page now uses.
        groups = self._build_catalog_groups()
        return groups[0] if groups else Adw.PreferencesGroup()

    def _badge(self, icon_name: str | None, key: str) -> Gtk.Widget:
        """Each row's leading icon, in the onboarding-store style: a rounded
        tile holding the plugin's REAL popup icon. Symbolic icons render white
        on a brand-coloured tile (the colour is stable per plugin); a colourful
        logo (e.g. an AI service) sits on a light tile so it reads. Falls back
        to a coloured initial only when there's no usable icon."""
        try:
            import icon_style

            icon_name = icon_style.resolve(icon_name)
        except Exception:
            pass
        key = (key or "?").strip()
        idx = (sum(ord(c) for c in key) % 4) if key else 0
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box.set_size_request(34, 34)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        box.set_margin_end(10)
        box.add_css_class("lp-badge")
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        has = bool(icon_name) and theme.has_icon(icon_name)
        if has and icon_name.endswith("-symbolic"):
            box.add_css_class(f"lp-badge-{idx}")
            child = Gtk.Image.new_from_icon_name(icon_name)
            child.set_pixel_size(18)
            child.add_css_class("lp-badge-glyph")
        elif has:
            box.add_css_class("lp-badge-plain")
            child = Gtk.Image.new_from_icon_name(icon_name)
            child.set_pixel_size(22)
        else:
            box.add_css_class(f"lp-badge-{idx}")
            child = Gtk.Label(label=(key[:1].upper() if key else "*"))
            child.add_css_class("lp-badge-letter")
        child.set_hexpand(True)
        child.set_halign(Gtk.Align.CENTER)
        child.set_valign(Gtk.Align.CENTER)
        box.set_hexpand(False)  # stop the glyph's hexpand propagating to the row prefix
        box.append(child)
        return box

    def _make_catalog_row(self, entry: dict) -> Adw.ActionRow:
        row = _plain(Adw.ActionRow())
        row.set_title(entry.get("title", entry["file"]))
        row.add_prefix(self._badge(entry.get("icon"), entry.get("title", entry["file"])))
        desc = entry.get("description", "")
        tags = entry.get("tags") or []
        if tags:
            desc = f"{desc}\n{' · '.join(tags)}"
        row.set_subtitle(desc)

        update_btn = self._make_update_button(entry)
        row.add_suffix(update_btn)
        btn = Gtk.Button()
        btn.set_valign(Gtk.Align.CENTER)
        self._update_install_button(btn, entry, update_btn)
        btn.connect("clicked", self._on_install_clicked, entry, update_btn)
        row.add_suffix(btn)
        return row

    def _make_update_button(self, entry: dict) -> Gtk.Button:
        """An Update button, shown only while the installed copy is an older
        version of the bundled file."""
        btn = Gtk.Button(label="Update")
        btn.set_valign(Gtk.Align.CENTER)
        btn.add_css_class("suggested-action")
        btn.set_tooltip_text(
            "Replace your copy with the newer one bundled with vinoPop. "
            "Changes you made to the file are lost."
        )
        btn.set_visible(_status(entry) == "outdated")
        btn.connect("clicked", self._on_update_clicked, entry)
        return btn

    def _on_update_clicked(self, _btn: Gtk.Button, entry: dict) -> None:
        import plugin_loader

        try:
            plugin_loader.update_bundled(_source_path(entry), _target_dir(entry) / entry["file"])
            print(f"[plugin_manager] updated {entry['file']}")
        except (OSError, ValueError) as exc:
            print(f"[plugin_manager] could not update {entry['file']}: {exc}")
            return
        self._refresh_installed_group()
        self._refresh_custom_group()
        self._rebuild_catalog_buttons()
        if self._on_changed:
            self._on_changed()

    def _update_install_button(
        self, btn: Gtk.Button, entry: dict, update_btn: Gtk.Button | None = None
    ) -> None:
        if update_btn is not None:
            update_btn.set_visible(_status(entry) == "outdated")
        btn.remove_css_class("suggested-action")
        btn.remove_css_class("destructive-action")
        if _is_installed(entry):
            btn.set_label("Remove")
            btn.add_css_class("destructive-action")
        else:
            btn.set_label("Install")
            btn.add_css_class("suggested-action")

    def _on_install_clicked(self, btn: Gtk.Button, entry: dict, update_btn: Gtk.Button) -> None:
        filename = entry["file"]
        src = _source_path(entry)
        dst_dir = _target_dir(entry)
        dst = dst_dir / filename
        dst_dir.mkdir(parents=True, exist_ok=True)
        try:
            if _is_installed(entry):
                dst.unlink()
                print(f"[plugin_manager] uninstalled {filename}")
            else:
                shutil.copy2(src, dst)
                print(f"[plugin_manager] installed {filename}")
        except OSError as exc:
            print(f"[plugin_manager] error: {exc}")
            return
        self._update_install_button(btn, entry, update_btn)
        # Refresh installed-tab content if the window is still up
        self._refresh_installed_group()
        if self._on_changed:
            self._on_changed()

    # ---- installed tab -------------------------------------------------------

    def _build_installed_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("User plugins")
        group.set_description(
            "Plugins you currently have. Install more from the Available tab, "
            "or remove the ones you don't use."
        )
        self._fill_installed_group(group)
        return group

    def _fill_installed_group(self, group: Adw.PreferencesGroup) -> None:
        USER_PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(USER_PLUGIN_DIR.glob("*.py"))
        if not files:
            empty = _plain(Adw.ActionRow())
            empty.set_title("No plugins installed yet")
            empty.set_subtitle("Install one from the Available tab to get started.")
            self._add_row(group, empty)
            return
        manifest_by_file = {e["file"]: e for e in _load_manifest()}
        import plugin_loader as _pl

        retired = _pl.retired_bundled()

        # Map source-file -> list of plugin names that file registered.
        # When a bundle exposes more than one action (editing_actions
        # registers cut/paste/paste-and-enter/backspace/select-all), we
        # render the file as an expander with one switch per sub-plugin
        # so the user can keep what they want and silence the rest.
        try:
            by_source = _pl.plugins_by_source()
            all_plugins = {p.name: p for p in _pl.all_plugins()}
        except Exception:
            by_source = {}
            all_plugins = {}
        try:
            from settings import get_settings as _gs

            settings_obj = _gs()
            disabled_names = set(settings_obj.get("disabled_plugins") or [])
        except Exception:
            settings_obj = None
            disabled_names = set()

        for path in files:
            entry = manifest_by_file.get(path.name)
            sub_names = sorted(by_source.get(path.name, []))
            # Two layouts: single-action bundles render as ActionRow with
            # a Remove button on the right; multi-action bundles render
            # as ExpanderRow with sub-rows per sub-plugin, plus Remove.
            if len(sub_names) > 1:
                row = _plain(Adw.ExpanderRow())
                row.set_enable_expansion(True)
                row.set_show_enable_switch(False)
            else:
                row = _plain(Adw.ActionRow())
            if entry is not None:
                row.set_title(entry.get("title", path.name))
                row.set_subtitle(entry.get("description", ""))
            else:
                row.set_title(path.stem.replace("_", " ").title())
                if path.name in retired:
                    row.set_subtitle(f"No longer bundled with vinoPop. {retired[path.name]}")
                else:
                    row.set_subtitle("Installed by hand (not in the catalogue)")
            _icon = entry.get("icon") if entry else None
            if not _icon and sub_names:
                _p0 = all_plugins.get(sub_names[0])
                _icon = getattr(_p0, "icon", None) if _p0 else None
            row.add_prefix(self._badge(_icon, row.get_title()))

            if entry is not None:
                row.add_suffix(self._make_update_button(entry))
            remove_btn = Gtk.Button(label="Remove")
            remove_btn.set_valign(Gtk.Align.CENTER)
            remove_btn.add_css_class("destructive-action")
            remove_btn.connect("clicked", self._on_remove_clicked, path)
            row.add_suffix(remove_btn)

            if isinstance(row, Adw.ExpanderRow):
                for sub_name in sub_names:
                    sub_plugin = all_plugins.get(sub_name)
                    sub_row = _plain(Adw.SwitchRow())
                    sub_row.set_title((sub_plugin.tooltip if sub_plugin else sub_name) or sub_name)
                    sub_row.set_subtitle(f"action: {sub_name}")
                    sub_row.set_active(sub_name not in disabled_names)
                    sub_row.connect(
                        "notify::active",
                        self._on_subplugin_toggle,
                        sub_name,
                    )
                    row.add_row(sub_row)
            self._add_row(group, row)

    def _on_subplugin_toggle(self, switch: Adw.SwitchRow, _param, sub_name: str) -> None:
        try:
            from settings import get_settings as _gs

            settings_obj = _gs()
            disabled = list(settings_obj.get("disabled_plugins") or [])
        except Exception:
            return
        if switch.get_active():
            disabled = [d for d in disabled if d != sub_name]
        else:
            if sub_name not in disabled:
                disabled.append(sub_name)
        settings_obj.set("disabled_plugins", disabled)
        settings_obj.save()
        # Trigger the on_changed callback so the daemon reloads plugins
        # and the filter takes effect immediately.
        if self._on_changed:
            try:
                self._on_changed()
            except Exception:
                pass

    def _refresh_installed_group(self) -> None:
        if self._installed_group is None:
            return
        # GTK 4 drops the widget once its last ref (the group) lets go,
        # so remove() is enough - no separate destroy() needed.
        self._clear_group(self._installed_group)
        self._fill_installed_group(self._installed_group)

    def _on_remove_clicked(self, _btn: Gtk.Button, path: Path) -> None:
        try:
            path.unlink()
        except OSError as exc:
            print(f"[plugin_manager] could not remove {path}: {exc}")
            return
        # Refresh both tabs (install button on catalog tab changes too)
        self._refresh_installed_group()
        self._rebuild_catalog_buttons()
        if self._on_changed:
            self._on_changed()

    # ---- custom (recipes) tab ------------------------------------------------

    def _build_custom_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Custom actions")
        group.set_description(
            "Create your own popup buttons without writing Python. Each recipe "
            "is a small JSON file in ~/.config/vinopop/recipes/."
        )
        self._fill_custom_group_with_header(group)
        return group

    def _fill_custom_group(self, group: Adw.PreferencesGroup) -> None:
        import recipe_loader

        # Translate the internal action-type slug into something a person
        # who didn't write the JSON would recognise.
        atype_labels = {
            "open_url": "Opens a web page",
            "run_command": "Runs a command",
            "notify": "Shows a notification",
            "copy_transformed": "Transforms text and copies it",
        }
        import plugin_loader

        retired = plugin_loader.retired_bundled()
        for path, recipe in recipe_loader.list_recipes():
            row = _plain(Adw.ActionRow())
            title, subtitle = _recipe_row_text(path, recipe, atype_labels, retired)
            row.set_title(title)
            row.set_subtitle(subtitle)

            # On/off toggle. Lets the user disable a custom button without
            # deleting it. Default-True for recipes that don't carry the
            # 'enabled' key yet.
            enabled_switch = Gtk.Switch()
            enabled_switch.set_valign(Gtk.Align.CENTER)
            enabled_switch.set_tooltip_text("Show this button in the popup")
            enabled_switch.set_active(bool(recipe.get("enabled", True)))
            enabled_switch.connect("notify::active", self._on_toggle_recipe, path)
            row.add_suffix(enabled_switch)

            edit_btn = _icon_button("document-edit-symbolic", "Edit")
            edit_btn.add_css_class("flat")
            edit_btn.connect("clicked", self._on_edit_recipe, path, recipe)
            row.add_suffix(edit_btn)

            del_btn = _icon_button("user-trash-symbolic", "Delete")
            del_btn.add_css_class("destructive-action")
            del_btn.connect("clicked", self._on_delete_recipe, path)
            row.add_suffix(del_btn)
            self._add_row(group, row)

    def _refresh_custom_group(self) -> None:
        if self._custom_group is None:
            return
        self._clear_group(self._custom_group)
        self._fill_custom_group_with_header(self._custom_group)

    def _fill_custom_group_with_header(self, group: Adw.PreferencesGroup) -> None:
        # "+ New action" row at the top (re-added on refresh)
        new_row = _plain(Adw.ActionRow())
        new_row.set_title("New action…")
        new_row.set_subtitle("Open URL · Run command · Notify · Transform clipboard")
        add_btn = _icon_button("list-add-symbolic")
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", lambda *_: self._open_recipe_wizard())
        new_row.add_suffix(add_btn)
        new_row.set_activatable_widget(add_btn)
        self._add_row(group, new_row)
        self._fill_custom_group(group)

    def _open_recipe_wizard(
        self, existing: dict | None = None, existing_path: Path | None = None
    ) -> None:
        from recipe_wizard import RecipeWizard

        parent = self._window
        wizard = RecipeWizard(parent=parent, recipe=existing, source_path=existing_path)
        new_recipe = wizard.run_and_get()
        if new_recipe is None:
            return
        import recipe_loader

        try:
            saved_path = recipe_loader.save_recipe(new_recipe, target_path=existing_path)
            print(f"[plugin_manager] saved recipe to {saved_path}")
        except OSError as exc:
            print(f"[plugin_manager] could not save recipe: {exc}")
            return
        self._refresh_custom_group()
        if self._on_changed:
            self._on_changed()

    def _on_edit_recipe(self, _btn, path: Path, recipe: dict) -> None:
        self._open_recipe_wizard(existing=recipe, existing_path=path)

    def _on_delete_recipe(self, _btn, path: Path) -> None:
        import recipe_loader

        recipe_loader.delete_recipe(path)
        self._refresh_custom_group()
        if self._on_changed:
            self._on_changed()

    def _on_toggle_recipe(self, switch: Gtk.Switch, _param, path: Path) -> None:
        """Flip the recipe's 'enabled' field and reload the plugin layer
        so the popup picks up the change without a restart."""
        import recipe_loader

        recipe_loader.set_recipe_enabled(path, switch.get_active())
        # Don't repaint the whole group -- that would rebuild every row
        # and lose the switch animation feedback. Just trigger the
        # plugin-reload callback so the popup reflects the change.
        if self._on_changed:
            self._on_changed()

    # ---- order tab -----------------------------------------------------------

    def _build_order_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.set_title("Popup button order")
        group.set_description(
            "Drag-free reordering: ↑ and ↓ buttons move a plugin up or down. "
            "The popup shows plugins in this order (those not listed fall back "
            "to their built-in priority)."
        )
        self._fill_order_group(group)
        return group

    def _fill_order_group(self, group: Adw.PreferencesGroup) -> None:
        import plugin_loader

        try:
            from settings import get_settings

            settings = get_settings()
        except Exception:
            settings = None

        all_plugins = plugin_loader.all_plugins()
        if not all_plugins:
            row = _plain(Adw.ActionRow())
            row.set_title("No plugins loaded")
            self._add_row(group, row)
            return

        # Compute effective order: user list first, then unlisted by priority
        order = list((settings.get("plugin_order") if settings else None) or [])
        order_idx = {n: i for i, n in enumerate(order)}
        sorted_plugins = sorted(
            all_plugins,
            key=lambda p: ((0, order_idx[p.name]) if p.name in order_idx else (1, p.priority),),
        )

        for index, plugin in enumerate(sorted_plugins):
            row = _plain(Adw.ActionRow())
            row.set_title(plugin.tooltip or plugin.name)
            row.set_subtitle(plugin.name)
            try:
                import icon_style

                img = Gtk.Image.new_from_icon_name(icon_style.resolve(plugin.icon))
                img.set_pixel_size(20)
                row.add_prefix(img)
            except Exception:
                pass

            up_btn = _icon_button("go-up-symbolic", "Move up")
            up_btn.add_css_class("flat")
            up_btn.set_sensitive(index > 0)
            up_btn.connect("clicked", self._on_move, index, -1, sorted_plugins)
            row.add_suffix(up_btn)

            down_btn = _icon_button("go-down-symbolic", "Move down")
            down_btn.add_css_class("flat")
            down_btn.set_sensitive(index < len(sorted_plugins) - 1)
            down_btn.connect("clicked", self._on_move, index, +1, sorted_plugins)
            row.add_suffix(down_btn)

            self._add_row(group, row)

    def _refresh_order_group(self) -> None:
        if self._order_group is None:
            return
        self._clear_group(self._order_group)
        self._fill_order_group(self._order_group)

    def _on_move(self, _btn, index: int, delta: int, sorted_plugins) -> None:
        """Swap index with index+delta; persist the new full ordering."""
        new_idx = index + delta
        if new_idx < 0 or new_idx >= len(sorted_plugins):
            return
        ordered_names = [p.name for p in sorted_plugins]
        ordered_names[index], ordered_names[new_idx] = (
            ordered_names[new_idx],
            ordered_names[index],
        )
        try:
            from settings import get_settings

            s = get_settings()
            s.set("plugin_order", ordered_names)
            s.save()
        except Exception as exc:  # noqa: BLE001
            print(f"[plugin_manager] could not save order: {exc}")
            return
        self._refresh_order_group()
        if self._on_changed:
            self._on_changed()

    def _rebuild_catalog_buttons(self) -> None:
        page = getattr(self, "_catalog_page", None)
        if page is None:
            return
        # Wipe every existing category group, then append fresh ones.
        old_groups = list(getattr(self, "_catalog_groups", []) or [])
        for g in old_groups:
            page.remove(g)
        self._catalog_groups = []
        for group in self._build_catalog_groups():
            page.add(group)
            self._catalog_groups.append(group)
        self._catalog_group = self._catalog_groups[0] if self._catalog_groups else None


if __name__ == "__main__":
    _dlg = PluginManagerDialog()
    _dlg.show()
    gtk4_app.run_window(_dlg._window)
