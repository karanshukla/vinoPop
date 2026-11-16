"""Local LLM tasks: summarize, rewrite, explain, translate, describe.

One OpenVINO GenAI pipeline is loaded on first use and kept until it has
been idle for `ai_idle_unload_minutes`. Compiled blobs are cached under
$XDG_CACHE_HOME/vinopop/ov-cache/llm, so a reload is quick. Generation is
serialised with a lock, so two clicks in a row queue instead of fighting
over the device.
"""

from __future__ import annotations

import gc
import json
import logging
import re
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from core.xdg_paths import CACHE_DIR

from . import devices, failures, idle, models, npu_canary, telemetry
from .idle import IDLE_UNLOAD_MINUTES
from .idle import float_setting as _setting

log = logging.getLogger("vinopop")

# The NPU compiles a static pipeline: a prompt window of MAX_PROMPT_LEN tokens
# and a KV cache of MAX_PROMPT_LEN + MIN_RESPONSE_LEN.
MAX_PROMPT_LEN = 3072
MIN_RESPONSE_LEN = 512
MAX_INPUT_CHARS = 8000
# Tokens the pipeline adds around the templated prompt: ~40 measured on
# Gemma 4's VLM pipeline (2026-10-17).
_PROMPT_MARGIN = 96
# Seconds per device. A first NPU compile of a big model can take minutes.
LOAD_TIMEOUT = 120.0
# The NPU decoder emits NaNs on a single-token step (openvino#35222).
_NPU_MIN_NEW_TOKENS = 2
_BUDGET_PER_INPUT_TOKEN = 1.5
_BUDGET_FLOOR = 64
_MAX_CUT_PASSES = 8
_CUT_UNDERSHOOT = 0.97
_SENTENCE_CUT_MIN_SHARE = 0.8
_SPACE_CUT_MIN_SHARE = 0.9
_SKIPPED_BEFORE = "failed before"

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

# Without the checklist the model skips faces, art style and mood, and it
# quotes text in the image only when handed the OCR text.
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
PipeKey = tuple[str, str]  # (model key, device)
_pipe_key: PipeKey | None = None
_late: dict[str, tuple[object, PipeKey]] = {}  # compiled after their timeout, by device
_inflight: set[str] = set()  # devices whose abandoned compile still runs
_state_lock = threading.Lock()  # guards _late and _inflight
_last_used = 0.0
_idle_timer: threading.Timer | None = None
_told_stuck = False


def _load_timeout() -> float:
    return _setting("ai_load_timeout_seconds", LOAD_TIMEOUT) or LOAD_TIMEOUT


