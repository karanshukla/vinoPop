"""The Settings window's text rules: what a pattern list or the snippet
variables look like as editable text and as a row's subtitle. No GTK, so
tests/test_settings_text.py runs without a display."""

from __future__ import annotations

EXAMPLES_SHOWN = 3


def patterns_from_text(text: str) -> list[str]:
    """One pattern per line; blank lines are dropped."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def _first_few(items: list[str]) -> str:
    shown = ", ".join(items[:EXAMPLES_SHOWN])
    if len(items) > EXAMPLES_SHOWN:
        shown += f", +{len(items) - EXAMPLES_SHOWN} more"
    return shown


def blocked_patterns_subtitle(patterns: list[str], when_empty: str) -> str:
    if not patterns:
        return when_empty
    count = len(patterns)
    plural = "s" if count != 1 else ""
    return f"{count} pattern{plural} blocked: {_first_few(patterns)}. Click to edit."


def variables_from_text(text: str) -> dict[str, str]:
    """`name = value` lines. Lines starting with #, without an = or without
    a name are skipped; a later line wins over an earlier one."""
    variables: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name:
            variables[name] = value.strip()
    return variables


def variables_as_text(variables: dict) -> str:
    return "\n".join(f"{name} = {value}" for name, value in sorted(variables.items()))


def variables_subtitle(variables: dict, when_empty: str) -> str:
    if not variables:
        return when_empty
    count = len(variables)
    plural = "s" if count != 1 else ""
    tokens = [f"{{var:{name}}}" for name in sorted(variables.keys())]
    return f"{count} variable{plural} defined: {_first_few(tokens)}. Click to edit."


def _has_fix(check: dict) -> bool:
    return bool(check.get("fix")) and check.get("status") != "ok"


def doctor_check_detail(check: dict) -> str:
    """A doctor check's detail, with its fix unless the check passed."""
    detail = check.get("detail", "")
    if _has_fix(check):
        detail += f"\nFix: {check['fix']}"
    return detail


def doctor_report(checks: list[dict]) -> str:
    """The doctor's checks as aligned plain text, for a bug report."""
    width = max((len(check.get("label", "")) for check in checks), default=0)
    return "\n".join(
        f"[{check.get('status', ''):>4}] {check.get('label', ''):<{width}}  {check.get('detail', '')}"
        + (f"\n{'':6} fix: {check['fix']}" if _has_fix(check) else "")
        for check in checks
    )
