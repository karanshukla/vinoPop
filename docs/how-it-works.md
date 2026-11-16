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

## The KDE Wayland backend

`platform_backend/wayland_kde.py`, first proven on Fedora 44 KDE Plasma (KWin 6.6.5).

- **Selection.** `wl-paste --primary --watch echo` writes a newline on every change of the primary selection. One drag sends a burst of them as the selection grows, so the watcher waits until they have been quiet for the debounce (200 ms at least) and then reads the selection once. Reading on every ping made the popup chase the mouse for a second or two. The first settled value is whatever was selected before the watcher started and shows nothing. `wl-paste` exits when the compositor restarts, so it is restarted with a backoff of up to 30 s, and it is told to die with the daemon: before that, every restart left one behind, each pulling every new selection out of the source app.
- **Where the popup goes.** KWin has no API for the selection's rectangle, so the anchor is the pointer. It is read at each ping of the burst and not again after the quiet: the last ping is the drag's release at the end of the text, and by the time the burst has settled the mouse is already moving toward the popup.
- **Pointer and active window.** `workspace.cursorPos` and the active window come from KWin scripts, asked through one long-lived `_kwin_query` process. A process per query cost about 61 ms, 55 of it Python starting. The selection watcher, the double-click thread and the hotkey often ask for the pointer at the same moment, so callers that arrive during a query share its answer and a good answer is reused for 100 ms. A query that gets no answer returns (0, 0), which puts the popup in the top-left corner, and the helper is replaced, since a late answer would be read as the reply to the next question. The active-window lookup starts when a selection starts, so it overlaps the settle wait.
- **Modifier + double-click.** XRecord only sees XWayland windows on KWin, so buttons and modifier keys are read from `/dev/input` through the shared evdev hub, which covers every toolkit, keyd's virtual keyboard and hotplugged devices. It follows the X11 watcher: 300 ms, 8 px, and the popup 20 ms after the click so the app sees the click first. evdev buttons carry no coordinates, so the pointer is asked from KWin, and only while the modifier is held: a plain click costs no round-trip. The 300 ms are measured on the events' kernel timestamps, so the pointer query (about 100 ms) doesn't count against them. Modifiers go through libxkbcommon with the user's layout, so a swapped Ctrl and Alt is respected; without the library the raw keycodes are used.
- **The popup** is a layer-shell surface on the overlay layer with keyboard mode `NONE`. With `ON_DEMAND` it takes focus when mapped, loses it straight away, and the popup's focus-loss handler hides it before it is seen. Esc therefore doesn't dismiss it on Wayland. Layer-shell margins are relative to the surface's own output, so the surface is pinned to the output under the point and that output's origin subtracted; otherwise a popup on a monitor at x=1920 lands 1920 px too far right.
- **Opening a URL.** Focus-stealing prevention leaves the browser behind the current window when it is launched from an unfocused popup, so half a second later a KWin script raises the default browser's window.
- **The hotkey** goes through KGlobalAccel (`kde_hotkey.py`). If registering fails the app runs on without it.

`tests/test_wayland_watcher.py` and `tests/test_placement.py` pin the burst, the anchor, the pointer cache, the double-click window and the margins. None of it has a compositor under it in the tests.

## Telling a command from prose

`core/classifier.py` decides whether a selection gets Run in terminal. Every non-blank line votes: command, prose, or neither (comments, `fi` and `done`, backslash continuations, heredoc bodies, bare assignments). The selection is a command when at least one line is and commands aren't outnumbered by prose, so a comment above a command doesn't hide it and one line starting "at the end" doesn't turn a paragraph into a script. A shebang or a `$ ` prompt line settles it.

- **Ambiguous first words.** `make`, `file`, `python`, `go` and the like are also everyday words. They count only with evidence in the same segment: a flag, a path, `=`, a glob, a redirect, a known subcommand or a known command after them. Subcommands are kept to verbs prose rarely puts there, so there is no `go run` ("go run errands").
- **An unknown tool** counts only as `tool --flag`. An unknown word before a path is as often prose ("in /etc you'll find").
- **A single pipe** counts only when its left side is a command. As a whole-text hint it flagged every title with a separator ("Channel Name | Category") and hid the AI buttons. Flags, paths and redirects are per-segment evidence for the same reason: searched for anywhere, they flagged "Settings > Privacy" and "Set `foo` to true".
- **`&&`, `||`, `$(`, `${` and a heredoc** are enough anywhere in a selection under 4,000 characters (500 at first, raised so pasted scripts classify). Prose almost never has them, and a Run button on technical writing that quotes shell is fine. The length is checked first so no regex runs over a huge selection.
- **Hyphenated tools** (`xdg-settings`, `systemd-run`) that are normally run with a bare argument are listed by name. A hyphenated first word alone needs a flag or a path.
- **The regexes are linear.** The old email pattern, `[^@\s]+\.[^@\s]+`, could split `b.b.b...` in more than one way and took 0.6 s on 8k characters and 7 s on 32k (minified CSS with one `@`), on the GTK thread. The old hyphenated-name hint, `[a-z]+(?:-[a-z][\w-]*)+`, was exponential on `x-a-a-a-...!`.

