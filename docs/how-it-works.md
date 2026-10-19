# How it works

The detail that used to live in the README: which backend runs on which desktop, the offline AI setup, and every setting.

## Backends

vinoPop picks a backend at startup:

- **X11** (Cinnamon, GNOME-on-X11, KDE, XFCE, MATE): selection through `xclip`, keystrokes through `xdotool`, cursor position through Xlib.
- **KDE Plasma 6 / Wayland**: selection through `wl-clipboard`, popup placement through `gtk4-layer-shell` (XWayland when it isn't installed), cursor position through KWin's scripting API.
- **Every other Wayland desktop** (GNOME, sway, Hyprland) runs the X11 path under XWayland. The native backend needs KWin's scripting API for the cursor position.

`VINOPOP_BACKEND=x11` or `=wayland_kde` overrides the choice.

On KDE Wayland the global hotkey runs through KGlobalAccel (check it actually bound under System Settings, Shortcuts), Paste/Cut press keys through the desktop's Remote Control portal (KDE asks once; `ydotool` + `ydotoold` is the fallback), and modifier+double-click needs your user in the `input` group. Popup placement, hover persistence, click-outside dismissal and the app blocklist all work natively. You can anchor the popup to the selected text instead of the mouse if you turn on desktop accessibility (AT-SPI). There's more in [FEDORA-KDE.md](FEDORA-KDE.md).

## Offline AI

OCR, summarize, rewrite, explain and translate run on your own machine through OpenVINO: the LLM on an Intel NPU when there is one (the iGPU or CPU otherwise), and OCR on the CPU. Install the Python side for the same Python that runs vinoPop:

```sh
pip install --user -r requirements-ai.txt
```

On Debian/Ubuntu, whose system Python refuses `pip --user`, add `--break-system-packages` or run vinoPop from a venv created with `--system-site-packages` so it still sees PyGObject.

The model downloads once: Gemma 4 E2B, about 4 GB, fast and light on RAM. Set `"ai_llm_model": "gemma-4-e4b"` for the more accurate 5 GB one. Onboarding asks whether to get it now, in the background; otherwise the first AI action downloads it. Settings → Advanced → *Download the AI model in the background* changes that answer later. After that nothing goes online. `"ai_device"` forces `"NPU"`, `"GPU"` or `"CPU"`.

Before loading on the NPU, vinoPop probes it in a short-lived subprocess. A stuck NPU (it can happen after long, heavy runs) makes the probe crash instead of vinoPop, so the model loads on the GPU and the notification gives you the `modprobe` command that resets the driver.

`vinopop --doctor` (add `--json` for a bug report) checks the AI packages, which devices OpenVINO sees and the order each model will try them in, and whether the model is downloaded. With [ovfetch](https://github.com/karanshukla/ovfetch) installed it also reports whether the pinned OpenVINO is inside the range Intel records for your NPU driver. It's diagnosis only: the NPU is still tried, and falls back to GPU/CPU if it won't compile.

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
| `ai_idle_unload_minutes` | `5` | Unload the model after this long idle. `0` keeps it loaded. |
| `editable_atspi_listener_enabled` | `false` | Use AT-SPI for editable detection + selection geometry |
| `popup_anchor_to_selection` | `true` | Anchor popup to the selection rect (needs AT-SPI enabled) |

## Demos

The animations in the README are drawn, not recorded. `python3 docs/media/demos.py` regenerates `popup.svg` and `clipboard.svg`.
