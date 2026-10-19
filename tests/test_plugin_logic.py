"""Behaviour of the bundled plugins' pure functions and handlers."""

import hashlib
import json

import pytest


@pytest.fixture
def replaced(monkeypatch):
    """Capture what a handler would paste over the selection, and silence notifications."""
    from core import actions

    out: list[str] = []
    monkeypatch.setattr(actions, "replace_selection", out.append)
    monkeypatch.setattr(actions, "notify", lambda *a, **kw: out.append("<notify>"))
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)
    return out


# ---- calculator -------------------------------------------------------------


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("2+2", "4"),
        ("2^8", "256"),
        ("0.1+0.2", "0.3"),
        ("10/4", "2.5"),
        ("6/3", "2"),
        ("sqrt(16)", "4"),
        ("-3 * (2 + 1)", "-9"),
        ("2**0.5", "1.4142135623731"),
    ],
)
def test_calculator_results(replaced, expr, expected):
    import calculator

    calculator._calc(expr)
    assert replaced[0] == expected


def test_calculator_handles_every_node_type_without_ast_num():
    # ast.Num is gone in Python 3.14; every BinOp used to pass through it
    import ast

    import calculator

    assert calculator._eval(ast.parse("1+2*3", mode="eval").body) == 7


def test_calculator_refuses_huge_powers(replaced):
    import calculator

    calculator._calc("9**9**9")
    assert replaced == []  # error path notifies, never pastes


# ---- converters -------------------------------------------------------------


@pytest.mark.parametrize(
    "stamp",
    ["2024-01-02T03:04:05Z", "2024-01-02T03:04:05.250Z", "2024-01-02T03:04:05+01:00"],
)
def test_timestamp_accepts_what_the_predicate_accepts(replaced, stamp):
    import converters

    assert converters._looks_like_timestamp(stamp)
    converters._timestamp_convert(stamp)
    assert replaced[0].isdigit()


def test_timestamp_z_is_utc(replaced):
    import converters

    converters._timestamp_convert("2024-01-02T03:04:05Z")
    assert replaced[0] == "1704164645"


def test_color_hsl_converts(replaced):
    import converters

    assert converters._looks_like_color("hsl(120, 100%, 50%)")
    converters._color_convert("hsl(120, 100%, 50%)")
    assert replaced[0] == "#00ff00"


def test_color_hex_and_rgb(replaced):
    import converters

    converters._color_convert("#fff")
    converters._color_convert("rgb(255, 0, 0)")
    assert replaced == ["rgb(255, 255, 255)", "#ff0000"]


# ---- case_convert -----------------------------------------------------------


def test_title_case_keeps_apostrophes_intact():
    import case_convert

    assert case_convert._title_case("it's a dog's life") == "It's A Dog's Life"
    assert case_convert._title_case("don’t stop") == "Don’t Stop"
    assert case_convert._title_case("3rd place") == "3rd Place"
    assert not case_convert._not_already_title("It's Fine")


def test_slugify_transliterates_nordic_letters():
    import case_convert

    assert case_convert._slug("Blåbærsyltetøy & Øl!") == "blabaersyltetoy-ol"
    assert case_convert._slug("Straße") == "strasse"
    assert case_convert._slug("  Hello,   World  ") == "hello-world"


def test_slugify_never_replaces_text_with_nothing(replaced):
    import case_convert

    case_convert._slugify("привет")
    assert replaced == ["<notify>"]


# ---- base64 -----------------------------------------------------------------


def test_base64_decodes_wrapped_and_urlsafe_input(replaced):
    import base64_codec

    wrapped = "SGVsbG8s\nIHdvcmxk\nIQ=="
    assert base64_codec._looks_like_base64(wrapped)
    assert base64_codec._decode_base64(wrapped) == "Hello, world!"
    assert base64_codec._decode_base64("PDw_Pz8-Pg") == "<<???>>"
    assert base64_codec._decode_base64("SGk") == "Hi"  # padding optional


def test_base64_binary_payload_is_not_pasted_as_mojibake(replaced):
    import base64_codec

    base64_codec._decode("/9j/4AAQSkZJRg==")
    assert replaced == []


