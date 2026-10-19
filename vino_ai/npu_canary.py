"""Check the NPU isn't stuck before the LLM loads onto it.

After heavy NPU use (seen after a day of model quantisation) the intel_vpu
driver can get into a state where a second OpenVINO Core in a process
breaks the NPU requests of the first: their next inference segfaults
inside libze_loader. OpenVINO GenAI's VLMPipeline makes such a Core on
every generate() (its cdpruner kernel builder), so in that state every
Gemma 4 action takes vinoPop down. Reloading the driver clears it; so
does a reboot.

The probe recreates the trigger in a throwaway process: a tiny model on
the NPU, a second Core listing devices, then the model again. About a
second, and only run before a load onto the NPU.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys

log = logging.getLogger("vinopop")

RELOAD = "sudo modprobe -r intel_vpu && sudo modprobe intel_vpu"
STUCK = f"driver stuck, reload it with: {RELOAD}"
_TIMEOUT_S = 60

# openvino_telemetry is blocked the way telemetry.prepare() does it.
_PROBE = """
import sys
sys.modules["openvino_telemetry"] = None
import numpy as np
import openvino as ov
import openvino.opset13 as op

x = op.parameter([1, 64], np.float32)
w = op.constant(np.ones((64, 16), np.float32))
model = ov.Model([op.relu(op.matmul(x, w, False, False))], [x])
request = ov.Core().compile_model(model, "NPU").create_infer_request()
data = np.ones((1, 64), np.float32)
request.infer([data])
ov.Core().available_devices
request.infer([data])
"""


def stuck() -> bool:
    """True when the probe segfaulted. A probe that fails any other way
    says nothing about this state, so the NPU is still tried."""
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _PROBE],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("[vino_ai] NPU probe didn't run: %s", exc)
        return False
    if proc.returncode == -signal.SIGSEGV:
        log.warning("[vino_ai] NPU probe segfaulted: %s", STUCK)
        return True
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-1:] or ["no output"]
        log.info("[vino_ai] NPU probe failed (%s), trying the NPU anyway", tail[0])
    return False
