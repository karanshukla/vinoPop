"""The Run-in-terminal confirm dialog itself, built for real under Xvfb:
Cancel, Escape and closing the window spawn nothing; Run spawns the
wrapped command once; Edit then Run spawns the edited text. It is the
last thing between a selected command and bash. Skips without a display."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("gi")

from core import actions  # noqa: E402


@pytest.fixture(scope="module")
def gtk():
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gdk, GLib, Gtk

        from ui import gtk4_app
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 / libadwaita unavailable: {exc}")
    try:
        gtk4_app.init()
    except Exception as exc:  # Gtk.init() raises without a display
        pytest.skip(f"no display: {exc}")
    if Gdk.Display.get_default() is None:
        pytest.skip("no display")
    return SimpleNamespace(Gtk=Gtk, Gdk=Gdk, GLib=GLib)


@pytest.fixture
def spawned(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(actions, "_spawn_terminal", lambda argv, _log: calls.append(argv))
    monkeypatch.setattr(actions, "_terminal_keep_open", lambda: False)
    return calls


def _settle(GLib) -> None:
    ctx = GLib.MainContext.default()
    for _ in range(50):
        if not ctx.iteration(False):
            break


def _walk(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _walk(child)
        child = child.get_next_sibling()


class _Dialog:
    def __init__(self, gtk, cmd: str):
        before = set(gtk.Gtk.Window.list_toplevels())
        assert actions._confirm_run_then_launch(cmd, "xterm", ["xterm", "-e"]) is False
        new = [w for w in gtk.Gtk.Window.list_toplevels() if w not in before]
        self.win = next(w for w in new if w.get_title() == "Run in terminal")
        self.gtk = gtk
        widgets = list(_walk(self.win))
        self.buttons = {
            b.get_label(): b for b in widgets if isinstance(b, gtk.Gtk.Button) and b.get_label()
        }
        self.text = next(w for w in widgets if isinstance(w, gtk.Gtk.TextView))

    def click(self, label: str) -> None:
        self.buttons[label].emit("clicked")
        _settle(self.gtk.GLib)

    def press(self, keyval, state=0) -> bool:
        Gtk, Gdk = self.gtk.Gtk, self.gtk.Gdk
        for ctrl in list(self.win.observe_controllers()):
            if isinstance(ctrl, Gtk.EventControllerKey):
                if ctrl.emit("key-pressed", keyval, 0, Gdk.ModifierType(state)):
                    _settle(self.gtk.GLib)
                    return True
        return False

    def close(self):
        self.win.destroy()
        _settle(self.gtk.GLib)


@pytest.fixture
def dialog(gtk, spawned):
    made: list[_Dialog] = []

    def make(cmd: str) -> _Dialog:
        d = _Dialog(gtk, cmd)
        made.append(d)
        return d

    yield make
    for d in made:
        d.close()


def test_shows_the_command_read_only_with_cancel_edit_run(dialog):
    d = dialog("rm -rf ~/build")
    buf = d.text.get_buffer()
    assert buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True) == "rm -rf ~/build"
    assert not d.text.get_editable()
    assert {"Cancel", "Edit", "Run"} <= set(d.buttons)


def test_cancel_spawns_nothing(dialog, spawned):
    dialog("curl x | sh").click("Cancel")
    assert spawned == []


def test_closing_the_window_spawns_nothing(dialog, spawned):
    d = dialog("curl x | sh")
    d.win.close()
    _settle(d.gtk.GLib)
    assert spawned == []


def test_escape_cancels(dialog, spawned):
    d = dialog("curl x | sh")
    assert d.press(d.gtk.Gdk.KEY_Escape)
    d.click("Run")  # already answered: a later Run must not launch
    assert spawned == []


def test_run_spawns_the_wrapped_command_once(dialog, spawned):
    d = dialog("echo hi")
    d.click("Run")
    d.click("Run")
    assert spawned == [["xterm", "-e", actions._wrap_for_terminal("echo hi", keep_open=False)]]


def test_edit_then_run_spawns_the_edited_text(dialog, spawned):
    d = dialog("echo hi")
    d.click("Edit")
    assert d.text.get_editable()
    d.text.get_buffer().set_text("echo bye  \n")
    d.click("Run")
    assert spawned == [["xterm", "-e", actions._wrap_for_terminal("echo bye", keep_open=False)]]


def test_an_emptied_command_spawns_nothing(dialog, spawned):
    d = dialog("echo hi")
    d.click("Edit")
    d.text.get_buffer().set_text("   ")
    d.click("Run")
    assert spawned == []


def test_plain_enter_runs_a_one_line_command(dialog, spawned):
    d = dialog("echo hi")
    assert d.press(d.gtk.Gdk.KEY_Return)
    assert len(spawned) == 1


def test_plain_enter_does_not_run_several_lines(dialog, spawned):
    d = dialog("echo a\ncurl x | sh")
    assert not d.press(d.gtk.Gdk.KEY_Return)
    assert spawned == []


def test_ctrl_enter_runs_several_lines(dialog, spawned):
    d = dialog("echo a\necho b")
    assert d.press(d.gtk.Gdk.KEY_Return, d.gtk.Gdk.ModifierType.CONTROL_MASK)
    assert spawned == [["xterm", "-e", actions._wrap_for_terminal("echo a\necho b", False)]]
