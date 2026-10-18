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

import actions
from classifier import ContentType
from plugin_base import Plugin
from xdg_paths import CONFIG_DIR, HICOLOR_APPS, USER_ICONS_DIR

_PLUGINS: list[Plugin] = []

USER_PLUGIN_DIR = CONFIG_DIR / "plugins"
VINOPOP_DIR = str(Path(__file__).resolve().parent)
REPO_PLUGIN_DIR = Path(VINOPOP_DIR) / "plugins_repo"
ICONS_DIR = Path(VINOPOP_DIR) / "icons"

# Curated set seeded into a brand-new install. Aim is "useful within
# 5 seconds of first selection" without overwhelming the popup. Dev-
# heavy plugins (base64, json_format, etc.) and niche transforms stay
# in the catalogue - they're one click away from Plugin Manager.
DEFAULT_PLUGIN_SEEDS = (
    "editing_actions.py",  # Cut / Paste / Backspace / Select All
    "clipboard_history.py",  # the core picker
    "wordcount.py",
    "text_transformations.py",  # case + sort/dedupe/trim, all predicate-guarded
    "translate.py",  # offline translation bubble, language picker
    "local_ai.py",  # offline summarize / rewrite / explain
)
_PLUGIN_SEED_MARKER = USER_PLUGIN_DIR.parent / ".default-plugins-seeded"


def _ensure_on_path() -> None:
    """User plugins import classifier/plugin_base - make sure those resolve."""
    if VINOPOP_DIR not in sys.path:
        sys.path.insert(0, VINOPOP_DIR)


# Cache of (icons_dir_mtime, user_icons_dir_mtime) → (any_copied) from the
# previous _install_all_icons / _sync_user_icons run. Lets load_all() skip
# the whole icon stat/copy/rescan dance when the source dirs haven't
# changed -- which is the common case after first startup.
_icon_sync_cache: dict[str, float] = {}


def _dir_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _install_all_icons() -> None:
    """Copy every SVG from vinopop/icons/ into the user's hicolor theme,
    register an extra search path with the running GTK process, and refresh
    the icon-theme cache so newly installed icons are findable.
    """
    if not ICONS_DIR.is_dir():
        return
    # Fast path: if neither the bundled icons dir nor the user icons dir
    # has been touched since the last run, skip the stat-storm + rescan.
    user_icons_dir = USER_ICONS_DIR
    bundled_mtime = _dir_mtime(ICONS_DIR)
    user_mtime = _dir_mtime(user_icons_dir)
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

    # Tell the running GTK process about our icon dir.
    # Without this, GTK has already scanned its theme dirs before we copied,
    # and falls back to the "image-missing" icon for our vinopop-* names.
    # (GTK 4 re-checks its theme directories on its own; adding the search
    # path is all that's needed in-process.)
    try:
        theme = _icon_theme()
        if theme is not None and str(ICONS_DIR) not in (theme.get_search_path() or []):
            theme.add_search_path(str(ICONS_DIR))
    except Exception as exc:  # noqa: BLE001
        print(f"[plugin_loader] could not refresh icon theme: {exc}")

    # Mirror any user-supplied icons from ~/.config/vinopop/icons/ into
    # hicolor so they're searchable in the picker and usable by recipes.
    _sync_user_icons()

    # Remember that we've completed an install pass with these source-dir
    # mtimes so subsequent load_all() calls can short-circuit.
    _icon_sync_cache["bundled"] = bundled_mtime
    _icon_sync_cache["user"] = user_mtime
    _icon_sync_cache["done"] = True  # type: ignore[assignment]


def _icon_theme():
    """The GTK 4 icon theme for the default display, or None when GTK isn't
    loaded / has no display (e.g. tests, --doctor)."""
    import sys

    if "gi.repository.Gtk" not in sys.modules:
        return None
    from gi.repository import Gdk, Gtk

    display = Gdk.Display.get_default()
    return Gtk.IconTheme.get_for_display(display) if display is not None else None


