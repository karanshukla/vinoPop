"""Desktop notifications, sent with notify-send.

Kept apart from core.actions so the typed modules (vino_ai) can use it.
Plugins reach it as actions.notify, which re-exports it.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

# Every notification names vinoPop, not "notify-send", as the sending app.
# desktop-entry is the launcher's file id (install.sh), which GNOME and KDE
# use to group the notification under the app and show its icon.
APP_NAME = "vinoPop"
DESKTOP_ENTRY = "io.github.karanshukla.vinoPop"


def notify_argv(
    title: str,
    body: str,
    icon: str = "vinopop",
    timeout_ms: int | None = 3000,
    *,
    urgency: str | None = None,
    transient: bool = True,
    extra: Sequence[str] = (),
) -> list[str]:
    """The notify-send command line for a vinoPop notification. `extra` goes
    before the title (e.g. -A/-w for an action button). `--` keeps a title
    or body that starts with "-" from being read as an option."""
    argv = ["notify-send", f"--app-name={APP_NAME}", f"--hint=string:desktop-entry:{DESKTOP_ENTRY}"]
    if transient:
        argv.append("--hint=byte:transient:1")
    if timeout_ms is not None:
        argv += ["-t", str(timeout_ms)]
    if urgency:
        argv += ["-u", urgency]
    if icon:
        argv += ["-i", icon]
    return [*argv, *extra, "--", title, body]


def notify(
    title: str,
    body: str,
    icon: str = "vinopop",
    timeout_ms: int | None = 3000,
    *,
    urgency: str | None = None,
    transient: bool = True,
) -> None:
    """Best-effort desktop notification; a missing notify-send is not an error."""
    try:
        subprocess.run(
            notify_argv(title, body, icon, timeout_ms, urgency=urgency, transient=transient),
            check=False,
        )
    except OSError:
        pass
