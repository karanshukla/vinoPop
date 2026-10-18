# vinopop-plugin-version: 1
"""Generate a QR code for the selected text and show it.

Uses a rolling cache directory under ~/.cache/vinopop/qr/ - keeps the
last 20 QR images and trims older ones, so /tmp doesn't accumulate forever.
"""

from __future__ import annotations

import shutil
import subprocess
import time

from classifier import ContentType
from plugin_base import Plugin
from xdg_paths import CACHE_DIR

QR_CACHE = CACHE_DIR / "qr"
MAX_CACHED = 20
# The most a QR code holds: 2953 bytes at version 40, error correction L
# (qrencode's default level)
MAX_QR_BYTES = 2953


def _cleanup_cache() -> None:
    if not QR_CACHE.is_dir():
        return
    pngs = sorted(QR_CACHE.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in pngs[MAX_CACHED:]:
        try:
            old.unlink()
        except OSError:
            pass


def _error(title: str, body: str) -> None:
    subprocess.run(
        [
            "notify-send",
            "--hint=byte:transient:1",
            "-t",
            "3000",
            "-i",
            "dialog-error",
            title,
            body[:200],
        ],
        check=False,
    )


def _install_hint() -> str:
    if shutil.which("dnf"):
        return "Install with: sudo dnf install qrencode"
    if shutil.which("apt-get"):
        return "Install with: sudo apt-get install qrencode"
    return "Install qrencode with your package manager"


def _qr(text: str) -> None:
    if not shutil.which("qrencode"):
        _error("QR plugin missing dependency", _install_hint())
        return
    size = len(text.encode("utf-8"))
    if size > MAX_QR_BYTES:
        _error("Too long for a QR code", f"{size} bytes selected; a QR code holds {MAX_QR_BYTES}")
        return
    QR_CACHE.mkdir(parents=True, exist_ok=True)
    _cleanup_cache()
    out_path = QR_CACHE / f"qr-{int(time.time() * 1000):x}.png"
    try:
        # On stdin, not argv: an argument has a size limit (E2BIG) and shows
        # up in `ps` for every other user
        subprocess.run(
            ["qrencode", "-s", "8", "-o", str(out_path)],
            input=text.encode("utf-8"),
            capture_output=True,
            check=True,
        )
        subprocess.Popen(["xdg-open", str(out_path)], start_new_session=True)
    except subprocess.CalledProcessError as exc:
        reason = exc.stderr.decode("utf-8", "replace").strip() if exc.stderr else ""
        _error("QR error", reason or str(exc))
    except OSError as exc:
        _error("QR error", str(exc))


def register(register_plugin) -> None:
    register_plugin(
        Plugin(
            name="qr-code",
            icon="vinopop-qr-symbolic",
            tooltip="Make QR code",
            handler=_qr,
            content_types=(ContentType.URL, ContentType.PLAIN_TEXT),
            priority=90,
        )
    )
