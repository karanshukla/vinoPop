"""Remember devices that couldn't load the LLM, so a broken NPU doesn't
cost every daemon start a full load timeout.

A record only holds for the fingerprint it was made under: a new NPU
driver, OpenVINO release or model build may fix the failure, so any
change there retries the device. Modelled on vinoOpenEffects'
npu_failure.rs; the NPU-reset check comes from vinowhisper/failures.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from xdg_paths import STATE_DIR

log = logging.getLogger("vinopop")

PATH = STATE_DIR / "device-failures.json"
DRIVER_LIB = "libze_intel_npu.so.1"
_LIB_DIRS = ("/usr/lib64", "/usr/lib/x86_64-linux-gnu", "/usr/lib", "/usr/local/lib")
# Level Zero's code when the NPU resets under any of its users: every
# compiled model on it is gone, the device itself is fine.
_DEVICE_LOST = "ZE_RESULT_ERROR_DEVICE_LOST"
# A compile that never finished in this many daemon runs counts as broken.
TIMEOUTS_TO_SKIP = 2
_ERROR_LIMIT = 300
_LOCATION = re.compile(r"^Exception from .*:\d+:$")


def is_device_lost(exc: BaseException) -> bool:
    return _DEVICE_LOST in str(exc)


def driver_version(lib_dirs: tuple[str, ...] = _LIB_DIRS) -> str | None:
    """The user-space NPU driver version, from the file its soname links to
    (libze_intel_npu.so.1 -> libze_intel_npu.so.1.35.0)."""
    for d in lib_dirs:
        link = Path(d) / DRIVER_LIB
        try:
            target = os.path.basename(os.path.realpath(link)) if link.exists() else ""
        except OSError:
            continue
        prefix = "libze_intel_npu.so."
        if target.startswith(prefix) and target != DRIVER_LIB:
            return target[len(prefix) :] or None
    return None


def fingerprint(model_key: str, revision: str) -> dict[str, str | None]:
    try:
        import openvino

        runtime = str(openvino.__version__)
    except Exception:  # noqa: BLE001 - unknown runtime still fingerprints
        runtime = None
    return {
        "driver": driver_version(),
        "openvino": runtime,
        "model": model_key,
        "revision": revision,
    }


def _summary(error: str) -> str:
    lines = [ln.strip() for ln in error.splitlines() if ln.strip()]
    kept = [ln for ln in lines if not _LOCATION.match(ln)] or lines
    return " ".join(kept)[:_ERROR_LIMIT]


def _read(path: Path | None) -> dict[str, dict]:
    try:
        raw = json.loads((path or PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}


def _write(data: dict[str, dict], path: Path | None) -> None:
    path = path or PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("[vino_ai] couldn't save device failures: %s", exc)


def blocking(device: str, fp: dict, path: Path | None = None) -> dict | None:
    """The record that should keep `device` from being tried, if any."""
    rec = _read(path).get(device)
    if not rec or rec.get("fingerprint") != fp:
        return None
    if rec.get("kind") == "error" or int(rec.get("timeouts", 0)) >= TIMEOUTS_TO_SKIP:
        return rec
    return None


def record_error(device: str, fp: dict, error: str, path: Path | None = None) -> None:
    data = _read(path)
    data[device] = {
        "kind": "error",
        "fingerprint": fp,
        "error": _summary(error),
        "failed_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    _write(data, path)


def record_timeout(device: str, fp: dict, path: Path | None = None) -> None:
    """A compile outlived the load timeout. Counted per fingerprint and
    cleared by forget() if it finishes, so only a compile that never
    completes across daemon runs ends up skipped."""
    data = _read(path)
    rec = data.get(device)
    timeouts = int(rec.get("timeouts", 0)) if rec and rec.get("fingerprint") == fp else 0
    data[device] = {
        "kind": "timeout",
        "fingerprint": fp,
        "timeouts": timeouts + 1,
        "error": "compile never finished",
        "failed_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    _write(data, path)


def forget(device: str, path: Path | None = None) -> None:
    data = _read(path)
    if data.pop(device, None) is not None:
        _write(data, path)


def all_records(path: Path | None = None) -> dict[str, dict]:
    return _read(path)
