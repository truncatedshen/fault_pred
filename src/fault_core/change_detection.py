"""Univariate time-series structure detectors: shift, volatility, seasonality, drift.

批次 3 的七个检测器回答同一类问题——"这条序列的结构从哪一刻起不一样了"——但各自判定
"不一样"的口径完全不同，因此这个模块的第一原则是**把口径写进返回值**：

* ``level_shift``：候选点前后各 ``window`` 点的均值差的 t 统计量，阈值 ``threshold``（t 量纲）；
* ``volatility_shift``：前后两段方差之比的对数，阈值 ``threshold``（对数比量纲）；
* ``seasonal``：偏离"参考段季节剖面"的稳健 z 分数，阈值 ``threshold``（z 量纲）；
* ``autoregression``：AR(p) 单步预测残差的稳健 z 分数，阈值 ``threshold``（z 量纲）；
* ``esd``：广义 ESD（Rosner）的 R 统计量，临界值由 ``alpha`` 与 t 分布给出；
* ``nsigma``：偏离中心 ``sigma`` 倍尺度，阈值就是那个倍数；
* ``mean_drift``：CUSUM 累积偏差，阈值 ``decision``（sigma 量纲），另有 ``slack``。

两条共同纪律：

* **不跨分组边界**。给了 ``group_column`` 就逐组独立检测，组与组之间既不比较也不累积
  （否则"设备 A 的尾部 + 设备 B 的头部"会被判成一次阶跃）。
* **窗口不足处不评分**。输出里的 ``anomaly_score`` 在这些位置是 ``NaN``（``is_anomaly``
  为 ``False``），metrics 里给出 ``scored_count``。用 0 填充会让人误以为"那一刻分数很低、
  很安全"，而事实是"还没法判断"。

这些都是规则型或轻量估计型检测器，没有留出集的概念，阈值在完整输入上得到；因此每条
metrics 都带一条固定的探索性警告。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from fault_core.data import numeric_columns

#: 判定"这一段完全没有波动"的相对下限：方差为 0 时对数比无定义，但一个恒定的预热段
#: 不该让整次检测失败，因此用相对下限兜底，并把"出现了恒定段"写进 warnings。
_VARIANCE_FLOOR = 1e-12

#: 本模块支持的全部方法名（组件 schema 从它取值）。
METHODS = (
    "level_shift",
    "volatility_shift",
    "seasonal",
    "autoregression",
    "esd",
    "nsigma",
    "mean_drift",
)


def _ordered(data: pd.DataFrame, group_column: str | None, time_column: str | None) -> pd.DataFrame:
    """按「分组 → 时间」稳定排序；未给排序列时保持原行序（调用方负责它是时间序）。"""
    for column in (group_column, time_column):
        if column and column not in data.columns:
            raise ValueError(f"Missing ordering column: {column}")
    sort_columns = [column for column in (group_column, time_column) if column]
    return data.sort_values(sort_columns, kind="stable") if sort_columns else data


def _scopes(data: pd.DataFrame, group_column: str | None) -> list[tuple[str | None, pd.DataFrame]]:
    """把数据切成"一段一段独立检测"的序列；不分组时整列就是一段。"""
    if not group_column:
        return [(None, data)]
    return [(str(name), frame) for name, frame in data.groupby(group_column, sort=False, dropna=False)]


def _robust_scale(values: np.ndarray) -> float:
    """稳健尺度 ``1.4826 * MAD``：正态下与标准差同量纲，但不会被离群点带跑。"""
    median = float(np.median(values))
    return float(1.4826 * np.median(np.abs(values - median)))


def _level_shift_scores(values: np.ndarray, window: int) -> np.ndarray:
    """候选点前后各 ``window`` 个点的两样本 t 统计量（Welch 形式，越像阶跃越大）。

    用 t 而不是"均值差"本身，是为了让判定对噪声水平免疫：同样的两个单位跳变，在噪声很小
    时是显著阶跃，在噪声很大时什么都不是。前后各不足 ``window`` 个点的位置返回 NaN。
    """
    count = len(values)
    scores = np.full(count, np.nan, dtype=float)
    for split in range(window, count - window + 1):
        before = values[split - window : split]
        after = values[split : split + window]
        standard_error = np.sqrt(
            float(np.var(before, ddof=1)) / window + float(np.var(after, ddof=1)) / window
        )
        if not np.isfinite(standard_error) or standard_error <= 0:
            # 两段都没波动：t 统计量在此无定义，如实留空而不是编一个很大的数。
            continue
        scores[split] = abs(float(np.mean(after) - np.mean(before))) / standard_error
    return scores


def _volatility_shift_scores(values: np.ndarray, window: int) -> np.ndarray:
    """前后两段方差之比的对数，再按它在原假设下的抽样标准差归一化（z 量纲）。

    直接用"对数比"当分数会让阈值变得没法解释：两段同样来自 N(0,1) 的窗口，其对数比也有
    大约 ``sqrt(2/(window-1))`` 的波动，窗口越小抖动越大。这里除掉这个标准差，于是
    **分数与 ``level_shift`` 同为 z 量纲**，``threshold=4`` 在两个方法里含义一致
    （这是正态近似，窗口很小时它偏低，报告里应当写明）。
    """
    count = len(values)
    scores = np.full(count, np.nan, dtype=float)
    floor = _VARIANCE_FLOOR * max(float(np.var(values, ddof=1)), 1.0)
    null_spread = np.sqrt(2.0 / max(window - 1, 1))
    for split in range(window, count - window + 1):
        variance_before = float(np.var(values[split - window : split], ddof=1))
        variance_after = float(np.var(values[split : split + window], ddof=1))
        scores[split] = abs(np.log((variance_after + floor) / (variance_before + floor))) / null_spread
    return scores


def _seasonal_scores(
    values: np.ndarray, period: int, reference_fraction: float
) -> tuple[np.ndarray, dict[str, Any]]:
    """用"参考段的季节剖面"给整条序列打分：``|y - 该相位的中位数| / 稳健尺度``。

    剖面取自**前 ``reference_fraction`` 段**（默认一半），因此后半段是真正的样本外打分；
    参考段自身属于样本内，``reference_rows`` 会如实写出来。

    ``seasonal_strength`` = ``1 - var(参考段残差) / var(参考段)``，**只在参考段上算**：
    它回答"这条序列本身有多季节"，而不是"整条序列有没有变化"。把后半段也算进去的话，
    一次阶跃会把强度压到很低，看起来像"信号不季节"——那是把两件事混成一件。
    后半段的变化由分数与标记负责。
    """
    count = len(values)
    boundary = max(period, int(count * reference_fraction))
    if count < 2 * period or boundary >= count:
        raise ValueError("Seasonal detection needs two full periods and a shorter reference segment")
    phase = np.arange(count) % period
    reference_phase = phase[:boundary]
    profile = np.array(
        [float(np.median(values[:boundary][reference_phase == index])) for index in range(period)]
    )
    residual = values - profile[phase]
    scale = _robust_scale(residual[:boundary]) or _robust_scale(values)
    scores = np.abs(residual) / scale if scale > 0 else np.zeros(count)
    reference_values = values[:boundary]
    variance = float(np.var(reference_values, ddof=1))
    strength = (
        float(max(0.0, 1.0 - float(np.var(residual[:boundary], ddof=1)) / variance)) if variance else 0.0
    )
    details = {
        "period": int(period),
        "seasonal_strength": strength,
        "reference_rows": int(boundary),
        "scale": float(scale),
    }
    return scores, details


def _autoregression_scores(
    values: np.ndarray, order: int, train_fraction: float
) -> tuple[np.ndarray, dict[str, Any]]:
    """AR(p) 单步预测残差的稳健 z 分数（只用前 ``train_fraction`` 段拟合）。

    评估段每一步都用**真实历史**做一步预测（而不是递归外推），所以一条稳定的序列其残差
    应当接近白噪声；残差突然变大就意味着"这条序列的动态变了"。训练段的分数属于样本内，
    一律留空（NaN），只有评估段参与告警。
    """
    count = len(values)
    boundary = int(count * train_fraction)
    if boundary <= order + 2 or count - boundary < 2:
        raise ValueError("Autoregression detection needs more training rows than its order")
    rows = np.arange(order, boundary)
    design = np.column_stack([np.ones(len(rows)), *[values[rows - lag] for lag in range(1, order + 1)]])
    coefficients = np.linalg.lstsq(design, values[rows], rcond=None)[0]
    scale = _robust_scale(values[rows] - design @ coefficients)
    scores = np.full(count, np.nan, dtype=float)
    if scale > 0:
        for position in range(boundary, count):
            history = np.array([1.0, *[values[position - lag] for lag in range(1, order + 1)]])
            scores[position] = abs(float(values[position]) - float(history @ coefficients)) / scale
    details = {
        "order": int(order),
        "coefficients": [float(value) for value in coefficients],
        "train_rows": int(boundary),
        "scale": float(scale),
    }
    return scores, details


def _esd(values: np.ndarray, max_outliers: int, alpha: float) -> tuple[np.ndarray, dict[str, Any]]:
    """广义 ESD（Rosner）检验：迭代剔除最极端点，与 t 分布导出的临界值 λ 比较。

    每一步的 R 统计量是"当前最极端点偏离剩余样本均值的标准差倍数"，λ 随剩余样本量与
    显著性水平变化。取**最后一个满足 R > λ 的步数**作为判定——这是 Rosner 的关键：
    中途不满足就说明前面的"离群点"其实只是长尾，不能算异常。
    """
    count = len(values)
    r_values: list[float] = []
    lambda_values: list[float] = []
    removed: list[int] = []
    remaining = list(range(count))
    for _ in range(max_outliers):
        current = values[remaining]
        deviation = float(np.std(current, ddof=1))
        if deviation <= 0:
            break
        statistic = np.abs(current - float(np.mean(current))) / deviation
        index = int(np.argmax(statistic))
        r_values.append(float(statistic[index]))
        remaining_count = len(remaining)
        # λ_i = (n-i-1) * t_{p, n-i-2} / sqrt((n-i) * (n-i-2 + t²))，p = 1 - alpha/(2*(n-i))
        probability = 1 - alpha / (2 * remaining_count)
        t_value = float(stats.t.ppf(probability, remaining_count - 2))
        lambda_values.append(
            (remaining_count - 1) * t_value / np.sqrt(remaining_count * (remaining_count - 2 + t_value**2))
        )
        removed.append(remaining.pop(index))
    detected = 0
    for step, (r_value, lambda_value) in enumerate(zip(r_values, lambda_values), start=1):
        if r_value > lambda_value:
            detected = step
    outliers = removed[:detected]
    scale = float(np.std(values, ddof=1))
    mean = float(np.mean(values))
    raw = np.abs(values - mean) / scale if scale > 0 else np.zeros(count)
    flags = np.zeros(count, dtype=float)
    flags[outliers] = 1.0
    details = {
        "alpha": float(alpha),
        "max_outliers": int(max_outliers),
        "outlier_count": int(detected),
        "outliers": [int(position) for position in outliers],
        # 只保留前若干个 R/λ 对照：够复核判定过程，又不至于把响应撑大。
        "R": [round(value, 4) for value in r_values[:10]],
        "lambda": [round(value, 4) for value in lambda_values[:10]],
    }
    return flags * raw, details


def detect(
    data: pd.DataFrame,
    column: str,
    method: str = "level_shift",
    window: int = 20,
    threshold: float = 4.0,
    sigma: float = 3.0,
    group_column: str | None = None,
    time_column: str | None = None,
    period: int = 24,
    reference_fraction: float = 0.5,
    order: int = 2,
    train_fraction: float = 0.5,
    max_outlier_fraction: float = 0.1,
    alpha: float = 0.05,
    mode: str = "global",
    slack: float = 0.5,
    decision: float = 5.0,
) -> dict[str, Any]:
    """跑一个结构变化检测器，返回 ``model / prediction / metrics`` 三件套。

    参数按方法取值，未用到的不生效（schema 里全部列出，各自写清用途）。
    ``anomaly_score`` 的量纲随方法不同（t 统计量 / 对数比 / z 分数 / sigma 倍数），
    所以报告里引用分数时必须同时写上方法名与阈值——跨方法的分数不可比较。
    """
    if method not in METHODS:
        raise ValueError(f"Unknown change detection method: {method}")
    numeric_columns(data, [column])
    ordered = _ordered(data, group_column, time_column)
    scores = pd.Series(np.nan, index=ordered.index, dtype=float)
    flags = pd.Series(False, index=ordered.index, dtype=bool)
    details: dict[str, Any] = {}
    for scope, frame in _scopes(ordered, group_column):
        values = frame[column].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError(f"Change detection requires finite values: {column}")
        label = "" if scope is None else f" (group {scope})"
        raw_scores = np.full(len(values), np.nan, dtype=float)
        if method == "level_shift":
            if len(values) < 2 * window + 1:
                raise ValueError(f"level_shift needs at least 2*window+1 rows{label}")
            raw_scores = _level_shift_scores(values, window)
            raw_flags = raw_scores > threshold
            details.setdefault("window", int(window))
        elif method == "volatility_shift":
            if len(values) < 2 * window + 1:
                raise ValueError(f"volatility_shift needs at least 2*window+1 rows{label}")
            raw_scores = _volatility_shift_scores(values, window)
            raw_flags = raw_scores > threshold
            details.setdefault("window", int(window))
        elif method == "seasonal":
            raw_scores, info = _seasonal_scores(values, period, reference_fraction)
            raw_flags = raw_scores > threshold
            details = {**info, **details}
        elif method == "autoregression":
            raw_scores, info = _autoregression_scores(values, order, train_fraction)
            raw_flags = raw_scores > threshold
            details = {**info, **details}
        elif method == "esd":
            if len(values) < 8:
                raise ValueError(f"ESD needs at least eight rows{label}")
            limit = max(1, min(int(round(max_outlier_fraction * len(values))), len(values) - 2))
            raw_scores, info = _esd(values, limit, alpha)
            raw_flags = np.zeros(len(values), dtype=bool)
            if info["outliers"]:
                raw_flags[np.asarray(info["outliers"], dtype=int)] = True
            details = {**info, **details}
        elif method == "nsigma":
            raw_scores, raw_flags, info = _nsigma(values, mode, sigma, window, bool(group_column))
            details = {**info, **details}
        else:  # mean_drift
            raw_scores, raw_flags, info = _mean_drift(values, reference_fraction, slack, decision)
            details = {**info, **details}
            if info["first_alarm"] is not None:
                # 位置换算成真实的索引标签，报告里可以直接定位到那一行。
                details.setdefault("first_alarm_index", str(frame.index[info["first_alarm"]]))
        scores.loc[frame.index] = raw_scores
        flags.loc[frame.index] = raw_flags
    warnings = [
        "Threshold or centre/scale was derived from the complete input; treat this as a "
        "descriptive screen, not a validated detector."
    ]
    scored = int(scores.notna().sum())
    if scored < len(scores):
        warnings.append(f"{len(scores) - scored} rows were left unscored (window or reference too short).")
    if method == "esd":
        threshold_value = float("inf") if not flags.any() else float(scores[flags.to_numpy()].min())
    elif method == "nsigma":
        threshold_value = float(sigma)
    elif method == "mean_drift":
        threshold_value = float(decision)
    else:
        threshold_value = float(threshold)
    scores = scores.loc[data.index]
    flags = flags.loc[data.index]
    model = FittedChangeDetector(
        method=method,
        column=column,
        parameters={
            key: value
            for key, value in (
                ("window", window),
                ("threshold", threshold),
                ("sigma", sigma),
                ("group_column", group_column),
                ("time_column", time_column),
                ("period", period),
                ("reference_fraction", reference_fraction),
                ("order", order),
                ("train_fraction", train_fraction),
                ("max_outlier_fraction", max_outlier_fraction),
                ("alpha", alpha),
                ("mode", mode),
                ("slack", slack),
                ("decision", decision),
            )
        },
        threshold=threshold_value,
        details=details,
    )
    prediction = pd.DataFrame({"anomaly_score": scores, "is_anomaly": flags}, index=data.index)
    metrics = {
        "algorithm": method,
        "column": column,
        "threshold": threshold_value,
        "anomaly_count": int(flags.sum()),
        "anomaly_rate": float(flags.mean()),
        "scored_count": scored,
        "sample_count": int(len(data)),
        "group_column": group_column,
        **details,
        "warnings": warnings,
    }
    return {"model": model, "prediction": prediction, "metrics": metrics}


def _nsigma(
    values: np.ndarray, mode: str, sigma: float, window: int, grouped: bool
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """N-sigma 打分：中心与尺度可以来自整列、整组或滚动窗口。

    ``global``/``group`` 用全量统计量（等于"在完整输入上拟合"，属于描述性筛查）；
    ``rolling`` 用滚动均值/标准差，因此能跟上缓慢漂移，代价是窗口内样本少时尺度不稳，
    这时该点留空（NaN）而不是给一个虚高的分数。
    """
    if mode == "rolling":
        series = pd.Series(values)
        minimum = max(2, window // 2)
        center = series.rolling(window, min_periods=minimum).mean().to_numpy()
        scale = series.rolling(window, min_periods=minimum).std(ddof=0).to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            scores = np.where(scale > 0, np.abs(values - center) / scale, np.nan)
        return scores, scores > sigma, {"mode": mode, "sigma": float(sigma), "window": int(window)}
    if mode not in {"global", "group"}:
        raise ValueError(f"Unknown nsigma mode: {mode}")
    if mode == "group" and not grouped:
        raise ValueError("nsigma mode=group requires group_column")
    center = float(np.mean(values))
    scale = float(np.std(values, ddof=0))
    scores = np.abs(values - center) / scale if scale > 0 else np.zeros(len(values))
    return (
        scores,
        scores > sigma,
        {"mode": mode, "sigma": float(sigma), "center": center, "scale": scale},
    )


def _mean_drift(
    values: np.ndarray, reference_fraction: float, slack: float, decision: float
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Page 的 CUSUM 均值漂移检测：把"偏离目标超过 slack 倍尺度"的部分累积起来。

    目标是前 ``reference_fraction`` 段的均值，尺度是同一段的样本标准差。``slack`` 是容许的
    慢漂移（以尺度为单位），``decision`` 是累积量的告警线。累积量按尺度归一化，所以
    ``decision=5`` 的含义在任何量纲下都一样：大约连续 5 倍尺度的同向偏离才会报警。
    """
    boundary = int(len(values) * reference_fraction)
    if boundary < 3 or len(values) - boundary < 2:
        raise ValueError("Mean-drift detection needs a longer reference segment")
    reference = values[:boundary]
    target = float(np.mean(reference))
    scale = float(np.std(reference, ddof=1))
    if scale <= 0:
        raise ValueError("Mean-drift detection needs a non-constant reference segment")
    scores = np.full(len(values), np.nan, dtype=float)
    positive = negative = 0.0
    for position in range(boundary, len(values)):
        deviation = (values[position] - target) / scale - slack
        positive = max(0.0, positive + deviation)
        negative = min(0.0, negative + deviation)
        scores[position] = max(positive, -negative)
    flags = scores > decision
    alarms = np.flatnonzero(flags)
    return (
        scores,
        flags,
        {
            "slack": float(slack),
            "decision": float(decision),
            "target_mean": target,
            "reference_rows": int(boundary),
            "reference_scale": scale,
            # CUSUM 的警报是**锁存**的：越过告警线后会持续为真直到序列结束，因此更要紧的
            # 信息是"第一次报警发生在哪一行"，而不是报警了多少行。
            "first_alarm": None if not len(alarms) else int(alarms[0]),
        },
    )


@dataclass
class FittedChangeDetector:
    """规则型结构变化检测器的可复用包装：重新打分时用完全相同的参数与口径。

    它保存的不是"训练出来的东西"（这些方法几乎没有可训练参数），而是**参数指纹**：
    方法、列、窗口、阈值。这样 ``predict`` 在新数据上复现的判定口径，与当初报告里写的
    完全一致，不会因为默认值变化而悄悄换了一套规则。
    """

    method: str
    column: str
    parameters: dict[str, Any] = field(default_factory=dict)
    threshold: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def score_samples(self, data: pd.DataFrame) -> np.ndarray:
        """按原参数重新计算逐行分数。"""
        return self._run(data)["prediction"]["anomaly_score"].to_numpy(dtype=float)

    def predict(self, data: pd.DataFrame) -> np.ndarray:
        """按原参数重新给出布尔标记（口径与 metrics 里记录的一致）。"""
        return self._run(data)["prediction"]["is_anomaly"].to_numpy(dtype=bool)

    def _run(self, data: pd.DataFrame) -> dict[str, Any]:
        """用保存的参数重放一次检测。"""
        return detect(data, column=self.column, method=self.method, **self.parameters)
