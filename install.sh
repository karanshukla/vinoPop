#!/usr/bin/env bash
# vinoPop installer - sets up autostart for the current user.
# Usage: bash install.sh           # install
#        bash install.sh --uninstall

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ID="io.github.karanshukla.vinoPop"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
AUTOSTART_DIR="$CONFIG_HOME/autostart"
AUTOSTART_FILE="$AUTOSTART_DIR/vinopop.desktop"
APPS_DIR="$DATA_HOME/applications"
APP_FILE="$APPS_DIR/${APP_ID}.desktop"
ICON_DIR="$DATA_HOME/icons/hicolor/scalable/apps"
ICON_FILE="$ICON_DIR/${APP_ID}.svg"
# Files left behind by older installs (short-name launcher, and the
# pre-rename LinuxPop launcher, autostart entry and icon) to clean up.
LEGACY_FILES=(
    "$APPS_DIR/vinopop.desktop"
    "$APPS_DIR/linuxpop.desktop"
    "$APPS_DIR/io.github.GaimsDevSoftware.LinuxPop.desktop"
    "$AUTOSTART_DIR/linuxpop.desktop"
    "$ICON_DIR/io.github.GaimsDevSoftware.LinuxPop.svg"
)
PLUGIN_DIR="$CONFIG_HOME/vinopop/plugins"
BIN_FILE="$HOME/.local/bin/vinopop"
# The interpreter the launchers run. The offline-AI packages usually live
# in a venv, and /usr/bin/python3 can't see them, so prefer one.
if [[ -n "${VINOPOP_PYTHON:-}" ]]; then
    # A bare name (python3.12) is looked up on PATH: the launchers need a path
    PYTHON="$(command -v -- "$VINOPOP_PYTHON" || echo "$VINOPOP_PYTHON")"
elif [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
    PYTHON="$REPO_DIR/.venv/bin/python"
else
    PYTHON="$(command -v python3 || true)"
fi

uninstall() {
    if [[ -f "$AUTOSTART_FILE" ]]; then
        rm -v "$AUTOSTART_FILE"
        echo "[install] vinoPop removed from autostart"
    fi
    for f in "$APP_FILE" "${LEGACY_FILES[@]}"; do
        if [[ -f "$f" ]]; then
            rm -v "$f"
        fi
    done
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
    if [[ -f "$ICON_FILE" ]]; then
        rm -v "$ICON_FILE"
    fi
    if [[ -f "$BIN_FILE" ]] && grep -q "vinoPop launcher" "$BIN_FILE"; then
        rm -v "$BIN_FILE"
    fi
    pkill -x vinopop 2>/dev/null || true
    pkill -i -f "python[0-9.]* .*(vinopop|linuxpop)/main\.py" 2>/dev/null || true
    pkill -f -- "${REPO_DIR}/main\.py" 2>/dev/null || true
    echo "[install] any running instances stopped"
}

if [[ "${1:-}" == "--uninstall" || "${1:-}" == "-u" ]]; then
    uninstall
    exit 0
fi

# Which backend this session will get; same rule as platform_backend.detect().
# "unknown" when run with no desktop session (e.g. over ssh).
session_backend() {
    local forced="${VINOPOP_BACKEND:-}" session="${XDG_SESSION_TYPE:-}" desktop="${XDG_CURRENT_DESKTOP:-}"
    forced="${forced,,}"
    if [[ "$forced" == "x11" || "$forced" == "wayland_kde" ]]; then
        echo "$forced"
    elif [[ -z "${WAYLAND_DISPLAY:-}" && -z "${DISPLAY:-}" ]]; then
        echo "unknown"
    elif [[ -n "${WAYLAND_DISPLAY:-}" && ( -z "${DISPLAY:-}" || "${session,,}" == "wayland" ) \
            && ":${desktop^^}:" == *":KDE:"* ]]; then
        echo "wayland_kde"
    else
        echo "x11"
    fi
}
has() { command -v "$1" >/dev/null 2>&1; }

# fedora, arch, debian or other, from /etc/os-release; picks the install hints.
distro_family() {
    local ids=""
    if [[ -r /etc/os-release ]]; then
        # shellcheck source=/dev/null
        ids="$(. /etc/os-release && echo "${ID:-} ${ID_LIKE:-}")"
    fi
    case " ${ids,,} " in
        *" fedora "* | *" rhel "*) echo fedora ;;
        *" arch "*) echo arch ;;
        *" debian "* | *" ubuntu "*) echo debian ;;
        *) echo other ;;
    esac
}
family="$(distro_family)"

