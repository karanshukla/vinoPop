"""vino_ai logic that doesn't need OpenVINO or hardware: the heavy modules
are stubbed, so these run in CI as they do on a laptop."""

import sys
import types

import pytest

from vino_ai import ai_worker, devices, doctor, llm, models, ovfetch
from vino_ai.ocr import _reading_order

# ---- OCR reading order ---------------------------------------------------


def _box(x, y, w=40, h=10):
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


def test_reading_order_merges_same_row_left_to_right():
    boxes = [_box(100, 0), _box(0, 1), _box(0, 30)]
    assert _reading_order(boxes, ["right", "left", "below"]) == "left right\nbelow"


def test_reading_order_empty():
    assert _reading_order([], []) == ""


# ---- device order ----------------------------------------------------------


@pytest.fixture
def fake_devices(monkeypatch):
    def use(found, setting="auto", warnings=()):
        monkeypatch.setattr(devices, "available_devices", lambda: tuple(found))
        monkeypatch.setattr(devices, "_setting", lambda: setting.upper())
        monkeypatch.setattr(devices, "_log_npu_notes", lambda: None)
        monkeypatch.setattr(ovfetch, "warnings", lambda _v: list(warnings))

    return use


def test_llm_prefers_npu(fake_devices):
    fake_devices(["CPU", "GPU.0", "NPU"])
    assert devices.candidates(devices.LLM_ORDER) == ["NPU", "GPU", "CPU"]


def test_ocr_skips_npu(fake_devices):
    fake_devices(["CPU", "GPU", "NPU"])
    assert devices.candidates(devices.OCR_ORDER) == ["GPU", "CPU"]


def test_setting_moves_device_first(fake_devices):
    fake_devices(["CPU", "GPU", "NPU"], setting="cpu")
    assert devices.candidates(devices.LLM_ORDER) == ["CPU", "NPU", "GPU"]


def test_unavailable_setting_is_ignored(fake_devices):
    fake_devices(["CPU"], setting="npu")
    assert devices.candidates(devices.LLM_ORDER) == ["CPU"]


def test_no_devices_falls_back_to_cpu(fake_devices):
    fake_devices([])
    assert devices.candidates(devices.LLM_ORDER) == ["CPU"]


# ---- ovfetch diagnosis ---------------------------------------------------

_STATUS = {
    "openvino_ceiling": "2026.4",
    "npu_driver": "1.28.0",
    "devices": [{"kind": "npu"}],
    "platforms": [{"codename": "Lunar Lake", "pci_id": "8086:643e", "min_openvino": "2024.4"}],
}


def _range_note(version):
    (note,) = [n for n in ovfetch.notes(_STATUS, version) if n.label == "npu: openvino range"]
    return note


def test_ovfetch_inside_range():
    assert _range_note("2026.4.1").ok is True


def test_ovfetch_newer_than_ceiling_warns():
    note = _range_note("2026.5.0")
    assert note.ok is False and "newer than 2026.4" in note.detail


def test_ovfetch_older_than_floor_warns():
    note = _range_note("2024.3.0")
    assert note.ok is False and "older than 2024.4" in note.detail


def test_ovfetch_without_npu_or_old_ovfetch():
    assert ovfetch.notes({**_STATUS, "devices": []}, "2026.4.1")[0].ok is True
    assert ovfetch.notes({}, "2026.4.1")[0].ok is None


def test_ovfetch_warnings_are_empty_without_ovfetch(monkeypatch):
    monkeypatch.setattr(ovfetch, "detect", lambda: None)
    assert ovfetch.warnings("2026.4.1") == []


def test_npu_is_still_tried_when_ovfetch_warns(monkeypatch):
    # Diagnosis only: an out-of-range OpenVINO is logged, never a reason to drop the NPU.
    monkeypatch.setattr(devices, "available_devices", lambda: ("CPU", "NPU"))
    monkeypatch.setattr(devices, "_setting", lambda: "AUTO")
    monkeypatch.setattr(devices, "_log_npu_notes", lambda: None)
    assert devices.candidates(devices.LLM_ORDER)[0] == "NPU"


