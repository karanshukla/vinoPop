"""Toggle whether vinoPop launches at login.

Writes / removes ~/.config/autostart/vinopop.desktop. Honoured by every
freedesktop-spec desktop environment (Cinnamon, GNOME, KDE Plasma, XFCE,
MATE, Budgie, etc.).

Used from the Settings dialog (Settings -> Activation -> Start at login)
so the user doesn't have to know where the file lives or edit it by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path

from xdg_paths import CONFIG_HOME

AUTOSTART_DIR = CONFIG_HOME / "autostart"
AUTOSTART_FILE = AUTOSTART_DIR / "vinopop.desktop"
# Written by releases from before the rename to vinoPop.
LEGACY_AUTOSTART_FILE = AUTOSTART_DIR / "linuxpop.desktop"
LAUNCHER = Path(__file__).resolve().parent / "vinopop"


def _exec_quote(arg: str) -> str:
    """One argument of a Desktop Entry `Exec=` line.

    The spec has no single quotes: an argument with spaces or shell-special
    characters is wrapped in double quotes, with `"`, backtick, `$` and `\\`
    backslash-escaped, and the whole value is then a string whose backslashes
    are escaped again. `%` introduces field codes, so a literal one is doubled.
    """
    arg = arg.replace("%", "%%")
    if arg and not any(c in arg for c in " \t\n\"'\\><~|&;$*?#()`%"):
        return arg
    for ch in ("\\", '"', "`", "$"):
        arg = arg.replace(ch, "\\" + ch)
    return '"' + arg.replace("\\", "\\\\") + '"'


def _desktop_file_contents(exec_path: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=vinoPop\n"
        "Comment=PopClip-inspired floating action popup\n"
        f"Exec={exec_path}\n"
        "Icon=vinopop\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
        "X-GNOME-Autostart-enabled=true\n"
        "StartupNotify=false\n"
    )


def is_enabled() -> bool:
    """True if the autostart .desktop file exists."""
    return AUTOSTART_FILE.is_file() or LEGACY_AUTOSTART_FILE.is_file()


def set_enabled(enabled: bool, main_py: Path | None = None) -> bool:
    """Create or remove the autostart .desktop file. Returns True on
    success. `main_py` (the script to run) defaults to the `vinopop`
    launcher in this checkout, so a user who runs vinoPop from a
    non-standard checkout still gets their copy on autostart."""
    try:
        if enabled:
            AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
            if main_py is None:
                main_py = LAUNCHER
            # Quote the path so checkouts in directories with spaces
            # (e.g. ~/My Code/vinopop/) don't produce a broken Exec line
            # that the DE silently fails to launch.
            # The running interpreter, so a venv that holds the AI
            # packages keeps being used at login.
            exec_line = f"{_exec_quote(sys.executable)} {_exec_quote(str(main_py))}"
            AUTOSTART_FILE.write_text(
                _desktop_file_contents(exec_line),
                encoding="utf-8",
            )
            # systemd-xdg-autostart-generator warns on an executable .desktop
            AUTOSTART_FILE.chmod(0o644)
            if LEGACY_AUTOSTART_FILE.is_file():
                LEGACY_AUTOSTART_FILE.unlink()
        else:
            for f in (AUTOSTART_FILE, LEGACY_AUTOSTART_FILE):
                if f.is_file():
                    f.unlink()
        return True
    except OSError:
        return False


def migrate_from_main_py() -> bool:
    """Point an autostart entry from before the `vinopop` launcher at it.

    Old entries run `python3 .../main.py`, which still works but shows up
    as `python3` in ps and top. Returns True if the file was rewritten.
    """
    try:
        text = AUTOSTART_FILE.read_text(encoding="utf-8")
    except OSError:
        return False
    exec_line = next((ln for ln in text.splitlines() if ln.startswith("Exec=")), "")
    if not exec_line.rstrip('"').endswith("/main.py"):
        return False
    return set_enabled(True)
