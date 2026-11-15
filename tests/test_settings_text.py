"""The Settings window's list editors and row subtitles (ui/settings_text.py)."""

from ui import settings_text as text


def test_patterns_are_one_per_line_without_blanks():
    assert text.patterns_from_text("  KeePassXC \n\n bank.no\n   \n") == ["KeePassXC", "bank.no"]


def test_no_patterns_shows_the_hint():
    assert text.blocked_patterns_subtitle([], "Click to add.") == "Click to add."


def test_one_pattern_is_singular():
    assert text.blocked_patterns_subtitle(["a"], "") == "1 pattern blocked: a. Click to edit."


def test_three_patterns_are_all_named():
    assert (
        text.blocked_patterns_subtitle(["a", "b", "c"], "")
        == "3 patterns blocked: a, b, c. Click to edit."
    )


def test_a_fourth_pattern_is_counted_not_named():
    assert (
        text.blocked_patterns_subtitle(["a", "b", "c", "d"], "")
        == "4 patterns blocked: a, b, c, +1 more. Click to edit."
    )


def test_variables_are_name_equals_value_lines():
    raw = "email = a@b.c\n# a comment\nno equals sign\n = nameless\nsig=Best, = K\nemail = z@b.c"
    assert text.variables_from_text(raw) == {"email": "z@b.c", "sig": "Best, = K"}


def test_variables_round_trip_sorted_by_name():
    as_text = text.variables_as_text({"sig": "K", "email": "a@b.c"})
    assert as_text == "email = a@b.c\nsig = K"
    assert text.variables_from_text(as_text) == {"email": "a@b.c", "sig": "K"}


def test_variables_subtitle_names_the_first_three_tokens():
    assert text.variables_subtitle({}, "Click to add.") == "Click to add."
    assert text.variables_subtitle({"b": "1"}, "") == "1 variable defined: {var:b}. Click to edit."
    four = dict.fromkeys(["d", "a", "c", "b"], "")
    assert (
        text.variables_subtitle(four, "")
        == "4 variables defined: {var:a}, {var:b}, {var:c}, +1 more. Click to edit."
    )


def test_a_failed_check_shows_its_fix_and_a_passed_one_does_not():
    failed = {"status": "FAIL", "label": "NPU", "detail": "no driver", "fix": "install it"}
    passed = {**failed, "status": "ok"}
    assert text.doctor_check_detail(failed) == "no driver\nFix: install it"
    assert text.doctor_check_detail(passed) == "no driver"


def test_the_doctor_report_aligns_labels_and_indents_fixes():
    checks = [
        {"status": "ok", "label": "GTK", "detail": "4.14"},
        {"status": "warn", "label": "ydotool", "detail": "missing", "fix": "dnf install ydotool"},
    ]
    assert text.doctor_report(checks) == (
        "[  ok] GTK      4.14\n[warn] ydotool  missing\n       fix: dnf install ydotool"
    )
    assert text.doctor_report([]) == ""
