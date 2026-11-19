# Install notes

For the standard release installation (system packages followed by `get.sh`), see the [README](../README.md#install). This page covers desktop-specific packages, installing from a git clone, immutable distributions, and shortcut configuration outside KDE.

## Packages for your desktop

Package names are the same across most distributions, with two exceptions:
- `python3-xlib` is named `python-xlib` on Arch and `python3-python-xlib` on openSUSE.
- `gtk4-layer-shell` is named `typelib-1_0-Gtk4LayerShell-1_0` on openSUSE (and `gir1.2-gtk4layershell-1.0` on Debian 13 / Ubuntu 24.10+).

| Desktop | Install | Why it is needed |
|---|---|---|
| GNOME, sway, Hyprland, and other non-KDE Wayland desktops | `xclip python3-xlib` | vinoPop runs under XWayland on these desktops. Selection and clipboard access go through `xclip` and the compositor's X selection bridge (`wl-clipboard` is not used because GNOME's mutter does not implement the data-control protocol). Keystrokes, typing, and window focus use Xlib and the XTEST extension, so `xdotool` is not required. |
| KDE Plasma Wayland | `wl-clipboard`, plus optional `gtk4-layer-shell` and `ydotool` | Runs natively on Wayland without `xclip`. `gtk4-layer-shell` enables native popup placement (`python3-xlib` is only needed if you omit it), and `ydotool` allows Paste/Cut actions without KDE's Remote Control prompt. |
| Plain Xorg (Cinnamon, XFCE, MATE, etc., best-effort) | `xclip python3-xlib` | Uses the same X11 code path as XWayland, though it receives less testing. |

## From a git clone

```sh
git clone https://github.com/karanshukla/vinoPop.git ~/vinopop
cd ~/vinopop
bash install.sh
pip install --user -r requirements-ai.txt   # optional: offline AI and OCR
vinopop
```

`install.sh` checks your dependencies, sets up autostart and the application launcher, and installs a `vinopop` wrapper in `~/.local/bin`.

To uninstall a git-based installation, run `bash ~/vinopop/install.sh --uninstall`. This removes the launchers, the downloaded AI model, and its compile cache, and prints which user data remains (such as settings and clipboard history). Pass `--purge` to delete that user data as well.

When installing from a release tarball, `get.sh --with-ai` installs the offline AI packages inside a virtual environment that still has access to system GTK bindings. You can inspect or delete downloaded models later under **Settings → Advanced → Offline AI**.

## Immutable distros

- **Fedora Atomic (Silverblue, Kinoite, Bazzite):** Because vinoPop is installed in your home directory, you only need to layer the required system packages from a host terminal (not inside a toolbox, since autostart launches vinoPop on the host). Running `install.sh` prints the exact `rpm-ostree install` command for any missing packages. Reboot after layering, then run `install.sh` again. For offline AI, create a virtual environment with system site packages inside the checkout (`python3 -m venv --system-site-packages .venv`, then `.venv/bin/pip install -r requirements-ai.txt`); `install.sh` detects `.venv` automatically.
- **openSUSE Aeon and Kalpa:** Layer the core GTK and Python packages in a single transaction:
  ```sh
  sudo transactional-update pkg install python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 gdk-pixbuf-loader-rsvg
  ```
  Include the desktop-specific packages from the table above, reboot, and then run `install.sh` again. Running inside Distrobox does not bypass this requirement because autostart runs vinoPop on the host.
- **NixOS and SteamOS:** These distributions are not officially supported targets. `install.sh` will still report missing dependencies, but installing them is left to you.

## Hotkeys off KDE

On GNOME, sway, Hyprland, and COSMIC, vinoPop's built-in hotkeys rely on X11 grabs through XWayland and only trigger when an X11 window is focused. To make shortcuts work across all windows, bind them in your desktop environment to `vinopop --trigger popup` (or `ocr`, `clipboard`, `settings`) and clear the matching hotkey in vinoPop's Settings so they do not conflict.

| Desktop | Configuration |
|---|---|
| GNOME | Settings → Keyboard → Custom Shortcuts, command `/home/you/.local/bin/vinopop --trigger popup` (GNOME does not expand `~`) |
| sway | `bindsym $mod+Shift+y exec ~/.local/bin/vinopop --trigger popup` |
| Hyprland | `bind = SUPER SHIFT, Y, exec, ~/.local/bin/vinopop --trigger popup` |
| COSMIC | Settings → Keyboard → Custom shortcuts, using the same full path as GNOME |

## OCR from the keyboard

After pressing `Shift+Super+O`, you can control the capture box with the keyboard:
- **Arrow keys:** place and move the selection box.
- **`Shift` + arrow keys:** resize the box.
- **`Enter`:** run OCR on the selected box (or on the entire screen if no box is placed).
- **`Escape`:** cancel capture.

## Additional notes

- **Running without a tray icon:** PySide6 is only used for the system tray icon. Without it, vinoPop still runs normally. Pass `--no-tray` to skip creating a tray icon, and run `python3 -m ui.settings_gui` or `python3 -m ui.plugin_manager` from the checkout to open those windows directly.
- **QR code plugin:** Install `qrencode` to enable the QR code plugin.
- **Debian and Ubuntu `pip --user`:** Because the system Python on Debian and Ubuntu restricts `pip --user`, pass `--break-system-packages` or use a virtual environment created with `--system-site-packages` so PyGObject remains accessible.
- **Running from a checkout without installing:** Run `./vinopop`. The process appears as `vinopop` in `ps`, so `pkill -x vinopop` stops it.
- **Diagnostics:** Run `vinopop --doctor` (or `vinopop --doctor --json` for bug reports) to verify AI packages, visible OpenVINO devices, and model download integrity.
- **Upgrading from LinuxPop:** On first startup, vinoPop automatically renames existing LinuxPop config, cache, and data directories to `vinopop`, preserving your settings, plugins, snippets, and clipboard history.
