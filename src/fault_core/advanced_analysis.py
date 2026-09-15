"""Distribution, time-dependence, drift and anomaly exploration.

这里是 ``explore.*`` 组件族的分析后端：分布检查、周期（自相关）检查、概念漂移、
互相关/互协方差，以及三种可解释的异常探索方法。它们都属于**终端分支**——
输出是给人看的统计结果或逐行标记，不会进入模型；因此这些函数不做训练/验证划分，
只负责把"数据长什么样"讲清楚。

几条共同约定：

* 入参列先过 :func:`fault_core.data.numeric_columns`，保证是真实存在的数值列；
* 统计前检查有限性，遇到 NaN 或 inf 直接抛错而不是让统计量变成 NaN；
* 结果一律带上方法名与关键阈值，便于前端展示与报告引用（阈值本身就是结论的一部分）。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from fault_core.data import numeric_columns


def distribution_check(
    data: pd.DataFrame, columns: list[str] | None = None, bins: int = 20
) -> dict[str, Any]:
    """逐列输出直方图与分布摘要。

    摘要包含样本数、缺失率、均值/标准差、偏度、峰度与 5%/50%/95% 分位数，
    足以回答"这个通道是否偏斜、是否被截断、有没有离群长尾"这三个常见问题。
    完全无值或含无穷值的列直接报错：这两种情况下分位数与直方图都没有意义。
    """
    cols = numeric_columns(data, columns)
    rows = []
    histograms = {}
    for column in cols:
        values = data[column].dropna().to_numpy(dtype=float)
        if not len(values):
            raise ValueError(f"Distribution column contains no finite values: {column}")
        if not np.isfinite(values).all():
            raise ValueError(f"Distribution column contains infinite values: {column}")
        counts, edges = np.histogram(values, bins=bins)
        rows.append(
            {
                "column": column,
                "count": int(len(values)),
                "missing_rate": float(data[column].isna().mean()),
                "mean": float(np.mean(values)),
                "std": float(np.std(values)),
                # 常数列的偏度/峰度在数学上未定义（分母为 0），这里约定为 0 而不是 NaN。
                "skewness": float(stats.skew(values)) if np.ptp(values) else 0.0,
                "kurtosis": float(stats.kurtosis(values)) if np.ptp(values) else 0.0,
                "q05": float(np.quantile(values, 0.05)),
                "median": float(np.median(values)),
                "q95": float(np.quantile(values, 0.95)),
            }
        )
        histograms[column] = {"counts": counts.tolist(), "edges": edges.tolist()}
    return {"rows": rows, "histograms": histograms, "bins": bins}


def periodicity_check(
    data: pd.DataFrame,
    columns: list[str] | None = None,
    max_lag: int = 100,
    sampling_rate: float | None = None,
) -> dict[str, Any]:
    """用自相关函数找主周期。

    对去均值后的序列计算滞后 1..max_lag 的归一化自相关（分母是总平方和，
    相当于 np.correlate 的归一化版本），取峰值作为主周期；给出 ``sampling_rate`` 时
    再换算成秒与赫兹。返回的 ``series`` 是每个通道的 ACF 曲线，供前端画图。
    """
    cols = numeric_columns(data, columns)
    # 至少要 4 行才可能看出滞后结构；max_lag 再被数据长度削到 len-2，
    # 保证参与相关的重叠片段不少于 2 个点。
    if len(data) < 4:
        raise ValueError("Periodicity check needs at least four rows")
    limit = min(max_lag, len(data) - 2)
    if limit < 1:
        raise ValueError("max_lag is too small for this dataset")
    rows, series = [], []
    for column in cols:
        values = data[column].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError("Periodicity check requires finite values")
        centered = values - values.mean()
        variance = float(np.dot(centered, centered))
        correlations = np.zeros(limit, dtype=float)
        # 常数列方差为 0，自相关无定义，此时保留全 0 曲线而不是抛错。
        if variance:
            correlations = np.array(
                [np.dot(centered[:-lag], centered[lag:]) / variance for lag in range(1, limit + 1)]
            )
        peak_index = int(np.argmax(correlations))
        peak_lag = peak_index + 1
        rows.append(
            {
                "column": column,
                "peak_lag": peak_lag,
                "peak_autocorrelation": float(correlations[peak_index]),
                "period_seconds": float(peak_lag / sampling_rate) if sampling_rate else None,
                "frequency_hz": float(sampling_rate / peak_lag) if sampling_rate else None,
            }
        )
        series.append({"name": column, "x": list(range(1, limit + 1)), "y": correlations.tolist()})
    return {"rows": rows, "series": series, "max_lag": limit}


def _psi(reference: np.ndarray, current: np.ndarray, bins: int) -> float:
    """Population Stability Index：比较两份样本在同一个分箱下的分布差异。

    分箱边界取自参考样本的分位数（等频分箱），两端扩到 ±inf 以免新数据落在箱外；
    空箱用 1e-6 兜底，避免 log(0) 与除零。常用经验阈值：<0.1 稳定，0.1~0.2 需关注，
    >0.2 视为明显漂移（组件参数 ``psi_threshold`` 默认 0.2）。

    参考样本退化为单一取值时没有可分箱：若当前样本也全等于该值则返回 0，否则返回
    ``inf``——"从常量变成非常量"本身就是最强的一种漂移。
    """
    edges = np.unique(np.quantile(reference, np.linspace(0, 1, bins + 1)))
    if len(edges) < 2:
        return 0.0 if np.all(current == reference[0]) else float("inf")
    edges[0], edges[-1] = -np.inf, np.inf
    ref_counts = np.histogram(reference, bins=edges)[0].astype(float)
    cur_counts = np.histogram(current, bins=edges)[0].astype(float)
    epsilon = 1e-6
    ref_share = np.clip(ref_counts / max(ref_counts.sum(), 1), epsilon, None)
    cur_share = np.clip(cur_counts / max(cur_counts.sum(), 1), epsilon, None)
    return float(np.sum((cur_share - ref_share) * np.log(cur_share / ref_share)))


def concept_drift(
    reference: pd.DataFrame,
    current: pd.DataFrame,
    columns: list[str] | None = None,
    bins: int = 10,
    psi_threshold: float = 0.2,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """比较参考集与当前集，逐列判断是否发生漂移。

    同时给出两个互补指标：PSI（对分箱分布敏感，工程上常用）与
    Kolmogorov–Smirnov 检验（对分布形状敏感，附带 p 值）。判定规则是"两者任一触发"：
    ``psi >= psi_threshold`` 或 ``ks_pvalue < alpha``，偏保守，宁可多报一次待查。

    ``drift=True`` 列会汇总到返回值的 ``drifted_columns``，便于直接写进报告。
    """
    cols = numeric_columns(reference, columns)
    # 列集合不一致说明两份数据不可比，先报缺失列，避免后续按名字取值时出现 KeyError。
    missing = set(cols) - set(current.columns)
    if missing:
        raise ValueError(f"Current dataset is missing columns: {sorted(missing)}")
    rows = []
    for column in cols:
        before = reference[column].dropna().to_numpy(dtype=float)
        after = current[column].dropna().to_numpy(dtype=float)
        if not len(before) or not len(after):
            raise ValueError(f"Drift comparison needs non-missing values for {column}")
        if not np.isfinite(before).all() or not np.isfinite(after).all():
            raise ValueError(f"Drift comparison requires finite values for {column}")
        ks = stats.ks_2samp(before, after)
        psi = _psi(before, after, bins)
        rows.append(
            {
                "column": column,
                "reference_count": int(len(before)),
                "current_count": int(len(after)),
                "reference_mean": float(np.mean(before)),
                "current_mean": float(np.mean(after)),
                "mean_shift": float(np.mean(after) - np.mean(before)),
                "psi": psi,
                "ks_statistic": float(ks.statistic),
                "ks_pvalue": float(ks.pvalue),
                "drift": bool(psi >= psi_threshold or ks.pvalue < alpha),
            }
        )
    return {
        "rows": rows,
        "drifted_columns": [row["column"] for row in rows if row["drift"]],
        "psi_threshold": psi_threshold,
        "alpha": alpha,
    }


def cross_relation(
    data: pd.DataFrame,
    first_column: str,
    second_column: str,
    method: str = "cross_correlation",
    max_lag: int = 20,
    normalize: bool = True,
) -> pd.DataFrame:
    """两条序列的互协方差/互相关，按滞后展开成表。

    滞后为负时取"x 的后段对 y 的前段"，为正时相反，等价于 ``correlate(x, y)`` 的对称写法；
    每行还记录参与计算的重叠样本数 ``sample_count``——样本数太少的滞后尾部不宜当结论。
    ``normalize`` 只影响 ``cross_correlation``：除以两侧标准差得到相关系数；
    标准差为 0（常值段）时退回未归一化的协方差，而不是让结果变成 NaN/inf。
    """
    numeric_columns(data, [first_column, second_column])
    values = data[[first_column, second_column]].dropna().to_numpy(dtype=float)
    if len(values) < 3:
        raise ValueError("Cross relation needs at least three paired values")
    if not np.isfinite(values).all():
        raise ValueError("Cross relation requires finite values")
    limit = min(max_lag, len(values) - 2)
    x, y = values[:, 0], values[:, 1]
    rows = []
    for lag in range(-limit, limit + 1):
        # 滑动取重叠片段：lag 越大重叠越短，这也是最大滞后被限制在 len-2 的原因。
        if lag < 0:
            left, right = x[-lag:], y[:lag]
        elif lag > 0:
            left, right = x[:-lag], y[lag:]
        else:
            left, right = x, y
        covariance = float(np.mean((left - left.mean()) * (right - right.mean())))
        if method == "cross_covariance":
            value = covariance
        elif method == "cross_correlation":
            denominator = float(np.std(left) * np.std(right))
            value = covariance / denominator if normalize and denominator else covariance
        else:
            raise ValueError(f"Unknown cross relation method: {method}")
        rows.append({"lag": lag, "value": value, "sample_count": len(left)})
    result = pd.DataFrame(rows).set_index("lag")
    result.attrs.update({"method": method, "first_column": first_column, "second_column": second_column})
    return result


def anomaly_exploration(
    data: pd.DataFrame,
    columns: list[str] | None = None,
    method: str = "boxplot",
    window: int = 20,
    threshold: float = 3.0,
    iqr_multiplier: float = 1.5,
    group_column: str | None = None,
    time_column: str | None = None,
) -> pd.DataFrame:
    """Return row-aligned scores and flags for exploratory anomaly methods.

    ``hyperbolic_smoothing`` uses a rolling median/MAD baseline and the bounded
    transform ``baseline + scale*tanh((x-baseline)/scale)``.

    三种方法：

    * ``boxplot``：按 IQR 上下界（``q1 - k*IQR`` / ``q3 + k*IQR``，k 由 ``iqr_multiplier``
      给出，默认 1.5）标记离群点，统计量可整表也可按 ``group_column`` 分组；
    * ``dynamic_threshold``：滚动中位数作基线、滚动 MAD 作尺度，偏离超过 ``threshold`` 倍判为异常；
    * ``hyperbolic_smoothing``：在上一行基础上额外输出 tanh 压缩后的平滑曲线，
      幅度被限制在 ±scale 内，适合做"去尖峰但保留趋势"的展示。

    输出按原始索引对齐（内部先排序再还原），每列产生 ``__value/__center/__lower/__upper/
    __score/__is_anomaly`` 六列，另加一列"任一通道异常"的 ``is_anomaly``。
    阈值类的探索结果**阈值就是结论**，报告时必须把阈值一起写出来。
    """
    cols = numeric_columns(data, columns)
    # 分组/时间列只用于排序与分组统计，必须是真实列，否则后面 index 会静默错位。
    for ordering_column in (group_column, time_column):
        if ordering_column and ordering_column not in data.columns:
            raise ValueError(f"Missing ordering column: {ordering_column}")
    sort_columns = [column for column in (group_column, time_column) if column]
    ordered = data.sort_values(sort_columns, kind="stable") if sort_columns else data
    output = pd.DataFrame(index=ordered.index)
    # 逐通道算完再按位或合并：任一通道异常即行异常，便于快速定位问题时刻。
    aggregate = pd.Series(False, index=ordered.index)
    for column in cols:
        values = ordered[column].astype(float)
        if not np.isfinite(values.to_numpy()).all():
            raise ValueError("Anomaly exploration requires finite values")
        if method == "boxplot":
            if group_column:
                # 分组统计：每台设备各用各的四分位距，避免设备间的基线差异被当成异常。
                grouped = values.groupby(ordered[group_column], sort=False, dropna=False)
                q1 = grouped.transform(lambda x: x.quantile(0.25))
                q3 = grouped.transform(lambda x: x.quantile(0.75))
                center = grouped.transform("median")
            else:
                q1 = pd.Series(float(values.quantile(0.25)), index=ordered.index)
                q3 = pd.Series(float(values.quantile(0.75)), index=ordered.index)
                center = pd.Series(float(values.median()), index=ordered.index)
            scale = q3 - q1
            lower = q1 - iqr_multiplier * scale
            upper = q3 + iqr_multiplier * scale
            # IQR 为 0（大量重复值）时用 1.0 兜底做分母，避免除零得到 inf。
            score = (values - center).abs() / scale.replace(0, 1.0)
        else:
            grouper = (
                values.groupby(ordered[group_column], sort=False, dropna=False) if group_column else None
            )
            if grouper is None:
                center = values.rolling(window, min_periods=1).median()
                deviation = (values - center).abs().rolling(window, min_periods=1).median()
            else:
                center = grouper.transform(lambda x: x.rolling(window, min_periods=1).median())
                deviation = (
                    (values - center)
                    .abs()
                    .groupby(ordered[group_column], sort=False, dropna=False)
                    .transform(lambda x: x.rolling(window, min_periods=1).median())
                )
            # 1.4826 是"正态下 MAD ≈ 0.6745σ"的换算系数，使尺度与标准差可比。
            robust_scale = (1.4826 * deviation).replace(0, np.nan)
            # 窗口内完全无波动时尺度为 0：退化为整列标准差，再退化为 1.0，保证分母非零。
            fallback = float(np.std(values)) or 1.0
            robust_scale = robust_scale.fillna(fallback)
            score = (values - center).abs() / robust_scale
            lower, upper = center - threshold * robust_scale, center + threshold * robust_scale
            if method == "hyperbolic_smoothing":
                output[f"{column}__smoothed"] = center + robust_scale * np.tanh(
                    (values - center) / robust_scale
                )
            elif method != "dynamic_threshold":
                raise ValueError(f"Unknown anomaly method: {method}")
        flags = (values < lower) | (values > upper)
        output[f"{column}__value"] = values
        output[f"{column}__center"] = center
        output[f"{column}__lower"] = lower
        output[f"{column}__upper"] = upper
        output[f"{column}__score"] = score
        output[f"{column}__is_anomaly"] = flags
        aggregate |= flags
    output.insert(0, "is_anomaly", aggregate)
    output = output.loc[data.index]
    output.attrs = {**data.attrs, "method": method, "columns": cols}
    return output
