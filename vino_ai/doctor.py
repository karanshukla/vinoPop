"""`vinopop --doctor`: what the offline AI needs from this machine.

Modelled on vinowhisper-doctor. Reads, never changes anything, and needs
no GTK, so it also works over SSH and in a bug report (`--json`). OpenVINO's
telemetry is blocked before openvino is imported to read its version (see
telemetry.py).

The JSON rows follow vinoOpenEffects' doctor (shared/src/doctor/types.rs):
{group, name, status, detail, fix}, with `label` kept as a copy of `name`
and the status strings unchanged for older readers (the settings Debug page).
Every warn and FAIL row carries a fix.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import npu_preflight

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "FAIL", "??"


@dataclass
class Result:
    status: str
    label: str
    detail: str = ""
    fix: str = ""
    group: str = ""

    @property
    def name(self) -> str:
        return self.label

    def as_dict(self) -> dict[str, str | None]:
        return {
            "group": self.group,
            "name": self.label,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix or None,
            "label": self.label,
        }


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
        ("vino_speak", "vinospeak", "read aloud"),
    ):
        version = _version(module)
        if version is None and module == "vino_speak":
            # One optional button, newer than the rest of the stack: an
            # install from before it isn't broken
            results.append(
                Result(
                    WARN,
                    label,
                    f"missing, needed for {needed_for}",
                    "pip install --user -r requirements-ai.txt",
                )
            )
        elif version is None:
            results.append(
                Result(
                    FAIL,
                    label,
                    f"missing, needed for {needed_for}",
                    "pip install --user -r requirements-ai.txt",
                )
            )
        else:
            results.append(Result(OK, label, version))
    return results


def _consent_file(home: Path | None = None) -> str:
    """What the user told OpenVINO's own tools, from the consent file they
    read (~/intel/openvino_telemetry: "1" in, "0" out). Read, never written."""
    path = (home or Path.home()) / "intel" / "openvino_telemetry"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable"
    return {"0": "opted out", "1": "opted in"}.get(value, f"unrecognised ({value!r})")


def _telemetry() -> list[Result]:
    from . import telemetry

    if importlib.util.find_spec("openvino") is None:
        return [Result(OK, "openvino telemetry", "openvino not installed")]
    telemetry.prepare()
    if telemetry.blocked():
        results = [Result(OK, "openvino telemetry", "blocked: vinoPop never loads it")]
    else:
        results = [
            Result(
                WARN,
                "openvino telemetry",
                "loaded before vinoPop could block it",
                "report it: something imported openvino_telemetry ahead of vino_ai.telemetry",
            )
        ]
    state = _consent_file()
    detail = f"~/intel/openvino_telemetry {state}; only OpenVINO's own tools read it"
    if state == "opted out":
        results.append(Result(OK, "telemetry opt-out", detail))
    else:
        # vinoPop blocks the module either way, so this is info, not a warn.
        if state == "absent":
            detail += ", and they take no file as consent"
        results.append(
            Result(
                UNKNOWN,
                "telemetry opt-out",
                detail,
                "opt_in_out --opt_out (from openvino-telemetry), or "
                "mkdir -p ~/intel && echo 0 > ~/intel/openvino_telemetry",
            )
        )
    return results


def _ov_devices() -> tuple[str, ...] | None:
    """What OpenVINO enumerates, or None without openvino."""
    if importlib.util.find_spec("openvino") is None:
        return None
    from . import devices

    return devices.available_devices()


def _devices() -> list[Result]:
    if importlib.util.find_spec("openvino") is None:
        return []
    from . import devices

    try:
        found = devices.available_devices()
    except Exception as exc:  # noqa: BLE001
        return [
            Result(
                FAIL,
                "devices",
                str(exc),
                "pip install --user --force-reinstall -r requirements-ai.txt",
            )
        ]
    llm = devices.candidates(devices.LLM_ORDER)
    ocr = devices.candidates(devices.OCR_ORDER)
    return [
        Result(OK, "devices", ", ".join(found) or "none"),
        Result(OK, "llm device order", " > ".join(llm)),
        Result(OK, "ocr device order", " > ".join(ocr)),
    ]


_NOTE_STATUS = {True: OK, None: UNKNOWN, False: WARN}


def _npu(root: Path = npu_preflight.ROOT, env: dict[str, str] | None = None) -> list[Result]:
    """Whether OpenVINO lists the NPU, and the preflight that says why not.
    Problems are warns, not FAILs: the LLM falls back to the GPU or CPU."""
    try:
        found = _ov_devices()
    except Exception:  # noqa: BLE001 - _devices reports it
        found = None
    preflight = npu_preflight.npu_preflight(root)
    has_npu = preflight[0].ok is True  # the PCI device
    notes = preflight + (npu_preflight.npu_userspace(root, env) if has_npu else [])
    problems = [n for n in notes if n.ok is False]

    if found is None:
        results = [Result(UNKNOWN, "npu", "openvino not installed, so not asked")]
    elif any(d.startswith("NPU") for d in found):
        results = [Result(OK, "npu", "enumerated by OpenVINO")]
    elif not has_npu:
        # The preflight's only note says the same thing.
        return [Result(UNKNOWN, "npu", "none on the PCI bus; the LLM uses the GPU or CPU")]
    else:
        fix = (
            problems[0].fix
            if problems
            else npu_preflight.driver_fix(npu_preflight.distro_family(root))
        )
        results = [
            Result(WARN, "npu", "not enumerated by OpenVINO; the LLM will use the GPU or CPU", fix)
        ]
    return results + [Result(_NOTE_STATUS[n.ok], n.label, n.detail, n.fix) for n in notes]


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
        return [
            Result(
                WARN,
                "ovfetch",
                "`ovfetch detect` gave no usable output",
                f"run `ovfetch detect` to see why, or update it from {ovfetch.INSTALL_HINT}",
            )
        ]
    # Its bounds are pessimistic on purpose, so nothing it says is a FAIL.
    return [
        Result(
            OK if n.ok else (UNKNOWN if n.ok is None else WARN),
            n.label,
            n.detail,
            "" if n.ok else "`ovfetch detect` shows the OpenVINO range Intel records for it",
        )
        for n in ovfetch.notes(status, _version("openvino"))
    ]


def _models() -> list[Result]:
    from . import models

    model = models.llm_model()
    path = models.model_dir(model)
    if models.is_downloaded(model):
        results = [Result(OK, "llm model", f"{model.label} at {path}")]
    elif models.downloaded_revision(path):
        results = [
            Result(
                WARN,
                "llm model",
                f"{model.label} at {path} is an older download",
                "none needed: the next AI action replaces it",
            )
        ]
    elif path.is_dir():
        results = [
            Result(
                UNKNOWN,
                "llm model",
                f"{model.label} at {path} has no download markers; the next AI action "
                "keeps it if every file matches, else downloads it again",
            )
        ]
    else:
        return [
            Result(
                UNKNOWN,
                "llm model",
                f"{model.label} not downloaded yet; the first AI action fetches it ({model.size})",
            )
        ]
    return results + [_model_files(model, path)]


def _tts_model() -> list[Result]:
    """vinoSpeak's Kokoro download, which it pins and hashes itself. Read
    aloud uses vinoSpeak's default directory, shared with its CLI."""
    if importlib.util.find_spec("vino_speak") is None:
        return []
    from vino_speak import model as kokoro

    path = kokoro.default_model_dir()
    if not path.is_dir():
        return [
            Result(
                UNKNOWN,
                "tts model",
                f"{kokoro.MODEL_ID} not downloaded yet; the first Read aloud fetches it (~160 MB)",
            )
        ]
    if not kokoro.model_is_ready(path):
        return [
            Result(
                UNKNOWN,
                "tts model",
                f"{kokoro.MODEL_ID} at {path} is a partial download; the next Read aloud finishes it",
            )
        ]
    problems = kokoro.verify(path)
    if problems:
        return [
            Result(
                FAIL,
                "tts model",
                f"{kokoro.MODEL_ID} at {path}: {problems[0]}",
                f"rm -rf {path}, and the next Read aloud downloads it again",
            )
        ]
    return [Result(OK, "tts model", f"{kokoro.MODEL_ID} at {path}, every file matches its pin")]


