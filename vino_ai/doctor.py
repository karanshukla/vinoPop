"""`vinopop --doctor`: what the offline AI needs from this machine.

Modelled on vinowhisper-doctor. Reads, never changes anything, and needs
no GTK, so it also works over SSH and in a bug report (`--json`). The one
write is OpenVINO's telemetry consent file, if there isn't one yet: without
it, importing openvino to read its version would phone home and update
~/intel/stats (see telemetry.py).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import asdict, dataclass

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "FAIL", "??"


@dataclass
class Result:
    status: str
    label: str
    detail: str = ""


def _version(module: str) -> str | None:
    if importlib.util.find_spec(module) is None:
        return None
    from . import telemetry

    telemetry.prepare()
    try:
        mod = __import__(module)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return f"import failed: {exc}"
    return str(getattr(mod, "__version__", "installed"))


def _packages() -> list[Result]:
    results = []
    for module, label, needed_for in (
        ("openvino", "openvino", "every AI action"),
        ("openvino_genai", "openvino-genai", "summarize, rewrite, explain, translate"),
        ("rapidocr", "rapidocr", "screen OCR"),
        ("huggingface_hub", "huggingface_hub", "the one-time model download"),
    ):
        version = _version(module)
        if version is None:
            results.append(
                Result(
                    FAIL,
                    label,
                    f"missing, needed for {needed_for}; pip install --user -r requirements-ai.txt",
                )
            )
        else:
            results.append(Result(OK, label, version))
    return results


def _telemetry() -> list[Result]:
    from . import telemetry

    path = telemetry.consent_path()
    on = telemetry.read_consent()
    if on is None:
        if importlib.util.find_spec("openvino") is None:
            return [Result(OK, "openvino telemetry", "openvino not installed")]
        return [Result(WARN, "openvino telemetry", f"ENABLED: no consent file at {path}")]
    if on:
        return [
            Result(
                WARN,
                "openvino telemetry",
                f"ENABLED ({path}); turn it off in Settings > Advanced > Privacy",
            )
        ]
    return [Result(OK, "openvino telemetry", f"opted out ({path})")]


def _devices() -> list[Result]:
    if importlib.util.find_spec("openvino") is None:
        return []
    from . import devices

    try:
        found = devices.available_devices()
    except Exception as exc:  # noqa: BLE001
        return [Result(FAIL, "devices", str(exc))]
    results = [Result(OK, "devices", ", ".join(found) or "none")]
    if not any(d.startswith("NPU") for d in found):
        results.append(
            Result(WARN, "npu", "not enumerated by OpenVINO; the LLM will use the GPU or CPU")
        )
    llm = devices.candidates(devices.LLM_ORDER)
    ocr = devices.candidates(devices.OCR_ORDER)
    results.append(Result(OK, "llm device order", " > ".join(llm)))
    results.append(Result(OK, "ocr device order", " > ".join(ocr)))
    return results


def _ovfetch() -> list[Result]:
    from . import ovfetch

    if not ovfetch.installed():
        return [
            Result(
                UNKNOWN,
                "ovfetch",
                f"not installed; {ovfetch.INSTALL_HINT} adds Intel's per-platform NPU driver data",
            )
        ]
    status = ovfetch.detect()
    if status is None:
        return [Result(WARN, "ovfetch", "`ovfetch detect` gave no usable output")]
    # Its bounds are pessimistic on purpose, so nothing it says is a FAIL.
    return [
        Result(OK if n.ok else (UNKNOWN if n.ok is None else WARN), n.label, n.detail)
        for n in ovfetch.notes(status, _version("openvino"))
    ]


def _models() -> list[Result]:
    from . import models

    model = models.llm_model()
    if models.is_downloaded(model):
        return [Result(OK, "llm model", f"{model.label} at {models.model_dir(model)}")]
    return [
        Result(
            UNKNOWN,
            "llm model",
            f"{model.label} not downloaded yet; the first AI action fetches it ({model.size})",
        )
    ]


def _failures() -> list[Result]:
    from . import failures, models

    model = models.llm_model()
    fp = failures.fingerprint(model.key, model.revision)
    results = []
    for dev, rec in sorted(failures.all_records().items()):
        if failures.blocking(dev, fp) is None:
            continue
        results.append(
            Result(
                WARN,
                f"{dev.lower()} skipped",
                f"failed on {rec.get('failed_at')}: {rec.get('error')}. Retried after a driver, "
                f"OpenVINO or model update; set ai_device to {dev} to retry now, or delete "
                f"{failures.PATH}",
            )
        )
    return results


def collect() -> list[Result]:
    try:
        from version import __version__
    except Exception:  # noqa: BLE001
        __version__ = "unknown"
    py = sys.version_info
    results = [
        Result(OK, "vinopop", __version__),
        Result(OK, "python", f"{py.major}.{py.minor}.{py.micro}"),
    ]
    results += _packages()
    results += _telemetry()
    results += _devices()
    results += _ovfetch()
    results += _models()
    results += _failures()
    return results


def main(as_json: bool = False) -> int:
    # A diagnostic must not create files (or print "[settings] wrote defaults"
    # into --json output on a fresh machine).
    try:
        import settings

        settings.write_defaults = False
    except Exception:  # noqa: BLE001
        pass
    results = collect()
    failed = [r.label for r in results if r.status == FAIL]
    if as_json:
        print(json.dumps({"checks": [asdict(r) for r in results], "failed": failed}, indent=2))
        return 1 if failed else 0
    width = max(len(r.label) for r in results)
    for r in results:
        print(f"  [{r.status:>4}] {r.label:<{width}}  {r.detail}")
    tally = {s: sum(r.status == s for r in results) for s in (OK, WARN, UNKNOWN)}
    print(f"\n{tally[OK]} ok, {tally[WARN]} warn, {tally[UNKNOWN]} unknown, {len(failed)} failed")
    return 1 if failed else 0
