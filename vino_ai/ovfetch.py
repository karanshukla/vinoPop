"""NPU diagnosis through ovfetch, the vino apps' version authority.

ovfetch pins the OpenVINO release the vino apps share (vinoPop's `ai`
extra matches it), so apps on one NPU never run mismatched runtimes. Its
`detect` also reports the OpenVINO range Intel records as working with
this machine's NPU driver. Like vinoWhisper's doctor, vinoPop only
reports that range: the bounds are pessimistic on purpose ("untested",
not "known to fail"), so nothing here stops the NPU from being tried, and
a real compile failure still falls back to GPU/CPU in llm.py.
"""

from __future__ import annotations

import functools
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("linuxpop")

BINARY = "ovfetch"
INSTALL_HINT = "https://github.com/karanshukla/ovfetch/releases"
_TIMEOUT_S = 10


@dataclass(frozen=True)
class Note:
    ok: bool | None  # None: nothing to judge by
    label: str
    detail: str


def _minor(version: str | None) -> tuple[int, int] | None:
    if not version:
        return None
    parts = version.split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None


def _dotted(v: tuple[int, int]) -> str:
    return f"{v[0]}.{v[1]}"


def installed() -> bool:
    return shutil.which(BINARY) is not None


@functools.lru_cache(maxsize=1)
def detect() -> dict[str, Any] | None:
    """`ovfetch detect` as a dict, or None when ovfetch isn't installed or
    its output is unusable (logged, never raised: this is advisory)."""
    exe = shutil.which(BINARY)
    if exe is None:
        return None
    try:
        done = subprocess.run(
            [exe, "detect"], capture_output=True, text=True, timeout=_TIMEOUT_S, check=False
        )
        payload = json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        log.warning("[vino_ai] ovfetch detect failed: %s", exc)
        return None
    return payload if isinstance(payload, dict) else None


def notes(status: dict[str, Any], openvino_version: str | None) -> list[Note]:
    """What ovfetch's data says about this machine's NPU and OpenVINO."""
    if "openvino_ceiling" not in status:
        return [Note(None, "ovfetch", "this ovfetch predates platform data in `detect`")]
    if not any(d.get("kind") == "npu" for d in status.get("devices", [])):
        return [Note(True, "ovfetch", "no Intel NPU, so no driver bounds apply")]

    driver = status.get("npu_driver") or "none"
    result = [
        Note(
            True,
            "npu: platform",
            f"{p.get('codename') or 'unnamed'} ({p.get('pci_id')}), "
            f"Intel verifies driver {p.get('min_npu_driver') or '?'} and newer",
        )
        for p in status.get("platforms", [])
    ]
    result += [Note(False, "npu: ovfetch", str(w)) for w in status.get("warnings", [])]

    installed_v = _minor(openvino_version)
    if installed_v is None:
        return result
    ceiling = _minor(status.get("openvino_ceiling"))
    floors = [_minor(p.get("min_openvino")) for p in status.get("platforms", [])]
    floor = max((f for f in floors if f is not None), default=None)
    if ceiling is None:
        result.append(
            Note(
                None,
                "npu: openvino range",
                f"no OpenVINO is recorded as working with NPU driver {driver}",
            )
        )
    elif installed_v > ceiling:
        result.append(
            Note(
                False,
                "npu: openvino range",
                f"OpenVINO {_dotted(installed_v)} is newer than {_dotted(ceiling)}, the newest "
                f"recorded as working with NPU driver {driver}. Untested rather than known "
                "to fail; if NPU compiles fail, suspect this first.",
            )
        )
    elif floor is not None and installed_v < floor:
        result.append(
            Note(
                False,
                "npu: openvino range",
                f"OpenVINO {_dotted(installed_v)} is older than {_dotted(floor)}, the oldest "
                "recorded as working on this platform",
            )
        )
    else:
        low = f"{_dotted(floor)} to " if floor else "up to "
        result.append(
            Note(
                True,
                "npu: openvino range",
                f"OpenVINO {_dotted(installed_v)} is inside {low}{_dotted(ceiling)}, "
                f"recorded as working with NPU driver {driver}",
            )
        )
    return result


def warnings(openvino_version: str | None) -> list[str]:
    """ovfetch's concerns, for the daemon log. Empty without ovfetch."""
    status = detect()
    if not status:
        return []
    return [f"{n.label}: {n.detail}" for n in notes(status, openvino_version) if n.ok is False]
