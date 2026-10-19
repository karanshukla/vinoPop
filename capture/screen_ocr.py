"""Screen-region OCR for vinoPop.

User holds a hotkey, drags a rectangle, the rectangle's contents land
on the clipboard as text. Capture uses the overlay selector or the
desktop's own region tool (spectacle, grim+slurp, maim, or
gnome-screenshot); recognition is PP-OCR running
offline on OpenVINO (vino_ai.ocr), on the CPU.

Why this matters beyond a one-off screenshot tool: the vinoPop popup
runs on X11 PRIMARY selection. Anywhere the user can't make a real
selection (PDF viewers' rasterized text, video frames, OS chrome,
CodeMirror / Monaco editors that don't propagate to PRIMARY, error
dialogs that block selection) becomes unreachable. OCR turns the
*pixels* into a PRIMARY-equivalent selection - the rest of vinoPop's
pipeline lights up automatically.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from platform_backend.base import expect_selection

log = logging.getLogger("vinopop")


def _stage_text(text: str) -> None:
    """Put `text` on the clipboard (and PRIMARY) with the right tool for the
    session: wl-copy on Wayland, xclip on X11. The old code only knew xclip,
    which isn't installed on a Wayland box - so OCR'd text silently never
    reached the clipboard."""
    data = text.encode("utf-8")
    # No gesture behind this PRIMARY change, and CLIPBOARD gets the same text
    # first, which the copy-button filter would otherwise swallow.
    expect_selection(text)
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        for extra in ([], ["--primary"]):
            try:
                subprocess.run(["wl-copy", *extra], input=data, check=False, timeout=2.0)
            except (OSError, subprocess.SubprocessError):
                pass
        return
    if shutil.which("xclip"):
        for sel in ("clipboard", "primary"):
            try:
                subprocess.run(["xclip", "-selection", sel], input=data, check=False, timeout=2.0)
            except (OSError, subprocess.SubprocessError):
                pass


def _distro_id() -> str:
    """Read /etc/os-release ID + ID_LIKE so we can pick the right
    package manager. ID_LIKE is the fallback distro family (e.g.
    Pop_OS has ID=pop, ID_LIKE=ubuntu debian) so we don't need to
    enumerate every derivative."""
    try:
        text = Path("/etc/os-release").read_text()
    except OSError:
        return ""
    info: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        info[k.strip()] = v.strip().strip('"').strip("'")
    return f"{info.get('ID', '')} {info.get('ID_LIKE', '')}".lower()


def install_command() -> str:
    """Install command for the capture tool, for when no region-capture
    tool is present (KDE and GNOME ship one, so this is mostly X11 WMs)."""
    ids = _distro_id()

    def has(needles: tuple[str, ...]) -> bool:
        return any(n in ids for n in needles)

    if has(("fedora", "rhel", "centos", "rocky", "alma")):
        return "sudo dnf install -y maim"
    if has(("arch", "manjaro", "endeavouros")):
        return "sudo pacman -S --noconfirm maim"
    if has(("opensuse", "suse")):
        return "sudo zypper --non-interactive install maim"
    return "sudo apt install -y maim"


def _has_capture_tool() -> bool:
    """True if a supported region-capture tool is available on PATH."""
    return bool(
        shutil.which("spectacle")
        or shutil.which("grim")
        or shutil.which("maim")
        or shutil.which("gnome-screenshot")
    )


def is_supported() -> tuple[bool, str]:
    """Return (ok, reason). ok=False means we can't run OCR right now;
    the reason is a SHORT human label of what's missing (the Settings row
    pairs it with an Install button, so it doesn't need to spell out a
    command)."""
    if not _has_capture_tool():
        return False, "screen-capture tool not installed"
    try:
        from vino_ai import ocr

        return ocr.available()
    except ImportError:
        return False, "the vino_ai package is missing from this install"


def install_argv() -> list[str] | None:
    """A pkexec argv that installs a capture tool when none is present
    (pkexec shows a graphical auth prompt). Returns None when nothing
    installable is missing, or we don't recognise the package manager. The
    OCR engine itself comes from pip (`vinopop[ai]`), not the distro."""
    if _has_capture_tool():
        return None
    ids = _distro_id()

    def has(needles: tuple) -> bool:
        return any(n in ids for n in needles)

    if has(("fedora", "rhel", "centos", "rocky", "alma")):
        return ["pkexec", "dnf", "install", "-y", "maim"]
    if has(("arch", "manjaro", "endeavouros")):
        return ["pkexec", "pacman", "-S", "--noconfirm", "maim"]
    if has(("opensuse", "suse")):
        return ["pkexec", "zypper", "--non-interactive", "install", "maim"]
    if has(
        ("debian", "ubuntu", "mint", "pop", "elementary", "zorin", "neon", "kali", "deepin", "mx")
    ):
        return ["pkexec", "apt-get", "install", "-y", "maim"]
    return None


def _has_image(path: Path) -> bool:
    """The capture target is created empty up front, so existing isn't enough:
    a cancelled capture leaves a zero-byte file behind."""
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _capture_region(out_path: Path) -> bool:
    """Use whichever region-capture tool is installed to grab a user-
    drawn rectangle and write it as a PNG. Returns False if the user
    cancelled or the tool errored out."""
    if shutil.which("spectacle"):
        # KDE's capture tool. Its rectangular-region selector works
        # natively on Wayland (maim is X11-only and grim needs wlroots),
        # so it's the right default on KWin. -r region, -b background (no
        # GUI window), -n no notification, -o write to file.
        try:
            subprocess.run(
                ["spectacle", "-r", "-b", "-n", "-o", str(out_path)],
                capture_output=True,
                timeout=120,
            )
            return _has_image(out_path)
        except subprocess.TimeoutExpired:
            log.warning("[ocr] spectacle timed out")
            return False
    if shutil.which("grim") and shutil.which("slurp"):
        # wlroots compositors (sway, Hyprland): slurp picks the region,
        # grim captures it.
        try:
            geom = subprocess.run(["slurp"], capture_output=True, timeout=60, text=True)
            if geom.returncode != 0 or not geom.stdout.strip():
                return False
            res = subprocess.run(
                ["grim", "-g", geom.stdout.strip(), str(out_path)],
                capture_output=True,
                timeout=30,
            )
            return res.returncode == 0 and _has_image(out_path)
        except subprocess.TimeoutExpired:
            return False
    if shutil.which("maim"):
        # `-s` puts maim in interactive region-select mode; output goes
        # to stdout if we don't pass a filename. We use a filename so
        # the OCR step can read it back.
        try:
            res = subprocess.run(
                ["maim", "-s", str(out_path)],
                capture_output=True,
                timeout=60,
            )
            if res.returncode != 0:
                log.info("[ocr] maim exited %d (user cancelled?)", res.returncode)
                return False
            return _has_image(out_path)
        except subprocess.TimeoutExpired:
            log.warning("[ocr] maim timed out after 60 s")
            return False
    if shutil.which("gnome-screenshot"):
        # gnome-screenshot --area is interactive too. Older versions
        # don't accept a target path on stdout, so use --file.
        try:
            res = subprocess.run(
                ["gnome-screenshot", "--area", "--file", str(out_path)],
                capture_output=True,
                timeout=60,
            )
            return res.returncode == 0 and _has_image(out_path)
        except subprocess.TimeoutExpired:
            return False
    return False


def _recognize(image_path: Path) -> str | None:
    """OCR the captured PNG offline. Returns None on failure (logged)."""
    try:
        from vino_ai import ocr

        return ocr.recognize(str(image_path))
    except Exception as exc:  # noqa: BLE001 - reported as "no text"
        log.warning("[ocr] recognition failed: %s", exc)
        return None


def _notify(title: str, body: str, *, icon: str = "dialog-information", ms: int = 3500) -> None:
    subprocess.run(
        ["notify-send", "--hint=byte:transient:1", "-t", str(ms), "-i", icon, title, body],
        check=False,
    )


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _capture_image() -> tuple[Path | None, str | None]:
    """Let the user drag a region and return (png, None); (None, None) when
    they cancelled the overlay, or (None, message) when the fallback tool
    captured nothing. The caller owns (and deletes) the png."""
    # Prefer the frictionless overlay selector (drag -> done, no Accept step).
    # Fall back to the desktop's region tool if it isn't available.
    try:
        from capture import ocr_selector

        if ocr_selector.available():
            crop = _capture_via_overlay()
            return (Path(crop) if crop else None), None
    except Exception as exc:  # noqa: BLE001
        print(f"[ocr] overlay selector failed, falling back: {exc}")
    with tempfile.NamedTemporaryFile(suffix=".png", prefix="vinopop-ocr-", delete=False) as tmp:
        png_path = Path(tmp.name)
    if _capture_region(png_path):
        return png_path, None
    _unlink(png_path)
    return None, "Region capture cancelled."


def _describe_enabled() -> bool:
    try:
        from core.settings import get_settings

        return bool(get_settings().get("ocr_describe_images", False))
    except Exception:
        return False


def _describe(image: Path, ocr_text: str) -> None:
    """Stream the AI model's alt text for `image` into a result bubble at
    the pointer, with Copy. Takes ownership of `image`."""
    from ui import ai_bubble
    from vino_ai import ai_worker

    def _work(bubble) -> None:
        try:
            ai_worker.describe_into(bubble, str(image), ocr_text)
        finally:
            _unlink(image)

    ai_bubble.show("Describe image", _work)


def _offer_describe(title: str, body: str, image: Path, ocr_text: str) -> None:
    """The capture notification with a Describe image button. Waits for
    the click on its own thread, so the next capture isn't held up. Takes
    ownership of `image`."""
    import threading

    def _wait() -> None:
        clicked = False
        try:
            res = subprocess.run(
                [
                    "notify-send",
                    "--hint=byte:transient:1",
                    "-t",
                    "10000",
                    "-i",
                    "edit-paste-symbolic",
                    "-A",
                    "describe=Describe image",
                    "-w",
                    title,
                    body,
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            clicked = res.stdout.strip() == "describe"
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("[ocr] describe notification failed: %s", exc)
        if clicked:
            _describe(image, ocr_text)
        else:
            _unlink(image)

    threading.Thread(target=_wait, daemon=True, name="ocr-describe-offer").start()


def _capture_via_overlay() -> str | None:
    """Run the frictionless overlay selector on the GTK main thread and return
    the cropped PNG path (or None if cancelled). Called from a worker thread,
    so it blocks on an Event until the user finishes the drag."""
    import threading

    from gi.repository import GLib

    from capture import ocr_selector

    done = threading.Event()
    holder: dict = {}

    def _cb(path):
        holder["path"] = path
        done.set()

    def _start() -> bool:
        # Without this a failed grab (or an exception building the overlay,
        # which GLib swallows) would leave us waiting the full timeout for a
        # callback that never comes.
        try:
            if not ocr_selector.select_and_capture(_cb):
                raise RuntimeError("could not grab the screen for the overlay")
        except Exception as exc:  # noqa: BLE001
            holder["error"] = exc
            done.set()
        return False  # idle_add: run once

    GLib.idle_add(_start)
    if not done.wait(180):
        return None
    if "error" in holder:
        raise holder["error"]
    return holder.get("path")


def run_ocr_to_clipboard() -> None:
    """User-facing entry point. Triggered by the OCR hotkey or by the
    tray menu. Captures a region, OCRs it, puts the result on the
    clipboard, and shows the result text in the popup (so it lands as
    a selection the rest of vinoPop's actions can pick up).

    With `ocr_describe_images` on, the AI model can also describe the
    capture: a button on the notification when there was text, and
    straight away when there wasn't (otherwise a dead end)."""
    ok_sup, reason = is_supported()
    if not ok_sup:
        _notify("vinoPop OCR", reason, ms=4000)
        return

    image, message = _capture_image()
    if image is None:
        if message:
            _notify("vinoPop OCR", message, ms=4000)
        return  # user cancelled (Esc / zero-size drag)
    kept = False
    try:
        text = _recognize(image)
        describe = _describe_enabled()
        if not text:
            if describe:
                _describe(image, "")
                kept = True
            else:
                _notify("vinoPop OCR", "No text found in the selection.")
            return
        # Park the text on the clipboard (and PRIMARY) so it's usable everywhere
        # and the popup can act on it like a real selection.
        _stage_text(text)
        # Friendly confirmation - tail the recognised text so the user knows
        # OCR ran and roughly what came out.
        preview = text.replace("\n", " ")[:120]
        body = f"{len(text)} chars on clipboard - “{preview}”"
        if describe:
            _offer_describe("OCR captured", body, image, text)
            kept = True
        else:
            _notify("OCR captured", body, icon="edit-paste-symbolic")
    finally:
        if not kept:
            _unlink(image)
