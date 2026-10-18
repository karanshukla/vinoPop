# vinoPop on Fedora KDE Plasma (Wayland)

Fedora KDE Plasma 6 runs Wayland by default (Fedora 42 dropped the Plasma X11
session entirely). vinoPop detects this and uses a native **Wayland/KDE
backend** instead of the X11 one - no Xorg session required.

How it maps each piece of the X11 design onto Wayland/KDE:

| Feature | Wayland/KDE implementation |
|---|---|
| Read selection / clipboard | `wl-clipboard` (`wl-paste` / `wl-copy`) |
| Auto-popup on selection | `wl-paste --primary --watch` |
| Popup placement at cursor | `gtk4-layer-shell` (anchor top-left + margins); XWayland if it isn't installed |
| Global cursor position | KWin scripting `workspace.cursorPos` over D-Bus |
| Keystroke / paste injection | xdg-desktop-portal RemoteDesktop (keysyms); `ydotool` + `ydotoold` as a fallback |
| Modifier+double-click | `/dev/input` (every keyboard and mouse, keyd's virtual keyboard included) |
| Global hotkey | KGlobalAccel over D-Bus |
| Tray icon | StatusNotifierItem via libappindicator (KDE SNI) |

The backend is chosen automatically from `XDG_SESSION_TYPE`. Force it with
`VINOPOP_BACKEND=x11` or `VINOPOP_BACKEND=wayland_kde`.

## Install dependencies

```sh
sudo dnf install -y \
    python3 python3-gobject python3-dbus \
    gtk4 libadwaita gtk4-layer-shell \
    wl-clipboard xdg-desktop-portal-kde
# optional: OCR + region capture
sudo dnf install -y tesseract
```

`xdg-desktop-portal-kde` is already installed on Fedora KDE; it's listed so
a minimal install doesn't miss it.

### Paste, Cut, Select all, Backspace

These press keys in the app you selected text in. On KDE Wayland vinoPop
does that through the desktop's **Remote Control** portal
(`org.freedesktop.portal.RemoteDesktop`):

- The first time you use one, KDE asks *"vinoPop wants to control your
  keyboard"*. Tick **Allow restoring on future sessions** (Plasma 6.6.6 or
  newer; older versions ask every time) and allow it. vinoPop keeps the
  restore token in `~/.local/state/vinopop/remote-desktop.token`; delete
  that file to be asked again.
- While vinoPop holds the session, Plasma shows a *Remote Control* icon in
  the system tray. vinoPop closes the session after 5 minutes without a
  keypress, so the icon goes away; the next Paste reopens it with a short
  "Remote Control Started" notification and no dialog.
- If you decline, Paste/Cut fall back to ydotool (below), or just leave the
  result on the clipboard with a notification. Restart vinoPop to be asked
  again.
- The portal sends key *symbols*, and KWin finds them in your current
  layout, so Ctrl+A is Select all on AZERTY and Dvorak too.

**Fallback: ydotool.** If you'd rather not use the portal, or it isn't
available:

```sh
sudo dnf install -y ydotool
sudo usermod -aG input "$USER"   # log out and back in
rpm -ql ydotool | grep '\.service$'   # which unit your package ships
```

Then start `ydotoold` with that unit (upstream ships a user unit,
`systemctl --user enable --now ydotoold`) or run it yourself. The daemon
needs write access to `/dev/uinput`.

vinoPop finds the daemon's socket at `$YDOTOOL_SOCKET`,
`$XDG_RUNTIME_DIR/.ydotool_socket` or `/tmp/.ydotool_socket`. If `ydotool`
is installed but `ydotoold` isn't running, vinoPop treats ydotool as
unavailable instead of pretending the paste worked. ydotool sends physical
keycodes; vinoPop maps them through the first layout in KDE's keyboard
settings, so a second layout you switch to at runtime isn't covered.

`wtype` does not work on KWin: KWin doesn't implement the
`zwp_virtual_keyboard_v1` protocol wtype uses. vinoPop only tries it on
other compositors (Sway, Hyprland).

Check what vinoPop found with `vinopop --doctor` (the
`key injection` row).

### Modifier+double-click

Turn it on in Settings. It reads the keyboard and mouse from `/dev/input`,
which needs your user in the `input` group:

```sh
sudo usermod -aG input "$USER"   # then log out and back in
```

Every keyboard and mouse counts, including Bluetooth ones plugged in after
vinoPop started and the virtual keyboard of remappers like
[keyd](https://github.com/rvaiya/keyd) (which grabs the physical one).
ydotool's own virtual device is ignored. The doctor's `input devices` row
shows how many vinoPop can read.

## Run

```sh
git clone https://github.com/karanshukla/vinoPop.git ~/vinopop
cd ~/vinopop
./vinopop                  # auto-detects the Wayland/KDE backend
```

Logs: `~/.cache/vinopop/vinopop.log`.

## Known rough edges on Wayland/KDE

- **Global hotkey** registers via KGlobalAccel; confirm the binding appears in
  *System Settings → Shortcuts*. If a press does nothing, file an issue - the
  Qt-keysequence encoding may need tuning for your layout.
- **Esc-to-dismiss / click-outside** are softer than on X11. The popup
  auto-hides on a timer (and when the pointer leaves), but does not grab the
  keyboard (so it never steals focus from the app you're pasting into).
- **Active-window blocklist** is disabled on Wayland (no portable way to read
  the focused window's class yet).
- First popup after a selection has ~100-150 ms extra latency (one cursor-
  position round-trip to KWin). A resident helper to remove this is planned.

Please report other issues in the [GitHub issue tracker](https://github.com/karanshukla/vinoPop/issues).
