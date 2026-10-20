"""Screen OCR on OpenVINO: PP-OCR detection + recognition via RapidOCR.

RapidOCR's wheel bundles the PP-OCR ONNX models, so OCR needs no download.
Its OpenVINO engine always compiles for "CPU"; `_DeviceCore` swaps in our
device choice (the CPU unless `ai_device` says otherwise, see
devices.OCR_ORDER) and
points the compiled-blob cache at our cache dir.

The engine is dropped once it has been idle for `ai_idle_unload_minutes`,
like the LLM: the CPU plugin holds ~800MB of buffers after the first
inference, and a reload comes from the compiled-blob cache.
"""

from __future__ import annotations

import ctypes
import gc
import logging
import threading
import time

from core.xdg_paths import CACHE_DIR

from . import devices, telemetry
from .llm import IDLE_UNLOAD_MINUTES, _setting

log = logging.getLogger("vinopop")

# Its own folder: the LLM prunes its siblings under ov-cache/llm.
_CACHE = CACHE_DIR / "ov-cache" / "ocr"

_lock = threading.Lock()
_engine = None
_device = None
# Devices that loaded but then failed to run, skipped for this session.
_broken: set[str] = set()
_last_used = 0.0
_idle_timer: threading.Timer | None = None


def _patch_device(dev: str) -> None:
    telemetry.prepare()
    from openvino import Core
    from rapidocr.inference_engine.openvino import main as ov_main

    cache = str(_CACHE)

    class _DeviceCore(Core):
        def compile_model(self, model, device_name="CPU", config=None):  # noqa: ARG002
            props = dict(config or {})
            props.setdefault("CACHE_DIR", cache)
            return super().compile_model(model, dev, props)

    ov_main.Core = _DeviceCore


def _load():
    global _engine, _device
    if _engine is not None:
        return _engine
    telemetry.prepare()
    from rapidocr import EngineType, RapidOCR

    _CACHE.mkdir(parents=True, exist_ok=True)
    params = {f"{stage}.engine_type": EngineType.OPENVINO for stage in ("Det", "Cls", "Rec")}
    params["Global.log_level"] = "warning"
    errors = []
    for dev in devices.candidates(devices.OCR_ORDER):
        if dev in _broken:
            continue
        try:
            _patch_device(dev)
            engine = RapidOCR(params=params)
        except Exception as exc:  # noqa: BLE001 - try the next device
            log.warning("[vino_ai] OCR on %s failed: %s", dev, exc)
            errors.append(f"{dev}: {exc}")
            continue
        log.info("[vino_ai] OCR loaded on %s", dev)
        _engine, _device = engine, dev
        return engine
    raise RuntimeError("could not load OCR: " + "; ".join(errors))


def device():
    return _device


def _trim_heap() -> None:
    """Hand freed heap pages back to the OS. glibc otherwise keeps them in
    its arenas after a big free; there is no malloc_trim off glibc."""
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def unload_if_idle() -> bool:
    """Free the engine once it has sat unused for the idle timeout.
    Skipped while a capture holds the lock; returns whether it unloaded."""
    global _engine, _device
    minutes = _setting("ai_idle_unload_minutes", IDLE_UNLOAD_MINUTES)
    if minutes <= 0 or _engine is None:
        return False
    if not _lock.acquire(blocking=False):
        return False
    try:
        if _engine is None or time.monotonic() - _last_used < minutes * 60:
            return False
        log.info("[vino_ai] OCR idle for %.0f min, unloading from %s", minutes, _device)
        _engine, _device = None, None
        gc.collect()
        _trim_heap()
        return True
    finally:
        _lock.release()


def _schedule_unload() -> None:
    global _idle_timer
    minutes = _setting("ai_idle_unload_minutes", IDLE_UNLOAD_MINUTES)
    if _idle_timer is not None:
        _idle_timer.cancel()
        _idle_timer = None
    if minutes <= 0:
        return
    _idle_timer = threading.Timer(minutes * 60 + 1, unload_if_idle)
    _idle_timer.daemon = True
    _idle_timer.start()


def _reading_order(boxes, texts) -> str:
    """Join detected lines top-to-bottom, merging boxes that sit on the
    same visual row (left-to-right) so a multi-column UI line doesn't come
    out as separate lines."""
    items = []
    for box, txt in zip(boxes, texts, strict=True):
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        items.append((min(ys), max(ys), min(xs), txt))
    items.sort(key=lambda it: (it[0], it[2]))
    rows: list[list] = []
    for top, bottom, left, txt in items:
        mid = (top + bottom) / 2
        if rows:
            r_top, r_bottom = rows[-1][0], rows[-1][1]
            if r_top <= mid <= r_bottom:
                rows[-1][2].append((left, txt))
                continue
        rows.append([top, bottom, [(left, txt)]])
    return "\n".join(" ".join(t for _x, t in sorted(r[2])) for r in rows)


def recognize(image_path: str) -> str:
    """OCR an image file and return its text ("" when nothing was found).
    Blocking; call it from a worker thread. Arms the idle unload afterwards,
    even when it failed, since a fallback engine may still be loaded."""
    global _engine, _device, _last_used
    try:
        with _lock:
            try:
                engine = _load()
                try:
                    result = engine(str(image_path))
                except Exception as exc:  # noqa: BLE001 - retried on the next device
                    # A device can compile fine and still fail at inference (GPU
                    # driver bugs); drop it and run this capture on the next one.
                    log.warning("[vino_ai] OCR on %s failed while running: %s", _device, exc)
                    if _device is not None:
                        _broken.add(_device)
                    _engine, _device = None, None
                    result = _load()(str(image_path))
            finally:
                _last_used = time.monotonic()
    finally:
        _schedule_unload()
    if result is None or not getattr(result, "txts", None):
        return ""
    return _reading_order(result.boxes, result.txts).strip()


def available() -> tuple[bool, str]:
    """(ok, reason) for the OCR engine's Python packages."""
    import importlib.util

    if importlib.util.find_spec("openvino") is None or importlib.util.find_spec("rapidocr") is None:
        return False, "OCR engine not installed (pip install --user -r requirements-ai.txt)"
    return True, ""
