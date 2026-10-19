"""The ~4 GB model must not download at startup until the user says yes
(issue #22). snapshot_download is faked, so nothing touches the network."""

import json
import sys
import types

import pytest

pytest.importorskip("gi")

import main  # noqa: E402
import vino_ai  # noqa: E402
from core import settings  # noqa: E402
from vino_ai import models  # noqa: E402


@pytest.fixture
def start_app(monkeypatch, tmp_path):
    """Build main.App with the GTK, X11 and hotkey parts stubbed out.
    Returns (start(stored settings or None), snapshot_download calls)."""
    calls = []
    fake_hub = types.ModuleType("huggingface_hub")
    fake_hub.snapshot_download = lambda **kw: calls.append(kw)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)
    monkeypatch.setattr(vino_ai, "available", lambda: (True, ""))
    monkeypatch.setattr(models, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(models, "_progress_class", lambda label: None)  # tqdm is AI-only
    monkeypatch.setattr(main, "PopupWindow", lambda **kw: types.SimpleNamespace())
    monkeypatch.setattr(main.plugin_loader, "load_all", lambda: None)
    monkeypatch.setattr(main, "_notify", lambda *a: None)
    for name in (
        "_start_watcher",
        "_start_hotkey",
        "_start_clipboard_hotkey",
        "_start_ocr_hotkey",
        "_maybe_start_dblclick_watcher",
        "_maybe_first_run",
    ):
        monkeypatch.setattr(main.App, name, lambda self: None)

    def start(stored=None):
        path = tmp_path / "settings.json"
        if stored is not None:
            path.write_text(json.dumps(stored))
        s = settings.Settings(path)
        monkeypatch.setattr(main, "get_settings", lambda: s)
        monkeypatch.setattr(settings, "get_settings", lambda: s)
        app = main.App(enable_tray=False)
        if app._prefetch_thread is not None:
            app._prefetch_thread.join(5)
        return app

    return start, calls


def _complete_model(key):
    path = models.model_dir(models.LLM_MODELS[key])
    path.mkdir(parents=True)
    for name in (
        "openvino_language_model.xml",
        "openvino_language_model.bin",
        "openvino_tokenizer.xml",
    ):
        (path / name).write_text("")


def test_fresh_config_does_not_download(start_app):
    start, calls = start_app
    app = start()
    assert app._prefetch_thread is None
    assert calls == []


def test_old_default_true_is_not_consent(start_app):
    # Every settings.json saved before the fix stores the old default.
    start, calls = start_app
    start({"ai_prefetch_model": True})
    assert calls == []


def test_yes_from_onboarding_or_settings_downloads(start_app):
    start, calls = start_app
    start({"ai_prefetch_model": True, "ai_prefetch_asked": True})
    assert len(calls) == 1


def test_later_does_not_download(start_app):
    start, calls = start_app
    start({"ai_prefetch_model": False, "ai_prefetch_asked": True})
    assert calls == []


def test_existing_install_with_a_model_keeps_prefetching(start_app):
    # Old default plus a model already on disk: switching models still
    # prefetches the new one, as it did before the fix.
    start, calls = start_app
    _complete_model("gemma-4-e2b")
    start({"ai_prefetch_model": True, "ai_llm_model": "gemma-4-e4b"})
    assert len(calls) == 1


def test_onboarding_asks_only_when_there_is_a_model_to_download(start_app, tmp_path):
    from ui import onboarding

    def asks():
        pages = onboarding._build_pages(settings.Settings(tmp_path / "s.json"))
        return any(p.get("ask_model") for p in pages)

    assert asks()
    _complete_model(models.DEFAULT_LLM)
    assert not asks()
