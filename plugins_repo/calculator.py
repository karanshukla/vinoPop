# vinopop-plugin-version: 1
"""Evaluate a math expression in the selection and show the result."""

from __future__ import annotations

import ast
import math
import operator
import re
import subprocess

from classifier import ContentType
from plugin_base import Plugin

# An expression must contain at least one digit AND at least one operator
# (or one of the allowed function/constant names) - anything else is just
# prose and would never evaluate.
_HAS_DIGIT = re.compile(r"\d")
_HAS_OP_OR_CALL = re.compile(
    r"[+\-*/^%()]"
    r"|\b(?:pi|e|tau|sqrt|log|log10|log2|exp|sin|cos|tan|"
    r"asin|acos|atan|floor|ceil|round|abs)\b"
)
# Only letters that are part of allowed function/constant names are OK.
_ALLOWED_WORD_RE = re.compile(
    r"^(?:pi|e|tau|sqrt|log|log10|log2|exp|sin|cos|tan|"
    r"asin|acos|atan|floor|ceil|round|abs)$"
)

# Hyphenated numbers that are data, not a subtraction: 2024-10-17 (an ISO date,
# "= 1997"), 17/10/2024, and phone numbers like 555-1234 or (555) 123-4567.
_NOT_MATH_RE = re.compile(
    r"^\d{4}-\d{1,2}-\d{1,2}$"
    r"|^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$"
    r"|^(?:\+\d{1,3}[\s-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])?\d{3}[\s.-]\d{4}$"
)


def _looks_like_math(text: str) -> bool:
    s = text.strip()
    if not s or len(s) > 200:
        return False
    if _NOT_MATH_RE.match(s):
        return False
    if not _HAS_DIGIT.search(s):
        return False
    if not _HAS_OP_OR_CALL.search(s):
        return False
    # Any word longer than 1 char that isn't an allowed function/constant
    # disqualifies the selection (e.g. "I have 3 cats" -> 'have', 'cats').
    return all(_ALLOWED_WORD_RE.match(word) for word in re.findall(r"[A-Za-z][A-Za-z_]*", s))


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UN_OPS = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_NAMES = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}

_FUNCS = {
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "floor": math.floor,
    "ceil": math.ceil,
    "round": round,
    "abs": abs,
}


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval(node.left)
        right = _eval(node.right)
        # ** with large operands can produce astronomically large ints
        # that take minutes of pure-CPU time to construct + format.
        # Selection like "9**9**9" used to freeze the worker thread. Cap
        # both operand magnitude and exponent at sane values.
        if isinstance(node.op, ast.Pow) and (abs(right) > 1000 or abs(left) > 1e6):
            raise ValueError("exponent too large - refusing to evaluate")
        return _BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UN_OPS:
        return _UN_OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name) and node.id in _NAMES:
        return _NAMES[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        return _FUNCS[node.func.id](*(_eval(a) for a in node.args))
    raise ValueError(f"not allowed: {ast.dump(node)}")


def _format_result(result) -> str:
    """Trim float noise (0.1 + 0.2 -> 0.3) while leaving integers exact."""
    if isinstance(result, float) and math.isfinite(result):
        if result.is_integer() and abs(result) < 1e15:
            return str(int(result))
        return format(result, ".15g")
    return str(result)


def _calc(text: str) -> None:
    expr = text.strip()
    try:
        # People write 2^8 meaning a power; in Python ^ is XOR
        tree = ast.parse(expr.replace("^", "**"), mode="eval")
        result = _eval(tree.body)
        if isinstance(result, complex):
            raise ValueError("the result is a complex number")
        if isinstance(result, float) and not math.isfinite(result):
            # 1e308*10: pasting "inf" over the selection helps nobody
            raise ValueError("the result is too large")
        # Inside the try: str() of an int with over 4300 digits raises
        result_str = _format_result(result)
    except Exception as exc:  # noqa: BLE001
        subprocess.run(
            [
                "notify-send",
                "--hint=byte:transient:1",
                "-t",
                "3000",
                "-i",
                "dialog-error",
                "Calculator error",
                str(exc)[:200],
            ],
            check=False,
        )
        return
    # Replace "2+2" with "4" in place, fallback to clipboard-only.
    import actions

    actions.replace_selection(result_str)
    subprocess.run(
        [
            "notify-send",
            "--hint=byte:transient:1",
            "-t",
            "2500",
            "-i",
            "vinopop-calculator",
            "Result",
            f"{expr} = {result_str}",
        ],
        check=False,
    )


def register(register_plugin) -> None:
    register_plugin(
        Plugin(
            name="calculator",
            icon="vinopop-calculator-symbolic",
            tooltip="Calculate",
            handler=_calc,
            content_types=(ContentType.PLAIN_TEXT,),
            priority=40,
            predicate=_looks_like_math,
        )
    )
