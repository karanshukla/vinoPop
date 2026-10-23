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
| `tray_icon_style` | `color` | Tray icon: `color` (recoloured by the panel), `light` or `dark` (fixed, for panels that don't recolour it) |

## Demos

The animations in the README are drawn, not recorded. `python3 docs/media/demos.py` regenerates `popup.svg` and `clipboard.svg`.
