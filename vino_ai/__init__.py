"""Offline AI for vinoPop, on OpenVINO.

Everything here runs on the user's own machine: OCR (PP-OCR through
RapidOCR's OpenVINO engine) and a small instruction-tuned LLM (OpenVINO
GenAI) for summarize / rewrite / explain / translate. The only network
access is the one-time model download from Hugging Face; after that the
models load from disk.

Heavy imports (openvino, openvino_genai, rapidocr) are deferred to first
use so the popup starts as fast as it did before, and so a box without
the Python packages still runs every non-AI plugin.
"""

from __future__ import annotations

import importlib.util


def available() -> tuple[bool, str]:
    """(ok, reason). ok=False means the OpenVINO packages are missing; the
    reason is a short label for the Settings row / a notification."""
    missing = [m for m in ("openvino", "openvino_genai") if importlib.util.find_spec(m) is None]
    if missing:
        return False, "OpenVINO not installed (pip install --user -r requirements-ai.txt)"
    return True, ""
