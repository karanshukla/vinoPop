"""Model download markers, per-file digests and the compile cache (#32).
snapshot_download is faked and every path is under tmp_path, so nothing
touches the network or the real model folder."""

import hashlib
import re
import sys
import threading
import time
import types

import pytest

from vino_ai import doctor, integrity, llm, models

FILES = {
    "openvino_language_model.xml": b"<net/>",
    "openvino_language_model.bin": b"weights" * 100,
    "openvino_tokenizer.xml": b"<tok/>",
    "openvino_detokenizer.xml": b"<detok/>",
}
OLD_REVISION = "0" * 40


def _digests(files):
    return {
        name: integrity.FileDigest(hashlib.sha256(data).hexdigest(), len(data))
        for name, data in files.items()
    }


@pytest.fixture
def model():
    return models.LLM_MODELS[models.DEFAULT_LLM]


@pytest.fixture
def hub(monkeypatch, tmp_path, model):
    """Fake huggingface_hub and a digest table pinning FILES at the model's
    revision. hub.serve is what the fake download writes."""
    calls = []

    def snapshot_download(**kw):
        local = models.Path(kw["local_dir"])
        calls.append(kw)
        for name, data in hub.serve.items():
            (local / name).write_bytes(data)
        return str(local)

    fake = types.ModuleType("huggingface_hub")
    fake.snapshot_download = snapshot_download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)
    monkeypatch.setattr(models, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(models, "_progress_class", lambda label: None)
    table = {model.repo_id: {model.revision: _digests(FILES)}}
    monkeypatch.setattr(integrity, "load", lambda path=None: table)
    hub = types.SimpleNamespace(calls=calls, serve=dict(FILES), table=table)
    return hub


def _folder(model, files=FILES, revision=None, complete=True):
    path = models.model_dir(model)
    path.mkdir(parents=True)
    for name, data in files.items():
        (path / name).write_bytes(data)
    if revision:
        (path / models.REVISION_MARKER).write_text(revision + "\n")
    if complete and revision:
        (path / models.COMPLETE_MARKER).touch()
    return path


def _leftovers():
    return sorted(p.name for p in models.MODELS_DIR.iterdir() if p.name.startswith("."))


# ---- markers and re-download ------------------------------------------------


def test_fresh_download_is_verified_marked_and_swapped_in(hub, model):
    assert not models.is_downloaded(model)
    path = models.ensure_llm(model)
    assert path == models.model_dir(model)
    assert models.is_downloaded(model)
    assert (path / models.REVISION_MARKER).read_text().strip() == model.revision
    assert hub.calls[0]["revision"] == model.revision
    assert hub.calls[0]["local_dir"] != str(path)  # downloaded aside, then swapped
    assert _leftovers() == []
    models.ensure_llm(model)
    assert len(hub.calls) == 1  # no download once it's marked


def test_revision_mismatch_re_downloads(hub, model):
    old = _folder(model, revision=OLD_REVISION)
    (old / "stale-only-in-old.bin").write_bytes(b"x")
    assert not models.is_downloaded(model)
    models.ensure_llm(model)
    assert len(hub.calls) == 1
    assert models.downloaded_revision(old) == model.revision
    assert not (old / "stale-only-in-old.bin").exists()  # the old folder is gone
    assert _leftovers() == []


def test_missing_complete_marker_re_downloads(hub, model):
    # Killed after the revision was written but before .complete: never adopted
    _folder(model, revision=model.revision, complete=False)
    assert not models.is_downloaded(model)
    models.ensure_llm(model)
    assert len(hub.calls) == 1
    assert models.is_downloaded(model)


def test_folder_without_markers_is_not_downloaded(hub, model):
    # The old check passed this: main xml/bin and tokenizer, no detokenizer
    partial = {k: v for k, v in FILES.items() if k != "openvino_detokenizer.xml"}
    _folder(model, files=partial)
    assert not models.is_downloaded(model)
    assert not models.prefetch_consented({"ai_prefetch_model": True})
    models.ensure_llm(model)
    assert len(hub.calls) == 1
    assert (models.model_dir(model) / "openvino_detokenizer.xml").is_file()


def test_verified_folder_from_before_markers_is_adopted(hub, model):
    _folder(model)
    staging = models._staging_dir(model)
    staging.mkdir(parents=True)  # a re-download an earlier run started
    models.ensure_llm(model)
    assert hub.calls == []
    assert models.is_downloaded(model)
    assert _leftovers() == []


def test_unmarked_folder_that_differs_is_downloaded_again(hub, model):
    _folder(model, files={**FILES, "openvino_tokenizer.xml": b"<bad/>"})
    models.ensure_llm(model)
    assert len(hub.calls) == 1
    assert (models.model_dir(model) / "openvino_tokenizer.xml").read_bytes() == b"<tok/>"


def test_hash_mismatch_is_reported_and_not_marked(hub, model):
    hub.serve["openvino_language_model.bin"] = b"tampered" * 100
    with pytest.raises(RuntimeError, match="integrity check"):
        models.ensure_llm(model)
    assert not models.is_downloaded(model)
    assert not models.model_dir(model).exists()
    # Only the bad file is dropped, so the retry fetches just that
    staging = models._staging_dir(model)
    assert not (staging / "openvino_language_model.bin").exists()
    assert (staging / "openvino_tokenizer.xml").is_file()

    hub.serve = dict(FILES)
    models.ensure_llm(model)
    assert models.is_downloaded(model)
    assert _leftovers() == []


def test_unpinned_revision_is_accepted_with_a_warning(hub, model, caplog):
    hub.table.clear()
    models.ensure_llm(model)
    assert models.is_downloaded(model)
    assert "no pinned digests" in caplog.text


def test_interrupted_swap_is_finished_without_downloading(hub, model):
    staging = models._staging_dir(model)
    staging.mkdir(parents=True)
    for name, data in FILES.items():
        (staging / name).write_bytes(data)
    models._mark(staging, model)
    models.ensure_llm(model)
    assert hub.calls == []
    assert models.is_downloaded(model)


def test_stale_staging_for_another_revision_is_removed(hub, model):
    stale = models.MODELS_DIR / f".{models.model_dir(model).name}.{OLD_REVISION[:12]}.partial"
    stale.mkdir(parents=True)
    models.ensure_llm(model)
    assert not stale.exists()


# ---- integrity.verify ---------------------------------------------------------


def test_verify_tells_drift_from_tampering(tmp_path, model):
    table = {model.repo_id: {model.revision: _digests(FILES)}}
    for name, data in FILES.items():
        (tmp_path / name).write_bytes(data)
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "meta").write_text("")
    (tmp_path / "notes.txt").write_text("")

    ok = integrity.verify(tmp_path, model.repo_id, model.revision, model.revision, table)
    assert ok.status == integrity.VERIFIED and ok.extra == ("notes.txt",)

    drift = integrity.verify(tmp_path, model.repo_id, OLD_REVISION, model.revision, table)
    assert drift.status == integrity.DRIFT and not drift.severe

    (tmp_path / "openvino_tokenizer.xml").write_bytes(b"<evil/>")  # same size
    bad = integrity.verify(tmp_path, model.repo_id, model.revision, model.revision, table)
    assert bad.status == integrity.MISMATCH and bad.severe
    assert bad.differing == ("openvino_tokenizer.xml",)
    assert "corrupt or was altered" in bad.summary()

    (tmp_path / "openvino_detokenizer.xml").unlink()
    gone = integrity.verify(tmp_path, model.repo_id, model.revision, model.revision, table)
    assert gone.status == integrity.INCOMPLETE

    assert integrity.verify(tmp_path, "other/repo", "abc", "", table).status == integrity.UNPINNED


