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
from typing import IO, Any

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
STATE_HOME = _base("XDG_STATE_HOME", "~/.local/state")


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
STATE_DIR = STATE_HOME / APP
LOG_FILE = CACHE_DIR / "vinopop.log"

# Icons the user drops in (picked up by the icon picker and plugin loader) and
# the hicolor theme folder GTK and the tray search for per-user icons.
USER_ICONS_DIR = CONFIG_DIR / "icons"
HICOLOR_APPS = DATA_HOME / "icons" / "hicolor" / "scalable" / "apps"


# ---- private files --------------------------------------------------------
# Clipboard history, snippets and settings can hold whatever the user copied,
# so they're written 0600 in 0700 folders whatever the umask or the mode of
# ~/.cache is (not every distro makes the home folder private).


def private_dir(path: Path) -> Path:
    """Create `path` if needed and make it 0700. Parents that don't exist
    yet are created with the default mode, so callers tighten each of
    vinoPop's own folders themselves."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    make_private(path)
    return path


def make_private(path: Path) -> None:
    """chmod an existing file to 0600 or folder to 0700; a no-op when it's
    already private or missing. Used to tighten files written by older
    versions with the default umask."""
    try:
        st = path.stat()
    except OSError:
        return
    want = 0o700 if path.is_dir() else 0o600
    if st.st_mode & 0o777 != want:
        try:
            os.chmod(path, want)
        except OSError:
            pass


def open_private(path: Path, mode: str = "w") -> IO[Any]:
    """open() for writing, but the file is created (or truncated) as 0600.
    `mode` is "w" (UTF-8 text) or "wb"."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)  # an existing file keeps its old mode otherwise
    except OSError:
        pass
    if "b" in mode:
        return os.fdopen(fd, mode)
    return os.fdopen(fd, mode, encoding="utf-8")
