"""`vinopop --doctor`: what the offline AI needs from this machine.

Modelled on vinowhisper-doctor. Reads, never changes anything, and needs
no GTK, so it also works over SSH and in a bug report (`--json`). OpenVINO's
telemetry is blocked before openvino is imported to read its version (see
telemetry.py).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import shutil
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

    if importlib.util.find_spec("openvino") is None:
        return [Result(OK, "openvino telemetry", "openvino not installed")]
    telemetry.prepare()
    if telemetry.blocked():
        return [Result(OK, "openvino telemetry", "blocked: vinoPop never loads it")]
    return [Result(WARN, "openvino telemetry", "loaded before vinoPop could block it")]


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


def _wayland_input() -> list[Result]:
    """Key injection (Paste/Cut) and /dev/input access with the KDE Wayland backend."""
    # import_module keeps mypy from following platform_backend into the
    # untyped GTK/X11 modules it can reach.
    if importlib.import_module("platform_backend").detect() != "wayland_kde":
        return []
    evdev_input = importlib.import_module("platform_backend.evdev_input")
    keyinject = importlib.import_module("platform_backend.keyinject")

    portal = keyinject.portal_keyboard_available()
    ydotool = keyinject.ydotool_usable()
    used = []
    if portal:
        used.append("RemoteDesktop portal (keysyms; asks once)")
    if ydotool:
        used.append(f"ydotool via {keyinject.ydotool_socket()}")
    elif shutil.which("ydotool"):
        used.append("ydotool installed but ydotoold isn't running (no socket)")
    if keyinject.wtype_usable():
        used.append("wtype")
    if portal or ydotool or keyinject.wtype_usable():
        results = [Result(OK, "key injection", "; ".join(used))]
    else:
        hint = (
            "none: Paste/Cut only copy. Needs xdg-desktop-portal-kde (RemoteDesktop), "
            "or ydotool with ydotoold running"
        )
        if portal is None:
            hint += " (couldn't ask the portal: no gi or session bus)"
        results = [Result(WARN, "key injection", hint)]
    try:
        kbd, ptr, denied = evdev_input.access_summary()
    except OSError as exc:
        return results + [Result(UNKNOWN, "input devices", str(exc))]
    detail = f"{kbd} keyboard(s), {ptr} pointer(s) readable"
    if kbd and ptr:
        results.append(Result(OK, "input devices", detail))
    else:
        if denied:
            detail += f"; {denied} not readable: add yourself to the 'input' group"
        results.append(Result(WARN, "input devices", detail + "; modifier+double-click needs both"))
    return results


def collect() -> list[Result]:
    try:
        from core.version import __version__
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
    results += _wayland_input()
    return results


def main(as_json: bool = False) -> int:
    # A diagnostic must not create files (or print "[settings] wrote defaults"
    # into --json output on a fresh machine).
    try:
        from core import settings

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
