"""Local LLM tasks: summarize, rewrite, explain, translate.

One OpenVINO GenAI pipeline is loaded on first use and kept until it has
been idle for `ai_idle_unload_minutes` (compiled blobs are cached under
$XDG_CACHE_HOME/vinopop/ov-cache, so a reload is quick). Generation is
serialised with a lock: two clicks in a row queue instead of fighting
over the device.
"""

from __future__ import annotations

import gc
import logging
import subprocess
import threading
import time
from collections.abc import Callable

from xdg_paths import CACHE_DIR

from . import devices, failures, models, telemetry

log = logging.getLogger("vinopop")

# Long selections are cut to keep the prompt inside the NPU's static
# prompt window (MAX_PROMPT_LEN). The char cap is a cheap first cut; the
# token count decides, since CJK runs well under 4 chars per token.
MAX_INPUT_CHARS = 8000
MAX_PROMPT_LEN = 3072
# Room for what the pipeline adds around the templated prompt (measured
# ~40 tokens on Gemma 4's VLM pipeline).
_PROMPT_MARGIN = 96
# Seconds to wait for one device before trying the next. A first NPU
# compile of a big model can take minutes; it carries on in the background
# (see _construct) instead of holding the click hostage.
LOAD_TIMEOUT = 120.0
IDLE_UNLOAD_MINUTES = 5.0

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
# Pipelines that finished compiling after their timeout, by device.
_late: dict[str, tuple[object, tuple[str, str]]] = {}
# Devices whose abandoned compile is still running: never start a second.
_inflight: set[str] = set()
_state_lock = threading.Lock()  # guards _late and _inflight
_last_used = 0.0
_idle_timer: threading.Timer | None = None


def _setting(key: str, default: float) -> float:
    try:
        from settings import get_settings

        value = get_settings().get(key)
        return default if value is None else float(value)
    except Exception:
        return default


def _load_timeout() -> float:
    return _setting("ai_load_timeout_seconds", LOAD_TIMEOUT) or LOAD_TIMEOUT


def _construct(cls, path, dev: str, props: dict, key: tuple[str, str], timeout: float, fp: dict):
    """Build the pipeline on a helper thread and stop waiting after
    `timeout` seconds. A native compile can't be cancelled, so a slow one
    keeps going: it fills the compile cache either way, and on success it
    is parked in _late and swapped in on the next run."""
    state: dict[str, object] = {}
    guard = threading.Lock()
    finished = threading.Event()

    def work():
        try:
            state["pipe"] = cls(str(path), dev, **props)
        except Exception as exc:  # noqa: BLE001 - re-raised in the caller
            state["error"] = exc
        with guard:
            finished.set()
            if not state.get("abandoned"):
                return
            with _state_lock:
                _inflight.discard(dev)
                if "pipe" in state:
                    log.info(
                        "[vino_ai] LLM finished compiling on %s, using it from the next run", dev
                    )
                    _late[dev] = (state["pipe"], key)
            if "pipe" in state:
                failures.forget(dev)
            else:
                log.warning(
                    "[vino_ai] LLM background compile on %s failed: %s", dev, state["error"]
                )
                failures.record_error(dev, fp, str(state["error"]))

    threading.Thread(target=work, name=f"vino-ai-load-{dev}", daemon=True).start()
    finished.wait(timeout)
    with guard:
        if not finished.is_set():
            state["abandoned"] = True
            with _state_lock:
                _inflight.add(dev)
            failures.record_timeout(dev, fp)
            raise TimeoutError(
                f"still compiling after {timeout:.0f}s (first run only), finishing in the background"
            )
    if "error" in state:
        raise state["error"]  # type: ignore[misc]
    return state["pipe"]


def _adopt_late(model_key: str, order: list[str]) -> None:
    """Swap in the best pipeline that finished after its timeout, if it
    beats the one in use; drop the rest so their memory goes back."""
    global _pipe, _pipe_key
    with _state_lock:
        late = [v for v in _late.values() if v[1][0] == model_key]
        _late.clear()
    if not late:
        return

    def rank(dev: str) -> int:
        return order.index(dev) if dev in order else len(order)

    best = min(late, key=lambda v: rank(v[1][1]))
    key = _pipe_key
    current = rank(key[1]) if _pipe is not None and key and key[0] == model_key else len(order) + 1
    if rank(best[1][1]) < current:
        log.info("[vino_ai] LLM moved to %s", best[1][1])
        _pipe, _pipe_key = best
    del late, best
    gc.collect()


def _load(on_status: Callable[[str], None]):
    global _pipe, _pipe_key
    model = models.llm_model()
    order = devices.candidates(devices.LLM_ORDER)
    _adopt_late(model.key, order)
    if _pipe is not None and _pipe_key and _pipe_key[0] == model.key:
        return _pipe
    if not models.is_downloaded(model):
        on_status(f"Downloading {model.label} ({model.size}, one time only)…")
    path = models.ensure_llm(model, on_status)

    telemetry.prepare()
    import openvino_genai as ov_genai

    cache = CACHE_DIR / "ov-cache"
    cache.mkdir(parents=True, exist_ok=True)
    errors = []
    timeout = _load_timeout()
    fp = failures.fingerprint(model.key, model.revision)
    forced = devices.forced()
    for dev in order:
        with _state_lock:
            busy = dev in _inflight
        if busy:
            errors.append(f"{dev}: still compiling in the background")
            continue
        rec = failures.blocking(dev, fp) if dev != forced else None
        if rec is not None:
            log.info(
                "[vino_ai] skipping %s, it failed before on this driver/runtime: %s",
                dev,
                rec.get("error"),
            )
            errors.append(f"{dev}: skipped, failed before ({rec.get('error')})")
            continue
        on_status(f"Loading model on {dev}…")
        props: dict[str, object] = {"CACHE_DIR": str(cache)}
        if dev == "NPU":
            props.update(MAX_PROMPT_LEN=MAX_PROMPT_LEN, MIN_RESPONSE_LEN=512)
        try:
            if (path / "openvino_model.xml").is_file():
                cls = ov_genai.LLMPipeline
            else:  # multimodal export (Gemma 4): text-only use of the VLM pipeline
                cls = ov_genai.VLMPipeline
            pipe = _construct(cls, path, dev, props, (model.key, dev), timeout, fp)
        except Exception as exc:  # noqa: BLE001 - try the next device
            log.warning("[vino_ai] LLM on %s failed: %s", dev, exc)
            if not isinstance(exc, TimeoutError) and not failures.is_device_lost(exc):
                failures.record_error(dev, fp, str(exc))
            errors.append(f"{dev}: {exc}")
            continue
        log.info("[vino_ai] LLM %s loaded on %s", model.repo_id, dev)
        failures.forget(dev)
        if errors and errors[0].startswith("NPU:") and "failed before" not in errors[0]:
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