# ---- models ----------------------------------------------------------------


def test_models_registry():
    assert models.DEFAULT_LLM in models.LLM_MODELS
    for m in models.LLM_MODELS.values():
        assert m.repo_id.startswith("OpenVINO/") and m.repo_id.endswith("-ov")
        # a full commit sha, or "main" until one is pinned
        assert m.revision == "main" or (len(m.revision) == 40 and int(m.revision, 16) >= 0)


def test_model_completeness(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "MODELS_DIR", tmp_path)
    model = models.LLM_MODELS[models.DEFAULT_LLM]
    assert not models.is_downloaded(model)
    path = models.model_dir(model)
    path.mkdir(parents=True)
    (path / "openvino_language_model.xml").write_text("")
    (path / "openvino_tokenizer.xml").write_text("")
    assert not models.is_downloaded(model)  # weights still missing
    (path / "openvino_language_model.bin").write_text("")
    assert models.is_downloaded(model)
    in_flight = path / ".cache" / "huggingface" / "download"
    in_flight.mkdir(parents=True)
    (in_flight / "x.incomplete").write_text("")
    assert not models.is_downloaded(model)  # a transfer is still running
    (in_flight / "x.incomplete").unlink()
    assert models.is_downloaded(model)
    assert models.ensure_llm(model) == path  # no download when complete


# ---- llm.run with a fake pipeline -----------------------------------------


class _FakeTokenizer:
    def apply_chat_template(self, history, add_generation_prompt):
        assert add_generation_prompt
        return " | ".join(f"{m['role']}: {m['content']}" for m in history)


class _FakePipe:
    def __init__(self):
        self.cfg = types.SimpleNamespace(max_new_tokens=0, do_sample=True)
        self.prompts = []

    def get_tokenizer(self):
        return _FakeTokenizer()

    def get_generation_config(self):
        return self.cfg

    def generate(self, prompt, cfg, streamer):
        self.prompts.append(prompt)
        for chunk in ("Hola", " mundo"):
            if streamer:
                streamer(chunk)
        return " Hola mundo "


@pytest.fixture
def fake_pipe(monkeypatch):
    pipe = _FakePipe()
    monkeypatch.setitem(sys.modules, "openvino_genai", types.ModuleType("openvino_genai"))
    monkeypatch.setattr(llm, "_load", lambda _status: pipe)
    return pipe


def test_llm_run_streams_and_formats_prompt(fake_pipe):
    tokens = []
    out = llm.run("translate", "hello world", lang="Spanish", on_token=tokens.append)
    assert out == "Hola mundo"
    assert tokens == ["Hola", " mundo"]
    assert "into Spanish" in fake_pipe.prompts[0]
    assert fake_pipe.cfg.max_new_tokens == llm._MAX_NEW_TOKENS["translate"]
    assert fake_pipe.cfg.do_sample is False


def test_llm_translate_stops_at_blank_line_for_single_paragraph(fake_pipe):
    llm.run("translate", "hello world", lang="German")
    assert fake_pipe.cfg.stop_strings == {"\n\n"}


def test_llm_translate_keeps_paragraphs_of_multi_paragraph_input(fake_pipe):
    llm.run("translate", "hello\n\nworld", lang="German")
    assert not hasattr(fake_pipe.cfg, "stop_strings")


def test_llm_run_truncates_long_input(fake_pipe):
    llm.run("summarize", "x" * (llm.MAX_INPUT_CHARS + 500))
    assert "x" * (llm.MAX_INPUT_CHARS + 1) not in fake_pipe.prompts[0]


