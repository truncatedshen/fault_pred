"""Descriptive statistics without graph dependencies.

集中趋势 / 离散度 / 相关性三个"看一眼数据长什么样"的函数。它们只接收 DataFrame，
不碰图与运行时，所以既能作为 explore.* 组件的后端，也能在离线脚本里直接调用。
返回值统一是 ``{"method": ..., "values": {...}}`` 或相关矩阵，便于 UI 直接渲染。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from fault_core.data import numeric_columns


def central_tendency(
    data: pd.DataFrame, columns: list[str], method: str = "mean", weight_column: str | None = None
) -> dict:
    """按列计算集中趋势。

    ``mean/median`` 走 pandas 同名方法；``mode`` 可能返回多个众数（因此值是列表）；
    ``weighted_mean`` 需要 ``weight_column``，权重要求有限、非负且总和为正，
    否则加权平均没有定义。
    """
    values = data[numeric_columns(data, columns)]
    if method == "mode":
        return {"method": method, "values": values.mode().to_dict(orient="list")}
    if method == "weighted_mean":
        if not weight_column:
            raise ValueError("weighted_mean requires weight_column")
        weights = data[weight_column].to_numpy(dtype=float)
        if not np.isfinite(weights).all() or (weights < 0).any() or weights.sum() <= 0:
            raise ValueError("Weights must be finite, nonnegative and have a positive sum")
        result = pd.Series(np.average(values, weights=weights, axis=0), index=values.columns)
    else:
        result = getattr(values, method)()
    return {"method": method, "values": result.to_dict()}


def dispersion(data: pd.DataFrame, columns: list[str], method: str = "std") -> dict:
    """按列计算离散程度：方差/标准差/极差/四分位距/MAD/变异系数。

    统一使用 ``ddof=1``（样本统计量）；``cv`` 用平均值做分母，均值为 0 时返回 NaN
    而不是抛错——"无法定义"本身就是要报告的信息。
    """
    x = data[numeric_columns(data, columns)]
    functions = {
        "variance": lambda: x.var(ddof=1),
        "std": lambda: x.std(ddof=1),
        "range": lambda: x.max() - x.min(),
        "iqr": lambda: x.quantile(0.75) - x.quantile(0.25),
        "mad": lambda: (x - x.median()).abs().median(),
        "cv": lambda: x.std(ddof=1) / x.mean().replace(0, np.nan),
    }
    return {"method": method, "values": functions[method]().to_dict()}


def correlation(data: pd.DataFrame, columns: list[str], method: str = "pearson") -> pd.DataFrame:
    """列间相关系数矩阵（pearson / spearman / kendall 由 pandas 实现）。

    只取数值列，非数值列会被 :func:`fault_core.data.numeric_columns` 挡掉。
    """
    return data[numeric_columns(data, columns)].corr(method=method)
