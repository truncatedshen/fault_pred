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
from scipy.signal import find_peaks
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import train_test_split

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


def peak_summary(
    data: pd.DataFrame,
    columns: list[str] | None = None,
    prominence: float = 0.0,
    distance: int = 1,
    group_column: str | None = None,
) -> dict[str, Any]:
    """逐列找局部极大值（山峰）并给出摘要与峰位。

    ``prominence`` 是 scipy 的"峰突出度"：只保留比两侧谷底高出至少这个值的峰。真实工业
    信号里量化台阶会产生大量高度相同的假峰（保持值段），把 ``prominence`` 设为量化的
    最小刻度是最省事的过滤方式；``distance`` 限制两个峰之间的最小间距，防止一个宽峰被
    数成多个。``count`` 是峰个数本身——它是"这段信号有几个周期"的粗粒度代理。

    **给了 ``group_column`` 就按组独立找峰**（本平台的默认纪律：统计量绝不跨设备）。
    不分组时，"设备 A 的结尾 + 设备 B 的开头"这个拼接处会被当成一个峰，结论就是假的。
    分组后 ``rows`` 每行是"一组 × 一列"，``peaks`` 的键是 ``"<组>:<列>"``。
    """
    cols = numeric_columns(data, columns)
    if len(data) < 3:
        raise ValueError("Peak analysis needs at least three rows")
    if group_column and group_column not in data.columns:
        raise ValueError(f"Missing group column: {group_column}")
    if isinstance(distance, bool) or not isinstance(distance, int) or distance < 1:
        raise ValueError("distance must be an integer >= 1")
    scopes = (
        [(str(name), frame) for name, frame in data.groupby(group_column, sort=False, dropna=False)]
        if group_column
        else [(None, data)]
    )
    rows, peaks = [], {}
    for scope, frame in scopes:
        if len(frame) < 3:
            raise ValueError(
                f"Peak analysis needs at least three rows per group; group {scope} has {len(frame)}"
            )
        for column in cols:
            values = frame[column].to_numpy(dtype=float)
            if not np.isfinite(values).all():
                raise ValueError(f"Peak analysis requires finite values: {column}")
            index, properties = find_peaks(values, prominence=prominence, distance=distance)
            heights = values[index]
            rows.append(
                {
                    **({"group": scope} if scope is not None else {}),
                    "column": column,
                    "count": int(len(index)),
                    "rate": float(len(index) / len(values)),
                    "mean_height": float(np.mean(heights)) if len(index) else None,
                    "mean_distance": float(np.mean(np.diff(index))) if len(index) > 1 else None,
                    "max_prominence": float(np.max(properties["prominences"])) if len(index) else None,
                }
            )
            peaks[column if scope is None else f"{scope}:{column}"] = {
                "positions": index.tolist(),
                "values": heights.tolist(),
                "prominences": properties["prominences"].tolist(),
            }
    return {
        "rows": rows,
        "peaks": peaks,
        "prominence": prominence,
        "distance": distance,
        "group_column": group_column,
    }


def normality_check(
    data: pd.DataFrame, columns: list[str] | None = None, method: str = "normaltest", alpha: float = 0.05
) -> dict[str, Any]:
    """逐列做正态性检验，给出统计量、p 值与结论。

    ``normaltest``（D'Agostino–Pearson）需要 **至少 8 个**样本；``shapiro``（Shapiro–Wilk）
    需要 3~5000 个样本，超过 5000 时 scipy 会给出警告，因此这里主动改成"报错 + 提示换
    检验"，而不是让使用者拿到一个自己都不信的 p 值。

    **结论解读必须谨慎**：``p > alpha`` 只说明"没有足够证据拒绝正态"，不等于数据服从正态；
    样本量很大时该检验会对毫无工程意义的微小偏离给出显著结果。返回值的 ``caveat``
    会把这句话带给使用者，报告里应当照抄而不是只写"通过/不通过"。
    """
    cols = numeric_columns(data, columns)
    if method not in {"normaltest", "shapiro"}:
        raise ValueError(f"Unknown normality test: {method}")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between 0 and 1")
    rows = []
    for column in cols:
        values = data[column].dropna().to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError(f"Normality check requires finite values: {column}")
        if method == "normaltest" and len(values) < 8:
            raise ValueError(
                f"normaltest needs at least 8 samples for {column} (got {len(values)}); "
                "use shapiro or provide more data"
            )
        if method == "shapiro":
            if len(values) < 3:
                raise ValueError(f"shapiro needs at least 3 samples for {column} (got {len(values)})")
            if len(values) > 5000:
                raise ValueError(
                    f"shapiro is unreliable above 5000 samples for {column} (got {len(values)}); "
                    "use normaltest"
                )
        result = stats.normaltest(values) if method == "normaltest" else stats.shapiro(values)
        constant = not np.ptp(values)
        rows.append(
            {
                "column": column,
                "count": int(len(values)),
                "statistic": float(result.statistic),
                "p_value": float(result.pvalue),
                # 常数列的偏度/峰度分母为 0：约定为 0，并把"这是常量"写进结果里。
                "skewness": float(stats.skew(values)) if not constant else 0.0,
                "kurtosis": float(stats.kurtosis(values)) if not constant else 0.0,
                "constant": bool(constant),
                "normal": bool(result.pvalue > alpha),
            }
        )
    return {
        "rows": rows,
        "method": method,
        "alpha": alpha,
        "caveat": (
            "p > alpha means the test failed to reject normality, not that the data is normal; "
            "with large samples the test flags deviations that have no engineering meaning."
        ),
    }