# The distro's package names for a dependency.
pkg() {
    case "$family:$1" in
        fedora:gtk) echo "python3-gobject gtk4 libadwaita" ;;
        arch:gtk) echo "python-gobject gtk4 libadwaita" ;;
        *:gtk) echo "python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1" ;;
        arch:python3) echo "python" ;;
        arch:xlib) echo "python-xlib" ;;
        *:xlib) echo "python3-xlib" ;;
        *) echo "$1" ;;
    esac
}

# The command that installs the given packages on this distro.
hint() {
    case "$family" in
        fedora) echo "sudo dnf install $*" ;;
        arch) echo "sudo pacman -S --needed $*" ;;
        debian) echo "sudo apt-get install -y $*" ;;
        *) echo "install with your package manager (see README.md): $*" ;;
    esac
}

# Verify dependencies
echo "[install] checking dependencies..."
backend="$(session_backend)"
missing=()
notes=()
clipboard_either=false
if [[ ! -x "$PYTHON" ]]; then
    if [[ -n "${VINOPOP_PYTHON:-}" ]]; then
        echo "[install] VINOPOP_PYTHON=$VINOPOP_PYTHON is not an executable"
        exit 1
    fi
    missing+=("python3")
fi
has xdg-open || missing+=("xdg-utils")
case "$backend" in
    x11)
        # The XWayland backend (GNOME, sway, Hyprland ...): the whole process
        # runs on XWayland, so selection and clipboard go through xclip and
        # the compositor's X selection bridge (not wl-clipboard: mutter has
        # no data-control protocol). Keystrokes use the X server's XTEST.
        has xclip || missing+=("xclip")
        has xdotool || notes+=("xdotool is not installed (optional): the clipboard picker pastes short text with Ctrl+V instead of typing it, which some Electron editors ignore")
        if [[ -z "${WAYLAND_DISPLAY:-}" ]]; then
            notes+=("this is a plain Xorg session: vinoPop runs on it best-effort; GNOME, sway and other Wayland desktops (through XWayland) and KDE Plasma Wayland are the supported targets")
        fi
        ;;
    wayland_kde)
        { has wl-copy && has wl-paste; } || missing+=("wl-clipboard")
        ;;
    *)
        echo "[install] no desktop session found, so checking for either clipboard tool"
        if ! has xclip && ! has wl-copy; then
            clipboard_either=true
        fi
        ;;
