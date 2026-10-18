"""Keep OpenVINO's bundled telemetry out of vinoPop.

`import openvino` loads Intel's `openvino_telemetry` (through ovc). With no
consent file at ~/intel/openvino_telemetry that counts as consent: it
writes a Google Analytics client id to ~/intel/openvino_ga_cid and posts
usage events. vinoPop sends no analytics, so prepare() makes
`openvino_telemetry` unimportable in this process before OpenVINO loads.
ovc then falls back to its own no-op stub
(openvino/tools/ovc/telemetry_stub.py).

Nothing is written to disk, so the consent file stays whatever the user
chose for their other OpenVINO tools, and nothing goes into the
environment of the commands vinoPop launches. (The only environment kill
switch is CI=true, which changes how a lot of tools behave.)
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("vinopop")

_MODULE = "openvino_telemetry"


def prepare() -> None:
    """Call before anything imports openvino, openvino_genai or rapidocr.
    Cheap to repeat."""
    if _MODULE in sys.modules:
        if sys.modules[_MODULE] is not None:
            # Shouldn't happen: every OpenVINO import goes through here.
            log.warning("[vino_ai] %s was loaded before vinoPop could block it", _MODULE)
        return
    # A None entry makes `import openvino_telemetry` raise ImportError.
    sys.modules[_MODULE] = None  # type: ignore[assignment]


def blocked() -> bool:
    """True when this process can't load OpenVINO's telemetry."""
    return _MODULE in sys.modules and sys.modules[_MODULE] is None