def _model_files(model, path) -> Result:
    """Every file's sha256 against the pinned digests. Slow on a 4 GB model,
    which is why only the doctor and a fresh download do it."""
    from . import integrity, models

    revision = models.downloaded_revision(path) or model.revision
    result = integrity.verify(path, model.repo_id, revision, model.revision)
    status = {
        integrity.VERIFIED: OK,
        integrity.UNPINNED: UNKNOWN,
        integrity.DRIFT: WARN,
    }.get(result.status, FAIL)
    if status == WARN:
        fix = "none needed: the next AI action downloads the pinned revision"
    elif status == FAIL:
        fix = f"rm -rf {path}, and the next AI action downloads it again"
    else:
        fix = ""
    return Result(status, "llm model files", result.summary(), fix)


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
                "OpenVINO or model update",
                f"set ai_device to {dev} to retry now, or rm {failures.PATH}",
            )
        )
    return results


# ---- input -------------------------------------------------------------------


def _which(tool: str) -> str:
    return shutil.which(tool) or ""


def _install(package: str, family: str) -> str:
    return npu_preflight.install_command(package, family)


def _xtest() -> bool | None:
    """Whether the X server has XTEST; None without a display to ask."""
    if not os.environ.get("DISPLAY"):
        return None
    try:
        dpy = importlib.import_module("Xlib.display").Display()
    except Exception:  # noqa: BLE001
        return None
    try:
        return bool(dpy.has_extension("XTEST"))
    finally:
        dpy.close()


