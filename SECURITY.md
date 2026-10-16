# Security

## What vinoPop does with your text

Your selections, clipboard history and snippets stay on your machine. There
is no telemetry and no account.

The only network traffic is something you click that is plainly online:

- web search and recipe buttons open a URL in your browser
- opening a link you selected
- the one-time AI model download from Hugging Face, the first time you use an
  AI action

The bundled translate bubble still calls Google's translation endpoint until
it moves to the local model.

Clipboard history is stored in plain JSON at `~/.cache/linuxpop/history.json`,
readable by your user. Treat it as sensitive as whatever you copy.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting on this repository
(Security tab → Report a vulnerability) rather than a public issue.
