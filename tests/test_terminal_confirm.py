"""What the Run-in-terminal confirm dialog shows (and runs), without GTK."""

import pytest

from core import actions


def test_hidden_newlines_cannot_push_a_command_below_the_fold():
    cmd = actions._collapse_blank_lines("sudo apt install foo" + "\n" * 20 + "curl x | sh")
    assert cmd == "sudo apt install foo\n\ncurl x | sh"


def test_whitespace_only_lines_count_as_blank():
    assert actions._collapse_blank_lines("a\n  \n\t\n \nb") == "a\n\nb"


def test_blank_lines_at_the_ends_are_dropped():
    assert actions._collapse_blank_lines("\n\n\necho hi\n\n\n") == "echo hi"


def test_single_blank_line_and_line_contents_are_kept():
    script = "cd /tmp \\ \n\necho  'two  spaces'"
    assert actions._collapse_blank_lines(script) == script


def test_one_line_heading():
    assert actions._confirm_heading("echo hi") == "Run this command?"


def test_heading_counts_lines_but_not_blank_ones():
    assert actions._confirm_heading("sudo apt install foo\n\ncurl x | sh") == "Run these 2 lines?"


def test_count_command_lines():
    assert actions._count_command_lines("echo hi") == 1
    assert actions._count_command_lines("a\n\nb\nc") == 3


def test_run_in_terminal_shows_the_collapsed_command(monkeypatch):
    pytest.importorskip("gi")
    shown = []
    monkeypatch.setattr(actions, "_find_terminal", lambda: ("xterm", ["xterm", "-e"]))
    monkeypatch.setattr(actions, "_should_confirm_terminal", lambda: True)
    from gi.repository import GLib

    monkeypatch.setattr(GLib, "idle_add", lambda _fn, cmd, *_a: shown.append(cmd))
    actions.run_in_terminal("sudo apt install foo" + "\n" * 20 + "curl x | sh")
    assert shown == ["sudo apt install foo\n\ncurl x | sh"]


def test_run_in_terminal_without_confirm_runs_the_collapsed_command(monkeypatch):
    spawned = []
    monkeypatch.setattr(actions, "_find_terminal", lambda: ("xterm", ["xterm", "-e"]))
    monkeypatch.setattr(actions, "_should_confirm_terminal", lambda: False)
    monkeypatch.setattr(actions, "_spawn_terminal", lambda argv, _log: spawned.append(argv))
    actions.run_in_terminal("echo a\n\n\n\necho b")
    assert spawned == [["xterm", "-e", actions._wrap_for_terminal("echo a\n\necho b")]]