def _sync_user_icons() -> None:
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
        # GTK 4's icon theme notices the new hicolor files on its own
        # (no rescan_if_needed() any more).
        print(f"[plugin_loader] synced user icons from {user_icons_dir}")


# Map plugin.name -> source file basename (e.g. "editing_actions.py").
# Populated by the file-loader so the Plugin Manager can show "which
# bundle does this come from", and so users can disable sub-plugins
# without removing the whole .py file.
_SOURCE_BY_NAME: dict[str, str] = {}


def register(plugin: Plugin) -> None:
    """Register a plugin. Silently drops it if its name appears in the
    `disabled_plugins` setting - lets users hide one sub-plugin from a
    bundled .py file (e.g. "select-all" from editing_actions) without
    deleting the whole bundle."""
    try:
        from settings import get_settings

        disabled = set(get_settings().get("disabled_plugins") or [])
    except Exception:
        disabled = set()
    if plugin.name in disabled:
        return
    _PLUGINS.append(plugin)


def all_plugins() -> list[Plugin]:
    return list(_PLUGINS)


def source_for(name: str) -> str | None:
    """Return the .py file that registered the plugin named `name`,
    or None for built-ins / unknown."""
    return _SOURCE_BY_NAME.get(name)


def plugins_by_source() -> dict[str, list[str]]:
    """Mapping of source-file basename to the list of plugin names it
    registered. Used by Plugin Manager → Installed to show a bundle as
    expandable with its sub-plugins listed underneath."""
    out: dict[str, list[str]] = {}
    for plugin_name, source in _SOURCE_BY_NAME.items():
        out.setdefault(source, []).append(plugin_name)
    return out


def for_content_type(content_type: ContentType, text: str | None = None) -> list[Plugin]:
    matched = [p for p in _PLUGINS if p.handles(content_type)]
    if text is not None:
        matched = [p for p in matched if p.matches(text)]
    # User-defined order takes precedence; plugins listed earlier come first.
    # Plugins not in the order list fall back to their built-in priority.
    try:
        from settings import get_settings

        order = list(get_settings().get("plugin_order") or [])
    except Exception:
        order = []
    order_index = {name: i for i, name in enumerate(order)}
    big = len(order) + 1_000_000

    def sort_key(p: Plugin):
        if p.name in order_index:
            return (0, order_index[p.name], p.priority)
        return (1, big, p.priority)

    matched.sort(key=sort_key)
    return matched


def _register_builtins() -> None:
    # Universal: copy works on anything
    register(
        Plugin(
            name="copy",
            icon="edit-copy-symbolic",
            tooltip="Copy",
            handler=actions.copy_to_clipboard,
            content_types=(),  # empty = all
            priority=10,
        )
    )

    # Universal: pin the current selection as a snippet. Sits next to
    # Copy in the popup so the path from "I keep retyping this" to
    # "now it's a snippet" is one click long. Only registers when the
    # clipboard_history plugin is actually loaded - otherwise there's
    # nothing to save into.
    def _snippets_loaded() -> bool:
        import sys

        ch = sys.modules.get("vinopop_user_clipboard_history") or sys.modules.get(
            "clipboard_history"
        )
        return ch is not None and hasattr(ch, "_create_snippet")

    register(
        Plugin(
            name="pin-as-snippet",
            icon="non-starred-symbolic",
            tooltip="Pin as snippet",
            handler=actions.pin_as_snippet,
            content_types=(),  # empty = all
            priority=11,
            predicate=lambda _t: _snippets_loaded(),
        )
    )

    # COMMAND - also shown for PATH-classified selections that look like
    # an executable script ('./build.sh', '~/bin/deploy'), via the
    # predicate. Lets users run a script with one click instead of having
    # to switch to a terminal and type the path.
    from classifier import is_runnable_path

    def _can_run(text: str) -> bool:
        # If classify() already returned COMMAND we wouldn't be here via
        # the PATH branch - for the COMMAND branch, the plugin's content_types
        # gate already matched. We only need to gate the PATH case.
        from classifier import classify as _classify

        ct = _classify(text)
        if ct == ContentType.COMMAND:
            return True
        if ct == ContentType.PATH:
            return is_runnable_path(text)
        return False

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

    # URL
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

    # EMAIL
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

    # PATH
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

    # PLAIN_TEXT: search
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
    """First-run only: drop the curated DEFAULT_PLUGIN_SEEDS into the
    user's plugin dir so a fresh install ships with a useful popup
    out of the box. Skipped if the seed marker is present OR the dir
    already has any .py files (a user who curated their own set
    should not get clobbered)."""
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


