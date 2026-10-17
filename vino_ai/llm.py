"""Local LLM tasks: summarize, rewrite, explain, translate.

One OpenVINO GenAI pipeline is loaded on first use and kept for the life
of the daemon, so only the first click pays the load/compile cost
(compiled blobs are cached under $XDG_CACHE_HOME/vinopop/ov-cache, which
makes later daemon starts fast too). Generation is serialised with a
lock: two clicks in a row queue instead of fighting over the device.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from xdg_paths import CACHE_DIR

from . import devices, models

log = logging.getLogger("vinopop")

# Long selections are cut to keep the prompt inside the NPU's static
# prompt window (MAX_PROMPT_LEN below). ~4 chars per token, with room for
# the instruction and chat template.
MAX_INPUT_CHARS = 8000
MAX_PROMPT_LEN = 3072

_PROMPTS = {
    "summarize": (
        "Summarize the user's text in two to four short sentences. "
        "Reply with the summary only, in the same language as the text."
    ),
    "rewrite": (
        "Rewrite the user's text so it reads clearly and naturally. Keep "
        "its meaning, tone and language. Reply with the rewritten text only."
    ),
    "explain": (
        "Explain what the user's text means in plain language, in a short "
        "paragraph. If it is code, a command or jargon, say what it does."
    ),
    "translate": (
        "Translate the user's text into {lang}. Reply with the "
        "translation only, without notes or quotes."
    ),
}

_MAX_NEW_TOKENS = {"summarize": 200, "rewrite": 600, "explain": 300, "translate": 800}

_lock = threading.Lock()
_pipe = None
_pipe_key: tuple[str, str] | None = None  # (model key, device)


def _load(on_status: Callable[[str], None]):
    global _pipe, _pipe_key
    model = models.llm_model()
    if _pipe is not None and _pipe_key and _pipe_key[0] == model.key:
        return _pipe
    if not models.is_downloaded(model):
        on_status(f"Downloading {model.label} ({model.size}, one time only)…")
    path = models.ensure_llm(model)

    import openvino_genai as ov_genai

    cache = CACHE_DIR / "ov-cache"
    cache.mkdir(parents=True, exist_ok=True)
    errors = []
    for dev in devices.candidates(devices.LLM_ORDER):
        on_status(f"Loading model on {dev}…")
        props: dict[str, object] = {"CACHE_DIR": str(cache)}
        if dev == "NPU":
            props.update(MAX_PROMPT_LEN=MAX_PROMPT_LEN, MIN_RESPONSE_LEN=512)
        try:
            if (path / "openvino_model.xml").is_file():
                pipe = ov_genai.LLMPipeline(str(path), dev, **props)
            else:  # multimodal export (Gemma 4): text-only use of the VLM pipeline
                pipe = ov_genai.VLMPipeline(str(path), dev, **props)
        except Exception as exc:  # noqa: BLE001 - try the next device
            log.warning("[vino_ai] LLM on %s failed: %s", dev, exc)
            errors.append(f"{dev}: {exc}")
            continue
        log.info("[vino_ai] LLM %s loaded on %s", model.repo_id, dev)
        _pipe, _pipe_key = pipe, (model.key, dev)
        return pipe
    raise RuntimeError("could not load the model: " + "; ".join(errors))


def device() -> str | None:
    """Device the loaded model runs on, or None before first use."""
    return _pipe_key[1] if _pipe_key else None


def run(
    task: str,
    text: str,
    *,
    lang: str = "English",
    on_token: Callable[[str], None] | None = None,
    on_status: Callable[[str], None] | None = None,
) -> str:
    """Run `task` on `text` and return the full answer. Blocking; call it
    from a worker thread. `on_token` receives text as it streams,
    `on_status` gets progress lines (download / load) worth showing."""
    if task not in _PROMPTS:
        raise ValueError(f"unknown task {task!r}")
    text = text.strip()[:MAX_INPUT_CHARS]
    status = on_status or (lambda _m: None)
    with _lock:
        pipe = _load(status)
        import openvino_genai as ov_genai

        tok = pipe.get_tokenizer()
        history = [
            {"role": "system", "content": _PROMPTS[task].format(lang=lang)},
            {"role": "user", "content": text},
        ]
        prompt = tok.apply_chat_template(history, add_generation_prompt=True)

        cfg = pipe.get_generation_config()
        cfg.max_new_tokens = _MAX_NEW_TOKENS[task]
        cfg.do_sample = False
        if task == "translate" and "\n\n" not in text:
            # Phi-3.5 appends "(Note: ...)" disclaimers after a blank line
            # however the prompt forbids them.
            cfg.stop_strings = {"\n\n"}
        if hasattr(cfg, "apply_chat_template"):
            cfg.apply_chat_template = False  # already applied above

        running = getattr(getattr(ov_genai, "StreamingStatus", None), "RUNNING", False)

        def _stream(chunk: str):
            if on_token is not None:
                on_token(chunk)
            return running

        result = pipe.generate(prompt, cfg, _stream if on_token else None)
    return str(result).strip()
