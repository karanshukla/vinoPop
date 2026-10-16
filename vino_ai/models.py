"""Model registry and one-time download.

LLMs come pre-converted from the OpenVINO org on Hugging Face, so there is
no conversion step on the user's machine. The default is Phi-3.5-mini in
symmetric channel-wise INT4 ("-cw-"), the weight layout OpenVINO
recommends for the NPU. The lighter Qwen option suits a box with little
RAM or no NPU.

Models land in $XDG_DATA_HOME/linuxpop/models/<name>/ and are reused from
there; nothing is fetched again once a model's folder is complete.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from xdg_paths import DATA_DIR

log = logging.getLogger("linuxpop")

MODELS_DIR = DATA_DIR / "models"


@dataclass(frozen=True)
class LlmModel:
    key: str
    repo_id: str
    label: str
    size: str  # rough download size, shown before the first download


LLM_MODELS = {
    m.key: m
    for m in (
        LlmModel(
            "phi-3.5-mini",
            "OpenVINO/Phi-3.5-mini-instruct-int4-cw-ov",
            "Phi-3.5 mini (best quality, NPU-friendly)",
            "2.2 GB",
        ),
        LlmModel(
            "qwen2.5-1.5b",
            "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov",
            "Qwen2.5 1.5B (lighter, faster)",
            "1 GB",
        ),
    )
}
DEFAULT_LLM = "phi-3.5-mini"

_download_lock = threading.Lock()


def llm_model() -> LlmModel:
    try:
        from settings import get_settings

        key = get_settings().get("ai_llm_model") or DEFAULT_LLM
    except Exception:
        key = DEFAULT_LLM
    return LLM_MODELS.get(key, LLM_MODELS[DEFAULT_LLM])


def model_dir(model: LlmModel) -> Path:
    return MODELS_DIR / model.repo_id.split("/", 1)[1]


def _complete(path: Path) -> bool:
    # snapshot_download writes the IR pair last-ish; the tokenizer IR is
    # what GenAI needs alongside the model, so check for both.
    return (path / "openvino_model.xml").is_file() and (path / "openvino_tokenizer.xml").is_file()


def is_downloaded(model: LlmModel) -> bool:
    return _complete(model_dir(model))


def ensure_llm(model: LlmModel) -> Path:
    """Return the local folder for `model`, downloading it first if needed.
    Blocking; call from a worker thread. Raises on failure."""
    path = model_dir(model)
    if _complete(path):
        return path
    with _download_lock:
        if _complete(path):
            return path
        from huggingface_hub import snapshot_download

        log.info("[vino_ai] downloading %s to %s", model.repo_id, path)
        path.mkdir(parents=True, exist_ok=True)
        snapshot_download(repo_id=model.repo_id, local_dir=str(path))
        if not _complete(path):
            raise RuntimeError(f"{model.repo_id} downloaded but has no OpenVINO IR")
    return path
