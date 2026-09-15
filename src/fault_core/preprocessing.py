"""Pure preprocessing APIs; fitted-on-all-data transformations are marked.

这里放"会改变数值分布"的预处理：缺失填充、缩放、数值变换。关键设计是
**拟合范围决定结论强度**——在整张表上拟合的缩放/变换/编码都属于探索性操作，
统一由 :func:`mark_fitted` 打上警告，警告会随 attrs 传到模型指标里；
而 SVM 验证器这种需要"只在训练折上拟合"的场景不走这里，而是在自己的训练集内部完成。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.preprocessing import (
    MaxAbsScaler,
    MinMaxScaler,
    PowerTransformer,
    RobustScaler,
    StandardScaler,
    normalize,
)

from fault_core.data import expression, numeric_columns


def mark_fitted(data: pd.DataFrame, operation: str) -> pd.DataFrame:
    """给"在全部行上拟合过"的结果追加一条探索性警告。

    警告写入 ``attrs["evaluation_warnings"]``，该属性会被窗口特征沿合并链一路传递，
    最终出现在模型 metrics 的 ``warnings`` 字段里，因此使用者在报告里无法忽略它。
    """
    data.attrs["evaluation_warnings"] = [
        *data.attrs.get("evaluation_warnings", []),
        f"{operation} fitted on the full dataset; metrics are exploratory and may contain leakage.",
    ]
    return data


def impute_features(
    data: pd.DataFrame,
    method: str = "mean",
    columns: list[str] | None = None,
    fill_value: float = 0.0,
) -> tuple[pd.DataFrame, list[str]]:
    """Fill or drop NaN in a feature table, reporting what was touched.

    Window features legitimately contain NaN (a flat window has no spectrum), and a model
    cannot consume them. ``mean``/``median``/``zero`` fill, ``drop_columns`` removes any
    column that is still incomplete. Returns the frame and the per-column notes.
    """
    numeric = numeric_columns(data, columns)
    result = data.copy()
    missing = result[numeric].isna()
    if not missing.any().any():
        return result, []
    affected = [column for column in numeric if bool(missing[column].any())]
    if method == "drop_columns":
        full = [column for column in numeric if bool(missing[column].all())]
        result = result.drop(columns=full)
        remaining = result[[column for column in numeric if column in result.columns]]
        notes = [f"Dropped all-NaN feature columns: {', '.join(full)}"] if full else []
        still = [column for column in remaining.columns if bool(remaining[column].isna().any())]
        if still:
            raise ValueError(f"drop_columns only removes all-NaN columns; still incomplete: {still[:5]}")
        return result, notes
    if method == "median":
        fills = result[affected].median()
    elif method == "zero":
        fills = {column: fill_value for column in affected}
    elif method == "mean":
        fills = result[affected].mean()
    else:
        raise ValueError(f"Unknown feature imputation method: {method}")
    for column in affected:
        value = fills[column]
        if pd.isna(value):
            raise ValueError(f"Column {column} is entirely NaN; drop it instead of filling")
        result[column] = result[column].fillna(float(value))
    notes = [f"Filled NaN feature values ({method}); most affected: " + ", ".join(affected[:6])]
    return result, notes


def scale(
    data: pd.DataFrame,
    columns: list[str],
    method: str,
    feature_range: list[float] | None = None,
    with_mean: bool = True,
    with_std: bool = True,
) -> pd.DataFrame:
    """对选定数值列做缩放，返回拷贝。

    ``l1``/``l2`` 是按行归一化（不拟合参数，因此不打警告）；
    ``minmax``/``maxabs``/``zscore``/``robust`` 需要先看全表统计量，属于拟合操作，会被标记。
    NaN 与无穷值在这里直接拒绝：缩放无法处理缺失，静默传播 NaN 只会把问题推给模型。
    """
    cols = numeric_columns(data, columns)
    result = data.copy()
    values = data[cols]
    if not np.isfinite(values.to_numpy(dtype=float, copy=False)).all():
        raise ValueError("Scaling requires finite values; clean missing/infinite values first")
    if method in {"l1", "l2"}:
        result[cols] = normalize(values, norm=method)
        return result
    bounds = feature_range or [0, 1]
    if method == "minmax" and (len(bounds) != 2 or bounds[0] >= bounds[1]):
        raise ValueError("feature_range must have two increasing values")
    scalers = {
        "minmax": lambda: MinMaxScaler(feature_range=tuple(bounds)),
        "maxabs": MaxAbsScaler,
        "zscore": lambda: StandardScaler(with_mean=with_mean, with_std=with_std),
        "robust": lambda: RobustScaler(with_centering=with_mean, with_scaling=with_std),
    }
    if method not in scalers:
        raise ValueError(f"Unknown scaling method: {method}")
    result[cols] = scalers[method]().fit_transform(values)
    return mark_fitted(result, method)


def transform(
    data: pd.DataFrame, columns: list[str], method: str, power: float = 2, expression_text: str = ""
) -> pd.DataFrame:
    """对数值列做单调变换：对数族、幂、Box-Cox / Yeo-Johnson 或自定义表达式。

    Box-Cox 需要严格正数，Yeo-Johnson 允许零与负数，两者都由 sklearn 在整表上拟合参数，
    因此带探索性警告。``expression`` 走 :func:`fault_core.data.expression` 的白名单求值，
    Python 侧先忽略浮点告警（对数域外的中间结果），最后再统一检查结果是否有限。
    """
    cols = numeric_columns(data, columns)
    result = data.copy()
    values = data[cols].astype(float)
    if not np.isfinite(values.to_numpy()).all():
        raise ValueError("Transformation requires finite values")
    if method in {"box-cox", "yeo-johnson"}:
        result[cols] = PowerTransformer(method=method).fit_transform(values)
        return mark_fitted(result, method)
    functions = {"log": np.log, "log1p": np.log1p, "sqrt": np.sqrt, "power": lambda x: np.power(x, power)}
    with np.errstate(all="ignore"):
        if method == "expression":
            for col in cols:
                result[col] = expression(pd.DataFrame({"x": data[col]}), expression_text)
        elif method in functions:
            result[cols] = functions[method](values)
        else:
            raise ValueError(f"Unknown transformation: {method}")
    if not np.isfinite(result[cols].to_numpy(dtype=float)).all():
        raise ValueError("Transformation domain error (e.g. log of a non-positive value)")
    return result
