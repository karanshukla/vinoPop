# Security

## What vinoPop does with your text

Your selections, clipboard history and snippets stay on your machine. There
is no telemetry and no account.

The only network traffic is something you click that is plainly online:

- web search and recipe buttons open a URL in your browser
- opening a link you selected
- the one-time AI model download from Hugging Face, the first time you use an
  AI action

OCR, summarize, rewrite, explain and translate run on the local OpenVINO
models; the selected text never leaves the machine.

Clipboard history is stored in plain JSON at `~/.cache/vinopop/history.json`,
readable by your user. Treat it as sensitive as whatever you copy.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting on this repository
(Security tab → Report a vulnerability) rather than a public issue.