def _construct(cls, path, dev: str, props: dict, key: tuple[str, str], timeout: float, fp: dict):
    """Build the pipeline on a helper thread and stop waiting after
    `timeout` seconds. A native compile can't be cancelled, so a slow one
    keeps going: it fills the compile cache either way, and on success it
    is parked in _late and swapped in on the next run (pinned by
    `tests/test_vino_ai.py::test_llm_falls_back_when_npu_compile_times_out_then_moves_back`)."""
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
    beats the one in use; drop the rest so their memory goes back (pinned by
    `tests/test_vino_ai.py::test_late_npu_beats_late_gpu_whatever_finishes_last`)."""
    global _pipe, _pipe_key
    with _state_lock:
        late = [entry for entry in _late.values() if entry[1][0] == model_key]
        _late.clear()
    if not late:
        return

    def rank(dev: str) -> int:
        return order.index(dev) if dev in order else len(order)

    def device_rank(entry: tuple[object, PipeKey]) -> int:
        return rank(entry[1][1])

    nothing_in_use = len(order) + 1
    in_use = _pipe_key if _pipe is not None and _pipe_key and _pipe_key[0] == model_key else None
    best = min(late, key=device_rank)
    if device_rank(best) < (rank(in_use[1]) if in_use else nothing_in_use):
        log.info("[vino_ai] LLM moved to %s", best[1][1])
        _pipe, _pipe_key = best
    del late, best
    gc.collect()


OV_CACHE = CACHE_DIR / "ov-cache"
LLM_CACHE = OV_CACHE / "llm"


def _ov_version() -> str:
    import openvino

    return str(getattr(openvino, "__version__", "") or openvino.get_version())


def cache_key(model: models.LlmModel, ov_version: str) -> str:
    """LLM_CACHE folder name for this model, revision and OpenVINO build (pinned by
    `tests/test_model_integrity.py::test_cache_key_changes_with_model_revision_and_openvino`)."""
    raw = f"{model.key}@{model.revision[:12]}-ov{ov_version}"
    return re.sub(r"[^A-Za-z0-9@._-]+", "_", raw)


def cache_dir(key: str, dev: str) -> Path:
    """Compile cache for one device (pinned by
    `tests/test_model_integrity.py::test_load_uses_a_per_device_cache_and_prunes_stale_ones`)."""
    return LLM_CACHE / key / re.sub(r"[^A-Za-z0-9._-]+", "_", dev)


def _loose_blobs() -> list[Path]:
    """Compile blobs older versions wrote straight into ov-cache, LLM and OCR mixed."""
    return [child for child in OV_CACHE.iterdir() if child.is_file()] if OV_CACHE.is_dir() else []


def prune_cache(key: str) -> list[Path]:
    """Delete the LLM compile caches of every other key (each a compile
    nothing will load again, 2+ GB on the NPU) and the loose blobs. Leaves
    the OCR cache alone. Returns what was removed (pinned by
    `tests/test_model_integrity.py::test_prune_keeps_only_the_current_key_and_leaves_ocr_alone`)."""
    removed: list[Path] = []
    if LLM_CACHE.is_dir():
        for child in LLM_CACHE.iterdir():
            if child.name == key:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
            removed.append(child)
    for blob in _loose_blobs():
        blob.unlink(missing_ok=True)
        removed.append(blob)
    return removed


def _prune_after_load(key: str) -> None:
    """prune_cache, unless a background compile may still be writing a cache
    for another key (pinned by `tests/test_model_integrity.py::test_prune_waits_for_background_compiles`)."""
    with _state_lock:
        if _inflight:
            return
    try:
        removed = prune_cache(key)
    except OSError as exc:
        log.warning("[vino_ai] couldn't prune the compile cache: %s", exc)
        return
    if removed:
        log.info("[vino_ai] removed %d stale compile cache entries", len(removed))


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

    key = cache_key(model, _ov_version())
    errors = []
    timeout = _load_timeout()
    fp = failures.fingerprint(model.key, model.revision)
    forced = devices.forced()
    for dev in order:
        skip = _skip_reason(dev, fp, forced, on_status)
        if skip is not None:
            errors.append(f"{dev}: {skip}")
            continue
        cache = cache_dir(key, dev)
        cache.mkdir(parents=True, exist_ok=True)
        msg = _load_status(dev, cache)
        props = _pipeline_props(dev, cache)
        try:
            cls = _pipeline_class(ov_genai, path)
            with _ticking(on_status, msg):
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
        if errors and _should_warn_fallback(errors[0]):
            _warn_fallback(dev, errors[0], on_status)
        _pipe, _pipe_key = pipe, (model.key, dev)
        _prune_after_load(key)
        return pipe
    raise RuntimeError("could not load the model: " + "; ".join(errors))


def _skip_reason(
    dev: str, fp: dict, forced: str | None, on_status: Callable[[str], None]
) -> str | None:
    """Why `dev` isn't tried this time, or None to try it. A device still
    compiling in the background isn't compiled twice, one with a recorded
    failure is skipped unless forced, and a stuck NPU is skipped without a
    record, since a driver reload fixes it without any version changing (pinned
    by `tests/test_vino_ai.py::test_device_still_compiling_is_not_compiled_twice`,
    `tests/test_vino_ai.py::test_failed_npu_is_recorded_and_skipped_next_start`,
    `tests/test_vino_ai.py::test_forced_device_is_tried_despite_a_record`,
    `tests/test_vino_ai.py::test_stuck_npu_is_skipped_and_reported_once_until_it_recovers`)."""
    with _state_lock:
        if dev in _inflight:
            return "still compiling in the background"
    rec = failures.blocking(dev, fp) if dev != forced else None
    if rec is not None:
        log.info(
            "[vino_ai] skipping %s, it failed before on this driver/runtime: %s",
            dev,
            rec.get("error"),
        )
        return f"skipped, {_SKIPPED_BEFORE} ({rec.get('error')})"
    if dev == "NPU":
        on_status("Checking the NPU…")
        if npu_canary.stuck():
            return npu_canary.STUCK
    return None


def _pipeline_class(ov_genai, path: Path):
    """LLMPipeline for a text-only export, else the VLM pipeline of a
    multimodal one (Gemma 4), which also serves text-only prompts."""
    if (path / "openvino_model.xml").is_file():
        return ov_genai.LLMPipeline
    return ov_genai.VLMPipeline


def _load_status(dev: str, cache: Path) -> str:
    """Compiling the first time on a device, loading after (pinned by
    `tests/test_model_integrity.py::test_load_says_compiling_only_when_there_is_no_cache`)."""
    if any(cache.iterdir()):
        return f"Loading model on {dev}…"
    log.info("[vino_ai] no compile cache for %s at %s, compiling", dev, cache)
    return f"Compiling the model for {dev}, one time only…"


def _pipeline_props(dev: str, cache: Path) -> dict[str, object]:
    props: dict[str, object] = {"CACHE_DIR": str(cache)}
    if dev == "NPU":
        props.update(MAX_PROMPT_LEN=MAX_PROMPT_LEN, MIN_RESPONSE_LEN=MIN_RESPONSE_LEN)
    return props


def _should_warn_fallback(first_error: str) -> bool:
    """Whether a load that skipped or lost the NPU should say so: not for a
    recorded failure (told on the start that recorded it), and a stuck NPU
    only until it recovers (pinned by
    `tests/test_vino_ai.py::test_llm_warns_when_npu_fails_and_another_device_loads`,
    `tests/test_vino_ai.py::test_npu_skipped_for_a_recorded_failure_is_not_warned_about_again`,
    `tests/test_vino_ai.py::test_stuck_npu_is_reported_once_not_on_every_reload`,
    `tests/test_vino_ai.py::test_npu_stuck_again_after_recovering_is_reported_again`)."""
    if not first_error.startswith("NPU:") or _SKIPPED_BEFORE in first_error:
        return False
    return not (_told_stuck and npu_canary.STUCK in first_error)


@contextmanager
def _ticking(on_status: Callable[[str], None], msg: str, every: float = 5.0) -> Iterator[None]:
    """Show `msg`, then repeat it with the elapsed time until the block
    ends, so a compile that takes minutes doesn't look hung."""
    on_status(msg)
    start = time.monotonic()
    stop = threading.Event()

    def tick() -> None:
        while not stop.wait(every):
            secs = int(time.monotonic() - start)
            on_status(f"{msg} ({secs // 60}:{secs % 60:02d})")

    ticker = threading.Thread(target=tick, name="vino_ai-load-ticker", daemon=True)
    ticker.start()
    try:
        yield
    finally:
        stop.set()
        ticker.join()


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
    from core.notify import notify

    notify(
        "AI is not on the NPU",
        f"{msg}\n{npu_error}".strip()[:300],
        timeout_ms=None,
        transient=False,
    )


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