def test_llm_warns_when_npu_fails_and_another_device_loads(monkeypatch, tmp_path):
    seen, notes = [], []

    class _Genai(types.ModuleType):
        def LLMPipeline(self, path, dev, **_props):
            if dev == "NPU":
                raise RuntimeError("compile failed")
            return object()

        VLMPipeline = LLMPipeline

    monkeypatch.setitem(sys.modules, "openvino_genai", _Genai("openvino_genai"))
    monkeypatch.setattr(llm, "_pipe", None)
    monkeypatch.setattr(llm.models, "ensure_llm", lambda m, s=None: tmp_path)
    monkeypatch.setattr(llm.models, "is_downloaded", lambda m: True)
    monkeypatch.setattr(llm.devices, "candidates", lambda order: ["NPU", "GPU"])
    monkeypatch.setattr(llm.subprocess, "run", lambda cmd, **kw: notes.append(cmd))
    llm._load(seen.append)
    assert any("running on GPU" in m for m in seen)
    assert notes and notes[0][0] == "notify-send"


def test_llm_falls_back_when_npu_compile_times_out_then_moves_back(monkeypatch, tmp_path):
    import threading

    release, seen = threading.Event(), []

    class _Genai(types.ModuleType):
        def LLMPipeline(self, path, dev, **_props):
            if dev == "NPU":
                release.wait(5)
            return dev

        VLMPipeline = LLMPipeline

    monkeypatch.setitem(sys.modules, "openvino_genai", _Genai("openvino_genai"))
    monkeypatch.setattr(llm, "_pipe", None)
    monkeypatch.setattr(llm, "_late", None)
    monkeypatch.setattr(llm, "_load_timeout", lambda: 0.05)
    monkeypatch.setattr(llm.models, "ensure_llm", lambda m, s=None: tmp_path)
    monkeypatch.setattr(llm.models, "is_downloaded", lambda m: True)
    monkeypatch.setattr(llm.devices, "candidates", lambda order: ["NPU", "GPU"])
    monkeypatch.setattr(llm.subprocess, "run", lambda cmd, **kw: None)
    assert llm._load(seen.append) == "GPU"
    assert any("still compiling" in m for m in seen)

    release.set()
    for _ in range(100):
        if llm._late is not None:
            break
        threading.Event().wait(0.01)
    assert llm._load(seen.append) == "NPU"
    assert llm.device() == "NPU"


def test_llm_run_rejects_unknown_task():
    with pytest.raises(ValueError):
        llm.run("poem", "text")


# ---- ai_worker ---------------------------------------------------------------


class _Sink:
    def __init__(self):
        self.status, self.tokens, self.done = [], [], None

    def push_status(self, msg):
        self.status.append(msg)

    def push_token(self, chunk):
        self.tokens.append(chunk)

    def push_done(self, text=None, error=None):
        self.done = (text, error)


def test_worker_reports_missing_openvino(monkeypatch):
    monkeypatch.setattr(ai_worker, "available", lambda: (False, "OpenVINO not installed"))
    sink = _Sink()
    ai_worker.run_into(sink, "summarize", "text")
    assert sink.done == (None, "OpenVINO not installed")


def test_worker_streams_result(monkeypatch, fake_pipe):
    monkeypatch.setattr(ai_worker, "available", lambda: (True, ""))
    sink = _Sink()
    ai_worker.run_into(sink, "translate", "hello", lang="Spanish")
    assert sink.tokens == ["Hola", " mundo"] and sink.done == ("Hola mundo", None)


def test_worker_turns_errors_into_messages(monkeypatch):
    monkeypatch.setattr(ai_worker, "available", lambda: (True, ""))

    def boom(*_a, **_k):
        raise RuntimeError("NPU compile failed")

    monkeypatch.setattr(llm, "run", boom)
    sink = _Sink()
    ai_worker.run_into(sink, "explain", "text")
    assert sink.done[0] is None and "NPU compile failed" in sink.done[1]


# ---- doctor ------------------------------------------------------------------


def test_doctor_json_runs_without_openvino(monkeypatch, capsys):
    monkeypatch.setattr(ovfetch, "installed", lambda: False)
    monkeypatch.setattr(doctor, "_devices", lambda: [])
    code = doctor.main(as_json=True)
    out = capsys.readouterr().out
    assert '"checks"' in out and code in (0, 1)
