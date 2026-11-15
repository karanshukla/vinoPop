"""Helpers behind the Settings window's rows that need GTK types but no window."""

from types import SimpleNamespace

import pytest

pytest.importorskip("gi")


@pytest.fixture
def gui():
    try:
        from ui import settings_gui
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 unavailable: {exc}")
    return settings_gui


def test_a_clean_ocr_install_reports_no_error(gui, monkeypatch):
    monkeypatch.setattr(gui.subprocess, "run", lambda argv, **_kw: SimpleNamespace(returncode=0))
    assert gui._run_ocr_install(["pkexec", "dnf"]) == (True, "")


def test_a_failed_ocr_install_reports_the_end_of_stderr(gui, monkeypatch):
    failed = SimpleNamespace(returncode=1, stderr=b"x" * 200 + b" no such package \n")
    monkeypatch.setattr(gui.subprocess, "run", lambda argv, **_kw: failed)
    ok, error = gui._run_ocr_install(["pkexec", "dnf"])
    assert ok is False
    assert len(error) == 160 and error.endswith("no such package")


def test_an_ocr_install_that_cannot_start_reports_why(gui, monkeypatch):
    def missing(argv, **_kw):
        raise FileNotFoundError("pkexec")

    monkeypatch.setattr(gui.subprocess, "run", missing)
    assert gui._run_ocr_install(["pkexec"]) == (False, "pkexec")


def test_a_missing_capture_tool_is_what_the_hint_names(gui, monkeypatch):
    import capture.screen_ocr as screen_ocr

    monkeypatch.setattr(screen_ocr, "install_command", lambda: "sudo dnf install grim")
    assert (
        gui._ocr_manual_install_hint("no capture tool found")
        == "Install a screen-capture tool: sudo dnf install grim"
    )
    assert gui._ocr_manual_install_hint("OCR engine missing").startswith(
        "Install the OCR engine with: "
    )
