# Contributing to vinoPop

Thanks for your interest in contributing. vinoPop is a small, modular project, and plugins are often the most useful way to extend it.

## Project layout

```
vinopop             launcher (used by autostart and ~/.local/bin)
main.py             daemon entry point that wires components together
core/               classifier, actions, settings, XDG paths, version, autostart, distro detection
triggers/           popup triggers: hotkeys, selection and double-click watchers, editable-field detection
plugin_system/      Plugin dataclass, plugin and recipe loaders, compatibility shims for bare-name imports
ui/                 GTK 4 windows: popup bar, AI bubble, settings, plugin manager, recipe wizard, onboarding, theme
capture/            screen OCR and region selector
tray/               tray protocol (daemon side) and Qt tray process
platform_backend/   X11 and KDE Wayland backends
vino_ai/            offline AI on OpenVINO: models, worker, OCR, doctor
plugins_repo/       bundled plugins and recipes (the catalog)
data/               AppStream metainfo (installed to ~/.local/share/metainfo by install.sh)
icons/  docs/  scripts/  tests/
```

## Writing a plugin

A plugin is a single Python file in `~/.config/vinopop/plugins/` that defines a top-level `register(register_plugin)` function. vinoPop discovers it at startup and after installation through the Plugin Manager.

### Minimal example

```python
"""Example plugin: shout the selected text into a notification."""

import actions
from classifier import ContentType
from plugin_base import Plugin


def _shout(text: str) -> None:
    actions.notify("SHOUT", text.upper(), "face-surprise")


def register(register_plugin) -> None:
    register_plugin(
        Plugin(
            name="shout",
            icon="face-surprise-symbolic",
            tooltip="SHOUT IT",
            handler=_shout,
            content_types=(ContentType.PLAIN_TEXT,),
            priority=50,
        )
    )
```

Save this file as `~/.config/vinopop/plugins/shout.py` and restart vinoPop. Selecting text will then display your new button in the popup bar.

### Plugin API

`Plugin` is a dataclass defined in `plugin_base`:

