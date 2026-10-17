"""Where vinoPop keeps its files.

This honours the XDG base-directory variables ($XDG_CONFIG_HOME,
$XDG_CACHE_HOME, $XDG_DATA_HOME) and only falls back to ~/.config,
~/.cache and ~/.local/share when they are unset.

Using the XDG paths keeps vinoPop's configuration, cache, and data
alongside the rest of the user's desktop application files.
"""

from __future__ import annotations

import os
from pathlib import Path

APP = "vinopop"
# The project's previous name; its folders are moved over on first run.
_LEGACY_APP = "linuxpop"


def _base(env_var: str, fallback: str) -> Path:
    val = (os.environ.get(env_var) or "").strip()
    if val and os.path.isabs(val):
        return Path(val)
    return Path(os.path.expanduser(fallback))


CONFIG_HOME = _base("XDG_CONFIG_HOME", "~/.config")
CACHE_HOME = _base("XDG_CACHE_HOME", "~/.cache")
DATA_HOME = _base("XDG_DATA_HOME", "~/.local/share")


def _migrate_legacy(home: Path) -> None:
    """Move a pre-rename ~/.config/linuxpop (etc.) to the vinoPop name so
    upgrading keeps settings, plugins, snippets and history. Only runs
    when the new folder doesn't exist yet; failures are left for the
    app to recreate fresh folders."""
    old, new = home / _LEGACY_APP, home / APP
    if old.is_dir() and not new.exists():
        try:
            old.rename(new)
        except OSError:
            pass


for _home in (CONFIG_HOME, CACHE_HOME, DATA_HOME):
    _migrate_legacy(_home)

# vinoPop's own per-user folders.
CONFIG_DIR = CONFIG_HOME / APP
CACHE_DIR = CACHE_HOME / APP
DATA_DIR = DATA_HOME / APP

# Icons the user drops in (picked up by the icon picker and plugin loader) and
# the hicolor theme folder GTK and the tray search for per-user icons.
USER_ICONS_DIR = CONFIG_DIR / "icons"
HICOLOR_APPS = DATA_HOME / "icons" / "hicolor" / "scalable" / "apps"
