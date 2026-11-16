"""Reading the numbers out of a dotted version string."""

from __future__ import annotations


def leading_numbers(text: str) -> tuple[int, ...]:
    """The dot-separated numbers `text` starts with: "1.35.0-rc1" is (1, 35)."""
    numbers: list[int] = []
    for part in text.split("."):
        if not part.isdigit():
            break
        numbers.append(int(part))
    return tuple(numbers)
