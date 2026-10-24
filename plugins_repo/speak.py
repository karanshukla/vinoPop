# vinopop-plugin-version: 3
"""Read the selection aloud, offline, with vinoSpeak.

Kokoro-82M on the NPU through OpenVINO GenAI (GPU or CPU when there's no
NPU). Speech starts after the first sentence and the rest is synthesized as
it plays. Stop on the "Reading aloud" notification, a second click on Read
aloud, or Esc (X11) stops it. The voice
downloads once (~160 MB) on first use; nothing leaves your machine after
that.
"""

from __future__ import annotations

import logging
import subprocess
import threading

import actions
from plugin_base import Plugin

log = logging.getLogger("vinopop")

_TITLE = "Read aloud"
# One click at a time: a second click while the first is still loading
# the voice would otherwise start a second read once it's up.
_busy = threading.Lock()


def _esc_watch(stop):
    """Esc stops the speech while it plays. X11 only, through the popup's
    keymap poll; returns a function that ends the watch."""
    try:
        from gi.repository import GLib

        from ui.popup import _XInputPoll
    except Exception:
        return lambda: None
    holder = {}

    def _start() -> bool:
        poll = _XInputPoll(lambda: None, stop)
        if poll.start():
            holder["poll"] = poll
        return False

    def _end() -> None:
        def _stop() -> bool:
            poll = holder.pop("poll", None)
            if poll is not None:
                poll.stop()
            return False

        GLib.idle_add(_stop)

    GLib.idle_add(_start)
    return _end


def _stop_notification(stop):
    """A "Reading aloud" notification with a Stop button while it plays.
    notify-send prints the notification's id, then the action when Stop is
    clicked; returns a function that takes the notification down."""
    try:
        proc = subprocess.Popen(
            actions.notify_argv(
                _TITLE,
                "Reading aloud…",
                "vinopop-vinospeak",
                0,
                extra=("-A", "stop=Stop", "-p", "-w"),
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return lambda: None
    shown = threading.Event()
    holder = {}

    def _listen() -> None:
        lines = iter(proc.stdout.readline, "")
        holder["id"] = next(lines, "").strip()
        shown.set()
        if any(line.strip() == "stop" for line in lines):
            stop()

    threading.Thread(target=_listen, daemon=True, name="speak-stop-offer").start()

    def _end() -> None:
        shown.wait(timeout=2)
        if holder.get("id", "").isdigit():
            _close_notification(int(holder["id"]))
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    return _end


def _close_notification(notification_id: int) -> None:
    try:
        from gi.repository import Gio, GLib

        Gio.bus_get_sync(Gio.BusType.SESSION, None).call_sync(
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "CloseNotification",
            GLib.Variant("(u)", (notification_id,)),
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
    except Exception as exc:  # noqa: BLE001 - it may already be gone
        log.debug("[speak] couldn't close the notification: %s", exc)


def _read(text: str) -> None:
    from vino_ai import tts

    # The button toggles: a click while it's talking only stops it
    if tts.stop():
        return
    if not text.strip():
        return
    if not _busy.acquire(blocking=False):
        return
    try:
        ok, reason = tts.available()
        if not ok:
            actions.notify(_TITLE, reason, "dialog-error")
            return
        if not tts.loaded():
            # The first load after a driver update compiles for ~45 s on
            # the NPU; without this the click looks like it did nothing.
            actions.notify(_TITLE, "Loading the voice…", "audio-speakers-symbolic")
        try:
            speech = tts.speak(text)
        except Exception as exc:  # noqa: BLE001 - shown to the user, logged in full
            log.exception("[speak] couldn't start")
            actions.notify(_TITLE, f"Couldn't read it aloud: {exc}", "dialog-error")
            return
    finally:
        _busy.release()
    end_watch = _esc_watch(tts.stop)
    end_offer = _stop_notification(tts.stop)
    try:
        speech.wait()
    except Exception as exc:  # noqa: BLE001
        log.exception("[speak] playback failed")
        actions.notify(_TITLE, f"Couldn't read it aloud: {exc}", "dialog-error")
    finally:
        end_offer()
        end_watch()


def register(register_plugin) -> None:
    register_plugin(
        Plugin(
            name="speak",
            icon="vinopop-vinospeak-symbolic",
            tooltip=_TITLE,
            handler=_read,
            priority=45,
        )
    )