| Field | Type | Meaning |
|---|---|---|
| `name` | `str` | Unique identifier (used in logs) |
| `icon` | `str` | Theme icon name (see the [freedesktop icon naming spec](https://specifications.freedesktop.org/icon-naming-spec/icon-naming-spec-latest.html)) |
| `tooltip` | `str` | Hover text shown on the popup button |
| `handler` | `Callable[[str], None]` | Called with the selected text when clicked |
| `content_types` | `Iterable[ContentType]` | `()` for all types, or specific values |
| `priority` | `int` | Lower numbers appear first in the popup (default `100`) |
| `predicate` | `Callable[[str], bool] \| None` | Optional predicate on the selected text; hides the button when it returns false |
| `requires_editable` | `bool` | Hide the button when the focused widget is read-only (default `false`) |

`ContentType` values: `COMMAND`, `URL`, `EMAIL`, `PATH`, `PLAIN_TEXT`. See `core/classifier.py` for classification rules.

### Tips

- **Threading and notifications:** Handlers run on a worker thread rather than the GTK main loop, so blocking work (subprocess calls, file I/O) is safe. Any GTK interaction (opening windows or updating widgets) must be scheduled via `GLib.idle_add`. Use `actions.notify(title, body, icon)` so desktop notifications are attributed to vinoPop rather than `notify-send`.
- **Clipboard access:** Copy results with `actions.copy_to_clipboard(text)` (`from actions import copy_to_clipboard`). This dispatches to the active platform backend (`xclip` on XWayland/X11, `wl-copy` on KDE Wayland) instead of hardcoding one tool.
- **Icons:** Most desktops ship the [hicolor / Adwaita icon themes](https://developer.gnome.org/hig/reference/icons.html).
  - Use the `-symbolic` suffix for monochrome icons that adapt to theme colors, and choose names present in both Adwaita and Breeze (`tests/test_icons.py` lists verified names) or add a fallback in `ui/icon_style.py`.
  - Give each action a distinct icon so buttons are easy to tell apart.
  - Bundled custom icons come in pairs in `icons/`: the color tile `vinopop-<concept>.svg` and the monochrome glyph `vinopop-<concept>-symbolic.svg`, mapped in `ui/icon_style.py` so the user's icon style setting switches between them. Draw glyphs as SVG paths rather than `<text>` elements (which depend on installed fonts), filling roughly 14/16 of the `viewBox` to match Adwaita.
- **Imports:** Do not add `sys.path.insert` calls to plugins. `plugin_system/compat.py` exposes `classifier`, `plugin_base`, `actions`, and `settings` as bare module names mapped to `core.classifier`, `plugin_system.plugin_base`, and so on.

### Submitting a plugin to the built-in catalogue

Open a pull request that adds:
1. Your plugin file at `plugins_repo/your_plugin.py`.
2. An entry in `plugins_repo/manifest.json`:
   ```json
   {
     "file": "your_plugin.py",
     "version": 1,
     "title": "Human-readable title",
     "description": "One-sentence summary shown in the manager",
     "tags": ["category", "more-tags"]
   }
   ```
3. A matching version header on the file's first line: `# vinopop-plugin-version: 1` (recipes specify `"version": 1` in JSON instead).

Because installing a plugin copies it into `~/.config/vinopop/plugins/`, always increment the version in both the plugin file and `manifest.json` when publishing fixes so the Plugin Manager offers an update. If you remove a bundled plugin, add it to `plugins_repo/retired.json` with a reason so existing installations report it cleanly.

If your plugin depends on an external command (such as `imagemagick`), check for it with `shutil.which(...)` and fail gracefully with an `actions.notify` message explaining what is missing.

## Bug reports & feature requests

Please open an issue and include:
- Your distribution and desktop environment (`echo $XDG_CURRENT_DESKTOP`)
- Output from `vinopop --debug` (or `./vinopop --debug` from a checkout)
- Steps to reproduce

## Development setup

vinoPop runs on your distribution's PyGObject with GTK 4 (4.14+) and libadwaita (1.5+), so run the application using the system Python (see [README.md](README.md#install) for the `gir1.2-gtk-4.0` / `gir1.2-adw-1` packages and their equivalents on other distros). To test the X11 backend on a Wayland desktop, launch with `GDK_BACKEND=x11`.

The test and lint suite does not require GTK or OpenVINO and matches CI:

```bash
uv venv
uv pip install --group dev
uv run poe check    # ruff, ruff format --check, mypy, pytest
```

Run `uv run poe fix` to apply ruff fixes and formatting, and `uv run poe security` to run the Bandit security scan used in CI.

Enable the repository's git hooks so commits match CI's lint and commit-message checks. The `pre-commit` hook runs `scripts/lint.sh` (ruff, ruff format --check, mypy) and `commit-msg` runs `scripts/check_commits.sh`:

```bash
git config core.hooksPath .githooks
```

`tests/test_a11y.py` constructs the real GTK windows and verifies accessibility: every control has a screen-reader label, dialogs close on `Escape`, and custom colors maintain at least 4.5:1 contrast in both light and dark themes. Because it opens real windows, run it under a virtual display and session bus (matching CI's `test-gtk` job):

```bash
xvfb-run -a dbus-run-session -- env VINOPOP_ATSPI_TEST=1 python3 -m pytest tests/test_a11y.py
```

When adding an icon-only button, always give it a tooltip or accessible label (`update_property([Gtk.AccessibleProperty.LABEL], [...])`), and associate text labels with input fields via `label.set_mnemonic_widget(field)`.

## Pull requests

- Keep pull requests focused on a single concern.
- Ensure `uv run poe check` passes before pushing.
- Add automated tests for behavior that runs headless. If a change can only be verified on a live desktop or Intel NPU, note that in the PR description instead of writing a superficial mock test.
- Follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, etc.), enforced by CI and used by `git-cliff` (`cliff.toml`) for changelog generation.
- Prefer clear naming, helper extraction, and tests over comments. When a comment documents a rule pinned by a test, cite it as ``(pinned by `tests/test_x.py::test_name`)``; see [docs/comment-style.md](docs/comment-style.md). `tests/test_doc_links.py` validates these citations. Include dates on measured performance claims.
- Keep user-facing strings in English.
- Update `README.md` for new top-level features.

## Releasing

1. Run `git cliff --unreleased` to list changes since the last tag and summarize them into user-facing entries under `## [Unreleased]` in `CHANGELOG.md`.
2. Rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, add a fresh empty `## [Unreleased]` heading above it, and update the compare links at the bottom.
3. Commit directly to `main` with the message `docs(changelog): vX.Y.Z` (skipped by `git-cliff`), without opening a separate release PR.
4. Tag that commit and push both `main` and the tag:

   ```bash
   git push origin main && git tag vX.Y.Z && git push origin vX.Y.Z
   ```

   `.github/workflows/release.yml` publishes the GitHub release using the `## [X.Y.Z]` changelog section as release notes, along with install instructions and a compare link. The workflow fails if `CHANGELOG.md` has no matching section. Because `core/version.py` reads the `VERSION` file packaged into the release tarball (and reports `dev` in a git checkout), no version number needs to be bumped in source files.
