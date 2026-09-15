"""Data operations preserving row identity and provenance.

这一层只做「表格进、表格出」的纯函数变换，不依赖 Graph / Runtime / 组件注册表，
因此既能被组件调用，也能在脚本里单独使用（见 README 的 Python API 示例）。

两条贯穿全文件的约定：

1. **索引即身份。** 所有函数都用 ``.copy()`` 返回新对象并保留原索引；下游的窗口特征、
   标签对齐、``feature.merge`` 的 provenance 校验全靠索引与 ``attrs`` 一致。
2. **宁可早失败。** 列不存在、不是数值列、表达式超限、结果出现重复列名等情况一律抛错，
   而不是静默产出形状对不上或语义已变的数据。
"""

from __future__ import annotations

import ast
import operator
from typing import Any

import numpy as np
import pandas as pd


def numeric_columns(data: pd.DataFrame, columns: list[str] | None = None) -> list[str]:
    """选出要参与计算的数值列，并校验它们确实存在、唯一且为数值类型。

    ``columns=None`` 表示"自动取所有数值列"——这是多数组件的默认行为；
    显式传入时则按调用者给的顺序返回，保证列顺序可预测。
    """
    selected = columns or list(data.select_dtypes(include="number").columns)
    # 空列表或重复列名都说明调用方参数写错了，继续算下去只会得到误导性结果。
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("Select at least one unique numeric column")
    missing = set(selected) - set(data.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")
    if not all(pd.api.types.is_numeric_dtype(data[c]) for c in selected):
        raise ValueError("Selected columns must be numeric")
    return selected


def expression(data: pd.DataFrame, text: str) -> Any:
    """解析并求值一个受限的算术表达式（例如 ``(a - b) / c``）。

    用户表达式来自组件参数，等于在服务端执行字符串，所以这里不用 ``eval``，
    而是把 AST 白名单化：只允许数值字面量、表内列名、六种二元算术、一元正负号，
    以及 ``log/log1p/sqrt/abs`` 四个单参数函数。长度、节点数、字面量大小与幂指数
    都有上限，避免构造出的表达式把内存或 CPU 打满。
    """
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
    # 先做规模约束再递归：超大表达式不必等到求值才拒绝。
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
            # 幂指数必须是有限标量且限制在 [-10, 10]，否则 x ** 1e9 这类写法会瞬间耗尽内存。
            if isinstance(node.op, ast.Pow) and (not np.isscalar(right) or not -10 <= right <= 10):
                raise ValueError("Power exponent must be a scalar between -10 and 10")
            return binary[type(node.op)](visit(node.left), right)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in functions and len(node.args) == 1 and not node.keywords:
                return functions[node.func.id](visit(node.args[0]))
        # 兜底：任何没被上面分支显式放行的节点类型（属性访问、下标、导入、函数定义……）都拒绝。
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
    """按一个或多个条件过滤行。

    ``column/operator/value`` 是第一个条件；``conditions`` 里的字典按 ``logical_operator``
    （``and``/``or``）与它逐条组合。返回的是一份拷贝，索引保持原样，
    因此"过滤后接窗口特征"仍然可以追溯到原始行号。
    """

    def mask(condition: dict[str, Any]) -> pd.Series:
        c, op, v = condition["column"], condition["operator"], condition["value"]
        series = data[c]
        # between 需要两个边界、in 需要列表，其余比较运算是逐元素标量比较。
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
    """行方向的裁剪/排序/去重/抽样，一次调用只做一种操作。

    所有分支都返回拷贝：调用者拿到的是独立表格，不会顺手改掉上游 workspace 里的产物。
    """
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
    extrema_count: int = 1,
) -> pd.DataFrame:
    """列方向的操作：选择/删除/改名/改序/新建/转类型/清理。

    ``create`` 走 :func:`expression` 的受限表达式；``drop_constant`` 与 ``drop_empty``
    是数据质量预检里最常用的两个廉价清理动作。
    """
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
    elif operation == "drop_empty":
        result = data.dropna(axis="columns", how="all").copy()
    elif operation == "drop_constant":
        drop = [column for column in data.columns if data[column].nunique(dropna=False) <= 1]
        result = data.drop(columns=drop).copy()
    elif operation == "trim_extrema":
        cols = numeric_columns(data, columns)
        if extrema_count < 1:
            raise ValueError("extrema_count must be at least 1")
        # 逐列收集要删的行号再统一 drop：一次删完，避免边删边算导致"第 k 大"变化。
        remove = set()
        for column in cols:
            finite = data[column].dropna()
            remove.update(finite.nsmallest(extrema_count).index)
            remove.update(finite.nlargest(extrema_count).index)
        result = data.drop(index=list(remove)).copy()
        if result.empty:
            raise ValueError("Trimming extrema removed every row")
    else:
        raise ValueError(f"Unknown column operation: {operation}")
    # 重名会让后续 df[column] 返回 DataFrame 而不是 Series，属于典型的静默错误，直接拒绝。
    if not result.columns.is_unique:
        raise ValueError("Column operation produces duplicate column names")
    return result
