# Security

## What vinoPop does with your text

Your selections, clipboard history and snippets stay on your machine. There
is no telemetry and no account.

The only network traffic is something you click that is plainly online:

- web search and recipe buttons open a URL in your browser
- opening a link you selected
- the one-time AI model download from Hugging Face: the first time you use an
  AI action, or in the background at startup if you chose "Download now" in
  onboarding or turned it on in Settings → Advanced

OCR, summarize, rewrite, explain and translate run on the local OpenVINO
models; the selected text never leaves the machine.

OpenVINO ships Intel's own usage telemetry (Google Analytics), which is on
unless you opt out. vinoPop never loads it: before OpenVINO is imported,
its `openvino_telemetry` module is blocked for the vinoPop process, so
OpenVINO falls back to a no-op stub. There is no setting to turn it on.
Your `~/intel/openvino_telemetry` consent file is left alone and still
applies to other OpenVINO tools you run.

## Clipboard history

Clipboard history is stored in plain JSON at
`~/.cache/vinopop/clipboard/history.json`, with copied images next to it in
`images/`. Snippets and settings live in `~/.config/vinopop/`. These files are
written `0600` in `0700` folders, so only your user can read them even if your
home folder is world-readable; files from older versions are tightened the next
time vinoPop starts. They aren't encrypted: treat them as sensitive as whatever
you copy.

Some copies are never stored:

- copies a password manager marks as secret. KeePassXC, KWallet and Plasma
  offer an `x-kde-passwordManagerHint` type with the value `secret`; some apps
  use `application/x-nspasteboard-concealed-type`. vinoPop checks the offered
  types (`wl-paste --list-types` on Wayland, `TARGETS` on X11) and skips those
  copies. A password manager that sets neither isn't detected
- copies made while the focused window matches your popup blocklist
  (`blocklist_patterns`). On Wayland the focused window comes from KWin, so
  this works on Plasma but not other Wayland desktops
- copies over 1 MB of text or 20 MB of image (`clipboard_history_max_text_kb`,
  `clipboard_history_max_image_mb`)

To remove something you've already copied, open the picker, select the entry in
Recent and press Delete (or click the bin). The entry is removed from disk
straight away, along with its image unless you've pinned it as a snippet.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting on this repository
(Security tab → Report a vulnerability) rather than a public issue.
