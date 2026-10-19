"""Notifications name vinoPop as the sending app (#80)."""

import re
import subprocess
from pathlib import Path

from core import actions, notify

ROOT = Path(__file__).resolve().parent.parent


def test_argv_names_vinopop_and_its_desktop_entry():
    argv = notify.notify_argv("Calculator", "2+2 = 4")
    assert argv[0] == "notify-send"
    assert "--app-name=vinoPop" in argv
    assert "--hint=string:desktop-entry:io.github.karanshukla.vinoPop" in argv
    assert argv[argv.index("-i") + 1] == "vinopop"
    assert argv[-3:] == ["--", "Calculator", "2+2 = 4"]


def test_desktop_entry_is_the_installed_launcher_id():
    app_id = re.search(r'^APP_ID="([^"]+)"', (ROOT / "install.sh").read_text(), re.M)
    assert app_id and app_id.group(1) == notify.DESKTOP_ENTRY


def test_argv_options():
    argv = notify.notify_argv("t", "b", "dialog-error", None, urgency="critical", transient=False)
    assert "-t" not in argv and "--hint=byte:transient:1" not in argv
    assert argv[argv.index("-u") + 1] == "critical"
    assert argv[argv.index("-i") + 1] == "dialog-error"
    argv = notify.notify_argv("t", "b", extra=("-A", "go=Go", "-w"))
    assert argv[argv.index("-t") + 1] == "3000"
    assert argv[-6:] == ["-A", "go=Go", "-w", "--", "t", "b"]


def test_a_leading_dash_is_not_read_as_an_option():
    argv = notify.notify_argv("Calculator", "-3 = -3")
    assert argv[argv.index("--") + 1 :] == ["Calculator", "-3 = -3"]


def test_notify_is_best_effort(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv))
    actions.notify("Word count", "3 words")
    assert calls and "--app-name=vinoPop" in calls[0]

    def missing(argv, **kw):
        raise FileNotFoundError("notify-send")

    monkeypatch.setattr(subprocess, "run", missing)
    actions.notify("Word count", "3 words")  # no notify-send: not an error


def test_nothing_else_runs_notify_send_directly():
    """Everything goes through notify_argv, so nothing shows up as "notify-send"."""
    offenders = []
    for path in ROOT.glob("**/*.py"):
        rel = path.relative_to(ROOT)
        if (
            rel.parts[0] in ("tests", "docs")
            or rel.parts[0].startswith(".")
            or rel.name == "notify.py"
        ):
            continue
        if re.search(r'\(\s*\[\s*"notify-send"', path.read_text(encoding="utf-8")):
            offenders.append(str(rel))
    assert offenders == []
