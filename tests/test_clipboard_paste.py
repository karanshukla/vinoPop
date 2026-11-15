"""The picker's paste: the entry goes on the clipboard, the window the
picker was opened over is activated, and the text is typed or pasted
there. X11, wmctrl and the clipboard tools are faked."""

from types import SimpleNamespace

import pytest

pytest.importorskip("gi")  # clipboard_history is a GTK plugin


@pytest.fixture
def ch():
    try:
        import clipboard_history
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 unavailable: {exc}")
    return clipboard_history


class _SyncThread:
    def __init__(self, target=None, **_kw):
        self.target = target

    def start(self):
        self.target()


@pytest.fixture
def desk(ch, monkeypatch):
    rec = SimpleNamespace(
        keys=[], keyups=[], activated=[], typed=[], runs=[], active="42", typing_works=True
    )
    monkeypatch.setattr(ch.threading, "Thread", _SyncThread)
    monkeypatch.setattr(ch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ch, "_wl_clipboard", lambda: False)
    monkeypatch.setattr(ch, "_x_can_inject", lambda: True)
    monkeypatch.setattr(ch.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(ch, "_paste_keystroke_for_window", lambda wid: "ctrl+v")
    monkeypatch.setattr(ch, "_get_active_window", lambda: rec.active)
    monkeypatch.setattr(ch, "_x_activate", lambda wid: rec.activated.append(wid) or True)
    monkeypatch.setattr(
        ch, "_x_key", lambda combo, repeat=1, **_kw: rec.keys.append((combo, repeat))
    )
    monkeypatch.setattr(ch, "_x_keyup", lambda *names: rec.keyups.append(names))
    monkeypatch.setattr(ch.subprocess, "run", lambda argv, **kw: rec.runs.append((argv, kw)))

    def type_text(text, delay_s=0.0):
        rec.typed.append(text)
        return rec.typing_works

    monkeypatch.setattr(ch, "_x11", lambda: SimpleNamespace(type_text=type_text))
    return rec


def _text(ch, text):
    return ch.Entry(id="e", timestamp=0, kind="text", text=text)


def test_text_goes_on_the_clipboard_and_the_target_is_activated(ch, desk):
    ch._paste_to_window(_text(ch, "hello"), "42")
    assert desk.runs[0] == (
        ["xclip", "-selection", "clipboard"],
        {"input": b"hello", "check": False, "timeout": 2.0},
    )
    assert desk.runs[1][0] == ["wmctrl", "-ia", "0x0000002a"]
    assert desk.activated == ["42"]


def test_a_short_line_is_typed_not_pasted(ch, desk):
    ch._paste_to_window(_text(ch, "hello"), "42")
    assert desk.typed == ["hello"]
    assert desk.keys == []


def test_text_at_the_typing_limit_is_typed_and_one_over_is_pasted(ch, desk):
    ch._paste_to_window(_text(ch, "a" * 2048), "42")
    assert desk.typed == ["a" * 2048] and desk.keys == []
    ch._paste_to_window(_text(ch, "a" * 2049), "42")
    assert desk.typed == ["a" * 2048] and desk.keys == [("ctrl+v", 1)]


def test_several_lines_are_pasted_so_a_shell_does_not_run_each(ch, desk):
    ch._paste_to_window(_text(ch, "ls\nrm -rf x"), "42")
    assert desk.typed == []
    assert desk.keys == [("ctrl+v", 1)]


def test_failed_typing_falls_back_to_the_paste_key(ch, desk):
    desk.typing_works = False
    ch._paste_to_window(_text(ch, "hello"), "42")
    assert desk.keys == [("ctrl+v", 1)]


def test_a_cursor_placeholder_moves_left_after_the_paste(ch, desk):
    ch._paste_to_window(_text(ch, "a\nb"), "42", cursor_left=3)
    assert desk.keys == [("ctrl+v", 1), ("Left", 3)]


def test_a_window_that_did_not_take_focus_is_activated_again(ch, desk):
    desk.active = "7"
    ch._paste_to_window(_text(ch, "hello"), "42")
    assert desk.activated == ["42", "42"]


def test_held_modifiers_are_released_before_and_after(ch, desk):
    ch._paste_to_window(_text(ch, "hello"), "42")
    before, after = desk.keyups
    assert {"super", "Super_L", "Shift_R", "Alt_L"} <= set(before)
    assert after == (
        "ctrl",
        "shift",
        "alt",
        "super",
        "Control_L",
        "Control_R",
        "Super_L",
        "Super_R",
    )


def test_without_a_target_window_only_the_clipboard_is_set(ch, desk):
    ch._paste_to_window(_text(ch, "hello"), None)
    assert len(desk.runs) == 1
    assert desk.activated == [] and desk.typed == [] and desk.keys == []


def test_an_image_whose_file_is_gone_is_not_pasted(ch, desk, tmp_path):
    entry = ch.Entry(id="i", timestamp=0, kind="image", image_path=str(tmp_path / "gone.png"))
    ch._paste_to_window(entry, "42")
    assert desk.runs == [] and desk.activated == []


def test_an_image_is_handed_to_xclip_by_path_and_pasted(ch, desk, tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(b"png")
    entry = ch.Entry(id="i", timestamp=0, kind="image", image_path=str(png))
    ch._paste_to_window(entry, "42")
    assert desk.runs[0] == (
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-i", str(png)],
        {"check": False},
    )
    assert desk.keys == [("ctrl+v", 1)]


def test_on_kde_wayland_text_goes_through_wl_copy(ch, desk, monkeypatch):
    monkeypatch.setattr(ch, "_wl_clipboard", lambda: True)
    ch._paste_to_window(_text(ch, "hello"), None)
    assert desk.runs == [(["wl-copy"], {"input": b"hello", "check": False, "timeout": 2.0})]