def test_shipped_digests_cover_every_pinned_model():
    table = integrity.load()
    for m in models.LLM_MODELS.values():
        files = table[m.repo_id][m.revision]
        assert "openvino_tokenizer.xml" in files
        assert all(len(d.sha256) == 64 and d.size > 0 for d in files.values())


def test_doctor_checks_the_files(hub, model, monkeypatch):
    monkeypatch.setattr(models, "llm_model", lambda: model)
    _folder(model, revision=model.revision)
    rows = {r.label: r for r in doctor._models()}
    assert rows["llm model files"].status == doctor.OK

    (models.model_dir(model) / "openvino_tokenizer.xml").write_bytes(b"<bad/>")
    rows = {r.label: r for r in doctor._models()}
    assert rows["llm model files"].status == doctor.FAIL


def test_doctor_checks_an_unmarked_folder(hub, model, monkeypatch):
    monkeypatch.setattr(models, "llm_model", lambda: model)
    _folder(model)
    rows = {r.label: r for r in doctor._models()}
    assert rows["llm model"].status == doctor.UNKNOWN
    assert rows["llm model files"].status == doctor.OK


class _FakeTqdm:
    def __init__(self, *a, desc="", total=None, unit="it", **kw):
        self.desc, self.total, self.unit, self.n = desc, total, unit, 0

    def update(self, n=1):
        self.n += n

    def close(self):
        pass