# ---- text_transformations ---------------------------------------------------


def test_strip_html_decodes_entities_once(replaced):
    import text_transformations as tt

    tt._strip_html("<p>a &amp;lt; b &amp; c&nbsp;d</p>")
    assert replaced[0] == "a &lt; b & c d"


def test_remove_diacritics_leaves_compatibility_chars_and_kana():
    import text_transformations as tt

    assert tt._strip_marks("café Ångström") == "cafe Angstrom"
    assert tt._strip_marks("x² ™ ﬁ") == "x² ™ ﬁ"
    assert tt._strip_marks("한국어 が") == "한국어 が"
    assert tt._has_diacritics("naïve")
    assert not tt._has_diacritics("x² 한국어")


# ---- dev_hashes -------------------------------------------------------------


def test_md5_works_where_fips_forbids_insecure_hashes(replaced, monkeypatch):
    import dev_hashes

    real_md5 = hashlib.md5

    def fips_md5(data=b"", *, usedforsecurity=True):
        if usedforsecurity:
            raise ValueError("disabled for FIPS")
        return real_md5(data)

    monkeypatch.setattr(hashlib, "md5", fips_md5)
    dev_hashes._md5("hello world, this is a key")
    assert replaced[0] == real_md5(b"hello world, this is a key").hexdigest()


def test_jwt_decode_with_odd_header_does_not_crash(replaced):
    import base64

    import dev_hashes

    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    dev_hashes._jwt_decode(f"{seg([1, 2])}.{seg({'sub': 'x'})}.sig")
    assert json.loads(replaced[0]) == {"sub": "x"}


# ---- wordcount --------------------------------------------------------------


def test_wordcount_counts_all_whitespace_and_lines(monkeypatch):
    import subprocess

    import wordcount

    bodies = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: bodies.append(argv[-1]))
    wordcount._count("a b\r\nc d\n")
    assert bodies == ["4 words, 9 chars (4 without whitespace), 2 lines"]


# ---- #33: per-plugin fixes ------------------------------------------------------


def test_json_format_refuses_duplicate_keys(replaced):
    import json_format

    json_format._format('{"a":1,"a":2}')
    assert replaced == []  # notified the error, pasted nothing


def test_json_format_keeps_numbers_exactly_as_written(replaced):
    import json_format

    json_format._format("[1.00000000000000000001, 12345678901234567890.5, 1e400]")
    assert replaced[0] == "[\n  1.00000000000000000001,\n  12345678901234567890.5,\n  1e400\n]"


def test_json_format_handles_huge_ints_and_refuses_nan(replaced):
    import json_format

    json_format._format("[" + "9" * 5000 + "]")
    assert replaced[0] == "[\n  " + "9" * 5000 + "\n]"
    json_format._format("[NaN, Infinity]")
    assert replaced[1:] == []


def test_json_format_matches_json_dumps_for_ordinary_json():
    import json_format

    text = '{"a": [1, 2.5, {"b": null, "c": "é\\n"}], "d": {}, "e": [], "f": true}'
    assert json_format._pretty(text) == json.dumps(json.loads(text), indent=2, ensure_ascii=False)


@pytest.mark.parametrize(
    "func,text,expected",
    [
        ("_sort_lines", "b\na\n", "a\nb\n"),
        ("_sort_lines", "y\r\nx\r\n", "x\r\ny\r\n"),
        ("_sort_lines", "b\na", "a\nb"),
        ("_dedupe_lines", "x\r\ny\r\nx\r\n", "x\r\ny\r\n"),
        ("_trim_lines", " a \n b \n", "a\nb\n"),
    ],
)
def test_line_transforms_keep_line_endings(replaced, func, text, expected):
    import text_transformations as tt

    getattr(tt, func)(text)
    assert replaced[0] == expected


@pytest.mark.parametrize("text", ["2024-10-17", "555-1234", "(555) 123-4567", "17/10/2024"])
def test_calculator_isnt_offered_on_dates_and_phone_numbers(text):
    import calculator

    assert not calculator._looks_like_math(text)


@pytest.mark.parametrize("text", ["10-4", "2024-10", "100 - 25", "3/4"])
def test_calculator_still_offered_on_plain_subtraction(text):
    import calculator

    assert calculator._looks_like_math(text)