def divergence(
    reference: pd.DataFrame,
    current: pd.DataFrame,
    columns: list[str] | None = None,
    bins: int = 10,
    epsilon: float = 1e-9,
    js_threshold: float = 0.1,
) -> dict[str, Any]:
    """逐列比较两份样本的分布差异：KL 散度与 Jensen–Shannon 散度。

    **方向性很重要**：KL 是非对称的，这里定义 ``kl = KL(Q || P)``，P 是 ``reference``、
    Q 是 ``current``，含义是"用参考分布去编码当前数据的额外代价"。JS 是对称且有界的
    （0 ~ ln2），更适合做跨列比较与阈值判断。

    分箱边界取自**两份样本合并后的等宽区间**（这样两边落在同一组箱里），并在概率上加
    ``epsilon`` 兜底以避免 log(0)。两份样本都退化成同一个常量时散度为 0；若一边恒定、
    一边在波动，合并区间非退化，epsilon 兜底会给出一个很大的有限值——"从恒定变成波动"
    本身就是最强的分布变化，这个值会把它显式暴露出来。
    """
    cols = numeric_columns(reference, columns)
    missing = set(cols) - set(current.columns)
    if missing:
        raise ValueError(f"Current dataset is missing columns: {sorted(missing)}")
    rows = []
    for column in cols:
        before = reference[column].dropna().to_numpy(dtype=float)
        after = current[column].dropna().to_numpy(dtype=float)
        if not len(before) or not len(after):
            raise ValueError(f"Divergence comparison needs non-missing values for {column}")
        if not np.isfinite(before).all() or not np.isfinite(after).all():
            raise ValueError(f"Divergence comparison requires finite values for {column}")
        combined = np.concatenate([before, after])
        if not np.ptp(combined):
            # 两份样本都只含同一个常量：分布完全一致。
            kl = js = 0.0
        else:
            edges = np.histogram_bin_edges(combined, bins=bins)
            p = np.histogram(before, bins=edges)[0].astype(float)
            q = np.histogram(after, bins=edges)[0].astype(float)
            p = np.clip(p / p.sum(), epsilon, None)
            q = np.clip(q / q.sum(), epsilon, None)
            kl = float(np.sum(q * np.log(q / p)))
            middle = 0.5 * (p + q)
            js = float(0.5 * np.sum(p * np.log(p / middle)) + 0.5 * np.sum(q * np.log(q / middle)))
        rows.append(
            {
                "column": column,
                "reference_count": int(len(before)),
                "current_count": int(len(after)),
                "reference_mean": float(np.mean(before)),
                "current_mean": float(np.mean(after)),
                "kl": kl,
                "js": js,
                "flagged": bool(js >= js_threshold),
            }
        )
    return {
        "rows": rows,
        "flagged_columns": [row["column"] for row in rows if row["flagged"]],
        "bins": bins,
        "js_threshold": js_threshold,
        "direction": "kl = KL(current || reference)",
    }