def test_progress_counts_written_bytes_once(monkeypatch):
    # tqdm comes with the AI extras, which CI doesn't install
    fake = types.ModuleType("tqdm.auto")
    fake.tqdm = _FakeTqdm  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tqdm", types.ModuleType("tqdm"))
    monkeypatch.setitem(sys.modules, "tqdm.auto", fake)
    seen = []
    monkeypatch.setattr(models, "_emit", seen.append)
    monkeypatch.setattr(models.time, "monotonic", iter(range(0, 100, 2)).__next__)
    bar_cls = models._progress_class("M")
    # the transfer is compressed, so its total is smaller than the files'
    transfer = bar_cls(desc="Downloading bytes", total=3 * 10**9, unit="B")
    written = bar_cls(desc="Reconstructing (incomplete total...)", total=4 * 10**9, unit="B")
    transfer.update(3 * 10**9)
    written.update(2 * 10**9)
    assert seen[-1] == "Downloading M: 50% (2.0 of 4.0 GB)"
    transfer.close()
    written.close()


def test_doctor_reports_an_older_download_as_drift(hub, model, monkeypatch):
    monkeypatch.setattr(models, "llm_model", lambda: model)
    _folder(model, revision=OLD_REVISION)
    rows = {r.label: r for r in doctor._models()}
    assert rows["llm model"].status == doctor.WARN
    assert rows["llm model files"].status == doctor.WARN


# ---- compile cache --------------------------------------------------------------


@pytest.fixture
def ov_cache(monkeypatch, tmp_path):
    root = tmp_path / "ov-cache"
    monkeypatch.setattr(llm, "OV_CACHE", root)
    monkeypatch.setattr(llm, "LLM_CACHE", root / "llm")
    return root


def _blob(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"blob")


def test_cache_key_changes_with_model_revision_and_openvino(model):
    key = llm.cache_key(model, "2026.4.1-1234-abc/def")
    assert "/" not in key and model.key in key
    assert key != llm.cache_key(model, "2026.5.0")
    other = models.LLM_MODELS["gemma-4-e4b"]
    assert key != llm.cache_key(other, "2026.4.1-1234-abc/def")


def test_prune_keeps_only_the_current_key_and_leaves_ocr_alone(ov_cache):
    _blob(ov_cache / "llm" / "current" / "NPU" / "a.blob")
    _blob(ov_cache / "llm" / "current" / "CPU" / "b.blob")
    _blob(ov_cache / "llm" / "old-model" / "NPU" / "c.blob")
    _blob(ov_cache / "ocr" / "d.blob")
    _blob(ov_cache / "e.blob")  # written by older versions straight into ov-cache
    llm.prune_cache("current")
    assert sorted(p.name for p in (ov_cache / "llm").iterdir()) == ["current"]
    assert (ov_cache / "llm" / "current" / "NPU" / "a.blob").is_file()
    assert (ov_cache / "llm" / "current" / "CPU" / "b.blob").is_file()
    assert (ov_cache / "ocr" / "d.blob").is_file()
    assert not (ov_cache / "e.blob").exists()


