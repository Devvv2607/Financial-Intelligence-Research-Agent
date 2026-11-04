"""Safe arithmetic for the financials agent: no `eval`, a whitelisted AST walk.

Allowed: numbers, named variables passed in by the caller, + - * / // % **, unary +/-,
parentheses, and a few functions (abs, round, min, max, sqrt, pct_change, cagr).
Everything else (attribute access, calls to anything else, comprehensions, strings,
names that aren't variables) is rejected before evaluation. Exponents and expression
length are bounded so a model can't request astronomically large work.
"""

import ast
import math
import operator
from collections.abc import Callable
from typing import Any

MAX_EXPRESSION_CHARS = 500
MAX_EXPONENT = 100
MAX_ABS_VALUE = 1e30


class CalculatorError(ValueError):
    pass


def _pct_change(new: float, old: float) -> float:
    """(new - old) / |old| * 100, e.g. revenue growth in percent."""
    if old == 0:
        raise CalculatorError("pct_change: old value is zero")
    return (new - old) / abs(old) * 100


def _cagr(end: float, start: float, years: float) -> float:
    """Compound annual growth rate in percent."""
    if start <= 0 or end <= 0 or years <= 0:
        raise CalculatorError("cagr: start, end and years must be positive")
    return float(((end / start) ** (1 / years) - 1) * 100)


_BINOPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_FUNCS: dict[str, Callable[..., float]] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "pct_change": _pct_change,
    "cagr": _cagr,
}


def evaluate(expression: str, variables: dict[str, float] | None = None) -> float:
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise CalculatorError(f"expression longer than {MAX_EXPRESSION_CHARS} characters")
    variables = variables or {}
    bad = [k for k in variables if not k.isidentifier() or k in _FUNCS]
    if bad:
        raise CalculatorError(f"invalid variable names: {bad}")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise CalculatorError(f"syntax error: {exc.msg}") from exc
    result = _eval(tree.body, variables)
    if not math.isfinite(result) or abs(result) > MAX_ABS_VALUE:
        raise CalculatorError("result is not a finite number in range")
    return float(result)


def _eval(node: ast.AST, variables: dict[str, float]) -> float:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise CalculatorError(f"unknown name {node.id!r}")
        return float(variables[node.id])
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _eval(node.left, variables), _eval(node.right, variables)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
            raise CalculatorError(f"exponent larger than {MAX_EXPONENT}")
        try:
            return float(_BINOPS[type(node.op)](left, right))
        except ZeroDivisionError as exc:
            raise CalculatorError("division by zero") from exc
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return float(_UNARY[type(node.op)](_eval(node.operand, variables)))
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCS
        and not node.keywords
    ):
        args = [_eval(a, variables) for a in node.args]
        if node.func.id == "round" and len(args) == 2:
            # Every value is a float here, but round()'s digits must be an int.
            if not args[1].is_integer():
                raise CalculatorError("round: digits must be a whole number")
            return float(round(args[0], int(args[1])))
        try:
            return float(_FUNCS[node.func.id](*args))
        except (TypeError, ValueError) as exc:
            raise CalculatorError(f"{node.func.id}: {exc}") from exc
    raise CalculatorError(f"unsupported expression: {type(node).__name__}")