`tests/test_classifier.py` holds the commands and the prose that must stay on their side, and the timing limits.

## Recipe commands

A recipe's `run_command` template is never rendered into shell source with the user's text in it (`plugin_system/recipe_loader.py`). The values go in as arguments, a prelude copies them into variables so a function or `set --` in the template can't shadow them, and each placeholder becomes a reference to one:

```
notify-send {text}  ->  bash -c 'vinopop_1=$1; notify-send "${vinopop_1}"' vinopop "<selection>"
```

bash expands the reference once and never parses the result as code, so no quoting in the template, missing or wrong, lets the selection, the clipboard or an `{ask:}` answer run as a command. `{text_shell}` maps to the same reference, for recipes written when quoting was the author's job; in a notification or on the clipboard it is still the `shlex`-quoted text.

- **Where a reference is refused.** Bash evaluates arithmetic operands as expressions, and an expression like `a[$(cmd)]` runs `cmd`. That happens in `$(( ))`, `(( ))`, `[[ -eq ]]`, `let`, `declare -i` and array subscripts, quoted or not, so a placeholder there is an error and the button explains itself when clicked. A quoted heredoc expands nothing, so a placeholder there is refused too.
- **Snippet placeholders** (`{date:+7d}`, `{clipboard}`, `{ask:Label}`) are expanded one token at a time. The selected text never goes through the snippet engine: a selection holding `{shell:...}` must not run, and one that merely contains `{date}` stays as it is.
- **A chain** runs its steps in order. A transform renders its template against the current value and passes the result on as `{text}`. The first other step (copy, paste, notify, open_url, run_command) uses the value and ends the chain; a chain of only transforms copies its result.
- **Copying** goes through the backend (`wl-copy` on KDE Wayland, `xclip` elsewhere). Plain `xclip` only reaches the X11 clipboard and misses native Wayland apps.
- **A misspelt content type** is an error. It used to be dropped, and a list of only unrecognised names then meant "every content type".
- **Default recipes** are copied into an empty recipes folder once. A marker file keeps a default the user deleted from coming back.

`tests/test_recipes.py` runs the hostile templates and selections through a real bash; `tests/test_recipe_rules.py` pins the chain and the seeding.

## Editable detection

Cut, Paste and the other editing buttons are hidden when the selection is in something read-only (`triggers/editable_detect.py`). The answers are tried in this order:

1. **The last focus event**, for 30 s. With `editable_atspi_listener_enabled` on, an AT-SPI listener records whether each widget that gains focus is editable. It is the only per-widget signal Chromium and Electron apps give: their accessibility tree shows the top-level frame without a focused descendant, but the event still fires when the user clicks an input and not the page. After 30 s the user may have switched apps without an event, so the answer is no longer trusted.
2. **A walk of the accessibility tree**, with the same setting, bounded to 150 ms and ten levels so it can't hold up the popup. It looks only inside the window marked active, then for the focused widget in it. The first version took the first focused widget in any app, and on Cinnamon the shell's own desktop window is permanently focused, so every app read as "cinnamon, not editable". An active window with no focused widget (Electron again) gives no answer: the frame itself isn't editable even when the input inside it is.
3. **The window class**, against a list of viewers (PDF and image viewers, media players, file managers, ebook readers) plus the user's own. Only the class is matched, never the title: "eog" would match a window titled "Geography notes". The class comes from the backend, `xprop` on X11 and KWin on KDE Wayland, so native Wayland windows are covered.
4. **Editable**, when nothing answered. A button that does nothing is better than a missing one the user wanted.

The same walk gives the popup the selected text's rectangle to anchor to. Kate and KWrite report text extents with zero height, so a zero height becomes 24 px; a zero width, or coordinates no screen has, means no anchor and the pointer is used.

