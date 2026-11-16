"""main.py rules that have no other home: what the log may say about a selection."""

import logging

import pytest

pytest.importorskip("gi")
pytest.importorskip("Xlib")

SECRET = "hunter2-password"


@pytest.fixture(scope="module")
def main_mod():
    mp = pytest.MonkeyPatch()
    mp.setenv("GDK_BACKEND", "x11")
    try:
        import main
    finally:
        mp.undo()
    return main


def test_info_logs_only_the_selection_length(main_mod, caplog):
    with caplog.at_level(logging.INFO, logger="vinopop"):
        main_mod._log_selection("text", SECRET)
    assert f"{len(SECRET)}-char selection" in caplog.text
    assert SECRET not in caplog.text


def test_debug_logs_the_selected_text(main_mod, caplog):
    with caplog.at_level(logging.DEBUG, logger="vinopop"):
        main_mod._log_selection("text", SECRET)
    assert SECRET in caplog.text
