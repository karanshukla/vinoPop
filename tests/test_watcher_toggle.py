"""The Settings auto-popup switch starts and stops the watcher live (#23)."""

import types

import pytest

pytest.importorskip("gi")


@pytest.fixture(scope="module")
def main_mod():
    # main picks a GDK backend at import and may set GDK_BACKEND; a preset
    # value is left alone, so pin one for the import and put it back.
    mp = pytest.MonkeyPatch()
    mp.setenv("GDK_BACKEND", "x11")
    try:
        import main
    finally:
        mp.undo()
    return main


class FakeTray:
    def __init__(self):
        self.refreshes = 0

    def refresh(self):
        self.refreshes += 1


def _app(show_on_selection, running):
    app = types.SimpleNamespace(
        settings={"show_on_selection": show_on_selection},
        watcher=object() if running else None,
        tray=FakeTray(),
        calls=[],
    )
    app._start_watcher = lambda: app.calls.append("start")
    app._stop_watcher = lambda: app.calls.append("stop")
    return app


@pytest.mark.parametrize(
    ("show", "running", "expected"),
    [
        (True, False, ["start"]),
        (False, True, ["stop"]),
        (True, True, []),
        (False, False, []),
    ],
)
def test_sync_matches_watcher_to_setting(main_mod, show, running, expected):
    app = _app(show, running)
    main_mod.App._sync_watcher_with_settings(app)
    assert app.calls == expected
    assert app.tray.refreshes == 1


def test_sync_without_tray(main_mod):
    app = _app(True, False)
    app.tray = None
    main_mod.App._sync_watcher_with_settings(app)
    assert app.calls == ["start"]