def autocorrelation_function(
    data: pd.DataFrame,
    columns: list[str] | None = None,
    max_lag: int = 50,
    alpha: float = 0.05,
    group_column: str | None = None,
) -> dict[str, Any]:
    """逐列给出 0..max_lag 的自相关曲线与 95% 置信带。

    与 :func:`periodicity_check` 的分工：那里只回答"主周期是多少"（取峰值），这里给出
    **整条 ACF 曲线** 并逐滞后判断显著性——用来回答"这条通道的记忆有多长""是不是白噪声"
    这类问题。置信带用经典的 ``±z / sqrt(n)`` 近似（白噪声下 ACF 近似正态），
    ``white_noise`` 表示 1..max_lag 内没有任何滞后超出带外。

    **给了 ``group_column`` 就按组独立计算**：ACF 假设序列连续，跨设备的拼接会凭空造出
    一个"长程相关"。分组后置信带按各组的样本量单独算，``series`` 的 ``name`` 是
    ``"<组>:<列>"``；``max_lag`` 会被最短的那一组削短，保证各条曲线可比较。
    """
    cols = numeric_columns(data, columns)
    if len(data) < 4:
        raise ValueError("ACF needs at least four rows")
    if group_column and group_column not in data.columns:
        raise ValueError(f"Missing group column: {group_column}")
    if isinstance(max_lag, bool) or not isinstance(max_lag, int) or max_lag < 1:
        raise ValueError("max_lag must be an integer >= 1")
    scopes = (
        [(str(name), frame) for name, frame in data.groupby(group_column, sort=False, dropna=False)]
        if group_column
        else [(None, data)]
    )
    limit = min(max_lag, min(len(frame) for _, frame in scopes) - 2)
    if limit < 1:
        raise ValueError("max_lag is too small for the shortest group in this dataset")
    z_score = float(stats.norm.ppf(1 - alpha / 2))
    rows, series = [], []
    for scope, frame in scopes:
        band = float(z_score / np.sqrt(len(frame)))
        for column in cols:
            values = frame[column].to_numpy(dtype=float)
            if not np.isfinite(values).all():
                raise ValueError(f"ACF requires finite values: {column}")
            centered = values - values.mean()
            variance = float(np.dot(centered, centered))
            if variance:
                correlations = np.array(
                    [1.0]
                    + [
                        float(np.dot(centered[:-lag], centered[lag:]) / variance)
                        for lag in range(1, limit + 1)
                    ]
                )
            else:
                # 常数列的自相关无定义：保留全 0 曲线（滞后 0 记 1），而不是抛错。
                correlations = np.array([1.0] + [0.0] * limit)
            outside = [lag for lag in range(1, limit + 1) if abs(correlations[lag]) > band]
            rows.append(
                {
                    **({"group": scope} if scope is not None else {}),
                    "column": column,
                    "count": int(len(values)),
                    "lag_1": float(correlations[1]),
                    "confidence": band,
                    "significant_count": len(outside),
                    "significant_lags": outside[:20],
                    "white_noise": not outside,
                }
            )
            series.append(
                {
                    "name": column if scope is None else f"{scope}:{column}",
                    "x": list(range(0, limit + 1)),
                    "y": correlations.tolist(),
                    "upper": band,
                    "lower": -band,
                }
            )
    return {
        "rows": rows,
        "series": series,
        "max_lag": limit,
        "alpha": alpha,
        "group_column": group_column,
    }


def _subsample_rows(count: int, limit: int) -> np.ndarray:
    """等间隔抽样出至多 ``limit`` 个位置，用于把曲线压到前端可画、MCP 可返回的长度。"""
    if count <= limit:
        return np.arange(count)
    return np.unique(np.linspace(0, count - 1, limit).round().astype(int))


def isotonic_fit(
    data: pd.DataFrame,
    x_column: str,
    y_column: str,
    increasing: bool = True,
    out_of_bounds: str = "clip",
    max_points: int = 500,
) -> dict[str, Any]:
    """保序（单调）回归：不放任何函数形式，只要求拟合曲线单调不减/不增。

    适合"某个监测量随时间单调劣化"的场景：它给出的是**形状约束下**的最小二乘拟合。
    注意两点，报告里必须一起写：

    * 保序回归的 R² 是**样本内**的，而且因为它是分段常数、又带有单调约束，天然比线性/
      多项式拟合更容易贴近数据，用它与别的模型比 R² 是不公平的；
    * ``blocks``（拟合出的平台段数）才是它真正提供的信息——段数少说明单调关系干净，
      段数多说明数据里的"单调趋势"其实很碎。

    ``out_of_bounds`` 决定新点落在训练范围之外时的取值（``clip``/``nan``）。
    """
    numeric_columns(data, [x_column, y_column])
    pairs = data[[x_column, y_column]].dropna().to_numpy(dtype=float)
    if len(pairs) < 3:
        raise ValueError("Isotonic regression needs at least three paired values")
    if not np.isfinite(pairs).all():
        raise ValueError("Isotonic regression requires finite values")
    order = np.argsort(pairs[:, 0], kind="stable")
    x, y = pairs[order, 0], pairs[order, 1]
    model = IsotonicRegression(increasing=increasing, out_of_bounds=out_of_bounds)
    fitted = model.fit_transform(x, y)
    blocks = int(np.unique(fitted).size)
    rho = stats.spearmanr(x, y)
    selected = _subsample_rows(len(x), max_points)
    r2 = float(r2_score(y, fitted)) if np.ptp(y) else 0.0
    return {
        "rows": [
            {
                "index": int(position),
                "x": float(x[position]),
                "y": float(y[position]),
                "fitted": float(fitted[position]),
            }
            for position in selected
        ],
        "increasing": bool(increasing),
        "blocks": blocks,
        "sample_count": int(len(x)),
        "spearman": float(rho.statistic) if np.isfinite(rho.statistic) else 0.0,
        "spearman_pvalue": float(rho.pvalue) if np.isfinite(rho.pvalue) else 1.0,
        "in_sample_r2": r2,
        "caveat": (
            "Isotonic R² is in-sample and a monotone step fit is more flexible than a line; "
            "do not compare it with linear/polynomial R² as if the models were equally constrained."
        ),
    }


