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
# The interpreter the launchers run. The offline-AI packages usually live
# in a venv, and /usr/bin/python3 can't see them, so prefer one.
if [[ -n "${VINOPOP_PYTHON:-}" ]]; then
    PYTHON="$VINOPOP_PYTHON"
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
    pkill -i -f "python[0-9.]* .*(vinopop|linuxpop)/main\.py" 2>/dev/null || true
    pkill -f -- "${REPO_DIR}/main\.py" 2>/dev/null || true
    echo "[install] any running instances stopped"
}

if [[ "${1:-}" == "--uninstall" || "${1:-}" == "-u" ]]; then
    uninstall
    exit 0
fi

# Verify dependencies
echo "[install] checking dependencies..."
missing=()
[[ -x "$PYTHON" ]] || missing+=("python3")
command -v xdg-open >/dev/null 2>&1 || missing+=("xdg-open")
# Clipboard tool: xclip on X11, wl-clipboard on Wayland (KDE Plasma)
if ! command -v xclip >/dev/null 2>&1 && ! command -v wl-copy >/dev/null 2>&1; then
    missing+=("xclip")
fi
if [[ ${#missing[@]} -gt 0 ]]; then
    echo "[install] missing: ${missing[*]}"
    echo "[install] install with: sudo apt-get install -y ${missing[*]/xdg-open/xdg-utils}"
    exit 1
fi

"$PYTHON" -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Gtk, Adw" 2>/dev/null \
    || { echo "[install] missing GTK 4 / libadwaita for python3-gi: sudo apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1"; exit 1; }
"$PYTHON" -c "import PySide6" 2>/dev/null \
    || echo "[install] note: PySide6 is not installed, so there will be no tray icon (pip install --user PySide6, or your distro's python3-pyside6)"
if ! "$PYTHON" -c "import Xlib" 2>/dev/null; then
    # Only the X11 backend needs it; a pure-Wayland (KDE Plasma) session runs without
    if [[ -n "${WAYLAND_DISPLAY:-}" && -z "${DISPLAY:-}" ]]; then
        echo "[install] note: python-xlib is not installed (not needed on a pure Wayland session)"
    else
        echo "[install] missing python-xlib: pip3 install python-xlib --break-system-packages"
        exit 1
    fi
fi

# Write the autostart .desktop with the absolute path to main.py
mkdir -p "$AUTOSTART_DIR" "$APPS_DIR" "$ICON_DIR" "$PLUGIN_DIR"

# Clean up old launchers so we don't end up with duplicate entries in
# the menu (or two autostart entries) after upgrading.
rm -f "${LEGACY_FILES[@]}"
cat > "$AUTOSTART_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=vinoPop
Comment=PopClip-inspired floating action popup
Exec="${PYTHON}" "${REPO_DIR}/main.py"
Icon=vinopop
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
Exec="${PYTHON}" "${REPO_DIR}/main.py"
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
echo "[install] to start now: ${PYTHON} ${REPO_DIR}/main.py"
echo "[install] uninstall: bash $0 --uninstall"
