"""Local LLM tasks: summarize, rewrite, explain, translate.

One OpenVINO GenAI pipeline is loaded on first use and kept until it has
been idle for `ai_idle_unload_minutes` (compiled blobs are cached under
$XDG_CACHE_HOME/vinopop/ov-cache, so a reload is quick). Generation is
serialised with a lock: two clicks in a row queue instead of fighting
over the device.
"""

from __future__ import annotations

import gc
import json
import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.xdg_paths import CACHE_DIR

from . import devices, failures, models, npu_canary, telemetry

log = logging.getLogger("vinopop")

# Long selections are cut to keep the prompt inside the NPU's static
# prompt window (MAX_PROMPT_LEN). The char cap is a cheap first cut; the
# token count decides, since CJK runs well under 4 chars per token.
MAX_INPUT_CHARS = 8000
MAX_PROMPT_LEN = 3072
MIN_RESPONSE_LEN = 512
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

# Alt text for a screen capture. The checklist is what got faces, art
# style and mood in (a bare "write alt text" skipped them), and handing
# over RapidOCR's text is what got captions quoted: the model alone
# reads text only when it's asked to transcribe.
_DESCRIBE_PROMPT = (
    "Write alt text for this image for a screen reader user, in two or three sentences. "
    "Cover: the main subject and what it is doing, any face and its expression, the setting, "
    "the art style if it is a drawing, and anything that makes the image funny or notable. "
    "If the image has text, quote it exactly.\n"
    "{ocr}\n"
    "Output only the alt text."
)
_DESCRIBE_MAX_NEW = 200

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
# The stuck-NPU notification went out; again only once the NPU has recovered.
_told_stuck = False


def _setting(key: str, default: float) -> float:
    try:
        from core.settings import get_settings

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
    global _pipe, _pipe_key, _told_stuck
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
        if dev == "NPU":
            on_status("Checking the NPU…")
            # Not recorded: a driver reload fixes it without any version changing.
            if npu_canary.stuck():
                errors.append(f"NPU: {npu_canary.STUCK}")
                continue
        on_status(f"Loading model on {dev}…")
        props: dict[str, object] = {"CACHE_DIR": str(cache)}
        if dev == "NPU":
            props.update(MAX_PROMPT_LEN=MAX_PROMPT_LEN, MIN_RESPONSE_LEN=MIN_RESPONSE_LEN)
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
        if dev == "NPU":
            _told_stuck = False
        warn = errors and errors[0].startswith("NPU:") and "failed before" not in errors[0]
        if warn and not (_told_stuck and npu_canary.STUCK in errors[0]):
            _warn_fallback(dev, errors[0], on_status)
        _pipe, _pipe_key = pipe, (model.key, dev)
        return pipe
    raise RuntimeError("could not load the model: " + "; ".join(errors))


def _warn_fallback(dev: str, npu_error: str, on_status: Callable[[str], None]) -> None:
    """The NPU is the point of vinoPop: say so loudly when the model ended
    up somewhere slower and hotter."""
    global _told_stuck
    if "still compiling" in npu_error:
        msg = f"The NPU is still compiling the model, so it's running on {dev} until that's done."
    elif npu_canary.STUCK in npu_error:
        _told_stuck = True
        msg = f"The NPU driver is stuck, so the model is running on {dev}. Reload it with: {npu_canary.RELOAD}"
        npu_error = ""
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
                f"{msg}\n{npu_error}".strip()[:300],
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


# Control tokens spelled out in a chat template: <|turn>, <turn|>, <|im_start|>.
_TEMPLATE_TOKEN = re.compile(r"<\|[^<>|\s]{1,40}\|?>|<[^<>|\s]{1,40}\|>")
_special_cache: dict[str, re.Pattern[str] | None] = {}