def _key_tools(backend: str, family: str) -> list[Result]:
    """ydotool, ydotoold and wtype: the KDE Wayland backend's fallbacks after
    the RemoteDesktop portal. The XWayland backend uses none of them, so
    there they're listed for bug reports only."""
    keyinject = importlib.import_module("platform_backend.keyinject")
    ydotool, wtype, sock = _which("ydotool"), _which("wtype"), keyinject.ydotool_socket()
    if backend != "wayland_kde":
        unused = "not used by the XWayland backend"
        return [
            Result(UNKNOWN, "ydotool", f"{ydotool or 'not installed'}; {unused}"),
            Result(UNKNOWN, "ydotoold socket", f"{sock or 'none'}; {unused}"),
            Result(UNKNOWN, "wtype", f"{wtype or 'not installed'}; {unused}"),
        ]
    results = []
    if ydotool:
        results.append(Result(OK, "ydotool", ydotool))
    else:
        results.append(
            Result(
                UNKNOWN,
                "ydotool",
                "not installed (optional): the fallback when the portal isn't allowed",
                _install("ydotool", family),
            )
        )
    if sock:
        results.append(Result(OK, "ydotoold socket", sock))
    else:
        results.append(
            Result(
                UNKNOWN,
                "ydotoold socket",
                "none at $YDOTOOL_SOCKET, $XDG_RUNTIME_DIR/.ydotool_socket or "
                "/tmp/.ydotool_socket, so ydotool can't send keys",
                "systemctl --user enable --now ydotoold "
                "(`rpm -ql ydotool | grep service` names your package's unit)",
            )
        )
    if keyinject.on_kwin():
        results.append(
            Result(UNKNOWN, "wtype", f"{wtype or 'not installed'}; KWin can't use it, so unused")
        )
    elif wtype:
        results.append(Result(OK, "wtype", wtype))
    else:
        results.append(Result(UNKNOWN, "wtype", "not installed", _install("wtype", family)))
    return results


