"""What an OCR capture does with its text and image, with describe off and on."""

import subprocess
import threading

import pytest

from capture import screen_ocr


@pytest.fixture
def capture(monkeypatch, tmp_path):
    image = tmp_path / "capture.png"
    image.write_bytes(b"png")
    calls = {"notify": [], "staged": [], "describe": [], "offer": []}
    state = {"text": "hello", "describe": False}
    monkeypatch.setattr(screen_ocr, "is_supported", lambda: (True, ""))
    monkeypatch.setattr(screen_ocr, "_capture_image", lambda: (image, None))
    monkeypatch.setattr(screen_ocr, "_recognize", lambda _p: state["text"])
    monkeypatch.setattr(screen_ocr, "_describe_enabled", lambda: state["describe"])
    monkeypatch.setattr(screen_ocr, "_notify", lambda *a, **_k: calls["notify"].append(a))
    monkeypatch.setattr(screen_ocr, "_stage_text", calls["staged"].append)
    monkeypatch.setattr(screen_ocr, "_describe", lambda *a: calls["describe"].append(a))
    monkeypatch.setattr(screen_ocr, "_offer_describe", lambda *a: calls["offer"].append(a))
    return image, state, calls


def test_text_goes_to_the_clipboard_and_the_image_is_deleted(capture):
    image, _state, calls = capture
    screen_ocr.run_ocr_to_clipboard()
    assert calls["staged"] == ["hello"] and calls["notify"][0][0] == "OCR captured"
    assert not calls["offer"] and not image.exists()


def test_no_text_without_describe_says_so(capture):
    image, state, calls = capture
    state["text"] = ""
    screen_ocr.run_ocr_to_clipboard()
    assert "No text found" in calls["notify"][0][1]
    assert not calls["describe"] and not image.exists()


def test_no_text_with_describe_describes_straight_away(capture):
    image, state, calls = capture
    state.update(text="", describe=True)
    screen_ocr.run_ocr_to_clipboard()
    assert calls["describe"] == [(image, "")] and not calls["notify"]
    assert image.exists()  # the bubble's worker deletes it


def test_text_with_describe_offers_the_button(capture):
    image, state, calls = capture
    state["describe"] = True
    screen_ocr.run_ocr_to_clipboard()
    assert calls["staged"] == ["hello"] and not calls["notify"]
    assert calls["offer"][0][2:] == (image, "hello") and image.exists()


class _SyncThread:
    def __init__(self, target, **_kw):
        self._target = target

    def start(self):
        self._target()


@pytest.mark.parametrize("stdout,described", [("describe\n", True), ("", False)])
def test_offer_describes_only_on_click(monkeypatch, tmp_path, stdout, described):
    image = tmp_path / "capture.png"
    image.write_bytes(b"png")
    seen = []
    monkeypatch.setattr(threading, "Thread", _SyncThread)
    monkeypatch.setattr(
        screen_ocr.subprocess,
        "run",
        lambda args, **_k: subprocess.CompletedProcess(args, 0, stdout, ""),
    )
    monkeypatch.setattr(screen_ocr, "_describe", lambda *a: seen.append(a))
    screen_ocr._offer_describe("OCR captured", "5 chars", image, "hello")
    assert (seen == [(image, "hello")]) is described
    assert image.exists() is described
