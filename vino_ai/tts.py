"""Read text aloud with vinoSpeak (Kokoro-82M on OpenVINO GenAI).

One Synthesizer is compiled on first use and reused, like the LLM pipeline
in llm.py, and freed after `ai_idle_unload_minutes` without speech. It goes
on the NPU when there is one, for battery: 1.4 J per second of audio against
6.4 J on the CPU (Wildcat Lake, NPU driver 1.32.0, 2026-10-17; vinoPop#45).
GPU and CPU are the fallback when there's no NPU or it fails to compile, in
the order devices.candidates() gives.

The model is vinoSpeak's own pinned, hash-checked download under
~/.cache/vinospeak/, shared with the vinospeak CLI. Passing a model_dir
would make vinoSpeak treat it as user-supplied and skip the pin checks.

Nothing here imports vino_speak or OpenVINO until the first speak().
"""

from __future__ import annotations

import functools
import gc
import importlib.util
import logging
import shutil
import subprocess
import threading
import time
from typing import Any

from . import devices, telemetry

log = logging.getLogger("vinopop")

ORDER = ("NPU", "GPU", "CPU")
IDLE_UNLOAD_MINUTES = 5.0
# Speech is synthesized a sentence or so at a time, so length costs nothing
# up front; this only stops a select-all from talking for an hour.
MAX_CHARS = 20000

_lock = threading.Lock()  # guards _synth, _speech, _idle_timer
_synth: Any = None
_speech: Any = None
_last_used = 0.0
_idle_timer: threading.Timer | None = None


def available() -> tuple[bool, str]:
    """(ok, reason), like vino_ai.available(): ok=False when vinoSpeak or a
    player is missing; the reason fits in a notification."""
    if importlib.util.find_spec("vino_speak") is None:
        return False, "vinoSpeak isn't installed (pip install --user -r requirements-ai.txt)"
    from vino_speak.playback import PLAYERS

    if player() is None:
        return False, f"No audio player found ({', '.join(PLAYERS)})"
    return True, ""


def _pw_play_streams(path: str) -> bool:
    """Whether this pw-play takes raw samples on stdin (--raw), which
    vinoSpeak streams to. PipeWire 1.0 (Ubuntu 24.04) has no --raw, so
    there it exits at once and nothing is heard."""
    try:
        out = subprocess.run([path, "--help"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return "--raw" in out.stdout + out.stderr


@functools.lru_cache(maxsize=1)
def player() -> str | None:
    """vinoSpeak's player order (pw-play, paplay, aplay), minus a pw-play
    too old to stream."""
    from vino_speak.playback import PLAYERS

    for name in PLAYERS:
        path = shutil.which(name)
        if path and (name != "pw-play" or _pw_play_streams(path)):
            return path
    return None


def _idle_minutes() -> float:
    try:
        from core.settings import get_settings

        return float(get_settings().get("ai_idle_unload_minutes") or 0)
    except Exception:
        return IDLE_UNLOAD_MINUTES


def device() -> str | None:
    """Device the loaded voice runs on, or None while unloaded."""
    synth = _synth
    return getattr(synth, "device", None) if synth is not None else None


def _load() -> Any:
    """A Synthesizer on the best device that compiles. Raises the last
    error when none does."""
    telemetry.prepare()
    from vino_speak import Synthesizer

    error: Exception | None = None
    for dev in devices.candidates(ORDER):
        started = time.monotonic()
        try:
            synth = Synthesizer(dev)
        except Exception as exc:  # noqa: BLE001 - try the next device
            log.warning("[vino_ai] TTS didn't load on %s: %s", dev, exc)
            error = exc
            continue
        log.info("[vino_ai] TTS loaded on %s in %.1f s", dev, time.monotonic() - started)
        return synth
    raise error or RuntimeError("no OpenVINO device to run the voice on")


def loaded() -> bool:
    return _synth is not None


def speak(text: str) -> Any:
    """Start reading `text` aloud and return the playing vino_speak Speech
    (wait() it to see errors). Stops whatever was playing first. Blocks for
    the first load, which compiles the model (~1 s from the driver cache,
    ~45 s on the NPU after a driver update)."""
    global _synth, _speech, _last_used
    text = " ".join(text.split())[:MAX_CHARS]
    if not text:
        raise ValueError("nothing to read")
    stop()
    with _lock:
        if _synth is None:
            _synth = _load()
        from vino_speak import Speech

        # Synthesizer.speak() with our choice of player
        _speech = Speech(_synth.stream(text), player())
        _last_used = time.monotonic()
        speech = _speech
    _schedule_unload()
    return speech


def speaking() -> bool:
    speech = _speech
    return speech is not None and not speech.done


def stop() -> bool:
    """Cut off what's playing. Returns whether anything was."""
    global _speech, _last_used
    with _lock:
        speech, _speech = _speech, None
        if speech is None:
            return False
        was_playing = not speech.done
        speech.stop()
        _last_used = time.monotonic()
    return was_playing


def unload_if_idle() -> bool:
    """Free the voice once nothing has played for the idle timeout."""
    global _synth, _speech, _last_used
    minutes = _idle_minutes()
    with _lock:
        if minutes <= 0 or _synth is None:
            return False
        if _speech is not None:
            if not _speech.done:
                return False
            _speech = None
            _last_used = time.monotonic()
        if time.monotonic() - _last_used < minutes * 60:
            return False
        log.info("[vino_ai] TTS idle for %.0f min, unloading from %s", minutes, device())
        _synth, _speech = None, None
    gc.collect()
    return True


def _schedule_unload() -> None:
    global _idle_timer
    minutes = _idle_minutes()
    with _lock:
        if _idle_timer is not None:
            _idle_timer.cancel()
            _idle_timer = None
        if minutes <= 0:
            return
        _idle_timer = threading.Timer(minutes * 60 + 1, _unload_or_wait)
        _idle_timer.daemon = True
        _idle_timer.start()


def _unload_or_wait() -> None:
    # Still talking when the timer fires (a long passage): look again later
    if not unload_if_idle() and _synth is not None:
        _schedule_unload()