def _x11_input(family: str) -> list[Result]:
    """Paste/Cut and the clipboard picker's typing go over the X server's
    XTEST extension (python-xlib), which Xorg and XWayland both ship."""
    xtest = _xtest()
    if xtest:
        results = [Result(OK, "key injection", "XTEST")]
    elif xtest is None:
        results = [
            Result(
                UNKNOWN,
                "key injection",
                "no X display to ask about XTEST",
                "run the doctor inside the desktop session",
            )
        ]
    else:
        results = [
            Result(
                WARN,
                "key injection",
                "none: the X server has no XTEST, so Paste/Cut only copy",
                "XTEST is built into Xorg and XWayland; check the server's config for a disabled extension",
            )
        ]
    return results + _key_tools("x11", family)


def _input() -> list[Result]:
    """Key injection for the backend vinoPop would pick, and the tools behind it."""
    # import_module keeps mypy from following platform_backend into the
    # untyped GTK/X11 modules it can reach.
    if importlib.import_module("platform_backend").detect() == "wayland_kde":
        return _wayland_input()
    return _x11_input(npu_preflight.distro_family())


def _wayland_input() -> list[Result]:
    """Key injection (Paste/Cut) and /dev/input access with the KDE Wayland backend."""
    if importlib.import_module("platform_backend").detect() != "wayland_kde":
        return []
    evdev_input = importlib.import_module("platform_backend.evdev_input")
    keyinject = importlib.import_module("platform_backend.keyinject")
    family = npu_preflight.distro_family()

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
        hint = "none: Paste/Cut only copy"
        if portal is None:
            hint += " (couldn't ask the portal: no gi or session bus)"
        results = [
            Result(
                WARN,
                "key injection",
                hint,
                _install("xdg-desktop-portal-kde", family)
                + " for the RemoteDesktop portal, or install ydotool and start ydotoold",
            )
        ]
    results += _key_tools("wayland_kde", family)
    group_fix = "sudo usermod -aG input $USER, then log out and back in"
    try:
        kbd, ptr, denied = evdev_input.access_summary()
    except OSError as exc:
        return results + [Result(UNKNOWN, "input devices", str(exc), group_fix)]
    detail = f"{kbd} keyboard(s), {ptr} pointer(s) readable"
    if kbd and ptr:
        results.append(Result(OK, "input devices", detail))
    else:
        if denied:
            detail += f"; {denied} not readable"
        results.append(
            Result(WARN, "input devices", detail + "; modifier+double-click needs both", group_fix)
        )
    return results


def _grouped(group: str, rows: list[Result]) -> list[Result]:
    for row in rows:
        row.group = row.group or group
    return rows


def collect() -> list[Result]:
    try:
        from core.version import __version__
    except Exception:  # noqa: BLE001
        __version__ = "unknown"
    py = sys.version_info
    results = _grouped(
        "system",
        [
            Result(OK, "vinopop", __version__),
            Result(OK, "python", f"{py.major}.{py.minor}.{py.micro}"),
        ],
    )
    results += _grouped("packages", _packages())
    results += _grouped("privacy", _telemetry())
    results += _grouped("devices", _devices())
    results += _grouped("npu", _npu() + _ovfetch() + _failures())
    results += _grouped("model", _models() + _tts_model())
    results += _grouped("input", _input())
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
        print(json.dumps({"checks": [r.as_dict() for r in results], "failed": failed}, indent=2))
        return 1 if failed else 0
    names = [f"{r.group}: {r.label}" for r in results]
    width = max(len(n) for n in names)
    for r, name in zip(results, names, strict=True):
        print(f"  [{r.status:>4}] {name:<{width}}  {r.detail}")
        if r.fix and r.status != OK:
            print(f"  {'':6} {'fix:':<{width}}  {r.fix}")
    tally = {s: sum(r.status == s for r in results) for s in (OK, WARN, UNKNOWN)}
    print(f"\n{tally[OK]} ok, {tally[WARN]} warn, {tally[UNKNOWN]} unknown, {len(failed)} failed")
    return 1 if failed else 0
