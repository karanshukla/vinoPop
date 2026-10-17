"""Screen OCR on OpenVINO: PP-OCR detection + recognition via RapidOCR.

RapidOCR's wheel bundles the PP-OCR ONNX models, so OCR needs no download.
Its OpenVINO engine always compiles for "CPU"; `_DeviceCore` swaps in our
device choice (the iGPU when there is one, see devices.OCR_ORDER) and
points the compiled-blob cache at our cache dir.
"""

from __future__ import annotations

import logging
import threading

from xdg_paths import CACHE_DIR

from . import devices, telemetry

log = logging.getLogger("vinopop")

_lock = threading.Lock()
_engine = None
_device = None
# Devices that loaded but then failed to run, skipped for this session.
_broken: set[str] = set()


def _patch_device(dev: str) -> None:
    telemetry.prepare()
    from openvino import Core
    from rapidocr.inference_engine.openvino import main as ov_main

    cache = str(CACHE_DIR / "ov-cache")

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

    (CACHE_DIR / "ov-cache").mkdir(parents=True, exist_ok=True)
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
    Blocking; call it from a worker thread."""
    global _engine, _device
    with _lock:
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
    if result is None or not getattr(result, "txts", None):
        return ""
    return _reading_order(result.boxes, result.txts).strip()


def available() -> tuple[bool, str]:
    """(ok, reason) for the OCR engine's Python packages."""
    import importlib.util

    if importlib.util.find_spec("openvino") is None or importlib.util.find_spec("rapidocr") is None:
        return False, "OCR engine not installed (pip install --user -r requirements-ai.txt)"
    return True, ""
