"""Screen-region OCR: the user drags a rectangle and its text lands on the
clipboard and PRIMARY, so the popup can act on text that can't be selected
(rasterized PDFs, video frames, editors that don't set PRIMARY).

Capture uses the overlay selector or the desktop's own region tool
(spectacle, grim+slurp, maim, gnome-screenshot, or the portal's
Screenshot); recognition is PP-OCR offline on OpenVINO (vino_ai.ocr), on
the CPU.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from core import distro
from platform_backend.base import expect_selection

log = logging.getLogger("vinopop")

_CLIPBOARD_TOOL_TIMEOUT_S = 2.0
_REGION_PICK_TIMEOUT_S = 60
_SPECTACLE_TIMEOUT_S = 120
_GRIM_TIMEOUT_S = 30
_GTK_STEP_TIMEOUT_S = 5.0
_OVERLAY_PICK_TIMEOUT_S = 180
_PREVIEW_CHARS = 120


def _stage_text(text: str) -> None:
    """Put `text` on the clipboard (and PRIMARY) with the backend's tool:
    wl-copy on KDE Wayland, xclip on the XWayland backend (GNOME, sway ...).
    Not wl-copy whenever WAYLAND_DISPLAY is set: mutter has no data-control
    protocol, so wl-copy there steals focus or fails silently, while xclip
    goes through the compositor's X selection bridge (pinned by
    `tests/test_screen_ocr.py::test_ocr_text_is_staged_with_the_backends_clipboard_tool`).

    The PRIMARY change is announced with expect_selection(): no gesture
    is behind it, and the copy-button filter would swallow it otherwise."""
    data = text.encode("utf-8")
    expect_selection(text)
    from platform_backend import detect

    if detect() == "wayland_kde" and shutil.which("wl-copy"):
        for extra in ([], ["--primary"]):
            try:
                subprocess.run(
                    ["wl-copy", *extra], input=data, check=False, timeout=_CLIPBOARD_TOOL_TIMEOUT_S
                )
            except (OSError, subprocess.SubprocessError):
                pass
        return
    if shutil.which("xclip"):
        for sel in ("clipboard", "primary"):
            try:
                subprocess.run(
                    ["xclip", "-selection", sel],
                    input=data,
                    check=False,
                    timeout=_CLIPBOARD_TOOL_TIMEOUT_S,
                )
            except (OSError, subprocess.SubprocessError):
                pass


def _capture_packages() -> list[str]:
    """The region-capture tool that works on this desktop. maim only sees
    X11, so on a Wayland session it would grab XWayland windows and black:
    wlroots compositors get grim + slurp, KDE spectacle, GNOME
    gnome-screenshot (allowed to use GNOME Shell's screenshot API)."""
    if not _wayland():
        return ["maim"]
    desktops = {d.strip().upper() for d in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":")}
    if "KDE" in desktops:
        return ["spectacle"]
    if "GNOME" in desktops:
        return ["gnome-screenshot"]
    return ["grim", "slurp"]


def install_command() -> str:
    """Install command for the capture tool, for when no region-capture
    tool is present."""
    return distro.install_command(_capture_packages())


def _wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def _portal_available() -> bool:
    try:
        from capture import portal_screenshot

        return portal_screenshot.available()
    except Exception:  # noqa: BLE001 - no Gio, no portal
        return False


def _has_capture_tool() -> bool:
    """True if a supported region-capture tool is available: one on PATH
    (maim only outside Wayland, where it sees just XWayland's windows), or
    the desktop portal's Screenshot (GNOME has nothing else) (pinned by
    `tests/test_screen_ocr.py::test_maim_does_not_count_on_wayland`,
    `tests/test_screen_ocr.py::test_the_portal_alone_is_enough`)."""
    return bool(
        shutil.which("spectacle")
        or shutil.which("grim")
        or (shutil.which("maim") and not _wayland())
        or shutil.which("gnome-screenshot")
        or _portal_available()
    )


def is_supported() -> tuple[bool, str]:
    """(ok, reason): the reason is a short label of what's missing, which
    the Settings row pairs with an Install button."""
    if not _has_capture_tool():
        return False, "screen-capture tool not installed"
    try:
        from vino_ai import ocr

        return ocr.available()
    except ImportError:
        return False, "the vino_ai package is missing from this install"


