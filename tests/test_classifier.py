import pytest

from classifier import ContentType, classify


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://github.com/karanshukla/vinoPop", ContentType.URL),
        ("someone@example.com", ContentType.EMAIL),
        ("sudo dnf install openvino", ContentType.COMMAND),
        ("ls -la ~/Downloads", ContentType.COMMAND),
        ("/etc/os-release", ContentType.PATH),
        ("The quick brown fox jumps over the lazy dog.", ContentType.PLAIN_TEXT),
    ],
)
def test_classify(text, expected):
    assert classify(text) is expected


def test_classify_ignores_surrounding_whitespace():
    assert classify("  https://example.com/\n") is ContentType.URL