class Busy(RuntimeError):
    pass


def delete_models() -> int:
    """Unload the LLM, then delete every downloaded model and the LLM
    compile cache (the OCR cache stays: its model is in the wheel). Raises
    Busy while an AI action runs, a compile is still going, or a download
    is running. Returns the bytes freed."""
    global _idle_timer
    if not _lock.acquire(blocking=False):
        raise Busy("An AI action is running. Try again once it's done.")
    try:
        with _state_lock:
            if _inflight:
                raise Busy("The AI model is still loading. Try again in a minute.")
            _late.clear()
        if _idle_timer is not None:
            _idle_timer.cancel()
            _idle_timer = None
        if _pipe is not None:
            log.info("[vino_ai] unloading the LLM to delete the model")
        _drop_pipe()
        try:
            freed = models.delete_downloads()
        except models.DownloadRunning as exc:
            raise Busy(str(exc)) from exc
        freed += models.dir_size(LLM_CACHE)
        shutil.rmtree(LLM_CACHE, ignore_errors=True)
        for blob in _loose_blobs():
            freed += blob.stat().st_size
            blob.unlink(missing_ok=True)
        return freed
    finally:
        _lock.release()


def _schedule_unload() -> None:
    global _idle_timer
    minutes = _setting("ai_idle_unload_minutes", IDLE_UNLOAD_MINUTES)
    _idle_timer = idle.rearm(_idle_timer, minutes, unload_if_idle)


