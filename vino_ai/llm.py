"""Local LLM tasks: summarize, rewrite, explain, translate.

One OpenVINO GenAI pipeline is loaded on first use and kept for the life
of the daemon, so only the first click pays the load/compile cost
(compiled blobs are cached under $XDG_CACHE_HOME/vinopop/ov-cache, which
makes later daemon starts fast too). Generation is serialised with a
lock: two clicks in a row queue instead of fighting over the device.
"""

from __future__ import annotations

import logging
import subprocess
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
# Seconds to wait for one device before trying the next. A first NPU
# compile of a big model can take minutes; it carries on in the background
# (see _construct) instead of holding the click hostage.
LOAD_TIMEOUT = 120.0

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
_late: tuple[object, tuple[str, str]] | None = None  # finished after its timeout


def _load_timeout() -> float:
    try:
        from settings import get_settings

        return float(get_settings().get("ai_load_timeout_seconds") or LOAD_TIMEOUT)
    except Exception:
        return LOAD_TIMEOUT


def _construct(cls, path, dev: str, props: dict, key: tuple[str, str], timeout: float):
    """Build the pipeline on a helper thread and stop waiting after
    `timeout` seconds. A native compile can't be cancelled, so a slow one
    keeps going: it fills the compile cache either way, and on success it
    is parked in _late and swapped in on the next run."""
    state: dict[str, object] = {}
    guard = threading.Lock()
    finished = threading.Event()

    def work():
        global _late
        try:
            state["pipe"] = cls(str(path), dev, **props)
        except Exception as exc:  # noqa: BLE001 - re-raised in the caller
            state["error"] = exc
        with guard:
            finished.set()
            if state.get("abandoned") and "pipe" in state:
                log.info("[vino_ai] LLM finished compiling on %s, using it from the next run", dev)
                _late = (state["pipe"], key)

    threading.Thread(target=work, name=f"vino-ai-load-{dev}", daemon=True).start()
    finished.wait(timeout)
    with guard:
        if not finished.is_set():
            state["abandoned"] = True
            raise TimeoutError(
                f"still compiling after {timeout:.0f}s (first run only), finishing in the background"
            )
    if "error" in state:
        raise state["error"]  # type: ignore[misc]
    return state["pipe"]


def _load(on_status: Callable[[str], None]):
    global _pipe, _pipe_key, _late
    model = models.llm_model()
    if _late is not None:
        late, _late = _late, None
        if late[1][0] == model.key:
            log.info("[vino_ai] LLM moved to %s", late[1][1])
            _pipe, _pipe_key = late
            return _pipe
    if _pipe is not None and _pipe_key and _pipe_key[0] == model.key:
        return _pipe
    if not models.is_downloaded(model):
        on_status(f"Downloading {model.label} ({model.size}, one time only)…")
    path = models.ensure_llm(model, on_status)

    import openvino_genai as ov_genai

    cache = CACHE_DIR / "ov-cache"
    cache.mkdir(parents=True, exist_ok=True)
    errors = []
    timeout = _load_timeout()
    for dev in devices.candidates(devices.LLM_ORDER):
        on_status(f"Loading model on {dev}…")
        props: dict[str, object] = {"CACHE_DIR": str(cache)}
        if dev == "NPU":
            props.update(MAX_PROMPT_LEN=MAX_PROMPT_LEN, MIN_RESPONSE_LEN=512)
        try:
            if (path / "openvino_model.xml").is_file():
                cls = ov_genai.LLMPipeline
            else:  # multimodal export (Gemma 4): text-only use of the VLM pipeline
                cls = ov_genai.VLMPipeline
            pipe = _construct(cls, path, dev, props, (model.key, dev), timeout)
        except Exception as exc:  # noqa: BLE001 - try the next device
            log.warning("[vino_ai] LLM on %s failed: %s", dev, exc)
            errors.append(f"{dev}: {exc}")
            continue
        log.info("[vino_ai] LLM %s loaded on %s", model.repo_id, dev)
        if errors and errors[0].startswith("NPU:"):
            _warn_fallback(dev, errors[0], on_status)
        _pipe, _pipe_key = pipe, (model.key, dev)
        return pipe
    raise RuntimeError("could not load the model: " + "; ".join(errors))


def _warn_fallback(dev: str, npu_error: str, on_status: Callable[[str], None]) -> None:
    """The NPU is the point of vinoPop: say so loudly when the model ended
    up somewhere slower and hotter."""
    if "still compiling" in npu_error:
        msg = f"The NPU is still compiling the model, so it's running on {dev} until that's done."
    else:
        msg = f"The NPU couldn't load the model, so it's running on {dev} (slower, warmer)."
    log.warning("[vino_ai] %s %s", msg, npu_error)
    on_status(msg)
    try:
        subprocess.run(
            [
                "notify-send",
                "-i",
                "vinopop",
                "vinoPop AI is not on the NPU",
                f"{msg}\n{npu_error}"[:300],
            ],
            check=False,
        )
    except FileNotFoundError:
        pass


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
        if device() == "NPU":
            # The NPU decoder emits NaNs on a single-token step
            # (openvino#35222); forcing a second token avoids it.
            cfg.min_new_tokens = 2
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

        # VLMPipeline (Gemma 4) takes these by name only, and rejects
        # streamer=None.
        extra = {"streamer": _stream} if on_token else {}
        result = pipe.generate(prompt, generation_config=cfg, **extra)
    return str(result).strip()
