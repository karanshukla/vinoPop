"""The Run-in-terminal wrapper string, run through a real bash (no terminal or GTK)."""

import shutil
import subprocess

import pytest

import actions
from classifier import strip_shell_prompts

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _run(wrapped: str, stdin: str = "") -> subprocess.CompletedProcess:
    # 'exec bash' reads stdin, so feeding it a command shows whether the
    # shell is still alive after the user's command ran
    return subprocess.run(
        ["bash", "-c", wrapped], input=stdin, capture_output=True, text=True, timeout=10
    )


def test_keep_open_survives_a_trailing_comment():
    wrapped = actions._wrap_for_terminal("echo hi # say hi", keep_open=True)
    assert wrapped.endswith("\nexec bash")
    out = _run(wrapped, "echo STILL_OPEN\n").stdout
    assert "hi\n" in out
    assert "STILL_OPEN" in out


def test_without_keep_open_there_is_no_shell_afterwards():
    wrapped = actions._wrap_for_terminal("echo hi # say hi", keep_open=False)
    assert "exec bash" not in wrapped
    assert "STILL_OPEN" not in _run(wrapped, "echo STILL_OPEN\n").stdout


def test_trailing_backslash_does_not_swallow_exec_bash():
    wrapped = actions._wrap_for_terminal("echo hi \\", keep_open=True)
    assert "STILL_OPEN" in _run(wrapped, "echo STILL_OPEN\n").stdout


def test_command_is_echoed_first():
    out = _run(actions._wrap_for_terminal("echo hi", keep_open=False)).stdout
    assert out.splitlines()[0].endswith("$\033[0m echo hi")
    assert out.splitlines()[1] == "hi"


def test_prompted_selection_runs_without_the_prompt():
    cmd = strip_shell_prompts("$ echo hi\nhi\n$ echo there")
    proc = _run(actions._wrap_for_terminal(cmd, keep_open=False))
    assert "command not found" not in proc.stderr
    assert proc.stdout.splitlines()[-2:] == ["hi", "there"]
