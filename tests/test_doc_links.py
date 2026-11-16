"""Every `tests/test_x.py::test_name` a comment or docstring cites must be a
real test, so a rule that names its test fails CI when the test is renamed or
deleted. See docs/comment-style.md."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", ".git", "__pycache__", "design", "build", "dist"}
# The launcher has no .py suffix.
EXTRA_SOURCES = ("vinopop",)
CITATION = re.compile(r"`(tests/[\w/]+\.py(?:::\w+)+)`")


def _sources() -> list[Path]:
    files = [
        p
        for p in ROOT.rglob("*.py")
        if not SKIP_DIRS.intersection(p.relative_to(ROOT).parts) and p != Path(__file__).resolve()
    ]
    return files + [ROOT / name for name in EXTRA_SOURCES]


def cited_tests(text: str) -> list[str]:
    return CITATION.findall(text)


def resolves(cited: str, root: Path = ROOT) -> bool:
    """`tests/f.py::test_x` or `tests/f.py::TestClass::test_x` names a
    `def test_...` in that file."""
    path, *names = cited.split("::")
    try:
        body: list[ast.stmt] = ast.parse((root / path).read_text()).body
    except (OSError, SyntaxError):
        return False
    *classes, name = names
    for class_name in classes:
        found = [n for n in body if isinstance(n, ast.ClassDef) and n.name == class_name]
        if not found:
            return False
        body = found[0].body
    return name.startswith("test") and any(
        isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name for n in body
    )


def test_every_cited_test_exists() -> None:
    broken = []
    for file in _sources():
        for line_no, line in enumerate(file.read_text().splitlines(), 1):
            for cited in cited_tests(line):
                if not resolves(cited):
                    broken.append(f"{file.relative_to(ROOT)}:{line_no}: `{cited}`")
    assert not broken, "no such test:\n" + "\n".join(broken)


def test_the_check_finds_a_cited_test(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_f.py").write_text(
        "def test_real(): pass\nclass TestGroup:\n    def test_inner(self): pass\n"
    )
    assert cited_tests("# (pinned by `tests/test_f.py::test_real`)") == [
        "tests/test_f.py::test_real"
    ]
    assert resolves("tests/test_f.py::test_real", tmp_path)
    assert resolves("tests/test_f.py::TestGroup::test_inner", tmp_path)


def test_the_check_rejects_a_missing_or_non_test_function(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_f.py").write_text("def helper(): pass\n")
    assert not resolves("tests/test_f.py::helper", tmp_path), "not a test"
    assert not resolves("tests/test_f.py::test_gone", tmp_path)
    assert not resolves("tests/test_missing.py::test_real", tmp_path)
