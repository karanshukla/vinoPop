# Security

## What vinoPop does with your text

Your selections, clipboard history, and snippets remain on your machine. vinoPop requires no account and collects no telemetry.

Network requests only happen for explicit online actions:

- Web search and recipe buttons that open a URL in your browser.
- Opening a URL you selected.
- The one-time AI model download from Hugging Face, triggered either on your first AI action or at startup if you enabled "Download now" in onboarding or under **Settings → Advanced**.

Only `http`, `https`, `ftp`, and `mailto` links are forwarded to your browser or mail client. Because web pages can control selectable text, custom URI schemes (such as `steam:` or `vscode:`) are rejected, and selecting a `file://` link opens its containing folder rather than executing the file through its MIME handler.

OCR, summarize, rewrite, explain, and translate all run on local OpenVINO models; your selected text never leaves your machine.

OpenVINO includes Intel's usage telemetry package (Google Analytics), which is enabled by default unless opted out. Before importing OpenVINO, vinoPop blocks its `openvino_telemetry` module for the vinoPop process so OpenVINO falls back to a no-op stub, with no option to enable it. Your `~/intel/openvino_telemetry` consent file is left untouched for any other OpenVINO tools you use.

## Clipboard history

Clipboard history is stored as plain JSON at `~/.cache/vinopop/clipboard/history.json`, with copied images saved alongside it in `images/`. Snippets and settings are stored in `~/.config/vinopop/`. These files are created with mode `0600` inside `0700` directories so other local users cannot read them even if your home directory is world-readable; files created by older versions have their permissions tightened on next startup. Because these files are not encrypted, treat them with the same sensitivity as the data you copy.

Certain clipboard entries are never recorded:

- **Password-manager secrets:** KeePassXC, KWallet, and Plasma attach an `x-kde-passwordManagerHint` MIME type set to `secret`, and some applications advertise `application/x-nspasteboard-concealed-type`. vinoPop inspects offered MIME types (`wl-paste --list-types` on Wayland, `TARGETS` on X11) and skips matching copies. Password managers that set neither hint cannot be detected automatically.
- **Blocked windows:** Copies made while the focused window matches your popup blocklist (`blocklist_patterns`). On Wayland, active-window metadata comes from KWin, so this filter works on Plasma but not on other Wayland compositors.
- **Oversized entries:** Copies exceeding 1 MB of text or 20 MB of image data (configurable via `clipboard_history_max_text_kb` and `clipboard_history_max_image_mb`).

To delete an existing entry, open the clipboard picker, select the item under **Recent**, and press `Delete` (or click the trash button). The entry and its image file are deleted from disk immediately unless the image is pinned as a snippet.

## Reporting a vulnerability

Please report security issues privately using GitHub's private vulnerability reporting (**Security** tab → **Report a vulnerability**) rather than opening a public issue.