def _config_special_tokens(path: Path) -> set[str]:
    """Special tokens listed in the export's tokenizer_config.json."""
    try:
        cfg = json.loads((path / "tokenizer_config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    found: set[str] = set()

    def add(value) -> None:
        if isinstance(value, dict):
            value = value.get("content")
        if isinstance(value, str) and value:
            found.add(value)

    for entry in (cfg.get("added_tokens_decoder") or {}).values():
        if isinstance(entry, dict) and entry.get("special"):
            add(entry)
    for key, value in cfg.items():
        if key.endswith("_token"):
            add(value)
        elif key in ("additional_special_tokens", "extra_special_tokens"):
            for v in value.values() if isinstance(value, dict) else value or ():
                add(v)
    return found


def _special_pattern(tok, path: Path | None) -> re.Pattern[str] | None:
    """One regex matching every special token of the active model, from its
    tokenizer config, the tokenizer's own BOS/EOS/PAD and the literal
    control tokens in its chat template. Cached per model directory."""
    key = str(path)
    if key in _special_cache:
        return _special_cache[key]
    found = _config_special_tokens(path) if path is not None else set()
    for getter in ("get_bos_token", "get_eos_token", "get_pad_token"):
        try:
            value = getattr(tok, getter)()
        except Exception:  # noqa: BLE001 - not every tokenizer has all three
            continue
        if isinstance(value, str) and value:
            found.add(value)
    try:
        template = tok.chat_template
    except Exception:  # noqa: BLE001
        template = None
    if isinstance(template, str):
        found.update(_TEMPLATE_TOKEN.findall(template))
    pattern = None
    if found:
        alts = sorted(found, key=len, reverse=True)
        pattern = re.compile("|".join(re.escape(t) for t in alts))
    _special_cache[key] = pattern
    return pattern


def _strip_special(text: str, pattern: re.Pattern[str] | None) -> str:
    """Remove control tokens from user text, so a selection can't close the
    user turn and forge a system or model turn. Repeated until stable, since
    removing one can join the halves of another."""
    if pattern is None:
        return text
    while True:
        cleaned = pattern.sub("", text)
        if cleaned == text:
            return cleaned
        text = cleaned


_SENTENCE_END = re.compile(r"[.!?…](?=\s)|[。！？]|\n")


def _cut(text: str, keep: int) -> str:
    """`text` cut to at most `keep` chars, at a sentence end (or failing that
    a space) when one is close to the cut."""
    head = text[:keep]
    ends = [m.end() for m in _SENTENCE_END.finditer(head)]
    if ends and ends[-1] >= keep * 0.8:
        return head[: ends[-1]].rstrip()
    space = head.rfind(" ")
    if space >= keep * 0.9:
        return head[:space].rstrip()
    return head


def _fit_prompt(tok, task: str, text: str, lang: str) -> tuple[str, str]:
    """The templated prompt and the (possibly shortened) text, cut by token
    count until the prompt fits the NPU's MAX_PROMPT_LEN."""
    limit = MAX_PROMPT_LEN - _PROMPT_MARGIN

    def template(t: str) -> str:
        history = [
            {"role": "system", "content": _PROMPTS[task].format(lang=lang)},
            {"role": "user", "content": t},
        ]
        return tok.apply_chat_template(history, add_generation_prompt=True)

    prompt = template(text)
    n = _count_tokens(tok, prompt)
    if n is None or n <= limit or not text:
        return prompt, text
    overhead = _count_tokens(tok, template("")) or 0
    for _ in range(8):
        # Scale by the text's share of the tokens, not the whole prompt's:
        # with a long system prompt that would cut too little per pass.
        room = max(limit - overhead, 0)
        keep = int(len(text) * room / max(n - overhead, 1) * 0.97)
        log.info("[vino_ai] prompt is %d tokens, cutting the text to %d chars", n, keep)
        text = _cut(text, min(keep, len(text) - 1))
        prompt = template(text)
        n = _count_tokens(tok, prompt) or 0
        if n <= limit and text:
            return prompt, text
        if not text:
            break
    raise RuntimeError(f"the prompt is still {n} tokens after cutting the text (limit {limit})")


def _num_generated(result, tok, text: str) -> int | None:
    """Tokens the model produced: the pipeline's own count when it reports
    one, else the answer re-encoded."""
    try:
        return int(result.perf_metrics.get_num_generated_tokens())
    except Exception:  # noqa: BLE001 - plain str results, older GenAI
        return _count_tokens(tok, text)


@dataclass
class Answer:
    text: str
    truncated_input: bool = False  # only part of the selection reached the model
    hit_max_new_tokens: bool = False  # the answer stopped at its length cap

    @property
    def incomplete(self) -> bool:
        return self.truncated_input or self.hit_max_new_tokens


class Cancelled(Exception):
    """The caller gave up on the run (bubble closed or superseded)."""


def _streamer(on_token, cancelled):
    """GenAI streamer kwargs for on_token/cancelled, or {} for neither."""
    # VLMPipeline (Gemma 4) takes these by name only, and rejects
    # streamer=None.
    if on_token is None and cancelled is None:
        return {}
    import openvino_genai as ov_genai

    statuses = getattr(ov_genai, "StreamingStatus", None)
    running = getattr(statuses, "RUNNING", False)
    # CANCEL and STOP both end generate() at the next token; they differ only
    # in the chat history, which a one-shot generate() doesn't keep. Older
    # GenAI took a bool, where True meant stop.
    cancel = getattr(statuses, "CANCEL", True)
    is_cancelled = cancelled or (lambda: False)
    sink = on_token or (lambda _c: None)

    def _stream(chunk: str):
        if is_cancelled():
            return cancel
        sink(chunk)
        return running

    return {"streamer": _stream}


def _generate(
    pipe,
    task: str,
    text: str,
    lang: str,
    on_token: Callable[[str], None] | None,
    cancelled: Callable[[], bool] | None = None,
) -> Answer:
    telemetry.prepare()
    tok = pipe.get_tokenizer()
    text = _strip_special(text, _special_pattern(tok, models.model_dir(models.llm_model())))
    before = len(text)
    prompt, text = _fit_prompt(tok, task, text, lang)
    truncated = len(text) < before

    cfg = pipe.get_generation_config()
    max_new = _MAX_NEW_TOKENS[task]
    n_text = _count_tokens(tok, text)
    if n_text:
        # Short selections get short budgets: a runaway answer to a one-line
        # rewrite ends (and is cancelled) sooner.
        max_new = min(max_new, int(1.5 * n_text) + 64)
    n_prompt = _count_tokens(tok, prompt)
    if device() == "NPU" and n_prompt:
        # The static pipeline's KV cache holds MAX_PROMPT_LEN + MIN_RESPONSE_LEN
        # tokens and generation ends when it's full; cap at that so an answer
        # cut short there is reported as one.
        max_new = max(2, min(max_new, MAX_PROMPT_LEN + MIN_RESPONSE_LEN - n_prompt))
    cfg.max_new_tokens = max_new
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

    result = pipe.generate(prompt, generation_config=cfg, **_streamer(on_token, cancelled))
    if cancelled is not None and cancelled():
        raise Cancelled()
    answer = str(result).strip()
    n_gen = _num_generated(result, tok, answer)
    hit = n_gen is not None and n_gen >= max_new
    if truncated or hit:
        log.info("[vino_ai] %s: input cut=%s, hit %d new tokens=%s", task, truncated, max_new, hit)
    return Answer(answer, truncated_input=truncated, hit_max_new_tokens=hit)


def run(
    task: str,
    text: str,
    *,
    lang: str = "English",
    on_token: Callable[[str], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Answer:
    """Run `task` on `text` and return the answer, flagged when the input
    was cut or the output hit its cap. Blocking; call it from a worker
    thread. `on_token` receives text as it streams, `on_status` gets
    progress lines (download / load) worth showing. Once `cancelled()` is
    true the run stops at the next token, or doesn't start if it was still
    queued, and raises Cancelled."""
    if task not in _PROMPTS:
        raise ValueError(f"unknown task {task!r}")
    full = text.strip()
    text = full[:MAX_INPUT_CHARS]
    result = _with_pipe(
        lambda pipe, sink: _generate(pipe, task, text, lang, sink, cancelled),
        on_token,
        on_status,
        cancelled,
    )
    if len(text) < len(full):
        result.truncated_input = True
    return result


def describe(
    image_path: str,
    ocr_text: str = "",
    *,
    on_token: Callable[[str], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Answer:
    """Alt text for the image at `image_path`, from the multimodal model
    (Gemma 4). `ocr_text` is what OCR read in it, passed along so the model
    quotes it. Blocking and cancellable like run()."""
    ocr = (
        f'Text found in the image by OCR (may have small errors): "{" ".join(ocr_text.split())}"'
        if ocr_text.strip()
        else "OCR found no text in the image."
    )
    prompt = _DESCRIBE_PROMPT.format(ocr=ocr)

    def _work(pipe, sink) -> Answer:
        # After any "Loading model…" line, which would otherwise stay up
        # through the ~5 s before the first word.
        if on_status is not None:
            on_status("Looking at the image…")
        return _describe(pipe, image_path, prompt, sink, cancelled)

    return _with_pipe(
        _work,
        on_token,
        on_status,
        cancelled,
    )


def _describe(pipe, image_path, prompt, on_token, cancelled) -> Answer:
    telemetry.prepare()
    import numpy as np
    import openvino as ov
    import openvino_genai as ov_genai
    from PIL import Image

    if not isinstance(pipe, ov_genai.VLMPipeline):
        raise RuntimeError(f"{models.llm_model().label} can't look at images")
    with Image.open(image_path) as img:
        pixels = np.array(img.convert("RGB"))
    cfg = pipe.get_generation_config()
    cfg.max_new_tokens = _DESCRIBE_MAX_NEW
    cfg.do_sample = False
    if device() == "NPU":
        cfg.min_new_tokens = 2  # see _generate
    # The pipeline applies the chat template itself: it places the image
    # tokens, which a hand-templated prompt would leave out.
    result = pipe.generate(
        prompt,
        images=[ov.Tensor(pixels[None])],
        generation_config=cfg,
        **_streamer(on_token, cancelled),
    )
    if cancelled is not None and cancelled():
        raise Cancelled()
    answer = str(result).strip()
    tok = pipe.get_tokenizer()
    n_gen = _num_generated(result, tok, answer)
    return Answer(answer, hit_max_new_tokens=n_gen is not None and n_gen >= _DESCRIBE_MAX_NEW)


def _with_pipe(
    work: Callable[[object, Callable[[str], None] | None], Answer],
    on_token: Callable[[str], None] | None,
    on_status: Callable[[str], None] | None,
    cancelled: Callable[[], bool] | None,
) -> Answer:
    """Run `work(pipe, on_token)` under the model lock: load the model,
    retry once if the NPU was reset before anything streamed, and arm the
    idle unload afterwards."""
    status = on_status or (lambda _m: None)
    global _last_used
    streamed = False

    def _sink(chunk: str) -> None:
        nonlocal streamed
        streamed = True
        if on_token is not None:
            on_token(chunk)

    with _lock:
        if cancelled is not None and cancelled():
            raise Cancelled()
        try:
            for attempt in range(2):
                pipe = _load(status)
                try:
                    result = work(pipe, _sink if on_token else None)
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
    return result
