# How it works

Architecture notes, desktop backend behavior, offline AI setup, and a reference for every setting in `settings.json`.

## Backends

vinoPop chooses a platform backend at startup based on your desktop session:

- **XWayland** (GNOME, sway, Hyprland, and other Wayland desktops except KDE Plasma): The entire process runs under XWayland.
  - Selection and clipboard access go through `xclip` and the compositor's X selection bridge. `wl-clipboard` is avoided here because GNOME's mutter does not implement the data-control protocol, causing `wl-copy` to steal focus or fail and `wl-paste --primary` to hang.
  - Keystrokes, typing, cursor position, popup placement, and active-window lookups use python-xlib and the X server's XTEST extension, so `xdotool` is not required. For characters missing from the current keyboard layout, the clipboard picker temporarily binds a spare keycode over XTEST.
  - Only KDE currently has a native Wayland backend because reading the global cursor position requires KWin's scripting API.
- **KDE Plasma 6 / Wayland:** Uses `wl-clipboard` for selections, `gtk4-layer-shell` for native popup placement (falling back to XWayland if unavailable), and KWin's scripting API for cursor position.
- **Plain Xorg** (Cinnamon, XFCE, MATE): Uses the same code path as the XWayland backend against a real X server (best-effort support).

Set `VINOPOP_BACKEND=x11` (the XWayland/X11 backend) or `VINOPOP_BACKEND=wayland_kde` to override auto-detection.

On KDE Plasma Wayland:
- Global hotkeys register through KGlobalAccel (you can verify the binding under **System Settings → Shortcuts**).
- Paste and Cut actions send keys through the desktop's Remote Control portal (KDE prompts once; `ydotool` + `ydotoold` serves as a fallback).
- Modifier + double-click requires your user account to be in the `input` group.
- Popup placement, hover persistence, click-outside dismissal, and the application blocklist work natively.
- Enabling desktop accessibility (AT-SPI) allows anchoring the popup to the selected text rather than the pointer.

See [FEDORA-KDE.md](FEDORA-KDE.md) for additional KDE Plasma details.

## Offline AI