@pytest.mark.parametrize("expr", ["999999**1000", "1e308*10", "(-1)**0.5"])
def test_calculator_reports_unpasteable_results_instead_of_raising(replaced, expr):
    import calculator

    calculator._calc(expr)
    assert replaced == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("a/b c?d", "a%2Fb%20c%3Fd"),
        ("https://example.com/a b?q=x y&r=é", "https://example.com/a%20b?q=x%20y&r=%C3%A9"),
        ("https://example.com/a%20b?q=100%", "https://example.com/a%20b?q=100%25"),
    ],
)
def test_url_encode(text, expected):
    import url_encode

    assert url_encode._url_encoded(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("hello+world", "hello world"),
        ("hello+world%21", "hello world!"),
        ("c%2B%2B+tips", "c++ tips"),
        ("1+1 = 2%25", "1+1 = 2%"),  # real spaces: the '+' is literal
    ],
)
def test_url_decode_reads_plus_as_space_in_form_encoded_text(text, expected):
    import url_encode

    assert url_encode._url_decoded(text) == expected


@pytest.fixture
def qr_env(tmp_path, monkeypatch):
    """qrencode 'installed', every subprocess call recorded, nothing run."""
    import shutil
    import subprocess

    import qr_code

    calls: list[tuple[list, dict]] = []
    monkeypatch.setattr(qr_code, "QR_CACHE", tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append((argv, kw)))
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    return calls


def test_qr_passes_the_text_on_stdin(qr_env):
    import qr_code

    qr_code._qr("hello -o /etc/x")
    argv, kw = qr_env[0]
    assert argv[0] == "qrencode" and "hello -o /etc/x" not in argv
    assert kw["input"] == b"hello -o /etc/x"


def test_qr_refuses_text_too_long_for_a_qr_code(qr_env):
    import qr_code

    qr_code._qr("x" * 200_000)  # used to raise OSError E2BIG from argv
    assert [argv[0] for argv, _ in qr_env] == ["notify-send"]
    assert "Too long" in qr_env[0][0][-2]


def test_qr_reports_an_oserror(qr_env, monkeypatch):
    import subprocess

    import qr_code

    def fail(argv, **kw):
        if argv[0] == "qrencode":
            raise OSError(7, "Argument list too long")
        qr_env.append((argv, kw))

    monkeypatch.setattr(subprocess, "run", fail)
    qr_code._qr("hello")
    assert qr_env[0][0][-2] == "QR error"


def test_qr_install_hint_names_the_package_manager(monkeypatch):
    import shutil

    import qr_code

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/dnf" if name == "dnf" else None)
    assert qr_code._install_hint() == "Install with: sudo dnf install qrencode"


def test_base64_decode_offered_on_unpadded_input():
    import base64_codec

    assert base64_codec._looks_like_base64("SGVsbG8gd29ybGQ")
    assert base64_codec._decode_base64("SGVsbG8gd29ybGQ") == "Hello world"
    # Still not on ordinary words, now that the length needn't divide by 4
    assert not base64_codec._looks_like_base64("Information")
    assert not base64_codec._looks_like_base64("HelloThere")


@pytest.mark.parametrize("text", ["42", "2024", "12345678"])
def test_timestamp_not_offered_on_small_numbers(text):
    import converters

    assert not converters._looks_like_timestamp(text)


@pytest.mark.parametrize("text", ["1704164645", "1704164645123", "123456789"])
def test_timestamp_still_offered_on_epochs(text):
    import converters

    assert converters._looks_like_timestamp(text)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("éa 🇳🇴", "🇳🇴 aé"),
        ("éa 🇳🇴", "🇳🇴 aé"),
        ("ab 👍🏽 👨‍👩‍👧", "👨‍👩‍👧 👍🏽 ba"),
        ("🇳🇴🇸🇪", "🇸🇪🇳🇴"),
        ("a\r\nb", "b\r\na"),
    ],
)
def test_reverse_keeps_graphemes_whole(text, expected):
    import text_transformations as tt

    assert tt._reversed_text(text) == expected