def gbr_fit(
    data: pd.DataFrame,
    columns: list[str],
    target_column: str,
    n_estimators: int = 100,
    learning_rate: float = 0.1,
    max_depth: int = 3,
    min_samples_leaf: int = 1,
    subsample: float = 1.0,
    test_size: float = 0.0,
    random_state: int = 42,
    max_points: int = 500,
) -> dict[str, Any]:
    """用梯度提升回归（GBR）量化"这些列能解释多少目标"，并给出特征重要性。

    这是**相关性度量**，不是验证器：``test_size=0``（默认）时只在全量数据上拟合并报告
    样本内 R²，并在警告里写明"这是拟合优度，不是泛化能力"。设置 ``test_size > 0``
    会再切一份随机留出集——但随机切分无视窗口重叠，结论只能当探索用；真正要下结论
    请走 ``validation.*``（它按组/资产/时间切分并检查原始行重叠）。

    ``importance`` 来自 GBR 的 ``feature_importances_``（基于分裂带来的不纯度下降），
    对相关特征会互相分流，排序只应作为"哪些列值得再看"的线索。
    """
    numeric_columns(data, [*columns, target_column])
    if target_column in columns:
        raise ValueError("The target column cannot also be a GBR feature")
    if not columns:
        raise ValueError("GBR needs at least one feature column")
    if not 0 <= test_size < 1:
        raise ValueError("test_size must be in [0, 1)")
    values = data[[*columns, target_column]].dropna()
    if len(values) < 10:
        raise ValueError("GBR fit needs at least ten complete rows")
    numeric = values.to_numpy(dtype=float)
    if not np.isfinite(numeric).all():
        raise ValueError("GBR fit requires finite values")
    frame = pd.DataFrame(numeric[:, :-1], columns=columns, index=values.index)
    target = pd.Series(numeric[:, -1], index=values.index)
    warnings: list[str] = []
    if test_size:
        train, test = train_test_split(np.arange(len(frame)), test_size=test_size, random_state=random_state)
        warnings.append(
            "Holdout split is random and ignores window overlap; use validation.* for a defensible estimate."
        )
    else:
        train = test = np.arange(len(frame))
        warnings.append("GBR was fitted and scored on the same rows; the R² is in-sample.")
    estimator = GradientBoostingRegressor(
        n_estimators=n_estimators,
        learning_rate=learning_rate,
        max_depth=max_depth,
        min_samples_leaf=min_samples_leaf,
        subsample=subsample,
        random_state=random_state,
    ).fit(frame.iloc[train], target.iloc[train])
    predicted = estimator.predict(frame.iloc[test])
    actual = target.iloc[test].to_numpy()
    constant = not np.ptp(actual)
    metrics = {
        "algorithm": "gbr_fit",
        "target_column": target_column,
        "train_count": int(len(train)),
        "test_count": int(len(test)),
        "r2": float(r2_score(actual, predicted)) if not constant else 0.0,
        "mae": float(mean_absolute_error(actual, predicted)),
        "rmse": float(np.sqrt(mean_squared_error(actual, predicted))),
        "train_r2": float(r2_score(target.iloc[train], estimator.predict(frame.iloc[train])))
        if np.ptp(target.iloc[train].to_numpy())
        else 0.0,
        "warnings": warnings,
    }
    importance = pd.DataFrame({"feature": columns, "importance": estimator.feature_importances_}).sort_values(
        "importance", ascending=False, ignore_index=True
    )
    selected = _subsample_rows(len(test), max_points)
    series = {
        "x": [int(position) for position in selected],
        "actual": actual[selected].tolist(),
        "predicted": predicted[selected].tolist(),
    }
    return {"metrics": metrics, "importance": importance, "series": series}
