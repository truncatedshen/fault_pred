"""Data operations preserving row identity and provenance."""

from __future__ import annotations

import ast
import operator
from typing import Any

import numpy as np
import pandas as pd


def numeric_columns(data: pd.DataFrame, columns: list[str] | None = None) -> list[str]:
    selected = columns or list(data.select_dtypes(include="number").columns)
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("Select at least one unique numeric column")
    missing = set(selected) - set(data.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")
    if not all(pd.api.types.is_numeric_dtype(data[c]) for c in selected):
        raise ValueError("Selected columns must be numeric")
    return selected


def expression(data: pd.DataFrame, text: str) -> Any:
    """Evaluate only numeric literals, column names and whitelisted arithmetic."""
    binary = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
        ast.Mod: operator.mod,
    }
    functions = {"log": np.log, "log1p": np.log1p, "sqrt": np.sqrt, "abs": np.abs}
    if len(text) > 1000:
        raise ValueError("Expression is too long")
    tree = ast.parse(text, mode="eval")
    if len(list(ast.walk(tree))) > 100:
        raise ValueError("Expression is too complex")

    def visit(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            if abs(node.value) > 1e12:
                raise ValueError("Numeric literal is too large")
            return node.value
        if isinstance(node, ast.Name) and node.id in data.columns:
            return data[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        if isinstance(node, ast.BinOp) and type(node.op) in binary:
            right = visit(node.right)
            if isinstance(node.op, ast.Pow) and (not np.isscalar(right) or not -10 <= right <= 10):
                raise ValueError("Power exponent must be a scalar between -10 and 10")
            return binary[type(node.op)](visit(node.left), right)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in functions and len(node.args) == 1 and not node.keywords:
                return functions[node.func.id](visit(node.args[0]))
        raise ValueError("Only column names, numeric literals, arithmetic and log/log1p/sqrt/abs are allowed")

    return visit(tree)


def filter_data(
    data: pd.DataFrame,
    column: str,
    operator: str,
    value: Any,
    conditions: list[dict[str, Any]] | None = None,
    logical_operator: str = "and",
) -> pd.DataFrame:
    def mask(condition: dict[str, Any]) -> pd.Series:
        c, op, v = condition["column"], condition["operator"], condition["value"]
        series = data[c]
        if op in {"between", "in"}:
            if not isinstance(v, list) or (op == "between" and len(v) != 2):
                raise ValueError(f"{op} requires a list" + (" of two bounds" if op == "between" else ""))
            return series.between(*v) if op == "between" else series.isin(v)
        functions = {
            "gt": series.gt,
            ">": series.gt,
            "ge": series.ge,
            ">=": series.ge,
            "lt": series.lt,
            "<": series.lt,
            "le": series.le,
            "<=": series.le,
            "eq": series.eq,
            "==": series.eq,
            "ne": series.ne,
            "!=": series.ne,
        }
        if op not in functions:
            raise ValueError(f"Unknown filter operator: {op}")
        return functions[op](v)

    selected = mask({"column": column, "operator": operator, "value": value})
    for condition in conditions or []:
        selected = selected & mask(condition) if logical_operator == "and" else selected | mask(condition)
    return data.loc[selected].copy()


def row_operation(
    data: pd.DataFrame,
    operation: str,
    count: int = 10,
    start: int = 0,
    columns: list[str] | None = None,
    ascending: bool = True,
    indices: list[Any] | None = None,
    random_state: int = 42,
) -> pd.DataFrame:
    if operation == "head":
        return data.head(count).copy()
    if operation == "tail":
        return data.tail(count).copy()
    if operation == "range":
        return data.iloc[start : start + count].copy()
    if operation == "sample":
        return data.sample(n=count, random_state=random_state).copy()
    if operation == "drop_rows":
        return data.drop(index=indices or []).copy()
    if operation == "sort":
        if not columns:
            raise ValueError("sort requires columns")
        return data.sort_values(columns, ascending=ascending, kind="stable").copy()
    if operation == "remove_duplicates":
        return data.drop_duplicates(subset=columns or None).copy()
    if operation == "drop_missing":
        return data.dropna(subset=columns or None).copy()
    raise ValueError(f"Unknown row operation: {operation}")


def column_operation(
    data: pd.DataFrame,
    operation: str,
    columns: list[str] | None = None,
    mapping: dict[str, str] | None = None,
    name: str = "",
    expression_text: str = "",
    dtype: str = "float64",
) -> pd.DataFrame:
    if operation in {"select", "reorder"}:
        if not columns:
            raise ValueError("Select columns first")
        if operation == "reorder" and set(columns) != set(data.columns):
            raise ValueError("reorder must include all columns exactly once")
        result = data.loc[:, columns].copy()
    elif operation == "drop":
        result = data.drop(columns=columns or []).copy()
    elif operation == "rename":
        result = data.rename(columns=mapping or {}, errors="raise").copy()
    elif operation == "cast_type":
        result = data.astype({c: dtype for c in columns or []}).copy()
    elif operation == "create":
        if not name:
            raise ValueError("New column name is required")
        result = data.copy()
        result[name] = expression(data, expression_text)
    else:
        raise ValueError(f"Unknown column operation: {operation}")
    if not result.columns.is_unique:
        raise ValueError("Column operation produces duplicate column names")
    return result