def install_argv() -> list[str] | None:
    """A pkexec argv that installs a capture tool when none is present, or
    None when nothing is missing, the package manager is unknown, or /usr is
    read-only (see core.distro). The OCR engine comes from pip
    (`vinopop[ai]`), not the distro."""
    if _has_capture_tool():
        return None
    return distro.install_argv(_capture_packages())


def _has_image(path: Path) -> bool:
    """The capture target is created empty up front, so existing isn't enough:
    a cancelled capture leaves a zero-byte file behind."""
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _capture_region(out_path: Path) -> bool:
    """Have the first installed region tool write a user-drawn rectangle to
    `out_path` as a PNG, ending with the portal's picker; False if the user
    cancelled or the tool failed (pinned by
    `tests/test_screen_ocr.py::test_region_falls_back_to_the_portal_picker`)."""
    if shutil.which("spectacle"):
        return _capture_with_spectacle(out_path)
    if shutil.which("grim") and shutil.which("slurp"):
        return _capture_with_slurp_and_grim(out_path)
    if shutil.which("maim") and not _wayland():
        return _capture_with_maim(out_path)
    if shutil.which("gnome-screenshot"):
        return _capture_with_gnome_screenshot(out_path)
    return _capture_with_portal(out_path)


def _capture_with_spectacle(out_path: Path) -> bool:
    """KDE's tool: -r region, -b no window, -n no notification, -o file."""
    try:
        subprocess.run(
            ["spectacle", "-r", "-b", "-n", "-o", str(out_path)],
            capture_output=True,
            timeout=_SPECTACLE_TIMEOUT_S,
        )
        return _has_image(out_path)
    except subprocess.TimeoutExpired:
        log.warning("[ocr] spectacle timed out")
        return False


def _capture_with_slurp_and_grim(out_path: Path) -> bool:
    """wlroots compositors (sway, Hyprland): slurp picks, grim captures."""
    try:
        geom = subprocess.run(
            ["slurp"], capture_output=True, timeout=_REGION_PICK_TIMEOUT_S, text=True
        )
        if geom.returncode != 0 or not geom.stdout.strip():
            return False
        res = subprocess.run(
            ["grim", "-g", geom.stdout.strip(), str(out_path)],
            capture_output=True,
            timeout=_GRIM_TIMEOUT_S,
        )
        return res.returncode == 0 and _has_image(out_path)
    except subprocess.TimeoutExpired:
        return False


def _capture_with_maim(out_path: Path) -> bool:
    try:
        res = subprocess.run(
            ["maim", "-s", str(out_path)],
            capture_output=True,
            timeout=_REGION_PICK_TIMEOUT_S,
        )
        if res.returncode != 0:
            log.info("[ocr] maim exited %d (user cancelled?)", res.returncode)
            return False
        return _has_image(out_path)
    except subprocess.TimeoutExpired:
        log.warning("[ocr] maim timed out after 60 s")
        return False


def _capture_with_gnome_screenshot(out_path: Path) -> bool:
    try:
        res = subprocess.run(
            ["gnome-screenshot", "--area", "--file", str(out_path)],
            capture_output=True,
            timeout=_REGION_PICK_TIMEOUT_S,
        )
        return res.returncode == 0 and _has_image(out_path)
    except subprocess.TimeoutExpired:
        return False


def _capture_with_portal(out_path: Path) -> bool:
    shot = _portal_region()
    if shot is None:
        return False
    try:
        shutil.move(shot, out_path)
    except OSError:
        return False
    return _has_image(out_path)


