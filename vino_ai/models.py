"""Model registry and one-time download.

LLMs come pre-converted from the OpenVINO org on Hugging Face, so there is
no conversion step on the user's machine. The default is Gemma 4 E2B
(INT4), the small, fast, low-RAM one; Gemma 4 E4B trades speed and memory
for accuracy. Both are multimodal exports, so the language model is split
out as openvino_language_model.xml.

Models land in $XDG_DATA_HOME/vinopop/models/<name>/ and are reused from
there; nothing is fetched again once a model's folder is complete.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.xdg_paths import DATA_DIR

# The xet transfer backend can hang for good on a multi-GB model with no
# error. Plain HTTPS resumes, and the timeout turns a stall into an
# exception that the retry loop in ensure_llm picks up. Must be set before
# huggingface_hub is imported.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "30")
# The download itself sends nothing extra, but keep any library that calls
# huggingface_hub.send_telemetry quiet too.
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

log = logging.getLogger("vinopop")

MODELS_DIR = DATA_DIR / "models"


@dataclass(frozen=True)
class LlmModel:
    key: str
    repo_id: str
    label: str
    size: str  # rough download size, shown before the first download
    # Pinned commit, so a changed or compromised upstream repo can't swap
    # the files under us. Bump deliberately (Hugging Face "sha"). "main"
    # means not pinned yet.
    revision: str = "main"


LLM_MODELS = {
    m.key: m
    for m in (
        LlmModel(
            "gemma-4-e2b",
            # Re-quantized to channel-wise INT4: the official group-128
            # export never finishes compiling on the NPU.
            "arankays/gemma-4-E2B-it-int4-cw-ov",
            "Gemma 4 E2B (fast, low RAM)",
            "4 GB",
            revision="8eb2f833845d0b56bc701b0ac8fff54c8e8a31d6",
        ),
        LlmModel(
            "gemma-4-e4b",
            "OpenVINO/gemma-4-E4B-it-int4-ov",
            "Gemma 4 E4B (more accurate, more RAM)",
            "5 GB",
        ),
    )
}
DEFAULT_LLM = "gemma-4-e2b"

_download_lock = threading.Lock()


def llm_model() -> LlmModel:
    try:
        from core.settings import get_settings

        key = get_settings().get("ai_llm_model") or DEFAULT_LLM
    except Exception:
        key = DEFAULT_LLM
    return LLM_MODELS.get(key, LLM_MODELS[DEFAULT_LLM])


def model_dir(model: LlmModel) -> Path:
    return MODELS_DIR / model.repo_id.split("/", 1)[1]


def _complete(path: Path) -> bool:
    """True once every file GenAI needs is fully on disk. The small XMLs
    land long before the weights, so the weights' .bin and the absence of
    in-flight .incomplete files are what mean "done"."""
    main_xml = next(
        (
            path / n
            for n in ("openvino_model.xml", "openvino_language_model.xml")
            if (path / n).is_file()
        ),
        None,
    )
    if main_xml is None or not main_xml.with_suffix(".bin").is_file():
        return False
    if not (path / "openvino_tokenizer.xml").is_file():
        return False
    in_flight = path / ".cache" / "huggingface" / "download"
    return not any(in_flight.glob("*.incomplete")) if in_flight.is_dir() else True


def is_downloaded(model: LlmModel) -> bool:
    return _complete(model_dir(model))


_listeners: set[Callable[[str], None]] = set()
_listeners_lock = threading.Lock()
_last_status = ""


def _emit(msg: str) -> None:
    global _last_status
    _last_status = msg
    with _listeners_lock:
        targets = list(_listeners)
    for fn in targets:
        try:
            fn(msg)
        except Exception:  # noqa: BLE001 - a bad listener must not kill the download
            log.exception("[vino_ai] download listener failed")


def _progress_class(label: str):
    """A tqdm subclass that folds every per-file byte bar into one
    "label: 42% (1.3 of 3.1 GB)" status line."""
    from tqdm.auto import tqdm

    bars: set = set()
    state = {"last": 0.0}

    class _Bar(tqdm):
        def __init__(self, *a, **kw):
            kw["disable"] = False
            super().__init__(*a, **kw)
            if self.unit == "B":
                bars.add(self)

        def update(self, n=1):
            out = super().update(n)
            now = time.monotonic()
            if self in bars and now - state["last"] >= 1.0:
                state["last"] = now
                total = sum(b.total or 0 for b in bars)
                done = sum(b.n for b in bars)
                if total:
                    _emit(
                        f"Downloading {label}: {done * 100 // total}% "
                        f"({done / 1e9:.1f} of {total / 1e9:.1f} GB)"
                    )
            return out

    return _Bar


def ensure_llm(
    model: LlmModel, on_status: Callable[[str], None] | None = None, attempts: int = 5
) -> Path:
    """Return the local folder for `model`, downloading it first if needed.
    Blocking; call from a worker thread. `on_status` gets progress lines,
    including when another thread is already doing the download. Stalled
    or dropped transfers resume up to `attempts` times. Raises on failure."""
    path = model_dir(model)
    if _complete(path):
        return path
    if on_status:
        with _listeners_lock:
            _listeners.add(on_status)
        if _last_status and _download_lock.locked():
            on_status(_last_status)
    try:
        with _download_lock:
            if _complete(path):
                return path
            from huggingface_hub import snapshot_download

            log.info("[vino_ai] downloading %s to %s", model.repo_id, path)
            path.mkdir(parents=True, exist_ok=True)
            for attempt in range(1, attempts + 1):
                try:
                    snapshot_download(
                        repo_id=model.repo_id,
                        revision=model.revision,
                        local_dir=str(path),
                        tqdm_class=_progress_class(model.label),
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - network errors vary widely
                    log.warning("[vino_ai] download attempt %d failed: %s", attempt, exc)
                    if attempt == attempts:
                        raise RuntimeError(f"download of {model.label} failed: {exc}") from exc
                    _emit(f"Download interrupted, retrying ({attempt}/{attempts - 1})…")
                    time.sleep(2 * attempt)
            if not _complete(path):
                raise RuntimeError(f"{model.repo_id} downloaded but has no OpenVINO IR")
            log.info("[vino_ai] downloaded %s", model.repo_id)
    finally:
        if on_status:
            with _listeners_lock:
                _listeners.discard(on_status)
    return path


def prefetch_consented(settings) -> bool:
    """True when the user agreed to download the model at startup. A True
    ai_prefetch_model without ai_prefetch_asked is the old default, which
    save() wrote into every settings.json; it still counts when a model is
    already on disk, so installs that had prefetched carry on as before."""
    if not settings.get("ai_prefetch_model"):
        return False
    if settings.get("ai_prefetch_asked"):
        return True
    return any(is_downloaded(m) for m in LLM_MODELS.values())


def prefetch(
    on_done: Callable[[LlmModel, str | None], None] | None = None,
) -> threading.Thread | None:
    """Download the configured LLM in the background so the first AI click
    doesn't wait for it. No-op (returns None) when it's already on disk."""
    model = llm_model()
    if is_downloaded(model):
        return None
    last_pct = {"v": -1}

    def _log_progress(msg: str) -> None:
        pct = msg.split("%")[0].rsplit(" ", 1)[-1]
        if pct.isdigit() and int(pct) // 10 != last_pct["v"]:
            last_pct["v"] = int(pct) // 10
            log.info("[vino_ai] %s", msg)

    def _work() -> None:
        error = None
        try:
            ensure_llm(model, _log_progress)
        except Exception as exc:  # noqa: BLE001 - reported through on_done and the log
            log.exception("[vino_ai] background model download failed")
            error = str(exc)
        if on_done:
            on_done(model, error)

    t = threading.Thread(target=_work, name="vinopop-model-prefetch", daemon=True)
    t.start()
    return t
