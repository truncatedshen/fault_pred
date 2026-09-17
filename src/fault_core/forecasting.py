"""Exponential smoothing and (optional) ARIMA forecasting.

清单里的"指数平滑 / 三阶指数平滑"在本平台**手写实现**，不引入 statsmodels：Holt 与
Holt–Winters 的递推只有十几行，把公式写在这里比把整个 statsmodels 拖进部署更划算。
ARIMA/SARIMAX 则相反——它的极大似然估计与信息准则不是几十行能写对的，所以走**可选依赖**
（``[statsmodels]`` extra，与 XGBoost 的处理方式一致），未安装时给出可执行的安装提示。

所有方法共用同一套留出协议：前 ``1 - test_size`` 段拟合、后段做多步预测并算指标；
返回的模型再在**全量数据**上重拟合一次，因此它可以直接用于"往后预测 N 步"。
这个"评估用训练段、交付用全量"的两步是有意为之，``refit_on_full`` 会写在 metrics 里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from fault_core.data import numeric_columns

METHODS = ("holt", "holt_winters")


def _ordered(data: pd.DataFrame, time_column: str | None) -> pd.DataFrame:
    """按时间列稳定排序；没有时间列时按现有行序（调用方负责它是时间序）。"""
    if time_column and time_column not in data.columns:
        raise ValueError(f"Missing time column: {time_column}")
    return data.sort_values(time_column, kind="stable") if time_column else data


def _initial_seasonal(values: np.ndarray, periods: int, seasonal: str) -> np.ndarray:
    """季节分量的初值：第一个完整季节相对该季节均值的偏离。

    加性口径是"值 − 季节均值"，乘法口径是"值 / 季节均值"（围绕 1 波动）。两种口径的
    初值必须分开算：把加性的 ±2 拿去当乘法因子会让递推直接发散。

    只用一个季节初始化是有意保守的——用全部数据求季节均值会把"未来"的信息带进起点，
    在留出评估里等于提前看了答案。
    """
    first = values[:periods]
    mean = float(np.mean(first))
    if seasonal == "multiplicative":
        if mean <= 0 or (first <= 0).any():
            raise ValueError("Multiplicative seasonality needs a strictly positive first season")
        return first / mean
    return first - mean


def _initial_trend(values: np.ndarray, span: int) -> float:
    """趋势初值 = 前 ``span`` 个点的最小二乘斜率。

    不用 ``y[1] - y[0]``：那个值把"采样起点落在周期里的哪个相位"当成了趋势，季节幅度越大
    偏得越离谱，而且要很久才衰减掉。跨若干点做最小二乘是标准做法，也让"初始瞬态"
    在报告里可以忽略。
    """
    window = max(2, min(span, len(values)))
    steps = np.arange(window, dtype=float)
    return float(np.polyfit(steps, values[:window], 1)[0])


def _holt(values: np.ndarray, alpha: float, beta: float) -> tuple[float, float, list[float]]:
    """Holt 线性趋势递推，返回最终的 (水平, 趋势, 逐点拟合值)。"""
    level = float(values[0])
    trend = _initial_trend(values, max(4, min(len(values), len(values) // 5)))
    fitted = [level]
    for value in values[1:]:
        previous_level = level
        level = alpha * float(value) + (1 - alpha) * (level + trend)
        trend = beta * (level - previous_level) + (1 - beta) * trend
        fitted.append(level + trend)
    return level, trend, fitted


def _holt_winters(
    values: np.ndarray, alpha: float, beta: float, gamma: float, periods: int, seasonal: str
) -> tuple[float, float, np.ndarray, list[float]]:
    """Holt–Winters 三阶平滑（加性/乘法季节），返回 (水平, 趋势, 季节分量, 逐点拟合值)。"""
    seasonal_component = _initial_seasonal(values, periods, seasonal)
    level = float(np.mean(values[:periods]))
    # 初值用两个完整季节的最小二乘斜率：只用"后一季减前一季"会把相位差当趋势。
    trend = _initial_trend(values, 2 * periods)
    fitted: list[float] = []
    for index, value in enumerate(values):
        phase = index % periods
        season = seasonal_component[phase]
        if seasonal == "multiplicative":
            if value <= 0 or level <= 0:
                raise ValueError("Multiplicative seasonality needs strictly positive values")
            forecast = (level + trend) * season
            previous_level = level
            level = alpha * (value / season) + (1 - alpha) * (level + trend)
            trend = beta * (level - previous_level) + (1 - beta) * trend
            seasonal_component[phase] = gamma * (value / level) + (1 - gamma) * season
        else:
            forecast = level + trend + season
            previous_level = level
            level = alpha * (value - season) + (1 - alpha) * (level + trend)
            trend = beta * (level - previous_level) + (1 - beta) * trend
            seasonal_component[phase] = gamma * (value - level) + (1 - gamma) * season
        fitted.append(float(forecast))
    return level, trend, seasonal_component, fitted


@dataclass
class SmoothedForecaster:
    """训练好的指数平滑模型：保存最终水平/趋势/季节分量与全部超参数。"""

    column: str
    method: str
    alpha: float
    beta: float
    gamma: float
    periods: int
    seasonal: str
    level: float
    trend: float
    seasonal_component: np.ndarray = field(default_factory=lambda: np.zeros(0))
    last_phase: int = 0

    def predict(self, values: int | pd.DataFrame | pd.Series = 1) -> np.ndarray:
        """向后预测 ``steps`` 步：水平项按趋势线性外推，季节项按周期回绕。"""
        steps = int(values) if isinstance(values, (int, np.integer)) else len(values)
        if steps < 1:
            raise ValueError("Forecasting needs at least one step")
        horizon = np.arange(1, steps + 1, dtype=float)
        if self.method == "holt":
            return self.level + self.trend * horizon
        # 季节相位必须从"最后一行观测的相位"往后接，否则预测会错位一整个季节。
        phase = (self.last_phase + 1 + np.arange(steps)) % self.periods
        season = self.seasonal_component[phase]
        if self.seasonal == "multiplicative":
            return (self.level + self.trend * horizon) * season
        return self.level + self.trend * horizon + season


def _score(
    actual: np.ndarray, predicted: np.ndarray, algorithm: str, extras: dict[str, Any]
) -> dict[str, Any]:
    """把预测结果压成与其它验证器同一套指标字段。"""
    constant = bool(not np.ptp(actual)) if len(actual) else True
    return {
        "algorithm": algorithm,
        "r2": float(r2_score(actual, predicted)) if len(actual) >= 2 and not constant else None,
        "mae": float(mean_absolute_error(actual, predicted)),
        "rmse": float(np.sqrt(mean_squared_error(actual, predicted))),
        **extras,
        "refit_on_full": True,
    }


def exponential_smoothing(
    data: pd.DataFrame,
    column: str,
    method: str = "holt",
    alpha: float = 0.3,
    beta: float = 0.1,
    gamma: float = 0.1,
    seasonal_periods: int = 24,
    seasonal: str = "additive",
    test_size: float = 0.25,
    time_column: str | None = None,
    group_column: str | None = None,
) -> dict[str, Any]:
    """指数平滑预测：``holt``（水平 + 趋势）或 ``holt_winters``（再加季节）。

    平滑系数 ``alpha``（水平）/``beta``（趋势）/``gamma``（季节）**不是拟合出来的**，
    而是使用者给的：它们编码"多快跟上新观测"。本函数不替你自动选系数，因为那会让
    "留出指标"变成"调过参的指标"——要调参请自己在留出集之外做，再把选定的系数写进报告。

    留出协议：前 ``1 - test_size`` 段拟合，后段**从训练段末尾一路外推**（不是滚动一步预测），
    指标来自这段外推；返回的模型再在全量数据上重拟合，因此 ``predict(steps)`` 是从"现在"往后。
    ``group_column`` 只用于分组（逐组各自拟合与留出），**不做跨组平滑**。
    """
    if method not in METHODS:
        raise ValueError(f"Unknown smoothing method: {method}")
    if seasonal not in {"additive", "multiplicative"}:
        raise ValueError(f"Unknown seasonal type: {seasonal}")
    for name, value in (("alpha", alpha), ("beta", beta), ("gamma", gamma)):
        if not 0 < value < 1:
            raise ValueError(f"{name} must be between 0 and 1")
    if not 0 < test_size < 1:
        raise ValueError("test_size must be between 0 and 1")
    if method == "holt_winters" and seasonal_periods < 2:
        raise ValueError("holt_winters needs seasonal_periods >= 2")
    numeric_columns(data, [column])
    if group_column and group_column not in data.columns:
        raise ValueError(f"Missing group column: {group_column}")
    ordering = [name for name in (group_column, time_column) if name]
    ordered = data.sort_values(ordering, kind="stable") if ordering else data
    groups = (
        [(str(name), frame) for name, frame in ordered.groupby(group_column, sort=False, dropna=False)]
        if group_column
        else [(None, ordered)]
    )
    actual_all: list[float] = []
    predicted_all: list[float] = []
    index_all: list[Any] = []
    details: dict[str, Any] = {}
    for scope, frame in groups:
        values = frame[column].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError(f"Exponential smoothing requires finite values: {column}")
        minimum = 4 if method == "holt" else 3 * seasonal_periods
        if len(values) < minimum + 2:
            raise ValueError(f"{method} needs at least {minimum + 2} rows per group (got {len(values)})")
        boundary = int(len(values) * (1 - test_size))
        if boundary < minimum:
            raise ValueError("Training segment is too short for this method; lower test_size")
        if method == "holt":
            level, trend, _ = _holt(values[:boundary], alpha, beta)
            forecast = level + trend * np.arange(1, len(values) - boundary + 1)
        else:
            level, trend, season_component, _ = _holt_winters(
                values[:boundary], alpha, beta, gamma, seasonal_periods, seasonal
            )
            horizon = np.arange(1, len(values) - boundary + 1, dtype=float)
            season = season_component[(np.arange(len(horizon)) + boundary) % seasonal_periods]
            forecast = (
                (level + trend * horizon) * season
                if seasonal == "multiplicative"
                else level + trend * horizon + season
            )
        actual_all.extend(values[boundary:].tolist())
        predicted_all.extend(np.asarray(forecast, dtype=float).tolist())
        index_all.extend(frame.index[boundary:].tolist())
        details.setdefault("train_count", int(boundary))
        details.setdefault("seasonal_periods", int(seasonal_periods) if method == "holt_winters" else None)
    # 交付用的模型在全量数据上重拟合：这样 predict(steps) 从"现在"往后预测，
    # 而不是从训练段末尾往后预测（那是评估口径，不是预测口径）。
    full_values = (groups[0][1] if group_column else ordered)[column].to_numpy(dtype=float)
    if method == "holt":
        level, trend, _ = _holt(full_values, alpha, beta)
        model = SmoothedForecaster(column, method, alpha, beta, gamma, 1, seasonal, level, trend)
    else:
        level, trend, season_component, _ = _holt_winters(
            full_values, alpha, beta, gamma, seasonal_periods, seasonal
        )
        model = SmoothedForecaster(
            column,
            method,
            alpha,
            beta,
            gamma,
            seasonal_periods,
            seasonal,
            level,
            trend,
            season_component,
            int((len(full_values) - 1) % seasonal_periods),
        )
    details["final_level"] = level
    details["final_trend"] = trend
    actual = np.asarray(actual_all, dtype=float)
    predicted = np.asarray(predicted_all, dtype=float)
    prediction = pd.DataFrame({"actual": actual, "predicted": predicted}, index=pd.Index(index_all))
    warnings = [
        "Smoothing coefficients are inputs, not fitted values; the holdout score is only "
        "meaningful for the coefficients you actually passed.",
        "Model is refit on the full series after scoring; predict() starts from the end of the data.",
    ]
    if group_column:
        warnings.append(
            "Groups share the same coefficients but are fitted separately; report the per-group "
            "holdout sizes when comparing."
        )
    metrics = _score(
        actual,
        predicted,
        method,
        {
            "column": column,
            "alpha": alpha,
            "beta": beta,
            "gamma": gamma if method == "holt_winters" else None,
            "seasonal": seasonal if method == "holt_winters" else None,
            "test_count": int(len(actual)),
            "group_column": group_column,
            "train_indices": [],
            "test_indices": index_all,
            "warnings": warnings,
            **details,
        },
    )
    return {"model": model, "prediction": prediction, "metrics": metrics}


def arima_forecast(
    data: pd.DataFrame,
    column: str,
    order: list[int] | None = None,
    test_size: float = 0.25,
    trend: str = "c",
    time_column: str | None = None,
) -> dict[str, Any]:
    """ARIMA 预测（**需要可选依赖 statsmodels**）。

    与指数平滑相反，ARIMA 的系数由极大似然估计，所以这里的 ``order=[p,d,q]`` 是"给定阶数"
    而不是"给定平滑速度"。留出协议同上：训练段拟合 → 后段多步预测 → 指标；另附 AIC/BIC，
    但**比较不同阶数要在留出集上做**，AIC 只用于初筛。

    未安装 statsmodels 时直接报错并给出安装命令，而不是偷偷换一个模型——换模型比报错更糟。
    """
    try:
        from statsmodels.tsa.arima.model import ARIMA
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise ValueError(
            "ARIMA requires statsmodels: pip install 'fault-prediction-platform[statsmodels]'"
        ) from exc
    numeric_columns(data, [column])
    ordered = _ordered(data, time_column)
    values = ordered[column].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError(f"ARIMA requires finite values: {column}")
    chosen = [2, 1, 1] if not order else list(order)
    if len(chosen) != 3 or any(not isinstance(item, int) or item < 0 for item in chosen):
        raise ValueError("order must be three non-negative integers [p, d, q]")
    boundary = int(len(values) * (1 - test_size))
    if boundary < 8 or len(values) - boundary < 1:
        raise ValueError("ARIMA needs a longer training segment")
    fitted = ARIMA(values[:boundary], order=tuple(chosen), trend=trend).fit()
    forecast = np.asarray(fitted.forecast(steps=len(values) - boundary), dtype=float)
    actual = values[boundary:]
    final = ARIMA(values, order=tuple(chosen), trend=trend).fit()
    model = ARIMAForecaster(column, chosen, trend, final)
    prediction = pd.DataFrame({"actual": actual, "predicted": forecast}, index=ordered.index[boundary:])
    metrics = _score(
        actual,
        forecast,
        "arima",
        {
            "column": column,
            "order": chosen,
            "trend": trend,
            "aic": float(final.aic),
            "bic": float(final.bic),
            "train_count": int(boundary),
            "test_count": int(len(actual)),
            "train_indices": ordered.index[:boundary].tolist(),
            "test_indices": ordered.index[boundary:].tolist(),
            "warnings": [
                "Model is refit on the full series after scoring; the holdout score comes from "
                "the training-only fit.",
                "AIC/BIC describe in-sample fit; compare orders on the holdout, not on AIC alone.",
            ],
        },
    )
    return {"model": model, "prediction": prediction, "metrics": metrics}


@dataclass
class ARIMAForecaster:
    """statsmodels 结果的薄包装：只暴露 ``predict(steps)``，不把 statsmodels 类型泄漏出去。"""

    column: str
    order: list[int] = field(default_factory=lambda: [2, 1, 1])
    trend: str = "c"
    result: Any = None

    def predict(self, values: int | pd.DataFrame | pd.Series = 1) -> np.ndarray:
        """向后预测 ``steps`` 步。"""
        steps = int(values) if isinstance(values, (int, np.integer)) else len(values)
        if steps < 1:
            raise ValueError("Forecasting needs at least one step")
        return np.asarray(self.result.forecast(steps=steps), dtype=float)
