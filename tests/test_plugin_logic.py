"""Behaviour of the bundled plugins' pure functions and handlers."""

import hashlib
import json

import pytest


@pytest.fixture
def replaced(monkeypatch):
    """Capture what a handler would paste over the selection, and silence notifications."""
    import actions

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