OCR, summarize, rewrite, explain, translate, and read aloud run locally through OpenVINO. The LLM and speech synthesis (vinoSpeak's Kokoro-82M, cached under `~/.cache/vinospeak/`) run on an Intel NPU when available, falling back to the iGPU or CPU. OCR runs on the CPU.

Install the Python dependencies into the same Python environment that runs vinoPop:

```sh
pip install --user -r requirements-ai.txt
```

On Debian and Ubuntu, where the system Python restricts `pip --user`, add `--break-system-packages` or run vinoPop from a virtual environment created with `--system-site-packages` so it can still access PyGObject.

The default model is **Gemma 4 E2B** (~4 GB), which is fast and uses less RAM. Set `"ai_llm_model": "gemma-4-e4b"` in `settings.json` for the more accurate ~5 GB model. Onboarding asks whether to download the model in the background; otherwise, it downloads on your first AI action. You can change that preference later under **Settings → Advanced → Download the AI model in the background**. Once downloaded, all inference runs locally. Set `"ai_device"` to `"NPU"`, `"GPU"`, or `"CPU"` to force a specific device.

Before loading a model on the NPU, vinoPop runs a quick probe in a short-lived subprocess. If the NPU driver is wedged (which can happen after long, heavy workloads), only the probe crashes rather than vinoPop itself; vinoPop then falls back to the GPU and shows a notification with the `modprobe` command to reset the driver.

### Diagnostics (`--doctor`)

Run `vinopop --doctor` (or add `--json` for bug reports) to inspect the offline AI environment. It checks:
- Installed AI packages, visible OpenVINO devices, and the fallback order each model will use.
- Whether the model is downloaded, verifying every file against its pinned SHA-256 digest (because hashing a 4 GB model is slow, full verification only runs during `--doctor` and fresh downloads).
- If [ovfetch](https://github.com/karanshukla/ovfetch) is installed, whether the pinned OpenVINO version falls within the supported range Intel records for your NPU driver.
- Specific causes when OpenVINO omits the NPU or cannot compile for it: no NPU on the PCI bus, missing or inaccessible `/dev/accel` device node, unloaded `intel_vpu` kernel module, missing Level Zero NPU driver, or missing NPU compiler (for example, Fedora's `intel-npu-driver` 1.32 RPM ships without one).
- OpenVINO telemetry opt-out status and available key-injection tools for your desktop.

Every warning includes a suggested fix. With `--json`, each row uses `{group, name, status, detail, fix}` (matching vinoOpenEffects). `--doctor` is strictly diagnostic: vinoPop still attempts to use the NPU at runtime and falls back to GPU or CPU if compilation fails.

## Settings

Everything configured in the Settings window is stored in `~/.config/vinopop/settings.json`.

| Key | Default | Description |
|---|---|---|
| `hotkey` | `super+shift+y` | Shortcut to open or close the popup (can be recorded in Settings). |
| `hotkey_source` | `primary` | Text source for the hotkey: `primary` (highlighted selection) or `clipboard`. |
| `clipboard_hotkey` | `super+shift+v` | Opens clipboard history and snippets. Leave empty to disable. |
| `ocr_hotkey` | `shift+super+o` | Captures a screen region and runs OCR. Leave empty to disable. |
| `clipboard_history_enabled` | `true` | Save copied text and images to clipboard history. |
| `snippet_triggers_enabled` | `false` | Expand a snippet automatically when you type its trigger. |
| `snippet_shell_enabled` | `false` | Allow snippets to run `{shell:CMD}`. Disabled by default because an imported snippet could run arbitrary commands. |
| `show_on_selection` | `true` | Automatically show the popup when you select text. |
| `auto_hide_initial_ms` | `6500` | Milliseconds before hiding the popup if the pointer never enters it. |
| `auto_hide_leave_ms` | `4000` | Milliseconds before hiding the popup after the pointer leaves the safe zone. |
| `min_selection_length_enabled` | `false` | Enable the minimum-selection-length filter. |
| `min_selection_length` | `2` | Minimum character count required for auto-popup when the filter is enabled. |
| `terminal_keep_open` | `true` | Keep the terminal window open after a command finishes. |
| `terminal_confirm_run` | `true` | Show the exact command in a confirmation dialog before running it in a terminal. |
| `search_engine` | `google` | Search engine: `google`, `duckduckgo`, `bing`, `brave`, `startpage`, `ecosia`, `kagi`, `qwant`, `yandex`, or `custom`. |
| `search_engine_custom_url` | `""` | Custom search URL template containing `{q}` when `search_engine` is `custom`. |
| `double_click_popup_enabled` | `false` | Open the popup when holding a modifier key and double-clicking a word. |
| `double_click_modifier` | `ctrl` | Modifier for the double-click trigger (`ctrl`, `alt`, `super`, or `shift`). |
| `translate_target_lang` | `en` | Target language for the Translate plugin (ISO language code). |
| `ai_device` | `auto` | Inference device: `auto`, `NPU`, `GPU`, or `CPU`. |
| `ai_llm_model` | `gemma-4-e2b` | Local LLM choice: `gemma-4-e2b` or `gemma-4-e4b`. |
| `ai_idle_unload_minutes` | `5` | Unload the LLM and Read aloud voice after this many idle minutes (`0` keeps them loaded). |
| `editable_atspi_listener_enabled` | `false` | Use AT-SPI for editable-field detection and selection geometry. |
| `popup_anchor_to_selection` | `true` | Anchor the popup to the selection bounding box (requires AT-SPI enabled). |
| `theme` | `dark` | UI theme: `dark`, `light`, or `system` (follows the GTK theme). |
| `popup_button_size` | `22` | Popup button size in pixels (14 to 48); icons scale with it. |
| `max_popup_buttons` | `24` | Maximum number of buttons shown in the popup; excess buttons are omitted. |
| `popup_overflow_mode` | `expand` | Behavior when a row is full: `expand` (chevron reveals remaining buttons), `wrap` (second row), or `cap` (`+N` chip). |
| `popup_force_all_modifier` | `""` | Hold this modifier (`alt`, `ctrl`, `shift`, or `super`) while selecting to show all matching plugins regardless of content type. Empty disables. |
| `selection_debounce_ms` | `150` | Quiet period in milliseconds after the last selection change before showing the popup. |
| `ignore_whitespace_only` | `true` | Do not show the popup for whitespace-only selections. |
| `blocklist_patterns` | `[]` | Case-insensitive window title or `WM_CLASS` substrings where the popup never appears (such as password managers or banking sites). |
| `readonly_app_classes` | `[]` | Additional `WM_CLASS` substrings treated as read-only so Cut, Paste, and Backspace stay hidden. |
| `hotkey_use_polling` | `true` | Poll the keyboard every 50 ms instead of calling `XGrabKey` to avoid window-manager grab conflicts (such as Cinnamon swallowing the first press). |
| `clipboard_history_size` | `25` | Number of clipboard entries retained. |
| `clipboard_history_images` | `true` | Store copied images in clipboard history. |
| `clipboard_history_max_text_kb` | `1024` | Skip storing text entries larger than this limit (KB). |
| `clipboard_history_max_image_mb` | `20` | Skip storing image entries larger than this limit (MB). |
| `clipboard_poll_interval` | `0.5` | Polling interval in seconds when XFixes selection events are unavailable. |
| `snippet_variables` | `{}` | `{name: value}` map referenced in snippets via `{var:NAME}`. |
| `trigger_blocklist_patterns` | `[]` | Window blocklist patterns (same format as `blocklist_patterns`) applied only to snippet trigger expansion. |
| `plugin_order` | `[]` | Plugin names displayed first in this order (configured via Plugin Manager > Order). |
| `disabled_plugins` | `[]` | Individual plugin names disabled inside a bundled file (for example, hiding `paste-and-enter` while keeping `paste`). |
| `ai_prefetch_model` | `false` | Download the ~4 GB LLM at daemon startup instead of waiting for the first AI click. |
| `ai_prefetch_asked` | `false` | Tracks whether onboarding or Settings explicitly asked about prefetching; a stored `ai_prefetch_model: true` without this flag is ignored. |
| `ai_load_timeout_seconds` | `120` | Maximum seconds a device may take to load the LLM before falling back to the next device (NPU → GPU → CPU). |
| `ocr_describe_images` | `false` | After OCR capture, offer a Gemma image description (or run it automatically when no text is detected). |
| `show_welcome_dialog` | `true` | Show the first-run welcome dialog (`false` skips it for tests or screencasts). |
| `icon_style` | `color` | Icon style for branded and converter buttons: `color` (vino tiles) or `glyph` (monochrome icons matching system edit buttons). |
| `popup_text_labels` | `false` | Display action names as plain text instead of icons in the popup. |
| `tray_icon_style` | `color` | Tray icon style: `color` (recolored by the panel), `light`, or `dark` (fixed for panels that do not recolor symbolic icons). |
| `tray_idle_minutes` | `0` | Minutes without a popup before hiding the tray icon; the next popup restores it (`0` keeps it always visible). |

### Why the defaults are what they are

Only keys you explicitly change are saved to `settings.json`, so updated defaults in newer releases apply automatically to unchanged settings.

**Privacy switches are opt-in.**

- `double_click_popup_enabled` monitors mouse clicks (via the X11 RECORD extension, or `/dev/input` on KDE Wayland) to detect the modifier + double-click chord. Events are processed locally and never logged or transmitted. Requiring a modifier prevents conflicts with normal double-click word selection. `double_click_modifier` is checked on every click, so changing it takes effect immediately without restarting.
- `snippet_triggers_enabled` monitors keystrokes the same way, matching them locally against your snippet triggers without logging or network access. Use `trigger_blocklist_patterns` to disable expansion in password managers, terminals, or banking tabs without disabling triggers globally.
- `snippet_shell_enabled` allows `{shell:CMD}` to execute bash (with a 5 s timeout) and paste its output. Treat it like document macros: an untrusted imported snippet containing `{shell:...}` could execute arbitrary commands.
- Setting `clipboard_history_enabled: false` prevents the clipboard watcher thread from starting, disables the picker hotkey, and hides the popup button.
- `ai_prefetch_model` triggers a ~4 GB download, so it only runs after you opt in during onboarding or in Settings > Advanced. Earlier versions defaulted this to `true` and wrote all defaults to disk, which is why a stored `true` only takes effect when `ai_prefetch_asked` is also `true`.

**Hotkeys.**

- Format hotkeys as `<modifiers>+<key>` (for example `super+shift+y`, `ctrl+alt+y`, or `super+space`). Set a key to `""` or `null` to disable it.
- `clipboard_hotkey` defaults to `super+shift+v` instead of `Meta+V` to avoid conflicting with KDE's Klipper and other clipboard managers.
- `hotkey_use_polling` is enabled by default because Cinnamon's muffin defers Super-key dispatch to distinguish taps from holds and swallows the first press of `Shift+Super` shortcuts (linuxmint/cinnamon #549). Polling uses under 0.1% CPU per hotkey in `top`; disable it to use the event-driven `XGrabKey` path.
- `popup_force_all_modifier` is empty by default because every modifier collides with common desktop or editor shortcuts (`Alt+drag` moves windows on Cinnamon and GNOME, `Super` is often reserved by the window manager, `Ctrl+drag` adds cursors or selects words, and `Shift` extends selections). Choose whichever works best for your setup under Settings > Activation when you want to override content-type filtering.

**Timing.**

- `auto_hide_initial_ms`: 6.5 s gives enough time to scan the buttons without lingering unnecessarily (8 s was too slow in testing).
- `auto_hide_leave_ms`: 4 s leaves a comfortable margin if you briefly move the pointer off the bar and return to it, whereas 1.5 s dismissed too quickly.
- `selection_debounce_ms`: Extending a drag fires repeated selection events, so vinoPop waits for the drag to settle. 150 ms covers typical drag pauses without feeling noticeably delayed.
- `min_selection_length`: A threshold of 2 ignores accidental single-character highlights while still allowing short words. It only applies when `min_selection_length_enabled` is true and never restricts the manual hotkey.

**Popup.**

- `theme` defaults to `dark` because the color palette was designed for dark surfaces. `system` follows your GTK theme name and prefer-dark flag.
- `max_popup_buttons` caps all `popup_overflow_mode` options; 24 comfortably accommodates a typical set of 12 to 20 enabled plugins.
- `popup_text_labels` overrides `icon_style` inside the popup bar.
- `popup_anchor_to_selection` requires `editable_atspi_listener_enabled` and an active app that exposes AT-SPI geometry; otherwise the popup anchors to the pointer.
- `readonly_app_classes` extends a built-in viewer list (Evince, Okular, image viewers, file managers) and is consulted only when AT-SPI does not provide a definite answer.

**Clipboard history.**

- `clipboard_history_size`: 25 entries typically covers a day of work while keeping the picker quick to browse.
- `clipboard_history_max_text_kb` and `clipboard_history_max_image_mb` cap entry sizes because history is rewritten to disk on every copy, avoiding repeated writes of multi-megabyte payloads.
- `snippet_variables` store reusable values (such as email, signature, or phone number) inserted into snippets via `{var:NAME}`.

**Offline AI.**

- `ai_device: auto` assigns the LLM to the NPU and OCR to the CPU, falling back to other devices if unavailable.
- `ai_load_timeout_seconds`: If a first-time NPU compilation exceeds the timeout and falls back, compilation continues in the background and the model switches to the NPU once ready.
- `ai_idle_unload_minutes` frees roughly 4 GB (LLM) and 1 GB (OCR) of memory when idle; the compile cache keeps subsequent loads fast.
- `ocr_describe_images` adds a "Describe image" button to the OCR notification when text is detected, and runs automatically when no text is found (~10 s per image on the NPU).

**Other.**

- `terminal_confirm_run` prevents accidentally executing a highlighted command if you click the wrong button. Setting `terminal_keep_open: false` closes the terminal immediately when the command exits, discarding its output.
- `search_engine_custom_url` must contain `{q}`, which is replaced with the URL-encoded selection (for example, `https://searx.example.com/search?q={q}`).
- `tray_icon_style` is configurable because plasmashell does not reliably recolor custom symbolic icons: choose `light` for dark panels and `dark` for light panels.
- `editable_atspi_listener_enabled` distinguishes an Electron chat input box from its read-only message history. It was removed from the Settings UI on 2026-05-29 because modifier + double-click covers the primary use case and AT-SPI was correlated with a Cinnamon panel segfault on 2026-05-25 (`xapp-sn-watcher` emitted `ATK_IS_STATE_SET` assertions before cinnamon dereferenced a freed GObject). It remains available as a hidden `settings.json` key.

## The KDE Wayland backend

Implemented in `platform_backend/wayland_kde.py` and first verified on Fedora 44 KDE Plasma (KWin 6.6.5).

- **Selection watching:** `wl-paste --primary --watch echo` prints a newline on each primary-selection change. Because a single mouse drag emits a burst of events as the highlight grows, the watcher waits for a quiet period (at least 200 ms) before reading the selection once, preventing the popup from chasing the cursor. The initial value at startup is ignored so nothing pops up on launch. If the compositor restarts and `wl-paste` exits, vinoPop restarts it with exponential backoff up to 30 s, and configures the child process to exit when the daemon exits so stale watchers do not accumulate.
- **Anchor position:** KWin exposes no API for the selection rectangle, so vinoPop anchors the popup to the pointer. The pointer position is recorded on each event during the selection burst rather than after the debounce period: the final event corresponds to releasing the drag at the end of the text, whereas after the settle delay the user has often already started moving the mouse.
- **Pointer and active window queries:** `workspace.cursorPos` and active-window metadata come from KWin scripts executed through a single long-lived `_kwin_query` helper process. Spawning a fresh process per query took ~61 ms (~55 ms in Python startup alone). Because the selection watcher, double-click thread, and hotkey often query concurrently, simultaneous callers share one in-flight request and reuse valid results for 100 ms. On a failed query, vinoPop returns `(0, 0)` (placing the popup at the top-left corner) and replaces the helper process so a late reply cannot desynchronize the next query. Active-window lookup starts as soon as selection begins so it overlaps the settle wait.
- **Modifier + double-click:** Because XRecord only sees XWayland windows on KWin, keyboard and mouse events are read from `/dev/input` via a shared evdev hub. This covers all toolkits, `keyd` virtual keyboards, and hotplugged devices, matching X11 thresholds (300 ms window, 8 px movement tolerance, and a 20 ms delay after the click so the target app receives it first). Because evdev button events lack screen coordinates, KWin is queried for the pointer only while the configured modifier is held, so regular clicks incur no round-trip. The 300 ms double-click window is measured using kernel event timestamps so the ~100 ms pointer query does not eat into it. Modifiers are resolved through `libxkbcommon` using the active layout (respecting swapped Ctrl/Alt keys), falling back to raw keycodes if `libxkbcommon` is unavailable.
- **Popup surface:** Uses a `gtk4-layer-shell` surface on the overlay layer with keyboard interactivity set to `NONE`. Using `ON_DEMAND` caused the surface to take focus on map, immediately lose it, and trigger its focus-loss dismissal handler before appearing. Because the surface never takes keyboard focus, `Esc` cannot dismiss it on Wayland. Layer-shell margins are relative to the surface's assigned output, so vinoPop pins the surface to the monitor under the pointer and subtracts that output's origin (otherwise a popup on a monitor at `x=1920` would appear 1920 px too far right).
- **Opening URLs:** Because KWin's focus-stealing prevention leaves the browser behind the active window when launched from an unfocused popup, vinoPop runs a KWin script 0.5 s later to raise the default browser window.
- **Global hotkey:** Registered through KGlobalAccel (`kde_hotkey.py`). If registration fails, vinoPop continues running without it.
- **OCR hotkey:** Enforces one capture at a time. KWin forwards key autorepeat to KGlobalAccel, and the first capture takes a few seconds to import the OCR stack, which previously caused held keys to stack multiple overlays. Capture runs on a worker thread so the GTK main loop stays responsive while dragging the region.

`tests/test_wayland_watcher.py` and `tests/test_placement.py` cover the event burst, anchor behavior, pointer cache, double-click timing, and layer-shell margins (without a live compositor).

## Telling a command from prose

`core/classifier.py` decides whether a selection should offer **Run in terminal**. Each non-blank line votes as command, prose, or neutral (comments, `fi` and `done`, backslash continuations, heredoc bodies, and bare assignments). A selection classifies as a command when at least one line votes command and command lines are not outnumbered by prose lines. This ensures a comment above a command does not hide it, while a prose paragraph containing one line starting with "at the end" is not mistaken for a script. A shebang (`#!`) or a `$ ` prompt line settles classification immediately.

- **Ambiguous first words:** Words such as `make`, `file`, `python`, and `go` also appear in everyday prose. They only count as commands when accompanied by supporting evidence in the same segment: a flag, a path, `=`, a glob, a redirect, a known subcommand, or a following known command. Allowed subcommands exclude phrases common in prose (for example, `go run` is excluded because of phrases like "go run errands").
- **Unknown tools:** An unrecognized first word only counts when written as `tool --flag`. An unknown word before a path is too often prose (for example, "in /etc you'll find").
- **Single pipes:** A single `|` only counts when its left side is itself a command. Treating `|` as a whole-text signal caused false positives on titles with separators (such as "Channel Name | Category") and hid the AI buttons. Flags, paths, and redirects are likewise evaluated per segment so prose like "Settings > Privacy" or "Set `foo` to true" is not misclassified.
- **Shell operators:** `&&`, `||`, `$(`, `${`, and heredocs are sufficient anywhere in selections under 4,000 characters (raised from 500 so pasted multi-line scripts classify). Prose rarely contains these constructs, and offering a Run button on technical text quoting shell snippets is acceptable. The length check runs first to avoid scanning very large selections with regexes.
- **Hyphenated tools:** Tools normally invoked with a bare argument (such as `xdg-settings` and `systemd-run`) are listed explicitly. Other hyphenated first words require a flag or path.
- **Linear-time regexes:** The previous email pattern (`[^@\s]+\.[^@\s]+`) suffered from catastrophic backtracking on repeated dots, taking 0.6 s on 8k characters and 7 s on 32k characters (such as minified CSS containing one `@`) on the GTK thread. Similarly, the previous hyphenated-name pattern (`[a-z]+(?:-[a-z][\w-]*)+`) was exponential on inputs like `x-a-a-a-...!`. Both are now linear.

`tests/test_classifier.py` verifies both command/prose test cases and runtime limits.

## Recipe commands

A recipe's `run_command` template is never interpolated directly into shell source (`plugin_system/recipe_loader.py`). Instead, placeholder values are passed as positional arguments to bash, copied into variables in a prelude (preventing a function or `set --` inside the template from shadowing them), and referenced safely:

```
notify-send {text}  ->  bash -c 'vinopop_1=$1; notify-send "${vinopop_1}"' vinopop "<selection>"
```

Because bash expands the variable reference once without reparsing the value as shell syntax, missing or incorrect quoting in a recipe template cannot cause the selection, clipboard contents, or an `{ask:}` prompt response to execute as code. `{text_shell}` maps to the same variable reference in shell commands (preserving compatibility with older recipes); in notifications or clipboard steps, it remains `shlex`-quoted text.

- **Disallowed arithmetic and heredoc contexts:** Bash evaluates arithmetic operands as expressions, where input like `a[$(cmd)]` would execute `cmd`. This occurs inside `$(( ))`, `(( ))`, `[[ -eq ]]`, `let`, `declare -i`, and array subscripts even when quoted, so placeholders in those positions are rejected with an explanation when clicked. Placeholders inside quoted heredocs are also rejected because bash does not expand variables there.
- **Snippet placeholders:** Placeholders such as `{date:+7d}`, `{clipboard}`, and `{ask:Label}` are expanded one token at a time. The selected text itself is never passed through the snippet engine, so a selection containing `{shell:...}` will not execute and one containing `{date}` stays literal.
- **Chains:** Steps execute sequentially. Each transform step renders its template against the current value and passes the result to the next step as `{text}`. The first terminal action (`copy`, `paste`, `notify`, `open_url`, or `run_command`) consumes the value and ends the chain; a chain consisting solely of transforms copies its final result.
- **Clipboard copying:** Recipes copy through the active platform backend (`wl-copy` on KDE Wayland, `xclip` elsewhere), since plain `xclip` only updates the X11 clipboard and misses native Wayland applications.
- **Content-type validation:** A misspelled content type now raises an error. Previously, unknown names were silently dropped, turning a list of typos into an empty filter that matched every content type.
- **Default recipe seeding:** Built-in default recipes are copied into an empty recipes directory once. A marker file ensures that any default recipe you delete is not recreated on future launches.

`tests/test_recipes.py` exercises hostile templates and selections against a real `bash` binary; `tests/test_recipe_rules.py` covers chaining and seeding.

## Editable detection

Cut, Paste, and other editing actions are hidden when the selection is in a read-only widget (`triggers/editable_detect.py`). Sources are checked in the following order:

1. **Last focus event (valid for 30 s):** When `editable_atspi_listener_enabled` is enabled, an AT-SPI listener records whether each newly focused widget is editable. This is the only reliable per-widget signal in Chromium and Electron apps, whose accessibility tree exposes the top-level frame without a focused descendant even though focus events still fire when clicking an input. After 30 s, the cached event expires because the user may have switched apps without generating a new event.
2. **Bounded accessibility tree walk:** Also gated on `editable_atspi_listener_enabled`, limited to 150 ms and 10 levels deep so it cannot delay the popup. It searches only inside the active window for a focused widget. Earlier code searched across all apps, causing Cinnamon's permanently focused desktop window to make every app appear non-editable. An active window with no focused widget (such as Electron) yields an unknown result rather than assuming read-only.
3. **Window class lookup:** Matches the active window's class against a built-in list of read-only viewers (PDF and image viewers, media players, file managers, ebook readers) plus `readonly_app_classes`. Only the class is checked—never the window title—so a class like `eog` does not match a title like "Geography notes". The class is retrieved via the platform backend (`xprop` on X11, KWin on KDE Wayland), covering native Wayland windows.
4. **Default to editable:** If none of the checks yields a definite answer, the widget is treated as editable so useful buttons are not hidden unnecessarily.

The accessibility walk also supplies the selected text's bounding rectangle for anchoring the popup. Because Kate and KWrite report text extents with zero height, a zero height is normalized to 24 px; a zero width or off-screen coordinates fall back to anchoring at the pointer.

AT-SPI is skipped entirely unless the current user's own accessibility bus socket exists and no `at-spi-bus-launcher` is running under another user. A launcher left over from a `sudo` session can intercept queries and abort the process inside GLib (`dbind-ERROR`, a `SIGTRAP` Python cannot catch). The listener is disabled by default for the reasons noted in [Why the defaults are what they are](#why-the-defaults-are-what-they-are).

`tests/test_editable_detect.py` verifies the precedence order and tree walk against a mock desktop (without a live accessibility bus).

## Keystrokes on X11

Paste, Cut, text typing, and window activation use python-xlib and the X server's XTEST extension instead of spawning `xdotool` and `xprop` subprocesses. As with `xdotool`, synthesized events only reach X11 clients (including XWayland windows on Wayland desktops).

- **Clearing held modifiers:** Currently held modifiers are released before sending a key chord (matching `xdotool key --clearmodifiers`) and restored afterward so a shortcut's `Super` or `Shift` does not alter `Ctrl+V`.
- **Characters outside the active layout:** Keysyms missing from the current keyboard map (such as accented letters on a US layout or emoji) are temporarily bound to a spare keycode and restored afterward.
- **Separate spare keycode per character:** Unlike `xdotool`, each missing keysym receives its own spare keycode. Reusing a single keycode per character raced against client processing under heavy CPU load—3 of 6 test runs produced garbled text on Plasma XWayland with all cores busy (2026-10-20).

`tests/test_x11_keys.py` covers all three behaviors.

## Hotkeys on X11

Each global hotkey runs in its own thread with a dedicated X connection (`triggers/hotkey.py`).

- **Lock-modifier variants:** Each hotkey is grabbed four times—one for every combination of Caps Lock and Num Lock—because X11 treats lock states as modifiers and requires an exact match. Individual refused variants are logged; the user is notified of a conflict only if all four grabs fail.
- **Event loop timeout:** The thread blocks in `select()` for up to 1 s (the upper bound on shutdown latency), reduced from 0.2 s to avoid waking every hotkey thread five times per second while idle.
- **Layout changes (`MappingNotify`):** Because X11 grabs target keycodes, switching keyboard layouts can move a keysym to a different keycode and silently break the grab until it is re-registered. Layout switchers like IBus and `kbdd` emit floods of `MappingNotify` events, which previously caused the first hotkey press to be dropped on Norwegian layouts.
- **Ignoring key autorepeat:** Autorepeat events arrive as a key release followed by a key press sharing the same server timestamp. Filtering these ensures holding the toggle hotkey (#44) does not immediately close the popup it just opened.
- **Polling mode (`hotkey_use_polling`):** Skips `XGrabKey` and samples the keymap every 50 ms, firing when the target key transitions down while the exact modifier mask is held (read via `query_pointer().mask`). Forty consecutive failed samples (~2 s) indicate a lost X connection and terminate the thread.

`tests/test_hotkey_listen.py` runs the listener loop against a mock display to verify grabs, conflict reporting, layout remapping, and autorepeat filtering.

## Popup placement on X11

Because GTK 4 removed `Gtk.Window.move()`, the `POPUP` window type, and type hints, vinoPop replicates GTK 3 popup behavior directly: right after GTK creates the underlying X11 window (before mapping it), vinoPop sets the override-redirect attribute and positions it with `XMoveWindow`. The window manager neither decorates, focuses, repositions, nor lists the window in taskbars. Because an override-redirect window never receives window-manager focus (equivalent to GTK 3's `accept_focus=False`), paste actions continue targeting the source application. The KDE Wayland backend uses this same path when running under XWayland without `gtk4-layer-shell`.

## The popup window

The popup is an undecorated `Gtk.Window` positioned, stacked, and kept unfocused by the active platform backend. On X11, it is the override-redirect window described above, re-raised on every move so it cannot slip behind an open Settings or Plugin Manager window (fixing an older Cinnamon issue). On KDE Wayland, it is a `gtk4-layer-shell` overlay surface with keyboard interactivity set to `NONE`. Because it never steals focus, paste actions and synthetic keystrokes always go to the application where the text was selected.

- **Placement:** When AT-SPI supplies a selection rectangle, the popup centers horizontally on it, 10 px above its top edge. Without a selection box, it centers on the pointer and shifts upward by 28 px on X11 to clear the text line (~24 px plus padding), or by 64 px on Wayland where the pointer rests inside or below the selection. If that placement would land within 4 px of the top of the monitor's usable work area, the popup flips below the text. X11 root coordinates are physical pixels divided by monitor scale; Wayland coordinates are already logical. `tests/test_popup_show.py` verifies all placement cases.
- **Button selection:** Displays plugins matching the classified content type, ordered by `plugin_order`, omitting editing actions when the focused widget is read-only and capping at `max_popup_buttons`. Holding `popup_force_all_modifier` from before selection release until the popup renders (outlasting the debounce) bypasses content-type filtering and shows every plugin whose `matches()` accepts the text.
- **Dismissal:** On X11, `_tick` polls mouse buttons and `Esc` over Xlib because an unfocused override-redirect window receives no key events. On Wayland, dismissal relies on GTK's leave, focus, and key events.
- **Keyboard navigation (#44):** When summoned via hotkey on X11, vinoPop grabs navigation keys on the root window while the popup is visible: arrow keys, `Tab`, `Home`, and `End` move the highlight, `Enter` and `Space` activate the highlighted button, and `Esc` closes the bar. The same keys also work if a window manager focuses the popup. Wayland does not support root-window key grabs, so the popup is mouse-only there for now. The AI result bubble uses the same mechanism for its action chips (#124).

## Clipboard history and snippets

The clipboard history and snippets plugin lives in a single file (`plugins_repo/clipboard_history.py`) because the Plugin Manager installs and upgrades plugins by copying one file into `~/.config/vinopop/plugins` and comparing its version header against the catalog. That file contains the JSON stores (`history.json`, `snippets.json`), clipboard watcher, trigger watchers, placeholder engine, and picker UI.

### Snippet trigger timing

Typing a snippet trigger followed by a boundary key (space, tab, or enter) deletes both and pastes the expanded snippet. Each delay below addresses a specific timing race observed on real desktops:

| Step | Wait | Reason |
|---|---|---|
| Before the first synthetic key | 60 ms | The boundary key (space, tab, or enter) is still physically pressed. Injecting keys immediately left `Ctrl` logically stuck in the target app, causing scroll-wheel zooming. |
| Between BackSpaces (X11) | 16 ms | Sending BackSpaces with no delay caused Electron and Chrome to drop events. One 60 Hz frame keeps deletion reliable while remaining imperceptible. |
| After the BackSpaces | 80 ms on X11, 30 ms with `ydotool` | Lets the target app finish processing deletions before staging the clipboard. |
| After the paste key, before Left arrows | 50 ms | Ensures the pasted text lands before moving the caret back to `{cursor}`. |
| Before releasing modifiers (X11) | 20 ms | Restoring held modifiers raced against the user's physical key-up and occasionally left `Ctrl` or `Shift` stuck (causing Firefox to zoom on scroll), so modifiers are released outright. |
| Before restoring the previous clipboard | 300 ms | The target application reads the clipboard asynchronously after receiving the paste shortcut; restoring too quickly pastes the old clipboard contents. |

BackSpace keys are sent without clearing modifiers first because BackSpace ignores them and clearing modifiers occasionally left `Ctrl` stuck.

On Wayland, the expanded text is placed on both the clipboard and `PRIMARY` selection and pasted with `Shift+Insert`. `ydotool type` maps through a US keycode table and drops non-ASCII characters such as `æ`, `ø`, and `å`, while `ydotool` `Ctrl` chords do not register reliably on KWin. To avoid reading its own synthetic keystrokes, the `/dev/input` watcher pauses for `1.2 s + 20 ms * key_count`, plus 250 ms after the final key.

Snippets containing `{ask:}` prompts do not expand from typed triggers to avoid interrupting typing with a dialog; they remain available from the picker window.

### Pasting from the clipboard picker

Because the picker window takes focus, pasting must first restore focus to the previously active application:

- Wait 60 ms after the click, then release all modifiers. Without this, modifiers from the picker shortcut (such as `Super+Shift+V`) can remain logically held and corrupt or block the paste.
- Activate the target window twice—via Xlib and via `wmctrl -ia` when installed—because on Cinnamon's muffin the first activation call can return before focus changes.
- Wait 180 ms (or 300 ms for terminal emulators). Cinnamon queues window activation for the next compositor frame (100 ms was too short), and VTE commits focus-in ~50 ms after activation.
- If the target window is still not active, activate it one more time and wait an additional 120 ms.
- Single-line entries up to 2,048 characters are typed via XTEST instead of pasted, because Electron editors (Slack, Discord, Notion, and similar apps) silently drop synthetic `Ctrl+V` events and terminal bracketed paste can desync with readline. Multi-line text is pasted (typing newlines into a shell would execute each line), and entries over 2,048 characters are pasted for speed. If typing fails, vinoPop falls back to sending the paste shortcut.

`tests/test_clipboard_paste.py` and `tests/test_trigger_expansion.py` verify the step ordering with mocked X11 and clipboard backends; the wait durations themselves were tuned by hand on live desktops.

### The picker window

- Implemented as an `Adw.Window` so confirmation prompts open as modal sheets attached to the picker. It remains resizable even when undecorated because a non-resizable `Adw.Window` detaches alert dialogs into separate floating windows.
- On X11, the picker is undecorated and opens at the pointer, with its title row acting as a drag handle. On KDE Wayland, clients cannot position their own toplevel windows, so the picker keeps standard window decorations and lets the compositor place it.
- On focus loss, the picker waits 120 ms and closes only if focus is still outside the window, avoiding spurious dismissal when IBus or Cinnamon briefly steals focus for a single frame. Auto-close is suspended while a prompt sheet or window drag is active.

## The Run in terminal dialog

`core/actions.py` presents a confirmation dialog showing **Cancel**, **Edit**, and **Run** above a read-only preview of the command. Because GTK 4 has no blocking `dialog.run()`, the dialog is an `Adw.Window` whose response callback triggers execution, with a header bar so the window can be moved and closed.

- The command is rendered inside a monospace `Gtk.TextView` rather than a `Gtk.Label`, because labels interpret `&`, `<`, and `>` as Pango markup.
- In read-only mode, it uses `.lp-cmd-preview` styling to appear frameless like a label. Clicking **Edit** switches to `.lp-cmd-edit` input-field styling, selects all text, and disables the **Edit** button.
- Single-line commands focus **Run** by default so pressing `Enter` executes immediately. Multi-line commands focus **Cancel** and ignore plain `Enter`, requiring you to review all lines and click **Run** or press `Ctrl+Enter`. While editing, plain `Enter` inserts a newline.
- `Ctrl+Enter` is handled on the text view during the capture phase so it runs before the text view's default `Enter` handler can insert a newline.
- Closing the window from the title bar acts as **Cancel**, and only the first response is honored.

`tests/test_terminal_confirm_dialog.py` constructs the dialog and verifies each of these behaviors.

## Threads and X11

Two deliberate omissions in the daemon's X11 and process handling:

- **No `XInitThreads`:** Multiple threads interact with X11 (selection watcher, hotkeys, popup tick, and clipboard watcher), but calling `XInitThreads` at import time causes hotkey threads to silently fail key grabs when python-xlib and PyGObject share a process. Instead, vinoPop relies on GTK's own locking plus a separate python-xlib `Display` connection (each with its own socket and lock) per thread. If `XIO: fatal IO error` crashes occur under load, give the offending Xlib caller its own `Display` connection rather than re-enabling `XInitThreads`.
- **`SIGCHLD` is not set to `SIG_IGN`:** Ignoring `SIGCHLD` to auto-reap child processes caused `subprocess.run()` to intermittently freeze the UI on Python 3.12 when opening the clipboard picker alongside frequent short-lived `xclip` calls and the GTK main loop. Leaving a small number of short-lived zombie PIDs is preferable until a periodic reaper is added.

## Demos

The SVG animations in the README are generated programmatically. Run `python3 docs/media/demos.py` to regenerate `popup.svg` and `clipboard.svg`.