def _count_tokens(tok, s: str) -> int | None:
    try:
        return len(tok.encode(s).input_ids.data[0])
    except Exception:  # noqa: BLE001 - tokenizer without encode(): skip the check
        return None


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
    control tokens (`<|turn>`, `<turn|>`, `<|im_start|>`) in its chat template.
    Cached per model directory (pinned by
    `tests/test_vino_ai.py::test_gemma_control_tokens_in_the_selection_are_stripped`,
    `tests/test_vino_ai.py::test_control_tokens_come_from_the_chat_template_without_a_config`)."""
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
    a space) when one is close to the cut (pinned by
    `tests/test_vino_ai.py::test_cut_ends_at_a_sentence_in_the_last_fifth`,
    `tests/test_vino_ai.py::test_cut_ignores_a_sentence_end_before_the_last_fifth`,
    `tests/test_vino_ai.py::test_cut_falls_back_to_a_space_in_the_last_tenth`,
    `tests/test_vino_ai.py::test_cut_is_mid_word_without_a_space_in_the_last_tenth`)."""
    head = text[:keep]
    ends = [m.end() for m in _SENTENCE_END.finditer(head)]
    if ends and ends[-1] >= keep * _SENTENCE_CUT_MIN_SHARE:
        return head[: ends[-1]].rstrip()
    space = head.rfind(" ")
    if space >= keep * _SPACE_CUT_MIN_SHARE:
        return head[:space].rstrip()
    return head


def _fit_prompt(tok, task: str, text: str, lang: str) -> tuple[str, str]:
    """The templated prompt and the (possibly shortened) text, cut by token
    count until the prompt fits the NPU's MAX_PROMPT_LEN. MAX_INPUT_CHARS is
    only a cheap first cut: CJK runs well under 4 chars per token (pinned by
    `tests/test_vino_ai.py::test_cjk_selection_is_cut_by_tokens_to_fit_the_npu_prompt`,
    `tests/test_vino_ai.py::test_prompt_that_cannot_fit_raises_instead_of_overflowing`)."""
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
    for _ in range(_MAX_CUT_PASSES):
        keep = _chars_to_keep(len(text), n, overhead, limit)
        log.info("[vino_ai] prompt is %d tokens, cutting the text to %d chars", n, keep)
        text = _cut(text, min(keep, len(text) - 1))
        prompt = template(text)
        n = _count_tokens(tok, prompt) or 0
        if n <= limit and text:
            return prompt, text
        if not text:
            break
    raise RuntimeError(f"the prompt is still {n} tokens after cutting the text (limit {limit})")


def _chars_to_keep(n_chars: int, n_tokens: int, overhead: int, limit: int) -> int:
    """Chars of text that should bring a prompt of `n_tokens` (`overhead` of
    them not the text's) under `limit`. Scaled by the text's share of the
    tokens: the whole prompt's would cut too little with a long system prompt."""
    room = max(limit - overhead, 0)
    return int(n_chars * room / max(n_tokens - overhead, 1) * _CUT_UNDERSHOOT)


def _num_generated(result, tok, text: str) -> int | None:
    """Tokens the model produced: the pipeline's own count when it reports
    one, else the answer re-encoded."""
    try:
        return int(result.perf_metrics.get_num_generated_tokens())
    except Exception:  # noqa: BLE001 - plain str results, older GenAI
        return _count_tokens(tok, text)


@dataclass
class Answer:
    """The model's answer, flagged when only part of the selection reached
    the model or the answer stopped at its length cap."""

    text: str
    truncated_input: bool = False
    hit_max_new_tokens: bool = False

    @property
    def incomplete(self) -> bool:
        return self.truncated_input or self.hit_max_new_tokens