def _portal_region() -> str | None:
    try:
        from capture import portal_screenshot

        return portal_screenshot.screenshot(interactive=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("[ocr] portal screenshot failed: %s", exc)
        return None


def _recognize(image_path: Path) -> str | None:
    """OCR the captured PNG offline. Returns None on failure (logged)."""
    try:
        from vino_ai import ocr

        return ocr.recognize(str(image_path))
    except Exception as exc:  # noqa: BLE001 - reported as "no text"
        log.warning("[ocr] recognition failed: %s", exc)
        return None


def _notify(title: str, body: str, *, icon: str = "dialog-information", ms: int = 3500) -> None:
    from core import actions

    actions.notify(title, body, icon, ms)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _capture_image() -> tuple[Path | None, str | None]:
    """Let the user drag a region and return (png, None); (None, None) when
    they cancelled the overlay, or (None, message) when the fallback tool
    captured nothing. The caller owns (and deletes) the png. The overlay
    selector comes first (no Accept step), the desktop's region tool after."""
    try:
        from capture import ocr_selector

        if ocr_selector.available():
            crop = _capture_via_overlay()
            return (Path(crop) if crop else None), None
    except Exception as exc:  # noqa: BLE001
        log.warning("[ocr] overlay selector failed, using the desktop's region tool: %s", exc)
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
            from core import actions

            res = subprocess.run(
                actions.notify_argv(
                    title,
                    body,
                    "edit-paste-symbolic",
                    10000,
                    extra=("-A", "describe=Describe image", "-w"),
                ),
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


def _on_gtk_thread(fn, timeout_s: float, idle_add=None):
    """Run fn() on the GTK main loop from a worker thread and hand back its
    result, re-raising what it raised (GLib would swallow it, leaving the
    caller waiting). TimeoutError when the loop didn't get to it in time
    (pinned by `tests/test_screen_ocr.py::test_a_frozen_gtk_loop_is_a_timeout_not_a_hang`)."""
    import threading

    if idle_add is None:
        from gi.repository import GLib

        idle_add = GLib.idle_add
    done = threading.Event()
    box: dict = {}

    def _run() -> bool:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            done.set()
        return False

    idle_add(_run)
    if not done.wait(timeout_s):
        raise TimeoutError("the GTK main loop is busy")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _capture_via_overlay(selector=None, idle_add=None) -> str | None:
    """Drive the overlay selector from a worker thread and return the cropped
    PNG path (None if the user cancelled). Only the GTK steps hop to the main
    thread; the screen grab, a tool run that takes a good part of a second,
    stays here so the main loop keeps responding (pinned by
    `tests/test_screen_ocr.py::test_overlay_grabs_on_the_worker_and_shows_on_the_gtk_thread`,
    `tests/test_screen_ocr.py::test_overlay_asks_the_pointer_off_the_gtk_thread_with_several_monitors`).
    `selector` and `idle_add` are for the tests."""
    import threading

    if selector is None:
        from capture import ocr_selector as selector  # type: ignore[no-redef]

    pointer = None
    if _on_gtk_thread(selector.monitor_count, _GTK_STEP_TIMEOUT_S, idle_add) > 1:
        pointer = selector.pointer_hint()
    monitor = _on_gtk_thread(lambda: selector.pick_monitor(pointer), _GTK_STEP_TIMEOUT_S, idle_add)
    shot = selector.grab(monitor)
    if not shot:
        raise RuntimeError("could not grab the screen for the overlay")
    done = threading.Event()
    holder: dict = {}

    def _cb(path) -> None:
        holder["path"] = path
        done.set()

    try:
        _on_gtk_thread(lambda: selector.show(shot, _cb, monitor), _GTK_STEP_TIMEOUT_S, idle_add)
    except Exception:
        _unlink(Path(shot))
        raise
    if not done.wait(_OVERLAY_PICK_TIMEOUT_S):
        return None
    return holder.get("path")


def run_ocr_to_clipboard() -> None:
    """The OCR hotkey and tray entry: capture a region, OCR it and stage the
    text as clipboard and PRIMARY. With `ocr_describe_images` on, the AI
    model can describe the capture: from a notification button when there
    was text, straight away when there wasn't (pinned by
    `tests/test_screen_ocr.py::test_text_goes_to_the_clipboard_and_the_image_is_deleted`,
    `tests/test_screen_ocr.py::test_no_text_without_describe_says_so`,
    `tests/test_screen_ocr.py::test_no_text_with_describe_describes_straight_away`,
    `tests/test_screen_ocr.py::test_text_with_describe_offers_the_button`)."""
    ok_sup, reason = is_supported()
    if not ok_sup:
        _notify("vinoPop OCR", reason, ms=4000)
        return

    image, message = _capture_image()
    if image is None:
        if message:
            _notify("vinoPop OCR", message, ms=4000)
        return
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
        _stage_text(text)
        preview = text.replace("\n", " ")[:_PREVIEW_CHARS]
        body = f"{len(text)} chars on clipboard - “{preview}”"
        if describe:
            _offer_describe("OCR captured", body, image, text)
            kept = True
        else:
            _notify("OCR captured", body, icon="edit-paste-symbolic")
    finally:
        if not kept:
            _unlink(image)