# ---- bundled file versions ---------------------------------------------------
#
# Installing from the catalogue copies a file out of plugins_repo/, so a later
# fix there never reaches that copy by itself. Each bundled file carries a
# version, the same number as its manifest entry: a "# vinopop-plugin-version: N"
# first line in a .py, a "version" key in a recipe. Bump both when changing a
# bundled file, and the Plugin Manager offers the newer copy as an Update.

_PY_VERSION_RE = re.compile(r"\A# vinopop-plugin-version: (\d+)[ \t]*\n")
RETIRED_FILE = REPO_PLUGIN_DIR / "retired.json"


def _read_bundled(path: Path) -> tuple[int, object]:
    """(version, content without the version) of a bundled or installed file.
    Version 0 means none: a copy installed before files had one. A recipe's
    "enabled" switch is the user's, not part of what's compared."""
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
    compares: "missing", "current", "outdated" (an older copy: offer the
    update) or "changed" (edited by hand, or newer: leave it alone)."""
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
    """Replace `dst` with the bundled `src`, keeping a recipe's on/off switch."""
    if src.suffix == ".json":
        enabled = None
        try:
            old = json.loads(dst.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                enabled = old.get("enabled")
        except (OSError, json.JSONDecodeError):
            pass
        if enabled is not None:
            import recipe_loader

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
        if path.name.startswith("_"):
            continue
        mod_name = f"vinopop_user_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            # Register in sys.modules BEFORE exec - Python 3.12 dataclass
            # introspection needs cls.__module__ to resolve.
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            if hasattr(module, "register"):
                # Buffer what the file registers and commit it only once
                # its register() returns: one that registers a button and
                # then raises must not leave that button behind.
                pending: list[Plugin] = []
                module.register(pending.append)
                for plugin in pending:
                    # Track which source file each plugin came from - the
                    # Plugin Manager uses this to show a bundle as
                    # expandable with its sub-plugins underneath, and
                    # per-action enable/disable.
                    _SOURCE_BY_NAME[plugin.name] = path.name
                    register(plugin)
                print(f"[plugin_loader] loaded user plugin: {path.name}")
        except KeyboardInterrupt:
            raise
        except BaseException:
            # BaseException: a plugin calling sys.exit() at import or in
            # register() must not take the daemon down with it.
            # Roll back the half-initialised module so main.py's
            # sys.modules.get("vinopop_user_clipboard_history") doesn't
            # find a broken stub and silently behave as if the plugin
            # were loaded. Next load_all() then gets a clean re-import.
            sys.modules.pop(mod_name, None)
            print(f"[plugin_loader] failed to load {path.name}:")
            traceback.print_exc()


def _shutdown_user_plugins() -> None:
    """Let the previous copy of each user plugin stop its background threads
    before a fresh copy of the file is executed: a plugin with a
    `shutdown()` function gets it called, so reloading doesn't stack watchers
    on top of each other. The stale module objects are dropped too, so code
    that looks a plugin up in sys.modules doesn't find one whose file has
    since been removed (the fresh copies re-register themselves on load)."""
    for name, module in list(sys.modules.items()):
        if not name.startswith("vinopop_user_"):
            continue
        hook = getattr(module, "shutdown", None)
        if callable(hook):
            try:
                hook()
            except KeyboardInterrupt:
                raise
            except BaseException:  # a sys.exit() in shutdown() too
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
        import recipe_loader

        recipe_loader.load_recipes(register)
    except Exception as exc:  # noqa: BLE001
        print(f"[plugin_loader] recipe loading failed: {exc}")
