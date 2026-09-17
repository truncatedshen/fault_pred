"""Trend separation, stationarity, shape similarity and wavelet features.

批次 4 里**不需要新依赖**的那几件事，全部收在这个模块：

* :func:`hp_filter` —— Hodrick–Prescott 趋势分离（稀疏线性系统，scipy 直接解）；
* :func:`adf_test` —— ADF 平稳性检验（自建回归 + MacKinnon 渐近临界值）；
* :func:`dtw_distance` —— 动态时间规整距离（Sakoe–Chiba 带约束）；
* :func:`shape_based_distance` —— SBD（归一化互相关峰值）；
* :func:`slope_cosine` —— 斜率与余弦夹角（同向性分析）；
* :func:`seasonal_difference` —— 同期差分/同期比值（"同比"口径）；
* :func:`wavelet_features` —— 滚动 Haar 多尺度能量 + 主尺度 + 山峰数。

三条共同约定：

1. **不跨分组边界**：需要连续序列的函数都收 ``group_column``，逐组独立处理；
2. **不编造统计量**：算不出来的地方留空（NaN）或直接报错，不给"看起来很安全"的假值；
3. **口径写进返回值**：临界值、窗口、带宽、λ、峰值判据都随结果一起返回。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from scipy import sparse
from scipy.sparse.linalg import spsolve

from fault_core.data import numeric_columns

#: ADF 的**渐近**临界值（MacKinnon 1996 响应面在 n→∞ 处的常用取值）。
#: 只给这三档是刻意的：完整 p 值需要整套响应面，与其插值出一个没人能复核的数字，
#: 不如把统计量与三条临界线一起给出来，让使用者自己判断。n 很小时这些值偏保守。
ADF_CRITICAL_VALUES: dict[str, dict[str, float]] = {
    "c": {"1%": -3.43, "5%": -2.86, "10%": -2.57},
    "ct": {"1%": -3.96, "5%": -3.41, "10%": -3.12},
    "n": {"1%": -2.58, "5%": -1.95, "10%": -1.62},
}

#: HP 滤波的默认平滑参数：月度 1600、季度 1600 常被混用，这里按"越大的 λ 越平滑"说明，
#: 由使用者按采样周期自己选（本平台不假设数据是月度的）。
HP_DEFAULT_LAMBDA = 1600.0


def _ordered(data: pd.DataFrame, group_column: str | None, time_column: str | None) -> pd.DataFrame:
    """按「分组 → 时间」稳定排序；未给排序列时保持原行序。"""
    for column in (group_column, time_column):
        if column and column not in data.columns:
            raise ValueError(f"Missing ordering column: {column}")
    sort_columns = [column for column in (group_column, time_column) if column]
    return data.sort_values(sort_columns, kind="stable") if sort_columns else data


def _scopes(data: pd.DataFrame, group_column: str | None) -> list[tuple[str | None, pd.DataFrame]]:
    """把数据切成"一段一段独立处理"的连续序列；不分组时整列就是一段。"""
    if not group_column:
        return [(None, data)]
    return [(str(name), frame) for name, frame in data.groupby(group_column, sort=False, dropna=False)]


def _series(data: pd.DataFrame, column: str) -> np.ndarray:
    """取一列并校验有限性——序列分析里的 NaN 会让趋势解不出、临界值失真。"""
    numeric_columns(data, [column])
    values = data[column].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError(f"Series analysis requires finite values: {column}")
    return values


def _subsample(count: int, limit: int) -> np.ndarray:
    """等间隔抽样出至多 ``limit`` 个位置，把曲线压到前端可画、MCP 可返回的长度。"""
    if count <= limit:
        return np.arange(count)
    return np.unique(np.linspace(0, count - 1, limit).round().astype(int))


def _second_difference_penalty(count: int) -> sparse.csr_matrix:
    """二阶差分算子 K，使 ``sum((Δ²T)²) = ||K T||²``。"""
    rows = count - 2
    return sparse.diags(
        diagonals=[np.ones(rows), -2 * np.ones(rows), np.ones(rows)],
        offsets=[0, 1, 2],
        shape=(rows, count),
        format="csr",
    )


def hp_filter(
    data: pd.DataFrame,
    column: str,
    lamb: float = HP_DEFAULT_LAMBDA,
    group_column: str | None = None,
    time_column: str | None = None,
    max_points: int = 500,
    max_rows: int = 20000,
) -> dict[str, Any]:
    """Hodrick–Prescott 趋势分离：解 ``min Σ(y-T)² + λ Σ(Δ²T)²``。

    ``λ`` 越大趋势越平滑（越大越接近一条直线）。**没有"正确"的 λ**：它编码的是"多快的变化
    算波动、多慢的算趋势"，所以必须和使用者一起写进报告，不能沿用别人的 1600 而不说。
    实证一下这个说法：周期 24 的正弦，在 λ=1600 处 HP 的高通增益约 0.77——也就是约 1/4 的
    周期方差会被算进趋势里。想按"目标周期"推 λ 可以用 Ravn–Uhlig 的经验规则
    ``λ = 1600 · (周期 / 参考周期)⁴``，但那条规则本身也是约定，不是物理定律。

    ``cycle_share = var(周期项) / var(原序列)`` 是本函数真正有用的产出：接近 0 说明这条序列
    基本就是一条趋势（做差分/趋势特征即可），接近 1 说明趋势解释不了任何东西（该看周期与
    噪声，而不是外推趋势）。求解用稀疏矩阵，行数超过 ``max_rows`` 直接报错——HP 是稠密耦合
    的全局拟合，偷偷分块会改变结果。
    """
    if isinstance(lamb, bool) or not isinstance(lamb, (int, float)) or lamb <= 0:
        raise ValueError("lambda must be a positive number")
    ordered = _ordered(data, group_column, time_column)
    rows: list[dict[str, Any]] = []
    series: list[dict[str, Any]] = []
    for scope, frame in _scopes(ordered, group_column):
        values = _series(frame, column)
        if len(values) < 4:
            raise ValueError("HP filter needs at least four rows per group")
        if len(values) > max_rows:
            raise ValueError(
                f"HP filter solves a dense system and is limited to {max_rows} rows "
                f"(got {len(values)}); resample the series first"
            )
        penalty = _second_difference_penalty(len(values))
        system = sparse.identity(len(values), format="csr") + float(lamb) * (penalty.T @ penalty)
        trend = np.asarray(spsolve(system.tocsc(), values), dtype=float)
        cycle = values - trend
        variance = float(np.var(values, ddof=1))
        cycle_share = float(np.var(cycle, ddof=1) / variance) if variance else 0.0
        rows.append(
            {
                **({"group": scope} if scope is not None else {}),
                "column": column,
                "lambda": float(lamb),
                "count": int(len(values)),
                "trend_variance": float(np.var(trend, ddof=1)),
                "cycle_variance": float(np.var(cycle, ddof=1)),
                "cycle_share": cycle_share,
                "trend_slope": float(np.polyfit(np.arange(len(values)), trend, 1)[0]),
            }
        )
        selected = _subsample(len(values), max_points)
        series.append(
            {
                "name": column if scope is None else f"{scope}:{column}",
                "x": [int(position) for position in selected],
                "observed": values[selected].tolist(),
                "trend": trend[selected].tolist(),
                "cycle": cycle[selected].tolist(),
            }
        )
    return {
        "rows": rows,
        "series": series,
        "lambda": float(lamb),
        "group_column": group_column,
        "caveat": (
            "lambda is a modelling choice, not a fact; the same series with a different lambda "
            "gives a different trend. Report the value you used."
        ),
    }


def adf_test(
    data: pd.DataFrame,
    column: str,
    max_lag: int = 0,
    regression: str = "c",
    group_column: str | None = None,
    time_column: str | None = None,
) -> dict[str, Any]:
    """增广 Dickey–Fuller 单位根检验：序列是"平稳"还是"有单位根"。

    回归式 ``Δy_t = α + β·y_{t-1} + Σ γ_i·Δy_{t-i} + ε``，看 β 的 t 统计量：
    越负越支持"平稳"（拒绝单位根）。``max_lag=0`` 时用 Schwert 经验规则
    ``floor(12·(n/100)^0.25)`` 自动定滞后阶数；``regression`` 三选一：``c``（含常数）、
    ``ct``（含常数与线性趋势）、``n``（都不含）。

    **只给临界值、不给 p 值**：完整的 MacKinnon p 值需要一整套响应面，而这里用的是渐近
    临界值（见 :data:`ADF_CRITICAL_VALUES`）。统计量与三条临界线一起给出，判定按 5% 档，
    并在返回值里说明"n 很小时临界值偏保守"。这不是统计软件的替代品，是"先看一眼"的工具。
    """
    if regression not in ADF_CRITICAL_VALUES:
        raise ValueError(f"Unknown ADF regression: {regression}")
    ordered = _ordered(data, group_column, time_column)
    rows: list[dict[str, Any]] = []
    for scope, frame in _scopes(ordered, group_column):
        values = _series(frame, column)
        count = len(values)
        if count < 20:
            raise ValueError("ADF needs at least twenty rows per group")
        lag = int(max_lag) if max_lag else int(np.floor(12 * (count / 100) ** 0.25))
        lag = max(0, min(lag, (count - 4) // 2))
        differences = np.diff(values)
        # 行 t 需要 y_{t-1}、若干阶 Δy 的历史，因此可用行数从 1+lag 起。
        start = lag + 1
        target = differences[start:]
        columns = [values[start : count - 1]]  # β·y_{t-1} 项
        if regression in {"c", "ct"}:
            columns.append(np.ones(len(target)))
        if regression == "ct":
            columns.append(np.arange(len(target), dtype=float))
        for offset in range(lag):
            columns.append(differences[start - 1 - offset : count - 2 - offset])
        design = np.column_stack(columns)
        coefficients, _, _, _ = np.linalg.lstsq(design, target, rcond=None)
        residual = target - design @ coefficients
        dof = len(target) - design.shape[1]
        if dof <= 0:
            raise ValueError("ADF regression has no degrees of freedom; lower max_lag")
        covariance = float(residual @ residual) / dof * np.linalg.inv(design.T @ design)[0, 0]
        statistic = float(coefficients[0] / np.sqrt(covariance)) if covariance > 0 else float("nan")
        critical = ADF_CRITICAL_VALUES[regression]
        rows.append(
            {
                **({"group": scope} if scope is not None else {}),
                "column": column,
                "count": int(count),
                "lag": int(lag),
                "regression": regression,
                "statistic": statistic,
                "critical_1pct": critical["1%"],
                "critical_5pct": critical["5%"],
                "critical_10pct": critical["10%"],
                "stationary_at_5pct": bool(statistic < critical["5%"]),
            }
        )
    return {
        "rows": rows,
        "regression": regression,
        "group_column": group_column,
        "caveat": (
            "Critical values are asymptotic (MacKinnon 1996); they are conservative for short "
            "series. No p-value is reported on purpose — use the statistic against the three "
            "critical lines."
        ),
    }


def _z_normalize(values: np.ndarray) -> np.ndarray:
    """零均值单位方差；标准差为 0 时返回全 0（距离类指标在该序列上没有意义）。"""
    deviation = float(np.std(values))
    return (values - float(np.mean(values))) / deviation if deviation > 0 else np.zeros_like(values)


def dtw_distance(
    data: pd.DataFrame,
    first_column: str,
    second_column: str,
    band: int = 0,
    normalize: bool = True,
    max_rows: int = 20000,
) -> dict[str, Any]:
    """动态时间规整（DTW）距离：允许时间轴伸缩的形状相似度。

    代价矩阵用欧氏距离，``band > 0`` 时限制在 Sakoe–Chiba 带内（把 O(n²) 内存降到 O(n·band)，
    同时防止"一个点对应一大段"的病态对齐）。``normalize=True`` 先对两条序列各自做 z 标准化——
    这是"比形状而不是比幅值"的常用口径；``path_length`` 与 ``normalized_distance``
    一起给出，因为不同长度的对齐路径会让原始距离不可比（``normalized_distance`` 已除以路径长度）。

    DTW 距离**没有天然的阈值**：判断"像不像"要么和同批数据的分布比，要么用 SBD 那种自带
    归一化的指标。所以这里只给距离，不给"异常/正常"的结论。
    """
    numeric_columns(data, [first_column, second_column])
    if first_column == second_column:
        raise ValueError("DTW needs two different columns")
    pairs = data[[first_column, second_column]].to_numpy(dtype=float)
    if not np.isfinite(pairs).all():
        raise ValueError("DTW requires finite values in both columns")
    if len(pairs) > max_rows:
        raise ValueError(f"DTW is limited to {max_rows} rows; resample the series first")
    first, second = pairs[:, 0], pairs[:, 1]
    if normalize:
        first, second = _z_normalize(first), _z_normalize(second)
    rows_count, columns_count = len(first), len(second)
    radius = int(band) if band else max(rows_count, columns_count)
    cost = np.full((rows_count + 1, columns_count + 1), np.inf)
    cost[0, 0] = 0.0
    for i in range(1, rows_count + 1):
        low = max(1, i - radius)
        high = min(columns_count, i + radius)
        for j in range(low, high + 1):
            distance = abs(first[i - 1] - second[j - 1])
            cost[i, j] = distance + min(cost[i - 1, j], cost[i, j - 1], cost[i - 1, j - 1])
    total = float(cost[rows_count, columns_count])
    # 回溯数一下路径长度：只用于把"总代价"换算成"每步代价"，不做别的解释。
    i, j, steps = rows_count, columns_count, 0
    while i > 0 and j > 0:
        steps += 1
        previous = int(np.argmin([cost[i - 1, j - 1], cost[i - 1, j], cost[i, j - 1]]))
        i, j = (i - 1, j - 1) if previous == 0 else (i - 1, j) if previous == 1 else (i, j - 1)
    return {
        "first_column": first_column,
        "second_column": second_column,
        "normalized_input": bool(normalize),
        "band": int(band),
        "row_count": int(rows_count),
        "column_count": int(columns_count),
        "distance": total,
        "path_length": int(steps),
        "normalized_distance": total / steps if steps else float("nan"),
    }


def shape_based_distance(
    data: pd.DataFrame,
    first_column: str,
    second_column: str,
    max_lag_fraction: float = 0.5,
    normalize: bool = True,
) -> dict[str, Any]:
    """SBD（Shape-Based Distance）：``1 - max(NCC)``，取值范围 ``[0, 2]``，自带归一化。

    NCC 是两条 z 标准化序列在所有平移下的归一化互相关峰值（Paparrizos & Gravano 2015 的
    定义）。与 DTW 的分工：DTW 允许时间轴非线性伸缩、结果无界；SBD 只允许整体平移、结果
    有界且 0 就是"形状完全相同"，因此**可以直接跨样本对比较**，适合做通道相似度筛选。
    """
    numeric_columns(data, [first_column, second_column])
    if first_column == second_column:
        raise ValueError("SBD needs two different columns")
    if not 0 < max_lag_fraction <= 1:
        raise ValueError("max_lag_fraction must be in (0, 1]")
    pairs = data[[first_column, second_column]].to_numpy(dtype=float)
    if not np.isfinite(pairs).all():
        raise ValueError("SBD requires finite values in both columns")
    if len(pairs) < 4:
        raise ValueError("SBD needs at least four rows")
    first, second = pairs[:, 0], pairs[:, 1]
    if normalize:
        first, second = _z_normalize(first), _z_normalize(second)
    norm = float(np.linalg.norm(first) * np.linalg.norm(second))
    if norm <= 0:
        return {
            "first_column": first_column,
            "second_column": second_column,
            "normalized_input": bool(normalize),
            "distance": 1.0,
            "best_lag": 0,
            "note": "At least one series is constant, so shape similarity is undefined; 1.0 returned.",
        }
    limit = int(len(first) * max_lag_fraction)
    best_lag, best = 0, -1.0
    for lag in range(-limit, limit + 1):
        if lag < 0:
            left, right = first[-lag:], second[:lag]
        elif lag > 0:
            left, right = first[:-lag], second[lag:]
        else:
            left, right = first, second
        if len(left) < 2:
            continue
        value = float(np.dot(left, right) / norm)
        if value > best:
            best_lag, best = lag, value
    return {
        "first_column": first_column,
        "second_column": second_column,
        "normalized_input": bool(normalize),
        "max_lag": limit,
        "best_lag": int(best_lag),
        "best_ncc": float(best),
        "distance": float(1.0 - best),
    }


def slope_cosine(
    data: pd.DataFrame,
    first_column: str,
    second_column: str,
    window: int = 20,
    group_column: str | None = None,
    time_column: str | None = None,
    threshold: float = 0.5,
) -> pd.DataFrame:
    """两条序列的滚动斜率与"同向性余弦"：``cos = <Δx, Δy> / (‖Δx‖·‖Δy‖)``。

    窗口内取两条序列的增量向量，算它们的余弦：``+1`` 表示同向同比例变化（联动），
    ``0`` 表示无关，``-1`` 表示一升一降（反向联动）。**用增量而不是原值**是有意的：
    原值上的余弦会被各自的均值与量级支配，增量才回答"它们是否一起动"。

    输出逐行对齐（行数与输入相同），窗口不足处为 NaN；``window`` 内任一序列完全不变时
    余弦无定义，同样留空（除以 0 会得到 inf，那是噪声不是结论）。斜率的窗口长度是
    ``window``，余弦用的是同一窗口内的 ``window-1`` 个增量，因此两者在同一点上对应同一段数据。
    """
    numeric_columns(data, [first_column, second_column])
    if first_column == second_column:
        raise ValueError("Slope/cosine analysis needs two different columns")
    if isinstance(window, bool) or not isinstance(window, int) or window < 3:
        raise ValueError("window must be an integer >= 3")
    ordered = _ordered(data, group_column, time_column)
    frames = []
    for _, frame in _scopes(ordered, group_column):
        x = frame[first_column].to_numpy(dtype=float)
        y = frame[second_column].to_numpy(dtype=float)
        for name, values in ((first_column, x), (second_column, y)):
            if not np.isfinite(values).all():
                raise ValueError(f"Slope/cosine requires finite values: {name}")
        slope_x = _rolling_slope(x, window)
        slope_y = _rolling_slope(y, window)
        # 增量向量：窗口内 window-1 个 Δ，用滚动和一次性算出来（不写 Python 循环）。
        span = window - 1
        delta_x = np.diff(x)
        delta_y = np.diff(y)
        numerator = pd.Series(delta_x * delta_y).rolling(span, min_periods=span).sum().to_numpy()
        norm_x = np.sqrt(pd.Series(delta_x**2).rolling(span, min_periods=span).sum().to_numpy())
        norm_y = np.sqrt(pd.Series(delta_y**2).rolling(span, min_periods=span).sum().to_numpy())
        denominator = norm_x * norm_y
        with np.errstate(invalid="ignore", divide="ignore"):
            cosine = np.where(denominator > 0, numerator / denominator, np.nan)
        # 增量窗口的最后一行对应原序列的最后一个点；把它右移一位对齐到原索引。
        cosine = np.concatenate([[np.nan], cosine])
        frames.append(
            pd.DataFrame(
                {
                    f"{first_column}__slope": slope_x,
                    f"{second_column}__slope": slope_y,
                    "cosine": cosine,
                },
                index=frame.index,
            )
        )
    output = pd.concat(frames).reindex(data.index)
    output["opposite"] = output["cosine"] < -abs(threshold)
    output.attrs = {
        **data.attrs,
        "window": int(window),
        "threshold": float(threshold),
        "grouped": bool(group_column),
        "mean_cosine": float(output["cosine"].mean()) if output["cosine"].notna().any() else None,
        "opposite_rows": int(output["opposite"].sum()),
    }
    return output


def _rolling_slope(values: np.ndarray, window: int) -> np.ndarray:
    """滚动最小二乘斜率，用卷积一次算完（等价于逐窗口 ``polyfit(degree=1)``）。

    逐窗口调 ``polyfit`` 在 50 万行的表上要跑几十万次 Python 调用，因此这里用两点事实把它
    向量化：窗口内 ``k = 0..w-1`` 的平方和是常数，而 ``Σ k·x`` 正好是与线性核的一次卷积。
    前 ``window-1`` 行窗口不完整，返回 NaN（不补值、不外推）。
    """
    count = len(values)
    slopes = np.full(count, np.nan, dtype=float)
    if count < window:
        return slopes
    indices = np.arange(window, dtype=float)
    sum_k = indices.sum()
    sum_k2 = float((indices**2).sum())
    denominator = window * sum_k2 - sum_k**2
    weighted = np.convolve(indices[::-1], values, mode="valid")
    total = pd.Series(values).rolling(window, min_periods=window).sum().to_numpy()[window - 1 :]
    slopes[window - 1 :] = (window * weighted - sum_k * total) / denominator
    return slopes


def seasonal_difference(
    data: pd.DataFrame,
    columns: list[str],
    period: int = 24,
    mode: str = "difference",
    group_column: str | None = None,
    time_column: str | None = None,
    keep_original: bool = True,
    suffix: str = "",
    drop_missing: bool = False,
) -> pd.DataFrame:
    """同期差分/同期比值（"同比"口径）：``y_t - y_{t-period}`` 或 ``y_t / y_{t-period}``。

    这是把"季节性"从信号里去掉的标准做法：去掉之后剩余的部分才是"和上一个周期相比多出来/
    少掉了什么"。``mode=ratio`` 时要求 ``y_{t-period}`` 严格为正（比值在没有正基线的通道上
    没有意义，直接报错而不是给出 inf）。

    **每组的前 ``period`` 行没有可比对象**，因此是 NaN：``drop_missing=False``（默认）会把这些
    行原样留下并写一条警告，``True`` 则直接删行。别默认删行——删掉的是数据，不是噪声。
    """
    if mode not in {"difference", "ratio"}:
        raise ValueError(f"Unknown seasonal mode: {mode}")
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        raise ValueError("period must be an integer >= 1")
    cols = numeric_columns(data, columns)
    ordered = _ordered(data, group_column, time_column)
    label = "diff" if mode == "difference" else "ratio"
    result = ordered.copy() if keep_original else ordered.drop(columns=cols).copy()
    generated: list[str] = []
    for column in cols:
        values = ordered[column].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError(f"Seasonal differencing requires finite values: {column}")
        series = pd.Series(values, index=ordered.index)
        if group_column:
            shifted = series.groupby(ordered[group_column], sort=False, dropna=False).shift(period)
        else:
            shifted = series.shift(period)
        if mode == "difference":
            computed = series - shifted
        else:
            if (shifted.dropna() <= 0).any():
                raise ValueError(f"Seasonal ratio needs a strictly positive baseline: {column}")
            computed = series / shifted
        name = f"{column}{suffix}__seasonal_{label}_{period}"
        if name in result.columns:
            raise ValueError(f"Seasonal column already exists: {name}")
        result[name] = computed
        generated.append(name)
    if drop_missing:
        result = result.dropna(subset=generated)
    result.attrs = {
        **data.attrs,
        "seasonal_period": int(period),
        "seasonal_mode": mode,
        "grouped": bool(group_column),
    }
    if not drop_missing:
        result.attrs["evaluation_warnings"] = [
            *data.attrs.get("evaluation_warnings", []),
            f"Seasonal {label} leaves the first {period} row(s) of every group empty; "
            "filter them before modelling.",
        ]
    return result


def _haar_windows(windows: np.ndarray, levels: int) -> list[np.ndarray]:
    """对一批等长窗口做 Haar 分解，返回每层的细节系数矩阵（行 = 窗口）。

    逐层做 ``(a+b)/√2``（近似）与 ``(a-b)/√2``（细节），系数正交归一化，因此各层能量
    可以直接比较、也可以相加。窗口先按边缘值补到 2 的整数次幂——补零会在两端造出假的
    高频能量，那正好是这一层特征要区分的东西。
    """
    length = windows.shape[1]
    size = int(2 ** np.ceil(np.log2(length))) if length > 1 else 1
    padded = np.pad(windows, ((0, 0), (0, size - length)), mode="edge") if size > length else windows
    approximation = padded.astype(float, copy=True)
    details: list[np.ndarray] = []
    for _ in range(levels):
        if approximation.shape[1] < 2:
            break
        even = approximation[:, 0::2]
        odd = approximation[:, 1::2]
        width = min(even.shape[1], odd.shape[1])
        even, odd = even[:, :width], odd[:, :width]
        details.append((even - odd) / np.sqrt(2.0))
        approximation = (even + odd) / np.sqrt(2.0)
    return details


def _peak_counts(details: list[np.ndarray], peak_sigma: float) -> np.ndarray:
    """每层细节系数上的局部极大值个数（超过 ``peak_sigma`` 倍稳健尺度才算），按行给出。"""
    columns = []
    for detail in details:
        if detail.shape[1] < 3:
            columns.append(np.zeros(detail.shape[0]))
            continue
        magnitude = np.abs(detail)
        median = np.median(magnitude, axis=1, keepdims=True)
        scale = 1.4826 * np.median(np.abs(magnitude - median), axis=1, keepdims=True)
        center = magnitude[:, 1:-1]
        flagged = (
            (center > magnitude[:, :-2])
            & (center >= magnitude[:, 2:])
            & (center > peak_sigma * scale)
            & (scale > 0)
        )
        columns.append(flagged.sum(axis=1).astype(float))
    return np.column_stack(columns)


def wavelet_features(
    data: pd.DataFrame,
    columns: list[str],
    window: int = 32,
    levels: int = 3,
    peak_sigma: float = 3.0,
    group_column: str | None = None,
    time_column: str | None = None,
) -> pd.DataFrame:
    """逐行对齐的滚动 Haar 小波特征：各层能量占比、主尺度、细节峰个数。

    与 :mod:`fault_core.features` 的窗口特征不同，这里**行数与输入一致**（像
    ``feature.rolling_statistics``），适合作为补充分支挂在已有特征旁边。

    输出三组列：

    * ``<列>__wavelet_energy_l<k>``：第 k 层细节能量占总能量的比例，刻画"波动集中在哪个尺度"；
    * ``<列>__wavelet_dominant_level``：占比最大的层（1 = 最细，越大越慢）；
    * ``<列>__wavelet_peak_count``：主尺度细节系数上超过 ``peak_sigma`` 倍稳健尺度的局部极大值个数。
      清单里的"连续小波变换的山峰数"要的就是这个量，但这里是**离散 Haar 的近似**，
      不声称与 Morlet CWT 的数值一致——换小波基，峰个数会变，报告里要写清用的是哪一种。

    窗口不足 ``window`` 行时全部留空（NaN），不补值。
    """
    cols = numeric_columns(data, columns)
    if window < 4:
        raise ValueError("Wavelet window must be at least four samples")
    if levels < 1:
        raise ValueError("levels must be at least 1")
    max_levels = int(np.floor(np.log2(window)))
    if levels > max_levels:
        raise ValueError(f"levels must be <= floor(log2(window)) = {max_levels} for this window")
    ordered = _ordered(data, group_column, time_column)

    def one_group(frame: pd.DataFrame) -> pd.DataFrame:
        """对一段连续序列做全窗口向量化 Haar 分解，返回与 ``frame`` 同索引的特征表。"""
        count = len(frame)
        columns: dict[str, np.ndarray] = {}
        usable = max(0, count - window + 1)
        for column in cols:
            values = frame[column].to_numpy(dtype=float)
            if not np.isfinite(values).all():
                raise ValueError(f"Wavelet features require finite values: {column}")
            energies = np.full((count, levels), np.nan)
            dominant = np.full(count, np.nan)
            peaks = np.full(count, np.nan)
            if usable > 0:
                # 一次滑动取出所有窗口，避免在 Python 里逐行调用小波分解。
                windows = np.lib.stride_tricks.sliding_window_view(values, window)
                details = _haar_windows(windows, levels)
                stacked = np.zeros((usable, levels))
                for level, detail in enumerate(details):
                    stacked[:, level] = np.sum(detail**2, axis=1)
                total = stacked.sum(axis=1, keepdims=True)
                with np.errstate(invalid="ignore"):
                    shares = np.where(total > 0, stacked / total, np.nan)
                positions = np.arange(window - 1, count)
                energies[positions] = shares
                # 能量全为 0（窗口内是常数）时不给"主尺度"，那是个没有意义的编号。
                valid = np.isfinite(shares).all(axis=1)
                chosen = np.where(valid, np.argmax(np.nan_to_num(shares), axis=1) + 1, np.nan)
                dominant[positions] = chosen
                counts = _peak_counts(details, peak_sigma)
                picked = np.where(
                    valid, counts[np.arange(usable), np.nan_to_num(chosen, nan=1).astype(int) - 1], np.nan
                )
                peaks[positions] = picked
            for level in range(levels):
                columns[f"{column}__wavelet_energy_l{level + 1}"] = energies[:, level]
            columns[f"{column}__wavelet_dominant_level"] = dominant
            columns[f"{column}__wavelet_peak_count"] = peaks
        return pd.DataFrame(columns, index=frame.index)

    frames = [one_group(frame) for _, frame in _scopes(ordered, group_column)]
    output = pd.concat(frames).reindex(data.index)
    output.attrs = {
        **data.attrs,
        "window": int(window),
        "levels": int(levels),
        "peak_sigma": float(peak_sigma),
        "grouped": bool(group_column),
    }
    return output
