# Contributing to vinoPop

Thanks for your interest! vinoPop is a small, hackable tool - most useful
contributions will be plugins.

## Project layout

```
vinopop             launcher (what autostart and ~/.local/bin run)
main.py             the daemon: wires everything together
core/               classifier, actions, settings, XDG paths, version, autostart
triggers/           what starts the popup: hotkeys, selection and double-click watchers, editable-field detection
plugin_system/      Plugin dataclass, plugin and recipe loaders, compat for old bare-name imports
ui/                 GTK 4 windows: popup bar, AI bubble, settings, plugin manager, recipe wizard, onboarding, theme
capture/            screen OCR and its region selector
tray/               tray protocol (daemon side) and the Qt tray process
platform_backend/   X11 and KDE Wayland backends
vino_ai/            offline AI on OpenVINO: models, worker, OCR, doctor
plugins_repo/       bundled plugins and recipes (the catalogue)
icons/  docs/  scripts/  tests/
```

## Writing a plugin

A plugin is a single Python file in `~/.config/vinopop/plugins/` that defines
a top-level `register(register_plugin)` function. vinoPop discovers it on
startup (and after install via the Plugin Manager).

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

Save as `~/.config/vinopop/plugins/shout.py` and restart vinoPop. Select
text → popup shows your new button.

### Plugin API

`Plugin` is a dataclass from `plugin_base`:

| Field | Type | Meaning |
|---|---|---|
| `name` | `str` | Unique identifier (used in logs) |
| `icon` | `str` | Theme icon name (try the [freedesktop icon spec](https://specifications.freedesktop.org/icon-naming-spec/icon-naming-spec-latest.html)) |
| `tooltip` | `str` | Hover text on the popup button |
| `handler` | `Callable[[str], None]` | Called with the selected text when clicked |
| `content_types` | `Iterable[ContentType]` | `()` for all types, or specific values |
| `priority` | `int` | Lower numbers appear first in the popup (default 100) |
| `predicate` | `Callable[[str], bool] \| None` | Optional selection-text check; the button is hidden when it returns false |
| `requires_editable` | `bool` | Hide the button when the focused widget is not editable (default false) |

`ContentType` values: `COMMAND`, `URL`, `EMAIL`, `PATH`, `PLAIN_TEXT`. See
`core/classifier.py` for the classification rules.

### Tips

- **The handler runs on a worker thread**, not the GTK main loop, so
  blocking work (subprocess calls, file reads) is fine there. Anything that
  touches GTK (opening a window, updating a widget) must go through
  `GLib.idle_add`. Use `actions.notify(title, body, icon)` to communicate
  results: it shows the notification as vinoPop's, not as `notify-send`'s.
- **Copy results to the clipboard** with `actions.copy_to_clipboard(text)`
  (`from actions import copy_to_clipboard`) so the user can paste them. It
  uses the backend's tool (xclip on the XWayland backend, wl-copy on KDE
  Wayland); calling either yourself picks the wrong one somewhere.
- **Use existing icons** - most desktops ship the [hicolor / Adwaita icon
  themes](https://developer.gnome.org/hig/reference/icons.html). Use
  `-symbolic` suffix for monochrome icons that pick up theme colors. Pick a
  name both Adwaita and Breeze ship (`tests/test_icons.py` has the list) or
  add a fallback in `ui/icon_style.py`, and give each of a plugin's actions
  its own icon so two buttons never look the same. A bundled icon is a pair
  in `icons/`: the tile `vinopop-<concept>.svg` and the glyph
  `vinopop-<concept>-symbolic.svg`, mapped in `ui/icon_style.py` so the
  user's icon style picks between them. Draw glyphs as paths, not `<text>`
  (text depends on installed fonts), with the mark filling about 14/16 of the
  viewBox like Adwaita's.
- **Don't depend on a specific path layout** - `classifier`, `plugin_base`,
  `actions` and `settings` are importable by those bare names because
  `plugin_system/compat.py` maps them to `core.classifier`,
  `plugin_system.plugin_base` and so on. Don't add `sys.path.insert` to your
  plugin.

### Submitting a plugin to the built-in catalogue

Open a PR adding:
1. Your plugin file to `plugins_repo/your_plugin.py`
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
3. The same version as the file's first line, `# vinopop-plugin-version: 1`
   (a recipe carries it as a `"version": 1` key instead).

Installing copies the file into `~/.config/vinopop/plugins/`, so a later fix
only reaches people who installed it if you bump the version in both places:
the Plugin Manager then offers their copy an Update. If you drop a bundled
file, add it to `plugins_repo/retired.json` with the reason, so installs that
still have it say so.

External system dependencies (e.g. your plugin needs `imagemagick` installed)
should fail gracefully - `shutil.which("convert")` check and an `actions.notify`
explaining what's missing.

## Bug reports & feature requests

Please file an issue. Include:
- Distro + desktop environment (`echo $XDG_CURRENT_DESKTOP`)
- Output of `vinopop --debug` (or `./vinopop --debug` from a checkout)
- Steps to reproduce

## Development setup

The app runs on your distro's PyGObject with GTK 4 (4.14+) and libadwaita
(1.5+), so run it with the system Python (see README for the
`gir1.2-gtk-4.0` / `gir1.2-adw-1` packages and their Fedora/Arch names). To
try the X11 path on a Wayland desktop, run it with `GDK_BACKEND=x11`. The checks don't need GTK or OpenVINO; this is what
CI does:

```bash
uv venv
uv pip install --group dev
uv run poe check    # ruff, ruff format --check, mypy, pytest
```

`uv run poe fix` applies ruff's fixes and formatting, and
`uv run poe security` runs the same Bandit scan as CI.

Turn on the hooks so a commit can't fail CI's lint or commits job. The
pre-commit hook runs `scripts/lint.sh` (ruff, ruff format --check, mypy) and
the commit-msg hook runs `scripts/check_commits.sh`, the same scripts CI
runs:

```bash
git config core.hooksPath .githooks
```

`tests/test_a11y.py` builds the real windows and checks them for
accessibility: every control has a name a screen reader can read, dialogs
close on Escape, and the custom colours keep 4.5:1 contrast in light and
dark. It needs a display, and it opens windows on yours, so run it on a
virtual one the way CI's test-gtk job does (the session bus also lets it
read the live AT-SPI tree):

```bash
xvfb-run -a dbus-run-session -- env VINOPOP_ATSPI_TEST=1 python3 -m pytest tests/test_a11y.py
```

When you add an icon-only button, give it a tooltip or an accessible label
(`update_property([Gtk.AccessibleProperty.LABEL], [...])`); label a text
field with `label.set_mnemonic_widget(field)`.

## Pull requests

- Keep PRs focused - one concern per PR
- `uv run poe check` passes before you push
- Tests cover what runs without a display. If a change can only be
  verified on a desktop or on the NPU, say so in the PR instead of faking
  a test
- Commits are conventional (`feat:`, `fix:`, `docs:`, ...), checked by CI;
  the changelog is generated from them with git-cliff (`cliff.toml`)
- Don't add user-facing strings in languages other than English
- For new top-level features, update README.md

## Releasing

1. `git cliff --unreleased` lists what's changed. Turn it into user-facing
   entries under `## [Unreleased]` in CHANGELOG.md, if they aren't there yet.
2. Rename that heading to `## [X.Y.Z] - YYYY-MM-DD`, add a new empty
   `## [Unreleased]` above it, and update the compare links at the bottom.
3. Commit it straight to main as `docs(changelog): vX.Y.Z`, with no PR
   (git-cliff skips that subject). A PR per release only adds clutter.
4. Tag that commit and push both:

   ```bash
   git push origin main && git tag vX.Y.Z && git push origin vX.Y.Z
   ```

   `.github/workflows/release.yml` creates the GitHub release with the
   `## [X.Y.Z]` section as its notes, plus install steps and a compare link.
   It fails if CHANGELOG.md has no section for the tag. The app reads its
   version from the tag (`core/version.py`), so there's nothing to bump.
