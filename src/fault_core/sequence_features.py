"""Rolling, difference, autocorrelation and entropy features.

这三类特征与 :mod:`fault_core.features` 里的"窗口聚合"不同：

* ``rolling_statistics`` / ``temporal_features`` 是**逐行对齐**的（行数不变，行 i 的特征只
  用到 i 及其之前/附近的点），适合作为补充分支；
* ``entropy_features`` 是**按窗口聚合**的（行数变成窗口数），并带标签与来源信息，
  可以像 ``feature.statistical`` 一样直接喂给验证器。

无论哪种，都通过 ``group_column`` 把分组边界当作硬边界：滚动窗口与差分绝不跨设备。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from scipy.signal import find_peaks

from fault_core.data import numeric_columns
from fault_core.features import (
    _assemble,
    _window_label,
    attach_assets,
    group_assets,
    prepared_windows,
    window_arguments,
    window_attrs,
    window_shape,
)

#: 熵特征的全部方法名。``binned_entropy`` 与 ``information_entropy`` 是同一实现的两个叫法
#: （组件清单里两个名字都出现过），对外都产出 ``<列名>__information_entropy``。
ENTROPY_METHODS = ("approximate_entropy", "information_entropy", "binned_entropy")


def _ordered(data: pd.DataFrame, group_column: str | None, time_column: str | None) -> pd.DataFrame:
    """校验排序用列存在，并按「分组 → 时间」稳定排序返回。

    没给排序列时原样返回（此时行序即时间序，由调用方负责）。
    """
    for column in (group_column, time_column):
        if column and column not in data.columns:
            raise ValueError(f"Missing ordering column: {column}")
    sort_columns = [column for column in (group_column, time_column) if column]
    return data.sort_values(sort_columns, kind="stable") if sort_columns else data


def rolling_statistics(
    data: pd.DataFrame,
    columns: list[str],
    method: str = "mean",
    window: int = 5,
    group_column: str | None = None,
    time_column: str | None = None,
) -> pd.DataFrame:
    """逐行输出滚动统计量，列名形如 ``vibration__rolling_mean_5``。

    ``method``：``mean``/``std``（总体标准差 ddof=0）/``variance``/``median``/``max``/``min``/
    ``max_repeat``。``max_repeat`` 是"窗口内最大值出现了不止一次"的 0/1 指示，用来发现
    保持值/量化平台，这类通道在频谱上不可用。``min_periods=1`` 让每组开头几行也能出值
    （不补 NaN）。
    """
    cols = numeric_columns(data, columns)
    ordered = _ordered(data, group_column, time_column)
    result = pd.DataFrame(index=ordered.index)
    for column in cols:
        grouped = (
            ordered[column].groupby(ordered[group_column], sort=False, dropna=False) if group_column else None
        )

        def calculate(series: pd.Series) -> pd.Series:
            rolling = series.rolling(window, min_periods=1)
            if method == "mean":
                return rolling.mean()
            if method == "std":
                return rolling.std(ddof=0)
            if method == "median":
                return rolling.median()
            if method == "variance":
                # 与 std 保持同一口径（ddof=0，总体方差），这样 variance == std ** 2。
                return rolling.var(ddof=0)
            if method == "max":
                return rolling.max()
            if method == "min":
                return rolling.min()
            if method == "max_repeat":
                # raw=True 直接传 ndarray，比默认的 Series 快很多；判据是窗口内极值重复出现。
                return rolling.apply(lambda values: float(np.sum(values == np.max(values)) > 1), raw=True)
            raise ValueError(f"Unknown rolling feature method: {method}")

        values = grouped.transform(calculate) if grouped is not None else calculate(ordered[column])
        result[f"{column}__rolling_{method}_{window}"] = values
    result = result.loc[data.index]
    # grouped/groups 记录分组是否生效与每组标签，供下游判断"这些特征行是否可分组划分"。
    result.attrs = {
        **data.attrs,
        "grouped": bool(group_column),
        "groups": data[group_column].astype(str).tolist() if group_column else None,
    }
    return result


def temporal_features(
    data: pd.DataFrame,
    columns: list[str],
    method: str = "first_difference",
    lag: int = 1,
    window: int = 20,
    prominence: float = 0.0,
    group_column: str | None = None,
    time_column: str | None = None,
) -> pd.DataFrame:
    """逐行输出差分或滚动自相关，列名形如 ``vibration__first_difference``。

    ``first_difference``/``second_difference``：组内一阶/二阶差分，用 0 填充各组首行
    （首行没有前值）。``autocorrelation``：窗口内滞后 ``lag`` 的自相关系数，
    ``min_periods=lag + 2`` 保证样本量足够。
    ``sum_abs_change``：窗口内相邻点绝对差之和（数值变化之和），刻画"这段信号走了多远"，
    对缓变漂移与高频抖动都敏感；``peak_count``：窗口内局部极大值个数（山峰数），
    ``prominence`` 大于 0 时按 scipy 的峰突出度过滤，用来压掉量化台阶造成的假峰。

    与窗口特征不同，这里的输出行数等于输入行数，且只有特征端口（没有标签端口）。
    """
    cols = numeric_columns(data, columns)
    ordered = _ordered(data, group_column, time_column)
    result = pd.DataFrame(index=ordered.index)
    for column in cols:
        # 分组取列：后面所有 diff()/rolling() 都在组内进行，绝不跨设备。
        grouped = (
            ordered[column].groupby(ordered[group_column], sort=False, dropna=False) if group_column else None
        )
        if method in {"first_difference", "second_difference"}:
            if grouped is not None:
                values = grouped.diff()
                if method == "second_difference":
                    values = values.groupby(ordered[group_column], sort=False, dropna=False).diff()
            else:
                values = ordered[column].diff()
                if method == "second_difference":
                    values = values.diff()
            values = values.fillna(0.0)
        elif method == "autocorrelation":

            def autocorrelation(series: pd.Series) -> pd.Series:
                # raw=False：autocorr 需要 Series，逐窗口构造一次属于必要开销。
                return series.rolling(window, min_periods=lag + 2).apply(
                    lambda values: pd.Series(values).autocorr(lag=lag), raw=False
                )

            values = (
                grouped.transform(autocorrelation)
                if grouped is not None
                else autocorrelation(ordered[column])
            )
            values = values.fillna(0.0)
        elif method == "sum_abs_change":

            def sum_abs_change(series: pd.Series) -> pd.Series:
                # min_periods=2：单个点之间没有"变化"可言，只能用 0 表示。
                return series.rolling(window, min_periods=2).apply(
                    lambda chunk: float(np.abs(np.diff(chunk)).sum()), raw=True
                )

            values = (
                grouped.transform(sum_abs_change) if grouped is not None else sum_abs_change(ordered[column])
            )
            values = values.fillna(0.0)
        elif method == "peak_count":

            def peak_count(series: pd.Series) -> pd.Series:
                def count(chunk: np.ndarray) -> float:
                    if prominence > 0:
                        return float(len(find_peaks(chunk, prominence=prominence)[0]))
                    return float(len(find_peaks(chunk)[0]))

                return series.rolling(window, min_periods=3).apply(count, raw=True)

            values = grouped.transform(peak_count) if grouped is not None else peak_count(ordered[column])
            values = values.fillna(0.0)
        else:
            raise ValueError(f"Unknown temporal feature method: {method}")
        result[f"{column}__{method}"] = values
    result = result.loc[data.index]
    result.attrs = {
        **data.attrs,
        "grouped": bool(group_column),
        "groups": data[group_column].astype(str).tolist() if group_column else None,
    }
    return result


def _information_entropy(values: np.ndarray, bins: int) -> float:
    """直方图信息熵（以 2 为底，单位 bit）。

    先把取值离散到 ``bins`` 个等宽箱，再对非空箱的概率分布求香农熵；
    只统计非空箱，因此不会出现 log(0)。熵越高说明取值越分散。
    """
    counts = np.histogram(values, bins=bins)[0].astype(float)
    probabilities = counts[counts > 0] / counts.sum()
    return float(-np.sum(probabilities * np.log2(probabilities)))


def _approximate_entropy(values: np.ndarray, dimension: int, tolerance: float) -> float:
    """近似熵（ApEn）：``phi(m) - phi(m+1)``，越小说明序列越规律。

    ``phi(k)`` 是"所有长度为 k 的模式对中，两两最大坐标差 ≤ tolerance 的比例"的对数均值，
    其中 tolerance 由调用方按 ``tolerance_ratio * std`` 给出。容差为 0 或窗口太短时
    无法定义，直接返回 0 / 报错，而不是给出 NaN 让下游去猜。
    """
    if len(values) <= dimension + 1:
        raise ValueError("Approximate entropy window is too short for the embedding dimension")
    if not tolerance:
        return 0.0

    def phi(size: int) -> float:
        # sliding_window_view 是零拷贝视图，配合广播算全对距离矩阵；
        # 复杂度 O(n²)，这也是下面把窗口长度限制在 2000 行的原因。
        patterns = np.lib.stride_tricks.sliding_window_view(values, size)
        distances = np.max(np.abs(patterns[:, None, :] - patterns[None, :, :]), axis=2)
        probabilities = np.mean(distances <= tolerance, axis=1)
        # 用 1e-15 下限避免 log(0)：完全没有相似模式时视为极小的正概率。
        return float(np.mean(np.log(np.clip(probabilities, 1e-15, None))))

    return phi(dimension) - phi(dimension + 1)


def entropy_features(
    data: pd.DataFrame,
    columns: list[str],
    methods: list[str] | None = None,
    group_column: str | None = None,
    label_column: str | None = None,
    time_column: str | None = None,
    window_size: int = 0,
    step: int = 0,
    label_policy: str = "strict",
    bins: int = 16,
    embedding_dimension: int = 2,
    tolerance_ratio: float = 0.2,
    asset_column: str | None = None,
    window_span: str | float = "",
    step_span: str | float = "",
    prediction_horizon: str | float = "",
    prediction_gap: str | float = "",
    current_fault_policy: str = "drop",
    normal_label: str = "0",
) -> dict[str, Any]:
    """按窗口计算熵特征，返回与统计/频域特征同构的输出字典。

    输出包含 ``features``（窗口 × 特征列）与 ``labels``（窗口标签向量），
    以及窗口键、分组、来源行等元数据；``asset_column`` 存在时还会把资产带进
    ``attrs["assets"]``，使验证器可以按资产留出。

    注意事项：熵是 O(n²) 计算，单窗口超过 2000 行会直接报错，请显式设置 ``window_size``；
    所有取值必须有限。
    """
    cols = numeric_columns(data, columns)
    # 资产随窗口走：只在"分组列 + 资产列都给了"时才有意义。
    asset_of = group_assets(data, group_column, asset_column) if asset_column and group_column else None
    # 标签列与分组列不能同时当特征输入，否则等于把答案喂给模型。
    if label_column in cols or group_column in cols:
        raise ValueError("Label/group columns cannot be feature inputs")
    selected = methods or ["approximate_entropy", "information_entropy"]
    if any(method not in ENTROPY_METHODS for method in selected):
        raise ValueError("Unknown entropy feature method")
    # 箱值熵与信息熵是同一套"按箱统计的香农熵"（同一段代码、同一个 bins 参数），
    # 保留两个名字是为了对齐组件清单里的叫法：叫 binned_entropy 也要能算出来。
    binned = "binned_entropy" in selected
    span, stride, horizon, gap = window_arguments(
        window_size=window_size,
        window_span=window_span,
        step_span=step_span,
        label_policy=label_policy,
        prediction_horizon=prediction_horizon,
        prediction_gap=prediction_gap,
        current_fault_policy=current_fault_policy,
        time_column=time_column,
        label_column=label_column,
    )
    counters = {"current_fault": 0, "unknown_future": 0}
    rows: list[dict[str, float]] = []
    labels: list[Any] = []
    keys: list[str] = []
    groups: list[str] = []
    coverage: list[list[Any]] = []
    for key, chunk, group, source_rows, ready_label in prepared_windows(
        data,
        group_column,
        label_column,
        time_column,
        window_size,
        step,
        label_policy,
        span,
        stride,
        horizon,
        gap,
        current_fault_policy,
        normal_label,
        counters,
    ):
        row = {}
        for column in cols:
            values = chunk[column].to_numpy(dtype=float)
            if not np.isfinite(values).all():
                raise ValueError("Entropy features require finite numeric values")
            # 近似熵是二次复杂度，这里显式设上限，避免用户忘记设窗口时把服务拖垮。
            if "approximate_entropy" in selected and len(values) > 2000:
                raise ValueError("Approximate entropy windows are limited to 2000 rows; set window_size")
            if "information_entropy" in selected or binned:
                row[f"{column}__information_entropy"] = _information_entropy(values, bins)
            if "approximate_entropy" in selected:
                tolerance = tolerance_ratio * float(np.std(values))
                row[f"{column}__approximate_entropy"] = _approximate_entropy(
                    values, embedding_dimension, tolerance
                )
        rows.append(row)
        keys.append(key)
        groups.append(group)
        coverage.append(source_rows)
        if label_column:
            labels.append(
                ready_label if ready_label is not None else _window_label(chunk, label_column, label_policy)
            )
    assemble_size, assemble_step, overlapping = window_shape(span, stride, window_size, step)
    attrs = window_attrs(
        data.attrs,
        label_policy=label_policy,
        span=span,
        stride=stride,
        horizon=horizon,
        gap=gap,
        current_fault_policy=current_fault_policy,
        counters=counters,
    )
    outputs = _assemble(
        rows,
        keys,
        labels,
        groups,
        coverage,
        attrs,
        group_column,
        label_column,
        assemble_size,
        assemble_step,
        overlapping=overlapping,
    )
    attach_assets(outputs, asset_of, groups)
    return outputs
