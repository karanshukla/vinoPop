"""Check for a newer release and install it, for Settings' Updates row.

Nothing goes online until the user clicks Check for updates: one anonymous
GET to GitHub's releases API. Installing runs ``vinopop-upgrade``, the copy of
``get.sh`` the release installer leaves in ``~/.local/bin``. A checkout has
none, and Settings points at the release page instead. The same steps as
vino-ui's About row (karanshukla/vinoUserInterface, src/update.rs).
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404
import urllib.request
from pathlib import Path

REPO = "karanshukla/vinoPop"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"

_TAG = re.compile(r"^v[0-9][0-9A-Za-z.\-]{0,31}$")


def version_parts(version: str) -> tuple[int, ...] | None:
    """``v1.3.0`` and ``1.3.0`` are (1, 3, 0). Anything else (``0.0.0-dirty``,
    a ``git describe`` past a tag) is None and is never offered an update."""
    text = version[1:] if version.startswith("v") else version
    parts = text.split(".")
    if not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def is_newer(current: str, latest: str) -> bool:
    cur, new = version_parts(current), version_parts(latest)
    if cur is None or new is None:
        return False
    width = max(len(cur), len(new))
    return new + (0,) * (width - len(new)) > cur + (0,) * (width - len(cur))


def latest_tag(payload: str) -> str | None:
    """The release's tag, if it looks like one: it ends up in the window and
    in the installer's environment."""
    try:
        tag = json.loads(payload).get("tag_name")
    except (ValueError, AttributeError):
        return None
    return tag if isinstance(tag, str) and _TAG.match(tag) else None


def fetch_latest(timeout: float = 10.0) -> str:
    """The newest full release's tag. Raises RuntimeError with a sentence for the row."""
    request = urllib.request.Request(API_URL, headers={"Accept": "application/vnd.github+json"})
    try:
        # A fixed https URL.
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            payload = response.read(1 << 20).decode("utf-8", "replace")
    except OSError as exc:
        raise RuntimeError("Couldn't reach GitHub to check. Try again when you're online.") from exc
    tag = latest_tag(payload)
    if tag is None:
        raise RuntimeError("GitHub's answer had no release in it.")
    return tag


def upgrade_command() -> Path | None:
    bin_dir = Path(os.environ.get("XDG_BIN_HOME") or Path.home() / ".local/bin")
    path = bin_dir / "vinopop-upgrade"
    return path if path.is_file() and os.access(path, os.X_OK) else None


def install(tag: str, upgrade: Path) -> None:
    """Run the upgrade for ``tag``. Raises RuntimeError with the installer's reason."""
    env = dict(os.environ, VINO_UPDATE_TAG=tag)
    # Our own installer, no shell.
    result = subprocess.run(  # nosec B603
        [str(upgrade)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    marked = [line for line in lines if line.startswith("✗")]
    reason = (marked or lines or [f"The update to {tag} failed."])[-1]
    raise RuntimeError(reason.lstrip("✗ ").strip())