esac
if [[ ${#missing[@]} -gt 0 ]]; then
    echo "[install] missing: ${missing[*]}"
    pkgs=()
    for m in "${missing[@]}"; do pkgs+=("$(pkg "$m")"); done
    echo "[install]   $(hint "${pkgs[@]}")"
fi
if $clipboard_either; then
    echo "[install] missing a clipboard tool: xclip (GNOME, sway and other desktops through XWayland, or Xorg) or wl-clipboard (KDE Plasma Wayland)"
    echo "[install]   $(hint xclip) (or wl-clipboard)"
fi
if [[ ${#missing[@]} -gt 0 ]] || $clipboard_either; then
    exit 1
fi

"$PYTHON" -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Gtk, Adw" 2>/dev/null \
    || { echo "[install] missing GTK 4 / libadwaita for $PYTHON: $(hint "$(pkg gtk)")"
         echo "[install]   (a venv only sees them if it was made with --system-site-packages on the distro's python3)"
         exit 1; }
"$PYTHON" -c "import PySide6" 2>/dev/null \
    || notes+=("PySide6 is not installed, so there will be no tray icon (your distro's PySide6 package, or pip install PySide6 into a venv)")
if ! "$PYTHON" -c "import Xlib" 2>/dev/null; then
    # The X11 backend can't run without it. On KDE Plasma Wayland only the
    # popup's XWayland fallback (no gtk4-layer-shell) needs it.
    if [[ "$backend" == "wayland_kde" ]] \
        && "$PYTHON" -c "import gi; gi.require_version('Gtk4LayerShell', '1.0')" 2>/dev/null; then
        notes+=("python-xlib is not installed (optional on KDE Plasma Wayland with gtk4-layer-shell)")
    else
        echo "[install] missing python-xlib: $(hint "$(pkg xlib)"), or pip install python-xlib into the venv"
        exit 1
    fi
fi
for note in "${notes[@]}"; do
    echo "[install] note: $note"
done

# Launchers run the vinopop script, not main.py, so the process is named
# vinopop.
mkdir -p "$AUTOSTART_DIR" "$APPS_DIR" "$ICON_DIR" "$PLUGIN_DIR" "$(dirname "$BIN_FILE")"

# Clean up old launchers so we don't end up with duplicate entries in
# the menu (or two autostart entries) after upgrading.
rm -f "${LEGACY_FILES[@]}"

# The Exec= line, quoted per the Desktop Entry spec by the function the
# Settings toggle uses, so a checkout path with spaces, quotes, $, ` or % works.
EXEC_LINE="$(PYTHONPATH="$REPO_DIR" "$PYTHON" -c '
import sys
from core.autostart import _exec_quote
print(" ".join(_exec_quote(a) for a in sys.argv[1:]))' "$PYTHON" "$REPO_DIR/vinopop")"
# The same for the sh launcher: single quotes, with any ' closed and reopened.
sh_quote() { printf "'%s'" "${1//\'/\'\\\'\'}"; }

cat > "$AUTOSTART_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=vinoPop
Comment=PopClip-inspired floating action popup
Exec=${EXEC_LINE}
Icon=${APP_ID}
Terminal=false
Categories=Utility;
X-GNOME-Autostart-enabled=true
StartupNotify=false
EOF
# systemd-xdg-autostart-generator warns on an executable .desktop
chmod 644 "$AUTOSTART_FILE"

# Write the launcher .desktop using the app ID as its icon name.
cat > "$APP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=vinoPop
GenericName=Text action popup
Comment=PopClip-inspired floating popup of context-aware actions on selected text
Exec=${EXEC_LINE}
Icon=${APP_ID}
Terminal=false
Categories=Utility;TextTools;
Keywords=popup;text;clipboard;snippets;ai;productivity;popclip;
StartupNotify=false
StartupWMClass=vinopop
X-GNOME-UsesNotifications=true
EOF
chmod +x "$APP_FILE"
update-desktop-database "$APPS_DIR" 2>/dev/null || true

# `vinopop` on PATH, with the same interpreter the launchers use.
cat > "$BIN_FILE" <<EOF
#!/bin/sh
# vinoPop launcher, written by install.sh; --uninstall removes it.
exec $(sh_quote "$PYTHON") $(sh_quote "$REPO_DIR/vinopop") "\$@"
EOF
chmod 755 "$BIN_FILE"

# Mirror the icon under the app ID so desktop environments can resolve it.
if [[ -f "$REPO_DIR/icons/vinopop.svg" ]]; then
    install -Dm644 "$REPO_DIR/icons/vinopop.svg" "$ICON_FILE"
fi
gtk-update-icon-cache -f "$ICON_DIR/.." 2>/dev/null || true

echo "[install] autostart written to $AUTOSTART_FILE"
echo "[install] launcher entry written to $APP_FILE"
echo "[install] vinoPop will start automatically next login,"
echo "[install]   and shows up in Synapse / Mint menu / GNOME Activities."
echo "[install] launchers run: ${PYTHON}"
echo "[install] command written to $BIN_FILE"
case ":$PATH:" in
    *":$(dirname "$BIN_FILE"):"*) echo "[install] to start now: vinopop" ;;
    *) echo "[install] to start now: $BIN_FILE ($(dirname "$BIN_FILE") is not on your PATH)" ;;
esac
echo "[install] uninstall: bash $0 --uninstall"
