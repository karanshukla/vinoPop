# How it works

The detail that used to live in the README: which backend runs on which desktop, the offline AI setup, and every setting.

## Backends

vinoPop picks a backend at startup:

- **XWayland** (GNOME, sway, Hyprland and every other Wayland desktop except KDE Plasma): the whole process runs on XWayland. Selection and clipboard through `xclip` and the compositor's X selection bridge, keystrokes through the X server's XTEST extension, cursor position, popup placement and the active window through Xlib. `wl-clipboard` is deliberately not used here: GNOME's mutter has no data-control protocol, so `wl-copy` steals focus or fails and `wl-paste --primary` can hang. The clipboard picker types short text over XTEST too, binding a spare keycode for characters the layout lacks, so `xdotool` isn't needed. The native backend needs KWin's scripting API for the cursor position, which is why only KDE has one.
- **KDE Plasma 6 / Wayland**: selection through `wl-clipboard`, popup placement through `gtk4-layer-shell` (XWayland when it isn't installed), cursor position through KWin's scripting API.
- **Plain Xorg** (Cinnamon, XFCE, MATE): the XWayland backend's code on a real X server. Best-effort.

`VINOPOP_BACKEND=x11` (the XWayland backend; the name predates it) or `=wayland_kde` overrides the choice.

On KDE Wayland the global hotkey runs through KGlobalAccel (check it actually bound under System Settings, Shortcuts), Paste/Cut press keys through the desktop's Remote Control portal (KDE asks once; `ydotool` + `ydotoold` is the fallback), and modifier+double-click needs your user in the `input` group. Popup placement, hover persistence, click-outside dismissal and the app blocklist all work natively. You can anchor the popup to the selected text instead of the mouse if you turn on desktop accessibility (AT-SPI). There's more in [FEDORA-KDE.md](FEDORA-KDE.md).

## Offline AI

OCR, summarize, rewrite, explain, translate and read aloud run on your own machine through OpenVINO: the LLM and the voice (vinoSpeak's Kokoro-82M, cached under `~/.cache/vinospeak/`) on an Intel NPU when there is one (the iGPU or CPU otherwise), and OCR on the CPU. Install the Python side for the same Python that runs vinoPop:

```sh
pip install --user -r requirements-ai.txt
```

On Debian/Ubuntu, whose system Python refuses `pip --user`, add `--break-system-packages` or run vinoPop from a venv created with `--system-site-packages` so it still sees PyGObject.

The model downloads once: Gemma 4 E2B, about 4 GB, fast and light on RAM. Set `"ai_llm_model": "gemma-4-e4b"` for the more accurate 5 GB one. Onboarding asks whether to get it now, in the background; otherwise the first AI action downloads it. Settings → Advanced → *Download the AI model in the background* changes that answer later. After that nothing goes online. `"ai_device"` forces `"NPU"`, `"GPU"` or `"CPU"`.

Before loading on the NPU, vinoPop probes it in a short-lived subprocess. A stuck NPU (it can happen after long, heavy runs) makes the probe crash instead of vinoPop, so the model loads on the GPU and the notification gives you the `modprobe` command that resets the driver.

`vinopop --doctor` (add `--json` for a bug report) checks the AI packages, which devices OpenVINO sees and the order each model will try them in, and whether the model is downloaded, checking every file against its pinned sha256 (slow on a 4 GB model, so only the doctor and a fresh download do it). With [ovfetch](https://github.com/karanshukla/ovfetch) installed it also reports whether the pinned OpenVINO is inside the range Intel records for your NPU driver. When OpenVINO doesn't list the NPU, or lists it without a compiler to use it, it says why: no NPU on the PCI bus, no `/dev/accel` node or no access to it, `intel_vpu` not loaded, no level-zero NPU driver, or no NPU compiler (Fedora's `intel-npu-driver` 1.32 rpm ships without one). It also shows the OpenVINO telemetry opt-out and the key-injection tools for your desktop. Every warning comes with a fix; `--json` rows are `{group, name, status, detail, fix}`, the same shape as vinoOpenEffects'. It's diagnosis only: the NPU is still tried, and falls back to GPU/CPU if it won't compile.

## Settings

Everything the Settings window sets lives in `~/.config/vinopop/settings.json`.

| Key | Default | Description |
|---|---|---|
| `hotkey` | `super+shift+y` | Combo to summon popup. Record via Settings dialog. |
| `hotkey_source` | `primary` | `primary` (highlighted) or `clipboard` |
| `clipboard_hotkey` | `super+shift+v` | Opens clipboard history and snippets. Empty disables. |
| `ocr_hotkey` | `shift+super+o` | Capture a screen region and OCR it. Empty disables. |
| `clipboard_history_enabled` | `true` | Remember what you copy |
| `snippet_triggers_enabled` | `false` | Expand a snippet when you type its trigger |
| `snippet_shell_enabled` | `false` | Let snippets run `{shell:CMD}`. Off for a reason: an imported snippet could run anything. |
| `show_on_selection` | `true` | Auto-popup when you select text |
| `auto_hide_initial_ms` | `6500` | Hide if mouse never reaches popup |
| `auto_hide_leave_ms` | `4000` | Hide this long after mouse leaves the safe zone |
| `min_selection_length_enabled` | `false` | Enable the minimum-selection-length filter |
| `min_selection_length` | `2` | When enabled, ignore selections shorter than this |
| `terminal_keep_open` | `true` | Keep terminal open after running a command |
| `terminal_confirm_run` | `true` | Show the exact command before Run in terminal launches it |
| `search_engine` | `google` | `duckduckgo`, `bing`, `brave`, `startpage`, `ecosia`, `kagi`, `qwant`, `yandex`, or `custom` |
| `search_engine_custom_url` | `""` | For `custom`: a URL with `{q}` in it |
| `double_click_popup_enabled` | `false` | Hold a modifier + double-click a word to pop the bar |
| `double_click_modifier` | `ctrl` | Modifier for the double-click trigger (`ctrl`/`alt`/`super`/`shift`) |
| `translate_target_lang` | `en` | Target language for the Translate plugin (ISO code) |
| `ai_device` | `auto` | `NPU`, `GPU` or `CPU` to force one |
| `ai_llm_model` | `gemma-4-e2b` | or `gemma-4-e4b` |
| `ai_idle_unload_minutes` | `5` | Unload the model (and the Read aloud voice) after this long idle. `0` keeps it loaded. |
| `editable_atspi_listener_enabled` | `false` | Use AT-SPI for editable detection + selection geometry |
| `popup_anchor_to_selection` | `true` | Anchor popup to the selection rect (needs AT-SPI enabled) |
| `theme` | `dark` | `dark`, `light`, or `system` (follows the GTK theme) |
| `popup_button_size` | `22` | Button size in px, 14 to 48. Icons scale with it. |
| `max_popup_buttons` | `24` | Cap on buttons the popup draws; the rest are dropped |
| `popup_overflow_mode` | `expand` | When one row is full: `expand` (chevron reveals the rest), `wrap` (second row) or `cap` (a `+N` chip) |
| `popup_force_all_modifier` | `""` | Hold this key (`alt`/`ctrl`/`shift`/`super`) while selecting to show every plugin regardless of content type. Empty disables. |
| `selection_debounce_ms` | `150` | Quiet time after the last selection change before the popup shows |
| `ignore_whitespace_only` | `true` | Don't pop up for whitespace-only selections |
| `blocklist_patterns` | `[]` | Window title / WM_CLASS substrings (case-insensitive) where the popup never shows: password managers, banking sites |
| `readonly_app_classes` | `[]` | Extra WM_CLASS substrings treated as read-only, hiding Cut/Paste/Backspace |
| `hotkey_use_polling` | `true` | Poll the keyboard every 50 ms instead of XGrabKey; dodges WM grab conflicts (Cinnamon eats the first press otherwise) |
| `clipboard_history_size` | `25` | Entries kept |
| `clipboard_history_images` | `true` | Keep image copies too |
| `clipboard_history_max_text_kb` | `1024` | Text copies above this aren't kept |
| `clipboard_history_max_image_mb` | `20` | Image copies above this aren't kept |
| `clipboard_poll_interval` | `0.5` | Seconds between polls when XFixes selection events aren't available |
| `snippet_variables` | `{}` | `{name: value}` pairs snippets use via `{var:NAME}` |
| `trigger_blocklist_patterns` | `[]` | Like `blocklist_patterns`, for snippet trigger expansion only |
| `plugin_order` | `[]` | Plugin names shown first, in this order (Plugin Manager > Order) |
| `disabled_plugins` | `[]` | Plugin names switched off inside a bundled file, e.g. hide `paste-and-enter` but keep `paste` |
| `ai_prefetch_model` | `false` | Download the ~4 GB LLM at daemon start instead of on the first AI click |
| `ai_prefetch_asked` | `false` | Set once onboarding or Settings asked about the prefetch; a stored `ai_prefetch_model: true` without it isn't consent |
| `ai_load_timeout_seconds` | `120` | How long a device may take to load the LLM before the next one (NPU, then GPU, then CPU) is tried |
| `ocr_describe_images` | `false` | After an OCR capture, offer (or run, if no text was found) a Gemma description of the image |
| `show_welcome_dialog` | `true` | First-run welcome dialog. `false` skips it (screencasts, tests). |
| `icon_style` | `color` | Popup buttons for branded and converter actions: `color` (vino tiles) or `glyph` (mono glyphs like the system edit icons) |
| `popup_text_labels` | `false` | Popup buttons show each action's name as plain text instead of its icon |
| `tray_icon_style` | `color` | Tray icon: `color` (recoloured by the panel), `light` or `dark` (fixed, for panels that don't recolour it) |
| `tray_idle_minutes` | `0` | Minutes without a popup before the tray icon hides; the next popup brings it back. `0` keeps it shown. |

### Why the defaults are what they are

Only keys you set are written to the file, so a default changed in a later release reaches everyone who never touched it.

**Privacy switches are off until you turn them on.**

- `double_click_popup_enabled` watches every mouse click (the X11 RECORD extension, or `/dev/input` on KDE Wayland) to spot the chord. Nothing is logged or sent. The modifier is required so the gesture doesn't collide with an app's own double-click-to-select. `double_click_modifier` is read on each click, so changing it needs no restart.
- `snippet_triggers_enabled` watches keystrokes the same way. They are matched against your snippet triggers locally and never logged or sent. `trigger_blocklist_patterns` silences expansion in password managers, terminals or your bank's site without turning triggers off everywhere.
- `snippet_shell_enabled` lets `{shell:CMD}` run bash (5 s timeout) and paste its output. Treat it like enabling macros in a document: an imported snippet with a hostile `{shell:...}` would run on your machine.
- `clipboard_history_enabled: false` means the selection watcher thread never starts, the picker hotkey does nothing and the popup button is hidden.
- `ai_prefetch_model` downloads about 4 GB, so it waits for a yes from onboarding or Settings > Advanced. It used to default to `true` and older versions wrote every default into the file, which is why a stored `true` only counts once `ai_prefetch_asked` is set.

**Hotkeys.**

- The format is `<modifiers>+<key>`: `super+shift+y`, `ctrl+alt+y`, `super+space`. Empty or `null` disables one.
- `clipboard_hotkey` avoids `Meta+V`, which KDE's Klipper and other clipboard managers grab.
- `hotkey_use_polling` is on because Cinnamon's muffin defers Super-key dispatch to tell a tap from a hold and eats the first press of Shift+Super combos (linuxmint/cinnamon #549). Polling costs under 0.1 % CPU per hotkey in `top`. Turn it off for the event-driven `XGrabKey` path.
- `popup_force_all_modifier` is empty because every choice collides with something: Alt+drag moves windows on Cinnamon and GNOME, Super is often the window manager's own modifier, Ctrl+drag selects word by word or adds cursors in editors and browsers, and Shift extends a selection everywhere. Pick the least bad one in Settings > Activation. It is for when the classifier got the selection wrong and the button you want was filtered out.

**Timing.**

- `auto_hide_initial_ms`: 6.5 s is long enough to read the buttons; 8 s felt sluggish.
- `auto_hide_leave_ms`: 4 s forgives leaving and coming back; 1.5 s felt twitchy.
- `selection_debounce_ms`: each extension of a drag fires a selection event, so some wait is needed for the drag to settle. People rarely extend again within 150 ms, and 150 ms doesn't read as a delay.
- `min_selection_length`: 2 filters stray single-letter selections without blocking short words. It only applies with `min_selection_length_enabled`, and never to the hotkey.

**Popup.**

- `theme` is `dark` because the palette was designed against it. `system` follows the GTK theme name and its prefer-dark flag.
- `max_popup_buttons` bounds every `popup_overflow_mode`. 24 fits a typical 12 to 20 enabled plugins.
- `popup_text_labels` overrides `icon_style` in the popup.
- `popup_anchor_to_selection` does nothing unless `editable_atspi_listener_enabled` is on and the focused app exposes accessibility; otherwise the popup sits at the pointer.
- `readonly_app_classes` adds to a built-in list (Evince, Okular, image viewers, file managers) and is only consulted when AT-SPI gave no definite answer.

**Clipboard history.**

- `clipboard_history_size`: 25 reaches back about a day of work; more makes the picker harder to search.
- `clipboard_history_max_text_kb` and `clipboard_history_max_image_mb` exist because history is rewritten to disk on every copy, and a 20 MB log would be rewritten each time.
- `snippet_variables` are shared values (email, signature, phone) that snippets pull in with `{var:NAME}`.

**Offline AI.**

- `ai_device: auto` puts the LLM on the NPU and OCR on the CPU, with the other devices as fallback.
- `ai_load_timeout_seconds`: a slow first NPU compile keeps running in the background after the fallback, and the model moves to the NPU once it is done.
- `ai_idle_unload_minutes` frees about 4 GB (LLM) and 1 GB (OCR). The compile cache keeps the next load quick.
- `ocr_describe_images` adds a Describe image button to the OCR notification when text was found, and runs straight away when none was. About 10 s per image on the NPU.

**Other.**

- `terminal_confirm_run` protects against highlighting a hostile-looking string and clicking the wrong button. With `terminal_keep_open: false` the terminal closes as soon as the command exits and its output is lost.
- `search_engine_custom_url` must contain `{q}`, which is replaced with the URL-encoded selection, as in `https://searx.example.com/search?q={q}`.
- `tray_icon_style` is a manual choice because plasmashell won't recolour a custom symbolic icon reliably: `light` is for dark panels, `dark` for light ones.
- `editable_atspi_listener_enabled` tells "cursor in the chat input" from "cursor in the chat history" in Electron apps. It left the Settings window on 2026-05-29: modifier + double-click covers the main case, and AT-SPI was correlated with a Cinnamon panel segfault on 2026-05-25 (xapp-sn-watcher threw `ATK_IS_STATE_SET` assertions and cinnamon dereferenced a freed GObject). It stays as a hidden key.

## Keystrokes on X11

Paste, Cut, typing and window activation go through python-xlib and the X server's XTEST extension, in place of the `xdotool` and `xprop` processes vinoPop used to spawn. Like `xdotool`, the events reach X11 clients only (XWayland ones on a Wayland desktop).

- **Held modifiers are lifted first** (what `xdotool key --clearmodifiers` did), the chord is pressed, and the held ones are put back, so a hotkey's Super or Shift doesn't turn Ctrl+V into something else.
- **A keysym the layout lacks** (an accented letter on a US layout, an emoji) is bound to a spare keycode for the duration and given back afterwards.
- **Each missing keysym gets its own spare keycode**, unlike `xdotool`. Rebinding one keycode per character raced the client, which under load read a character against the next one's binding: 3 of 6 runs came out mangled on Plasma XWayland with all cores busy (2026-10-20).

`tests/test_x11_keys.py` pins all three.

## Popup placement on X11

GTK 4 has no `Gtk.Window.move()`, no POPUP window type and no type hints, so vinoPop does what GTK 3's POPUP windows did itself: it marks the X window override-redirect right after GTK creates it, before it is ever mapped, and moves it with `XMoveWindow`. The window manager then neither decorates, focuses, places nor lists it. An override-redirect window never gets focus from the window manager either, which is what GTK 3's `accept_focus=False` gave: paste keeps going to the app the text came from. The KDE Wayland backend uses the same code when the process runs on XWayland (no `gtk4-layer-shell`).

## The popup window

The popup is a plain undecorated `Gtk.Window` that the platform backend places, stacks and keeps unfocused. On X11 that is the override-redirect window above, re-raised on every move so it can't end up under an open Settings or Plugin Manager window (an old Cinnamon bug). On KDE Wayland it is a `gtk4-layer-shell` overlay surface with keyboard mode NONE. It never takes focus, so the paste or keystroke an action sends lands in the app the text came from.

- **Placement.** With a selection box from AT-SPI the popup is centred on it, 10 px above its top edge. Without one it is centred on the pointer and lifted 28 px to clear the line the pointer is on (about 24 px of text plus a gap). On Wayland the pointer is all there is and it sits inside or under the selection, so the lift is 64 px. Less than 4 px from the top of the monitor's usable area, the popup goes below the text instead. X11 root coordinates are physical pixels and are divided by the monitor's scale; Wayland's are logical already. `tests/test_popup_show.py` pins all of it.
- **Which buttons.** Plugins that handle the classified content type, in `plugin_order`, minus the editing ones when the focused widget is read-only, cut at `max_popup_buttons`. Holding `popup_force_all_modifier` skips the classifier and offers every plugin whose own `matches()` accepts the text. The key has to be held from before the selection ends until the popup draws, so it outlasts the debounce.
- **Dismissal.** On X11 `_tick` polls the pointer buttons and Esc over Xlib, since an unfocused window gets no key events. On Wayland the popup hides on GTK's leave, focus and key events.
- **Keyboard (#44).** When the hotkey opened the popup the hands are on the keyboard, so on X11 the navigation keys are grabbed on the root window while it is up: arrows, Tab, Home and End move a highlight, Enter and Space run the highlighted button, Esc closes. The same keys work if a window manager focuses the popup anyway. Wayland has no such grab, so the popup is mouse-only there for now. The AI bubble does the same for its chips (#124).

## Clipboard history and snippets

The plugin is one file, `plugins_repo/clipboard_history.py`, because the Plugin Manager installs and updates a plugin by copying one file into `~/.config/vinopop/plugins` and compares that file's version with the catalogue's. It holds the stores (`history.json`, `snippets.json`), the clipboard watcher, the trigger watchers, the placeholder engine and the picker window.

### Snippet trigger timing

Typing a trigger and a space deletes both and pastes the snippet. The waits between the steps each cover a race seen on a real desktop.

| Step | Wait | Why |
|---|---|---|
| Before the first synthetic key | 60 ms | The boundary key (space, tab, enter) is still physically down. Sending keys then left Ctrl logically stuck in the receiving app, which showed as scroll zooming. |
| Between BackSpaces (X11) | 16 ms | With no gap, Electron and Chrome drop events. A frame at 60 Hz is quick enough that the typed characters are never seen. |
| After the BackSpaces | 80 ms on X11, 30 ms with ydotool | The app settles before the clipboard is staged. |
| After the paste key, before Left arrows | 50 ms | The paste has to land before the caret moves to `{cursor}`. |
| Before releasing modifiers (X11) | 20 ms | Putting held modifiers back races the user's own key-up and sometimes left Ctrl or Shift down (Firefox zooming on scroll), so they are released outright. |
| Before the clipboard is put back | 300 ms | The app asks for the clipboard after the paste key arrives. Too short and it pastes the old clipboard. |

The BackSpaces go out without lifting modifiers first: BackSpace ignores them, and lifting them occasionally left Ctrl stuck.

On Wayland the expansion goes on the clipboard and on PRIMARY and is pasted with Shift+Insert. `ydotool type` maps through a US keycode table and drops characters like æ, ø and å, and ydotool's Ctrl chords don't register on KWin. The watcher ignores `/dev/input` for 1.2 s plus 20 ms per injected key, then for 250 ms after the last one, so it doesn't read its own keystrokes back.

`{ask:}` snippets don't expand from a trigger, since a dialog mid-keystroke is jarring. They still work from the picker.

### Pasting from the clipboard picker

The picker takes focus, so pasting has to give it back to the window it was opened over.

- 60 ms after the click, every modifier is released. The hotkey (Super+Shift+V, say) leaves them logically held until the user lets go, and a paste sent meanwhile lands as garbage or not at all.
- The target is activated twice, over Xlib and with `wmctrl -ia` when it is installed. On muffin the first can return before focus has moved.
- Then 180 ms, or 300 ms for a terminal. Cinnamon queues the activation for the next compositor frame and 100 ms wasn't enough. VTE commits its focus-in about 50 ms after activation.
- If the target still isn't the active window it is activated once more, with a 120 ms wait.
- A single line of up to 2,048 characters is typed over XTEST, not pasted. Electron editors (Slack, Discord, Notion and the like) silently drop a synthetic Ctrl+V, and a terminal's bracketed paste gets out of step with readline. Text with a newline is pasted, because typed into a shell each line would run, and longer text is pasted because typing it is slow. If typing fails, the paste key is sent.

`tests/test_clipboard_paste.py` and `tests/test_trigger_expansion.py` pin the order of these steps with X11 and the clipboard faked. The waits themselves have only ever been tuned by hand on a desktop.

### The picker window

- It is an `Adw.Window` so its prompts open as sheets inside it. It stays resizable even when undecorated, because a non-resizable `AdwWindow` floats its alert dialogs as separate windows.
- On X11 it has no decoration and opens at the pointer; the title row is the drag handle. On KDE Wayland a client can't place its own toplevel, so the picker keeps its decoration and the compositor places it.
- It closes when it loses focus, but only if focus is still elsewhere 120 ms later: IBus and Cinnamon take focus for a frame and hand it back. A prompt or a drag in progress suspends this.

## Threads and X11

Notes on two things the daemon deliberately does not do.

- **No `XInitThreads`.** Several threads touch X (selection watcher, hotkeys, the popup tick, the clipboard watcher), and calling `XInitThreads` at import was tried. With python-xlib and PyGObject in one process the hotkey threads then silently fail to grab keys. python-xlib has its own socket and lock per `Display`, so it doesn't need it: GTK's own lock plus a separate `Display` per thread is the isolation vinoPop relies on. If `XIO: fatal IO error` crashes show up under load, give each Xlib user its own connection (mostly the case already) instead of turning `XInitThreads` back on.
- **`SIGCHLD` is not ignored.** `SIG_IGN` was a cheap way to reap subprocess zombies, and it was reverted: on Python 3.12, with the background watcher, many short-lived `xclip` calls and the GTK main loop, `subprocess.run()` intermittently froze the UI when the clipboard picker opened. A few zombie PIDs over the daemon's uptime are the lesser evil until there is a periodic reaper.

## Demos

The animations in the README are drawn, not recorded. `python3 docs/media/demos.py` regenerates `popup.svg` and `clipboard.svg`.