def _drop_pipe() -> None:
    global _pipe, _pipe_key
    _pipe, _pipe_key = None, None
    gc.collect()


def unload_if_idle() -> bool:
    """Free the pipeline once it has sat unused for the idle timeout.
    Skipped while a run holds the lock; returns whether it unloaded."""
    minutes = _setting("ai_idle_unload_minutes", IDLE_UNLOAD_MINUTES)
    if minutes <= 0 or _pipe is None:
        return False
    if not _lock.acquire(blocking=False):
        return False
    try:
        if _pipe is None or time.monotonic() - _last_used < minutes * 60:
            return False
        log.info("[vino_ai] LLM idle for %.0f min, unloading from %s", minutes, device())
        _drop_pipe()
        with _state_lock:
            _late.clear()
        return True
    finally:
        _lock.release()


def _schedule_unload() -> None:
    global _idle_timer
    minutes = _setting("ai_idle_unload_minutes", IDLE_UNLOAD_MINUTES)
    if _idle_timer is not None:
        _idle_timer.cancel()
        _idle_timer = None
    if minutes <= 0:
        return
    _idle_timer = threading.Timer(minutes * 60 + 1, unload_if_idle)
    _idle_timer.daemon = True
    _idle_timer.start()


def _count_tokens(tok, s: str) -> int | None:
    try:
        return len(tok.encode(s).input_ids.data[0])
    except Exception:  # noqa: BLE001 - tokenizer without encode(): skip the check
        return None


def _fit_prompt(tok, task: str, text: str, lang: str) -> tuple[str, str]:
    """The templated prompt and the (possibly shortened) text, cut until
    the prompt fits MAX_PROMPT_LEN tokens."""
    limit = MAX_PROMPT_LEN - _PROMPT_MARGIN
    for _ in range(4):
        history = [
            {"role": "system", "content": _PROMPTS[task].format(lang=lang)},
            {"role": "user", "content": text},
        ]
        prompt = tok.apply_chat_template(history, add_generation_prompt=True)
        n = _count_tokens(tok, prompt)
        if n is None or n <= limit or not text:
            return prompt, text
        keep = int(len(text) * limit / n * 0.95)
        log.info("[vino_ai] prompt is %d tokens, cutting the text to %d chars", n, keep)
        text = text[:keep]
    return prompt, text


def _generate(pipe, task: str, text: str, lang: str, on_token: Callable[[str], None] | None):
    telemetry.prepare()
    import openvino_genai as ov_genai

    tok = pipe.get_tokenizer()
    prompt, text = _fit_prompt(tok, task, text, lang)

    cfg = pipe.get_generation_config()
    cfg.max_new_tokens = _MAX_NEW_TOKENS[task]
    cfg.do_sample = False
    if device() == "NPU":
        # The NPU decoder emits NaNs on a single-token step
        # (openvino#35222); forcing a second token avoids it.
        cfg.min_new_tokens = 2
    if task == "translate" and "\n\n" not in text:
        # Small models append "(Note: ...)" disclaimers after a blank line
        # however the prompt forbids them.
        cfg.stop_strings = {"\n\n"}
    if hasattr(cfg, "apply_chat_template"):
        cfg.apply_chat_template = False  # already applied above

    running = getattr(getattr(ov_genai, "StreamingStatus", None), "RUNNING", False)

    # VLMPipeline (Gemma 4) takes these by name only, and rejects
    # streamer=None.
    extra = {}
    if on_token is not None:
        sink = on_token

        def _stream(chunk: str):
            sink(chunk)
            return running

        extra["streamer"] = _stream
    return pipe.generate(prompt, generation_config=cfg, **extra)


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
    global _last_used
    streamed = False

    def _sink(chunk: str) -> None:
        nonlocal streamed
        streamed = True
        if on_token is not None:
            on_token(chunk)

    with _lock:
        try:
            for attempt in range(2):
                pipe = _load(status)
                try:
                    result = _generate(pipe, task, text, lang, _sink if on_token else None)
                    break
                except Exception as exc:
                    if not failures.is_device_lost(exc):
                        raise
                    # Another app's fault can reset the NPU under us; every
                    # compiled model on it is gone, so reload and retry once.
                    log.warning("[vino_ai] %s was reset (device lost), reloading", device())
                    pipe = None
                    _drop_pipe()
                    if streamed or attempt:
                        raise
                    status("The NPU was reset, reloading the model…")
        finally:
            _last_used = time.monotonic()
    _schedule_unload()
    return str(result).strip()