def test_load_uses_a_per_device_cache_and_prunes_stale_ones(monkeypatch, tmp_path, ov_cache):
    seen = {}

    class _Genai(types.ModuleType):
        def LLMPipeline(self, path, dev, **props):
            seen[dev] = props["CACHE_DIR"]
            return dev

        VLMPipeline = LLMPipeline

    monkeypatch.setitem(sys.modules, "openvino_genai", _Genai("openvino_genai"))
    monkeypatch.setattr(llm, "_pipe", None)
    monkeypatch.setattr(llm, "_pipe_key", None)
    monkeypatch.setattr(llm, "_inflight", set())
    monkeypatch.setattr(llm, "_schedule_unload", lambda: None)
    monkeypatch.setattr(llm, "_ov_version", lambda: "2026.4.1")
    monkeypatch.setattr(llm.failures, "PATH", tmp_path / "device-failures.json")
    monkeypatch.setattr(llm.failures, "fingerprint", lambda key, rev: {"model": key})
    monkeypatch.setattr(llm.models, "ensure_llm", lambda m, s=None: tmp_path)
    monkeypatch.setattr(llm.models, "is_downloaded", lambda m: True)
    monkeypatch.setattr(llm.devices, "candidates", lambda order: ["CPU"])
    _blob(ov_cache / "llm" / "stale" / "NPU" / "x.blob")

    assert llm._load(lambda _m: None) == "CPU"
    key = llm.cache_key(models.llm_model(), "2026.4.1")
    assert seen["CPU"] == str(ov_cache / "llm" / key / "CPU")
    assert sorted(p.name for p in (ov_cache / "llm").iterdir()) == [key]


def test_load_says_compiling_only_when_there_is_no_cache(monkeypatch, tmp_path, ov_cache):
    class _Genai(types.ModuleType):
        def LLMPipeline(self, path, dev, **props):
            return dev

        VLMPipeline = LLMPipeline

    monkeypatch.setitem(sys.modules, "openvino_genai", _Genai("openvino_genai"))
    monkeypatch.setattr(llm, "_pipe", None)
    monkeypatch.setattr(llm, "_pipe_key", None)
    monkeypatch.setattr(llm, "_inflight", set())
    monkeypatch.setattr(llm, "_schedule_unload", lambda: None)
    monkeypatch.setattr(llm, "_ov_version", lambda: "2026.4.1")
    monkeypatch.setattr(llm.failures, "PATH", tmp_path / "device-failures.json")
    monkeypatch.setattr(llm.failures, "fingerprint", lambda key, rev: {"model": key})
    monkeypatch.setattr(llm.models, "ensure_llm", lambda m, s=None: tmp_path)
    monkeypatch.setattr(llm.models, "is_downloaded", lambda m: True)
    monkeypatch.setattr(llm.devices, "candidates", lambda order: ["CPU"])

    first: list[str] = []
    llm._load(first.append)
    assert "Compiling the model for CPU, one time only…" in first

    llm._drop_pipe()
    key = llm.cache_key(models.llm_model(), "2026.4.1")
    _blob(llm.cache_dir(key, "CPU") / "x.blob")
    second: list[str] = []
    llm._load(second.append)
    assert "Loading model on CPU…" in second
    assert not any("Compiling" in m for m in second)


def test_ticking_repeats_the_status_with_elapsed_time():
    seen: list[str] = []
    ticked = threading.Event()

    def status(msg):
        seen.append(msg)
        if len(seen) > 1:
            ticked.set()

    with llm._ticking(status, "Compiling…", every=0.01):
        assert ticked.wait(2)
    after = len(seen)
    assert seen[0] == "Compiling…"
    assert re.fullmatch(r"Compiling… \(0:\d\d\)", seen[1])
    time.sleep(0.05)
    assert len(seen) == after  # stops with the block


def test_prune_waits_for_background_compiles(monkeypatch, ov_cache):
    _blob(ov_cache / "llm" / "other" / "NPU" / "x.blob")
    monkeypatch.setattr(llm, "_inflight", {"NPU"})
    llm._prune_after_load("current")
    assert (ov_cache / "llm" / "other" / "NPU" / "x.blob").is_file()
