# vinoPop on Fedora KDE Plasma (Wayland)

Fedora KDE Plasma 6 runs Wayland by default (Fedora 42 dropped the Plasma X11 session). vinoPop detects this automatically and uses its native **Wayland/KDE backend**, with no Xorg session required.

The table below shows how each feature maps onto Wayland/KDE:

| Feature | Wayland/KDE implementation |
|---|---|
| Read selection / clipboard | `wl-clipboard` (`wl-paste` / `wl-copy`) |
| Auto-popup on selection | `wl-paste --primary --watch` |
| Popup placement at cursor | `gtk4-layer-shell` (anchor top-left + margins); falls back to XWayland if unavailable |
| Global cursor position | KWin scripting `workspace.cursorPos` over D-Bus |
| Keystroke / paste injection | xdg-desktop-portal RemoteDesktop (keysyms); `ydotool` + `ydotoold` as a fallback |
| Modifier + double-click | `/dev/input` (all keyboards and mice, including `keyd` virtual keyboards) |
| Global hotkey | KGlobalAccel over D-Bus |
| Tray icon | PySide6 `QSystemTrayIcon` (StatusNotifierItem over D-Bus) |

Backend selection is automatic: a Wayland session (`XDG_SESSION_TYPE=wayland`) where `XDG_CURRENT_DESKTOP` includes `KDE` uses the Wayland/KDE backend. All other desktops, including other Wayland compositors, use the XWayland/X11 backend (`x11`). You can force a backend with `VINOPOP_BACKEND=x11` or `VINOPOP_BACKEND=wayland_kde`.

## Install dependencies

```sh
sudo dnf install -y \
    python3 python3-gobject python3-dbus \
    gtk4 libadwaita gtk4-layer-shell \
    wl-clipboard xdg-desktop-portal-kde python3-pyside6
# optional: Paste/Cut without the Remote Control prompt (see below)
sudo dnf install -y ydotool
# optional: offline AI and screen OCR (OpenVINO, RapidOCR), from the checkout
pip install --user -r requirements-ai.txt
```

- `xdg-desktop-portal-kde` is preinstalled on Fedora KDE, but is listed above in case of a minimal installation.
- `python3-pyside6` is only needed for the system tray icon.
- OCR region capture uses Spectacle, which ships with Fedora KDE.
- `wtype` is not needed because it does not work on KWin (see below).

### Paste, Cut, Select all, Backspace

These actions inject keystrokes into the application where you selected text. On KDE Wayland, vinoPop uses the desktop's **Remote Control** portal (`org.freedesktop.portal.RemoteDesktop`):

- The first time you trigger one of these actions, KDE prompts: *"vinoPop wants to control your keyboard"*. Enable **Allow restoring on future sessions** (available on Plasma 6.6.6 and newer; older versions prompt on every session) and approve it. vinoPop saves the restore token to `~/.local/state/vinopop/remote-desktop.token`; delete that file if you want to be prompted again.
- While a Remote Control session is active, Plasma displays a *Remote Control* icon in the system tray. vinoPop closes the session after 5 minutes without a keypress so the icon disappears; the next Paste reopens it automatically with a brief "Remote Control Started" notification and no dialog.
- If you decline the prompt, Paste and Cut fall back to `ydotool` (below) or leave the result on the clipboard with a notification. Restart vinoPop to trigger the prompt again.
- The portal sends key *symbols* that KWin maps using your active keyboard layout, so `Ctrl+A` selects all on AZERTY and Dvorak layouts as well.

**Fallback: `ydotool`.** If you prefer not to use the portal or it is unavailable:

```sh
sudo dnf install -y ydotool
sudo usermod -aG input "$USER"   # log out and back in
rpm -ql ydotool | grep '\.service$'   # find the systemd unit shipped by your package
```

Start `ydotoold` using that unit (upstream provides a user unit: `systemctl --user enable --now ydotoold`) or run it manually. The daemon requires write access to `/dev/uinput`.

vinoPop looks for the daemon socket at `$YDOTOOL_SOCKET`, `$XDG_RUNTIME_DIR/.ydotool_socket`, or `/tmp/.ydotool_socket`. If `ydotool` is installed but `ydotoold` is not running, vinoPop treats `ydotool` as unavailable rather than silently failing. Because `ydotool` sends physical keycodes, vinoPop maps them through the first keyboard layout configured in KDE Settings; layouts switched to at runtime are not covered.

`wtype` does not work on KWin because KWin does not implement the `zwp_virtual_keyboard_v1` protocol. vinoPop only attempts to use `wtype` on other compositors such as sway and Hyprland.

Run `vinopop --doctor` and inspect the `key injection` row to verify which method vinoPop detected.

### Modifier+double-click

Enable this option in Settings. Because it reads keyboard and mouse events from `/dev/input`, your user must belong to the `input` group:

```sh
sudo usermod -aG input "$USER"   # then log out and back in
```

All keyboards and mice are monitored, including Bluetooth devices connected after vinoPop starts and virtual keyboards created by remappers such as [keyd](https://github.com/rvaiya/keyd) (which grabs the physical device). `ydotool`'s own virtual device is ignored. Check the `input devices` row in `vinopop --doctor` to see how many input devices vinoPop can read.

## Run

```sh
git clone https://github.com/karanshukla/vinoPop.git ~/vinopop
cd ~/vinopop
./vinopop                  # auto-detects the Wayland/KDE backend
```

Logs are written to `~/.cache/vinopop/vinopop.log`.

## Known limitations on Wayland/KDE

- **Global hotkey:** Registered through KGlobalAccel; verify that the binding appears under **System Settings → Shortcuts**. If pressing the shortcut does nothing, please file an issue, as the Qt key-sequence encoding may need adjustment for your keyboard layout.
- **Esc-to-dismiss and click-outside:** Because the popup never takes keyboard focus (so paste actions stay targeted at your active app), `Esc` does not dismiss it on Wayland; instead, it auto-hides on a timer or when the pointer leaves.
- **Active-window blocklist:** Supported natively: the focused window's class and title are queried via a KWin script while the selection settles.
- **Initial latency:** The first popup after a selection incurs ~100–150 ms of extra latency from the cursor-position round-trip to KWin. A resident helper to reduce this overhead is planned.

Please report other issues in the [GitHub issue tracker](https://github.com/karanshukla/vinoPop/issues).