class Cancelled(Exception):
    """The caller gave up on the run (bubble closed or superseded)."""


def _streamer(on_token, cancelled):
    """GenAI streamer kwargs for on_token/cancelled, or {} for neither:
    VLMPipeline takes them by name only and rejects streamer=None.

    CANCEL and STOP both end a one-shot generate() at the next token. GenAI
    before StreamingStatus took a bool, True meaning stop."""
    if on_token is None and cancelled is None:
        return {}
    import openvino_genai as ov_genai

    statuses = getattr(ov_genai, "StreamingStatus", None)
    running = getattr(statuses, "RUNNING", False)
    cancel = getattr(statuses, "CANCEL", True)
    is_cancelled = cancelled or (lambda: False)
    sink = on_token or (lambda _c: None)

    def _stream(chunk: str):
        if is_cancelled():
            return cancel
        sink(chunk)
        return running

    return {"streamer": _stream}


def _answer_budget(task: str, n_text: int | None, n_prompt: int | None, *, on_npu: bool) -> int:
    """max_new_tokens for `task`. Short selections get short budgets, so a
    runaway answer to a one-line rewrite ends sooner. On the NPU, generation
    also ends when the static KV cache is full, so the budget stops there and
    an answer cut short is reported as one (pinned by
    `tests/test_vino_ai.py::test_max_new_tokens_follows_the_input_length`,
    `tests/test_vino_ai.py::test_npu_answer_is_capped_by_the_static_kv_cache`)."""
    max_new = _MAX_NEW_TOKENS[task]
    if n_text:
        max_new = min(max_new, int(_BUDGET_PER_INPUT_TOKEN * n_text) + _BUDGET_FLOOR)
    if on_npu and n_prompt:
        kv_room = MAX_PROMPT_LEN + MIN_RESPONSE_LEN - n_prompt
        max_new = max(_NPU_MIN_NEW_TOKENS, min(max_new, kv_room))
    return max_new


def _greedy_config(pipe, max_new: int):
    cfg = pipe.get_generation_config()
    cfg.max_new_tokens = max_new
    cfg.do_sample = False
    if device() == "NPU":
        cfg.min_new_tokens = _NPU_MIN_NEW_TOKENS
    return cfg


def _stops_at_blank_line(task: str, text: str) -> bool:
    """Small models append "(Note: ...)" after a blank line however the
    prompt forbids it, so a translation of one paragraph stops there (pinned by
    `tests/test_vino_ai.py::test_llm_translate_stops_at_blank_line_for_single_paragraph`,
    `tests/test_vino_ai.py::test_llm_translate_keeps_paragraphs_of_multi_paragraph_input`)."""
    return task == "translate" and "\n\n" not in text


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

    max_new = _answer_budget(
        task, _count_tokens(tok, text), _count_tokens(tok, prompt), on_npu=device() == "NPU"
    )
    cfg = _greedy_config(pipe, max_new)
    if _stops_at_blank_line(task, text):
        cfg.stop_strings = {"\n\n"}
    if hasattr(cfg, "apply_chat_template"):
        cfg.apply_chat_template = False

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
    queued, and raises Cancelled (pinned by
    `tests/test_vino_ai.py::test_cancel_stops_the_stream_and_frees_the_lock`,
    `tests/test_vino_ai.py::test_queued_run_cancelled_before_it_starts_never_loads`)."""
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
        # Replaces "Loading model…", which would otherwise stay up until the first word.
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
    cfg = _greedy_config(pipe, _DESCRIBE_MAX_NEW)
    # Untemplated: the pipeline's own template places the image tokens.
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
    reload and retry once if the device was reset before anything streamed
    (another app's fault can reset the NPU, dropping every model compiled on
    it), and arm the idle unload afterwards, even after a cancel or failure
    (pinned by `tests/test_vino_ai.py::test_device_lost_reloads_and_retries_once`,
    `tests/test_vino_ai.py::test_device_lost_after_streaming_is_not_retried`,
    `tests/test_vino_ai.py::test_cancelled_run_still_arms_the_idle_unload`,
    `tests/test_vino_ai.py::test_failed_run_still_arms_the_idle_unload`)."""
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
