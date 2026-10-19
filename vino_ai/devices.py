"""Which OpenVINO device each model runs on.

The `ai_device` setting is "auto" (default), "NPU", "GPU" or "CPU". In
auto mode the LLM prefers the NPU, since OpenVINO GenAI handles the NPU's
static-shape requirement itself and the NPU is the efficient choice for a
model that sits idle between clicks. OCR prefers the CPU: PP-OCR
detection and recognition take a different input shape for every capture,
which the NPU can't compile for without padding every image to a fixed
size, and which the GPU recompiles for. Measured on a Core Ultra: CPU
0.12-0.53 s per capture, GPU 0.3-1.06 s for each new size (0.08 s only
for a size it has seen). The GPU plugin has also segfaulted in that
compile, taking vinoPop down with it.
"""

from __future__ import annotations

import functools
import logging

from . import telemetry

log = logging.getLogger("vinopop")

LLM_ORDER = ("NPU", "GPU", "CPU")
OCR_ORDER = ("CPU", "GPU")


@functools.lru_cache(maxsize=1)
def available_devices() -> tuple[str, ...]:
    telemetry.prepare()
    import openvino as ov

    devs = tuple(ov.Core().available_devices)
    log.info("[vino_ai] OpenVINO devices: %s", ", ".join(devs) or "none")
    return devs


@functools.lru_cache(maxsize=1)
def _log_npu_notes() -> None:
    """Pass ovfetch's view of the NPU to the log once. Diagnosis only: the
    NPU is still tried, and a failed compile falls back to GPU/CPU."""
    telemetry.prepare()
    import openvino as ov

    from . import ovfetch

    for warning in ovfetch.warnings(ov.__version__):
        log.warning("[vino_ai] %s (see `vinopop --doctor`)", warning)


def _setting() -> str:
    try:
        from core.settings import get_settings

        return str(get_settings().get("ai_device") or "auto").upper()
    except Exception:
        return "AUTO"


def forced() -> str | None:
    """The device the user picked by hand, or None in auto mode. A forced
    device is always tried, even one that failed before."""
    choice = _setting()
    return None if choice == "AUTO" else choice


def candidates(order: tuple[str, ...]) -> list[str]:
    """Devices to try, best first. An explicit setting comes first and the
    rest of the order stays as fallback, so a missing NPU driver degrades
    to GPU/CPU instead of breaking the feature."""
    devs = available_devices()
    # "GPU.0"/"GPU.1" on multi-GPU boxes: match on the family prefix.
    present = [d for d in order if any(x.split(".")[0] == d for x in devs)]
    if "NPU" in present:
        _log_npu_notes()
    choice = _setting()
    if choice != "AUTO" and choice in present:
        present.remove(choice)
        present.insert(0, choice)
    return present or ["CPU"]
