"""Loads built-in and user-defined plugins.

User plugins are Python files in ~/.config/vinopop/plugins/ that expose
a top-level `register(register_plugin)` function. Each plugin file may
register one or more Plugin instances.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
import traceback
from pathlib import Path

from core import actions
from core.classifier import ContentType
from core.xdg_paths import CONFIG_DIR, HICOLOR_APPS, USER_ICONS_DIR
from plugin_system import compat
from plugin_system.plugin_base import Plugin

_PLUGINS: list[Plugin] = []

USER_PLUGIN_DIR = CONFIG_DIR / "plugins"
VINOPOP_DIR = str(Path(__file__).resolve().parent.parent)
REPO_PLUGIN_DIR = Path(VINOPOP_DIR) / "plugins_repo"
ICONS_DIR = Path(VINOPOP_DIR) / "icons"

# What a new install starts with: useful at once without crowding the popup.
# The rest is one click away in Plugin Manager's catalogue.
DEFAULT_PLUGIN_SEEDS = (
    "editing_actions.py",
    "clipboard_history.py",
    "wordcount.py",
    "text_transformations.py",
    "translate.py",
    "local_ai.py",
)
_PLUGIN_SEED_MARKER = USER_PLUGIN_DIR.parent / ".default-plugins-seeded"


def _ensure_on_path() -> None:
    """Make the imports user plugins use (`plugin_base`, `classifier`) resolve."""
    compat.install()
    if VINOPOP_DIR not in sys.path:
        sys.path.insert(0, VINOPOP_DIR)


# The icon dirs' mtimes at the last completed _install_all_icons(), so a
# load_all() with neither changed skips the copying
_icon_sync_cache: dict[str, float] = {}


def _dir_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _install_all_icons() -> None:
    """Copy the bundled and the user's icons into the user's hicolor theme
    and point the running GTK at the bundled ones."""
    if not ICONS_DIR.is_dir():
        return
    bundled_mtime = _dir_mtime(ICONS_DIR)
    user_mtime = _dir_mtime(USER_ICONS_DIR)
    if (
        _icon_sync_cache.get("bundled") == bundled_mtime
        and _icon_sync_cache.get("user") == user_mtime
        and _icon_sync_cache.get("done")
    ):
        return

    HICOLOR_APPS.mkdir(parents=True, exist_ok=True)
    for src in ICONS_DIR.glob("*.svg"):
        dst = HICOLOR_APPS / src.name
        if not dst.is_file() or src.stat().st_mtime > dst.stat().st_mtime:
            try:
                shutil.copy2(src, dst)
            except OSError as exc:
                print(f"[plugin_loader] icon copy failed for {src.name}: {exc}")

    # GTK scanned its theme dirs before the copy, so without the search path
    # it draws "image-missing" for the vinopop-* names until restarted
    try:
        theme = _icon_theme()
        if theme is not None and str(ICONS_DIR) not in (theme.get_search_path() or []):
            theme.add_search_path(str(ICONS_DIR))
    except Exception as exc:  # noqa: BLE001
        print(f"[plugin_loader] could not refresh icon theme: {exc}")

    _sync_user_icons()

    _icon_sync_cache["bundled"] = bundled_mtime
    _icon_sync_cache["user"] = user_mtime
    _icon_sync_cache["done"] = True  # type: ignore[assignment]


def _icon_theme():
    """The GTK 4 icon theme for the default display, or None when GTK isn't
    loaded or has no display (tests, --doctor)."""
    if "gi.repository.Gtk" not in sys.modules:
        return None
    from gi.repository import Gdk, Gtk

    display = Gdk.Display.get_default()
    return Gtk.IconTheme.get_for_display(display) if display is not None else None


def _sync_user_icons() -> None:
    """Mirror ~/.config/vinopop/icons/ into hicolor, for the icon picker and
    recipes. GTK 4 notices the new files by itself."""
    user_icons_dir = USER_ICONS_DIR
    user_icons_dir.mkdir(parents=True, exist_ok=True)
    user_copied = False
    for src in list(user_icons_dir.glob("*.svg")) + list(user_icons_dir.glob("*.png")):
        dst = HICOLOR_APPS / src.name
        if not dst.is_file() or src.stat().st_mtime > dst.stat().st_mtime:
            try:
                shutil.copy2(src, dst)
                user_copied = True
            except OSError as exc:
                print(f"[plugin_loader] user icon copy failed for {src.name}: {exc}")
    if user_copied:
        print(f"[plugin_loader] synced user icons from {user_icons_dir}")


# plugin name -> the file that registered it ("editing_actions.py"), for
# Plugin Manager to list a file's buttons under it
_SOURCE_BY_NAME: dict[str, str] = {}


def register(plugin: Plugin) -> None:
    """Add a popup button, unless its name is in the `disabled_plugins`
    setting, which hides one button of a file that registers several
    (pinned by `tests/test_plugins_loader.py::test_a_disabled_plugin_is_not_registered`,
    `tests/test_plugins_loader.py::test_a_plugin_not_in_disabled_plugins_is_registered`)."""
    try:
        from core.settings import get_settings

        disabled = set(get_settings().get("disabled_plugins") or [])
    except Exception:
        disabled = set()
    if plugin.name in disabled:
        return
    _PLUGINS.append(plugin)


def all_plugins() -> list[Plugin]:
    return list(_PLUGINS)


def source_for(name: str) -> str | None:
    """The .py file that registered the plugin `name`; None for built-ins."""
    return _SOURCE_BY_NAME.get(name)


def plugins_by_source() -> dict[str, list[str]]:
    """{file name: names of the plugins it registered}."""
    out: dict[str, list[str]] = {}
    for plugin_name, source in _SOURCE_BY_NAME.items():
        out.setdefault(source, []).append(plugin_name)
    return out


def for_content_type(content_type: ContentType, text: str | None = None) -> list[Plugin]:
    matched = [p for p in _PLUGINS if p.handles(content_type)]
    if text is not None:
        matched = [p for p in matched if p.matches(text)]
    return in_user_order(matched)


def in_user_order(plugins: list[Plugin]) -> list[Plugin]:
    """`plugins` as the popup shows them: those in the plugin_order setting
    first, in that order, then the rest by their built-in priority (pinned
    by `tests/test_plugins_loader.py::test_plugin_order_puts_listed_plugins_first_in_its_order`,
    `tests/test_plugins_loader.py::test_unlisted_plugins_follow_by_priority`)."""
    try:
        from core.settings import get_settings

        order = list(get_settings().get("plugin_order") or [])
    except Exception:
        order = []
    position = {name: i for i, name in enumerate(order)}

    def sort_key(plugin: Plugin):
        if plugin.name in position:
            return (0, position[plugin.name], plugin.priority)
        return (1, 0, plugin.priority)

    return sorted(plugins, key=sort_key)


def _snippets_loaded() -> bool:
    """Whether the clipboard_history plugin is loaded, so Pin as snippet
    has somewhere to save (pinned by
    `tests/test_plugins_loader.py::test_pin_as_snippet_shows_with_the_clipboard_plugin_loaded`,
    `tests/test_plugins_loader.py::test_pin_as_snippet_hides_without_the_clipboard_plugin`)."""
    ch = sys.modules.get("vinopop_user_clipboard_history") or sys.modules.get("clipboard_history")
    return ch is not None and hasattr(ch, "_create_snippet")


def _can_run(text: str) -> bool:
    """Run in terminal: a command, or a path to an executable script
    (`./build.sh`)."""
    from core.classifier import classify, is_runnable_path

    content_type = classify(text)
    if content_type == ContentType.COMMAND:
        return True
    if content_type == ContentType.PATH:
        return is_runnable_path(text)
    return False


def _register_builtins() -> None:
    register(
        Plugin(
            name="copy",
            icon="edit-copy-symbolic",
            tooltip="Copy",
            handler=actions.copy_to_clipboard,
            content_types=(),
            priority=10,
        )
    )
    register(
        Plugin(
            name="pin-as-snippet",
            icon="non-starred-symbolic",
            tooltip="Pin as snippet",
            handler=actions.pin_as_snippet,
            content_types=(),
            priority=11,
            predicate=lambda _t: _snippets_loaded(),
        )
    )
    register(
        Plugin(
            name="run-terminal",
            icon="utilities-terminal-symbolic",
            tooltip="Run in terminal",
            handler=actions.run_in_terminal,
            content_types=(ContentType.COMMAND, ContentType.PATH),
            priority=20,
            predicate=_can_run,
        )
    )
    register(
        Plugin(
            name="open-url",
            icon="web-browser-symbolic",
            tooltip="Open in browser",
            handler=actions.open_url,
            content_types=(ContentType.URL,),
            priority=20,
        )
    )
    register(
        Plugin(
            name="compose-email",
            icon="mail-send-symbolic",
            tooltip="Compose email",
            handler=actions.compose_email,
            content_types=(ContentType.EMAIL,),
            priority=20,
        )
    )
    register(
        Plugin(
            name="open-path",
            icon="folder-open-symbolic",
            tooltip="Open path",
            handler=actions.open_path,
            content_types=(ContentType.PATH,),
            priority=20,
        )
    )
    register(
        Plugin(
            name="search-web",
            icon="system-search-symbolic",
            tooltip="Search the web",
            handler=actions.search_web,
            content_types=(ContentType.PLAIN_TEXT,),
            priority=30,
        )
    )


def _seed_default_plugins() -> None:
    """Copy DEFAULT_PLUGIN_SEEDS into the user's plugin dir, once, and not
    into a dir that already has plugins (pinned by
    `tests/test_plugins_loader.py::test_a_new_install_gets_the_default_plugins`,
    `tests/test_plugins_loader.py::test_a_plugin_dir_with_plugins_is_not_seeded`,
    `tests/test_plugins_loader.py::test_seeding_happens_only_once`)."""
    if _PLUGIN_SEED_MARKER.is_file():
        return
    USER_PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    has_existing = any(USER_PLUGIN_DIR.glob("*.py"))
    if has_existing:
        _PLUGIN_SEED_MARKER.touch()
        return
    if not REPO_PLUGIN_DIR.is_dir():
        _PLUGIN_SEED_MARKER.touch()
        return
    for filename in DEFAULT_PLUGIN_SEEDS:
        src = REPO_PLUGIN_DIR / filename
        if not src.is_file():
            continue
        dst = USER_PLUGIN_DIR / filename
        if dst.is_file():
            continue
        try:
            shutil.copy2(src, dst)
            print(f"[plugin_loader] seeded default plugin: {filename}")
        except OSError as exc:
            print(f"[plugin_loader] could not seed {filename}: {exc}")
    _PLUGIN_SEED_MARKER.touch()


# An installed file is a copy, so a fix in plugins_repo/ reaches it only as a
# Plugin Manager Update. Each bundled file carries the version of its
# manifest entry: a "# vinopop-plugin-version: N" first line in a .py, a
# "version" key in a recipe. Bump both when changing a bundled file (pinned by
# `tests/test_plugins.py::test_every_catalogue_entry_and_its_file_carry_the_same_version`).
_PY_VERSION_RE = re.compile(r"\A# vinopop-plugin-version: (\d+)[ \t]*\n")
RETIRED_FILE = REPO_PLUGIN_DIR / "retired.json"


def _read_bundled(path: Path) -> tuple[int, object]:
    """(version, content without the version) of a bundled or installed file.
    Version 0 is a copy from before files had one. A recipe's "enabled"
    switch is the user's, so it isn't content (pinned by
    `tests/test_plugins.py::test_a_turned_off_recipe_is_still_current`)."""
    text = path.read_text(encoding="utf-8")
    if path.suffix != ".json":
        m = _PY_VERSION_RE.match(text)
        return (int(m.group(1)), text[m.end() :]) if m else (0, text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return 0, text
    if not isinstance(data, dict):
        return 0, data
    version = data.pop("version", 0)
    data.pop("enabled", None)
    return (version if isinstance(version, int) else 0), data


def bundled_status(src: Path, dst: Path, version: int) -> str:
    """How the installed copy `dst` of bundled file `src` (at `version`)
    compares: "missing", "current", "outdated" (offer the update) or
    "changed" (edited by hand, or newer: leave it alone) (pinned by
    `tests/test_plugins.py::test_a_copy_without_a_version_line_is_outdated`,
    `tests/test_plugins.py::test_a_copy_at_an_older_version_is_outdated`,
    `tests/test_plugins.py::test_the_same_code_without_the_version_line_is_current`,
    `tests/test_plugins.py::test_a_copy_edited_at_the_current_version_is_changed`)."""
    if not dst.is_file():
        return "missing"
    try:
        dst_version, dst_body = _read_bundled(dst)
        _, src_body = _read_bundled(src)
    except (OSError, UnicodeDecodeError):
        return "changed"
    if dst_body == src_body:
        return "current"
    return "outdated" if dst_version < version else "changed"


def update_bundled(src: Path, dst: Path) -> None:
    """Replace `dst` with the bundled `src`, keeping a recipe's on/off switch
    (pinned by `tests/test_plugins.py::test_updating_a_recipe_keeps_its_on_off_switch`)."""
    if src.suffix == ".json":
        enabled = None
        try:
            old = json.loads(dst.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                enabled = old.get("enabled")
        except (OSError, json.JSONDecodeError):
            pass
        if enabled is not None:
            from plugin_system import recipe_loader

            recipe = json.loads(src.read_text(encoding="utf-8"))
            recipe_loader.save_recipe({**recipe, "enabled": enabled}, target_path=dst)
            return
    shutil.copy2(src, dst)


def retired_bundled() -> dict[str, str]:
    """{filename: why} for files vinoPop used to bundle and no longer does,
    so an install that still has one can say so instead of calling it
    "installed by hand"."""
    try:
        data = json.loads(RETIRED_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _load_user_plugins() -> None:
    _ensure_on_path()
    _seed_default_plugins()
    if not USER_PLUGIN_DIR.is_dir():
        return
    for path in sorted(USER_PLUGIN_DIR.glob("*.py")):
        if not path.name.startswith("_"):
            _load_user_plugin(path)


def _load_user_plugin(path: Path) -> None:
    """Run one plugin file and register its buttons, all or none.

    Anything it raises, sys.exit() included, is logged and its module
    dropped from sys.modules, so nothing finds a half-loaded plugin (pinned
    by `tests/test_plugins.py::test_a_plugin_calling_sys_exit_does_not_stop_the_daemon`,
    `tests/test_plugins.py::test_a_register_that_raises_leaves_no_buttons_behind`)."""
    mod_name = f"vinopop_user_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        # Before exec: dataclasses look their class's module up there
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        if hasattr(module, "register"):
            pending: list[Plugin] = []
            module.register(pending.append)
            for plugin in pending:
                _SOURCE_BY_NAME[plugin.name] = path.name
                register(plugin)
            print(f"[plugin_loader] loaded user plugin: {path.name}")
    except KeyboardInterrupt:
        raise
    except BaseException:
        sys.modules.pop(mod_name, None)
        print(f"[plugin_loader] failed to load {path.name}:")
        traceback.print_exc()


def _shutdown_user_plugins() -> None:
    """Call each loaded user plugin's `shutdown()`, if it has one, so a
    reload doesn't stack its threads, and drop the stale modules from
    sys.modules (pinned by
    `tests/test_clipboard_store.py::test_plugin_loader_shuts_down_the_previous_copy_before_reloading`)."""
    for name, module in list(sys.modules.items()):
        if not name.startswith("vinopop_user_"):
            continue
        hook = getattr(module, "shutdown", None)
        if callable(hook):
            try:
                hook()
            except KeyboardInterrupt:
                raise
            except BaseException:
                print(f"[plugin_loader] shutdown of {name} failed:")
                traceback.print_exc()
        sys.modules.pop(name, None)


def load_all() -> None:
    """Idempotent: clears existing registry and reloads everything."""
    _shutdown_user_plugins()
    _PLUGINS.clear()
    _SOURCE_BY_NAME.clear()
    _install_all_icons()
    _register_builtins()
    _load_user_plugins()
    _load_recipes()
    print(f"[plugin_loader] {len(_PLUGINS)} plugins loaded")


def _load_recipes() -> None:
    try:
        from plugin_system import recipe_loader

        recipe_loader.load_recipes(register)
    except Exception as exc:  # noqa: BLE001
        print(f"[plugin_loader] recipe loading failed: {exc}")
