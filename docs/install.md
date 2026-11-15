# Install notes

The [README](../README.md#install) has the short version: system packages, then `get.sh`. This page has the rest.

## Packages for your desktop

Package names are the same everywhere, except `python3-xlib` is `python-xlib` on Arch and `python3-python-xlib` on openSUSE, and `gtk4-layer-shell` is `typelib-1_0-Gtk4LayerShell-1_0` on openSUSE.

| Desktop | Install | Why |
|---|---|---|
| GNOME, sway, Hyprland and other Wayland desktops except KDE | `xclip python3-xlib` | vinoPop runs on XWayland. Selection and clipboard go through `xclip` and the compositor's X selection bridge (`wl-clipboard` isn't used: GNOME's mutter has no data-control protocol for it); keystrokes, typing and window focus through Xlib and the X server's XTEST extension, so no `xdotool` |
| KDE Plasma Wayland | `wl-clipboard`, plus `gtk4-layer-shell` for native popup placement (`gir1.2-gtk4layershell-1.0` on Debian 13 / Ubuntu 24.10+) and `ydotool` if you want Paste/Cut without KDE's Remote Control prompt | Native Wayland; `xclip` isn't used. `python3-xlib` only matters without `gtk4-layer-shell` |
| Plain Xorg (Cinnamon, XFCE, MATE ...), best-effort | same as the first row | Same code path as XWayland, less tested |

## From a git clone

```sh
git clone https://github.com/karanshukla/vinoPop.git ~/vinopop
cd ~/vinopop
bash install.sh
pip install --user -r requirements-ai.txt   # optional: the offline AI and OCR
vinopop
```

`install.sh` checks your dependencies, sets up autostart and the app launcher, and puts a `vinopop` command in `~/.local/bin`. `bash ~/vinopop/install.sh --uninstall` removes the launchers, the AI model and its compile cache, and lists what it left (settings, clipboard history); add `--purge` to delete those too.

From a release, `get.sh --with-ai` installs the offline AI in a venv that sees the system's GTK. Settings > Advanced > Offline AI shows the downloaded models and deletes them.

## Immutable distros

**Fedora Atomic (Silverblue, Kinoite, Bazzite):** vinoPop lives in your home directory, so only the system packages need layering, from a host terminal (not a toolbox: autostart runs vinoPop on the host). `install.sh` names whatever's missing as an `rpm-ostree install` line. Reboot after layering, then run `install.sh` again. Keep the offline AI in a venv made with `--system-site-packages` inside the checkout (`python3 -m venv --system-site-packages .venv`, then `.venv/bin/pip install -r requirements-ai.txt`): `install.sh` picks up `.venv` on its own.

**openSUSE Aeon and Kalpa:** expect to layer several packages in one go: `sudo transactional-update pkg install python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 gdk-pixbuf-loader-rsvg` plus your desktop's row above, then a reboot and `install.sh` again. Distrobox doesn't get around this: autostart runs vinoPop on the host, so the packages have to be on the host too.

NixOS and SteamOS aren't supported targets. `install.sh` still names what's missing there, but getting it installed is up to you.

## Hotkeys off KDE

On GNOME, sway, Hyprland and COSMIC the built-in hotkeys are X11 grabs through XWayland, which only fire while an X11 window has focus. A shortcut your desktop runs works in every app: point it at `vinopop --trigger popup` (or `ocr`, `clipboard`, `settings`) and clear the matching hotkey in vinoPop's Settings so the two don't fight.

| Desktop | Where |
|---|---|
| GNOME | Settings → Keyboard → Custom Shortcuts, command `/home/you/.local/bin/vinopop --trigger popup` (GNOME doesn't expand `~`) |
| sway | `bindsym $mod+Shift+y exec ~/.local/bin/vinopop --trigger popup` |
| Hyprland | `bind = SUPER SHIFT, Y, exec, ~/.local/bin/vinopop --trigger popup` |
| COSMIC | Settings → Keyboard → Custom shortcuts, same command as GNOME |

## OCR from the keyboard

After `Shift+Super+O`: arrows place and move the box, Shift+arrows resize, Enter reads it (or the whole screen if nothing is placed), Escape cancels.

## Odds and ends

- PySide6 is only for the tray icon. Without it vinoPop runs fine with no tray; `--no-tray` skips the attempt, and `python3 -m ui.settings_gui` / `python3 -m ui.plugin_manager` (from the checkout) open the windows directly.
- `qrencode` enables the QR plugin.
- On Debian/Ubuntu, whose system Python refuses `pip --user`, add `--break-system-packages` or use a venv created with `--system-site-packages` so it still sees PyGObject.
- From a checkout without installing, run `./vinopop`. It shows up as `vinopop` in `ps`, so `pkill -x vinopop` stops it.
- `vinopop --doctor` (add `--json` for a bug report) checks the AI packages, the devices OpenVINO sees and whether the model is downloaded.
- Upgrading from LinuxPop? On first run its config, cache and data folders are renamed to `vinopop`, so settings, plugins, snippets and history carry over.