AT-SPI is not loaded at all unless the user's own bus socket exists and no `at-spi-bus-launcher` is running as another user. One left behind by a `sudo` session can take the query, and the process then dies in glib (`dbind-ERROR`, a SIGTRAP Python can't catch). The setting is off by default for the reason given under [the defaults](#why-the-defaults-are-what-they-are).

`tests/test_editable_detect.py` runs the order and the walk against a fake desktop. Nothing here has run against a real accessibility bus in the tests.

## Keystrokes on X11

Paste, Cut, typing and window activation go through python-xlib and the X server's XTEST extension, in place of the `xdotool` and `xprop` processes vinoPop used to spawn. Like `xdotool`, the events reach X11 clients only (XWayland ones on a Wayland desktop).

- **Held modifiers are lifted first** (what `xdotool key --clearmodifiers` did), the chord is pressed, and the held ones are put back, so a hotkey's Super or Shift doesn't turn Ctrl+V into something else.
- **A keysym the layout lacks** (an accented letter on a US layout, an emoji) is bound to a spare keycode for the duration and given back afterwards.
- **Each missing keysym gets its own spare keycode**, unlike `xdotool`. Rebinding one keycode per character raced the client, which under load read a character against the next one's binding: 3 of 6 runs came out mangled on Plasma XWayland with all cores busy (2026-10-20).

`tests/test_x11_keys.py` pins all three.

## Hotkeys on X11

Each hotkey has its own thread and X connection (`triggers/hotkey.py`).

- **The key is grabbed four times**, once per combination of Caps Lock and Num Lock, because X treats those as modifiers and a grab matches the modifier state exactly. A refused variant is logged; only when all four are refused is the user told the combination is taken.
- **The thread sleeps in `select()` for up to a second**, which is how long stopping it can take. It was 0.2 s, which woke every hotkey thread five times a second for nothing.
- **A `MappingNotify` re-resolves the key.** The grab is on a keycode, and the keysym can move to another one when the layout changes; the grab then silently stops matching until it is made again on the new keycode. IBus and kbdd, which swap layouts per application, send these in floods, and that was the cause of "the first press of the hotkey is dropped" on Norwegian setups.
- **A held key fires once.** Autorepeat arrives as a release and a press with the same server time. With the toggle (#44) a repeat would close the popup the first press opened.
- **`hotkey_use_polling`** skips the grab: the keymap is sampled every 50 ms and the hotkey fires on the key going down with exactly its modifiers held (`query_pointer().mask` is the only way X gives the current modifier state). Forty failed samples in a row, about 2 s, mean the connection is gone and the thread stops.

`tests/test_hotkey_listen.py` runs the thread's loop against a fake display and pins the grab, the conflict notice, the remap and the repeat.

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

## The Run in terminal dialog

`core/actions.py` shows the command before it runs: Cancel, Edit and Run over a read-only preview. GTK 4 has no blocking `dialog.run()`, so it is an `Adw.Window` and the launch happens when it is answered. `Adw.Window` draws no title bar, so a header bar keeps it movable and closable.

- The command sits in a monospace text view, not a label: a label reads `&`, `<` and `>` as Pango markup.
- Read-only it has no frame and looks like a label (`.lp-cmd-preview`). Edit gives it the input-field look (`.lp-cmd-edit`), selects everything and disables the button.
- A one-line command opens with Run focused, so Enter runs it as shown. More than one line opens with Cancel focused and plain Enter does nothing: the user has to read every line and click Run or press Ctrl+Enter. While editing, plain Enter is a newline.
- Ctrl+Enter is caught on the text view in the capture phase. Keys go to the focused widget first, and the text view's own handler would take Enter as a newline before the window saw it.
- Closing the window from the title bar is Cancel, and only the first answer counts.

`tests/test_terminal_confirm_dialog.py` builds the dialog and pins each of these.

## Threads and X11

Notes on two things the daemon deliberately does not do.

- **No `XInitThreads`.** Several threads touch X (selection watcher, hotkeys, the popup tick, the clipboard watcher), and calling `XInitThreads` at import was tried. With python-xlib and PyGObject in one process the hotkey threads then silently fail to grab keys. python-xlib has its own socket and lock per `Display`, so it doesn't need it: GTK's own lock plus a separate `Display` per thread is the isolation vinoPop relies on. If `XIO: fatal IO error` crashes show up under load, give each Xlib user its own connection (mostly the case already) instead of turning `XInitThreads` back on.
- **`SIGCHLD` is not ignored.** `SIG_IGN` was a cheap way to reap subprocess zombies, and it was reverted: on Python 3.12, with the background watcher, many short-lived `xclip` calls and the GTK main loop, `subprocess.run()` intermittently froze the UI when the clipboard picker opened. A few zombie PIDs over the daemon's uptime are the lesser evil until there is a periodic reaper.

## Demos

The animations in the README are drawn, not recorded. `python3 docs/media/demos.py` regenerates `popup.svg` and `clipboard.svg`.
