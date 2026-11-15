"""What each {placeholder} in a snippet body turns into, with the clock,
the clipboard and the user's name pinned."""

import time

import pytest

pytest.importorskip("gi")  # clipboard_history is a GTK plugin

NOW = time.struct_time((2026, 5, 27, 14, 30, 0, 2, 147, -1))  # a Wednesday


@pytest.fixture
def ch(monkeypatch):
    try:
        import clipboard_history
    except (ImportError, ValueError) as exc:
        pytest.skip(f"GTK 4 unavailable: {exc}")
    monkeypatch.setattr(clipboard_history.time, "localtime", lambda *_a: NOW)
    return clipboard_history


def render(ch, text, answers=None):
    return ch.render_placeholders(text, lambda _fields: answers)


def test_text_without_a_brace_comes_back_untouched(ch):
    assert render(ch, "plain") == ("plain", 0, False)


@pytest.mark.parametrize(
    ("token", "want"),
    [
        ("{date}", "2026-05-27"),
        ("{time}", "14:30"),
        ("{datetime}", "2026-05-27 14:30"),
        ("{weekday}", "Wednesday"),
        ("{date:%d.%m.%Y}", "27.05.2026"),
        ("{date:+7d}", "2026-06-03"),
        ("{date:-1w}", "2026-05-20"),
        ("{date:+1m}", "2026-06-26"),  # a month is 30 days
        ("{date:+1y}", "2027-05-27"),  # a year is 365
        ("{date:+7d:%A %d %B}", "Wednesday 03 June"),
    ],
)
def test_dates_and_times(ch, token, want):
    assert render(ch, f"[{token}]")[0] == f"[{want}]"


def test_an_unknown_token_is_left_as_written(ch):
    assert render(ch, "{nope} {date:}")[0] == "{nope} {date:}"


def test_name_is_the_users_full_name(ch, monkeypatch):
    monkeypatch.setattr(ch, "_full_user_name", lambda: "Alex Morgan")
    assert render(ch, "Best, {name}")[0] == "Best, Alex Morgan"


def test_the_clipboard_is_read_once_however_often_it_is_used(ch, monkeypatch):
    reads = []
    monkeypatch.setattr(ch, "_read_clipboard_text", lambda: reads.append(1) or "copied")
    assert render(ch, "{clipboard}/{clipboard}")[0] == "copied/copied"
    assert len(reads) == 1


def test_selection_is_the_highlighted_text(ch, monkeypatch):
    monkeypatch.setattr(ch, "_read_primary_selection", lambda: "marked")
    assert render(ch, "Quote: {selection}")[0] == "Quote: marked"


def test_cursor_is_removed_and_counted_from_the_end(ch):
    assert render(ch, "[ ] {cursor} done") == ("[ ]  done", 5, False)


def test_only_the_first_cursor_counts(ch):
    assert render(ch, "a{cursor}b{cursor}c") == ("abc", 2, False)


def test_every_ask_field_is_asked_for_once_in_order(ch):
    asked = []

    def ask(fields):
        asked.append(fields)
        return {"Who": "Sam", "Status": "Open"}

    text = "Hi {ask:Who}, {ask:Status|Open|Closed}. Bye {ask:Who}"
    assert ch.render_placeholders(text, ask) == ("Hi Sam, Open. Bye Sam", 0, False)
    assert asked == [[("Who", None), ("Status", ["Open", "Closed"])]]


def test_an_ask_without_a_label_is_called_value(ch):
    asked = []
    ch.render_placeholders("{ask: } {ask: |a|b}", lambda fields: asked.append(fields) or {})
    assert asked == [[("Value", None)]]


def test_an_unanswered_ask_field_is_empty(ch):
    assert render(ch, "<{ask:Who}>", answers={})[0] == "<>"


def test_cancelling_the_ask_dialog_cancels_the_paste(ch):
    assert render(ch, "Hi {ask:Who} {date}", answers=None) == ("Hi {ask:Who} {date}", 0, True)


def test_var_comes_from_the_snippet_variables_setting(ch, monkeypatch):
    stored = {"snippet_variables": {"email": "a@example.org"}}
    monkeypatch.setattr(ch, "_settings", type("S", (), {"get": lambda _s, k, d=None: stored[k]})())
    assert render(ch, "{var:email}|{var:missing}|")[0] == "a@example.org||"


def test_every_token_the_help_chips_insert_is_a_placeholder(ch):
    for _label, token, *_rest in ch._PLACEHOLDER_CHIPS:
        assert ch._PLACEHOLDER_RE.fullmatch(token), token
