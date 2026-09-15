"""Regression, ARMA forecasting and unsupervised fault detectors.

三类"非分类器"的验证后端：

* ``validate_linear_regression``：在特征表上做回归，输出 R²/MAE/RMSE 与系数；
* ``validate_arma``：对单条时间序列做 ARMA 预测（后段留出，只前向预测）；
* ``fit_anomaly_detector`` / ``persistence_detection``：无监督检测，输出逐行分数与标记。

与前两类不同，无监督检测没有"留出集"的概念：阈值在**全部输入**上拟合，
因此 metrics 里固定带一条警告，提醒使用者这只是描述性结果。
所有模型类都实现 ``predict``，会被统一包装成 ``Model`` 产物（见 fault_platform.workspace.summarize）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import GroupShuffleSplit, train_test_split
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import StandardScaler

from fault_core.data import numeric_columns
from fault_core.features import coverage_subset, rows_without_overlap, windows_share_rows


def _aligned_numeric(features: pd.DataFrame, labels: pd.Series) -> np.ndarray:
    """校验特征与目标严格对齐，并返回目标的 float 数组。

    检查项：索引唯一且完全一致（否则按位置取数会错位）、样本量与特征数下限、
    目标列不能同时出现在特征里（信息泄漏），以及 ``source_rows``/``source_path`` 等
    provenance 字段一致（保证两者来自同一批窗口）。
    """
    if not features.index.is_unique or not labels.index.is_unique or not features.index.equals(labels.index):
        raise ValueError("Feature and target indices must be unique and exactly aligned")
    if len(features) < 5 or features.shape[1] == 0:
        raise ValueError("Regression needs at least five samples and one feature")
    if labels.name in features.columns:
        raise ValueError("The target column cannot also be a regression feature")
    for key in ("source_rows", "source_rows_ranges", "source_path", "source_id"):
        if features.attrs.get(key) != labels.attrs.get(key):
            raise ValueError(f"Feature and target provenance differs: {key}")
    values = features.to_numpy(dtype=float, copy=False)
    target = pd.to_numeric(labels, errors="raise").to_numpy(dtype=float)
    if not np.isfinite(values).all() or not np.isfinite(target).all():
        raise ValueError("Regression features and target must be finite numeric values")
    return target


def _split(
    features: pd.DataFrame, method: str, test_size: float, random_state: int
) -> tuple[np.ndarray, np.ndarray]:
    """回归任务的训练/测试位置索引切分。

    ``temporal`` 按行序切尾段，并用 :func:`rows_without_overlap` 把与测试窗口共享原始行的
    训练窗口剔除（purge），避免重叠窗口把答案漏给训练集；``group`` 按分组列整组划分；
    ``random`` 在存在重叠窗口时直接拒绝。
    """
    positions = np.arange(len(features))
    if method == "temporal":
        boundary = int(len(features) * (1 - test_size))
        train, test = positions[:boundary], positions[boundary:]
        if "source_rows_ranges" in features.attrs or features.attrs.get("source_rows"):
            train = np.asarray(rows_without_overlap(features.attrs, list(test), list(train)), dtype=int)
        return train, test
    if method == "group":
        groups = features.attrs.get("groups")
        if not features.attrs.get("grouped") or groups is None or len(groups) != len(features):
            raise ValueError("Group split requires grouped feature extraction")
        return next(
            GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=random_state).split(
                features, groups=groups
            )
        )
    if method == "random":
        if features.attrs.get("overlapping"):
            raise ValueError("Overlapping windows require group or temporal split")
        return train_test_split(positions, test_size=test_size, random_state=random_state)
    raise ValueError(f"Unknown regression split method: {method}")


@dataclass
class TrainedRegressor:
    """训练好的回归模型：估计器 + 训练时用到的列名。

    保留列名是为了在推理时校验输入表（缺列、顺序不同都能立刻发现），
    并让 summarize() 能展示"这个模型吃哪些特征"。
    """

    estimator: Any
    columns: list[str]

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        """按固定列顺序做预测，缺列或含非有限值时抛错。"""
        missing = set(self.columns) - set(features.columns)
        if missing:
            raise ValueError(f"Prediction data is missing columns: {sorted(missing)}")
        values = features.loc[:, self.columns]
        if not np.isfinite(values.to_numpy(dtype=float, copy=False)).all():
            raise ValueError("Prediction features must be finite numeric values")
        return np.asarray(self.estimator.predict(values), dtype=float)


def validate_linear_regression(
    features: pd.DataFrame,
    target: pd.Series,
    split_method: str = "random",
    test_size: float = 0.25,
    random_state: int = 42,
    fit_intercept: bool = True,
    positive: bool = False,
) -> dict[str, Any]:
    """线性回归验证：返回 model / prediction / metrics / importance 四件套。

    ``positive=True`` 约束系数非负（适合"特征越大故障越严重"的先验）。
    切分后还会复查训练与测试窗口是否共享原始行，共享即报错——这是重叠窗口下最常见的泄漏来源。
    """
    y = _aligned_numeric(features, target)
    train, test = _split(features, split_method, test_size, random_state)
    if len(train) < 2 or len(test) < 1:
        raise ValueError("Regression split leaves too few train or test rows")
    if "source_rows_ranges" in features.attrs or features.attrs.get("source_rows"):
        if windows_share_rows(
            coverage_subset(features.attrs, list(train)), coverage_subset(features.attrs, list(test))
        ):
            raise ValueError("Train/test regression windows share source rows")
    estimator = LinearRegression(fit_intercept=fit_intercept, positive=positive)
    estimator.fit(features.iloc[train], y[train])
    predicted = estimator.predict(features.iloc[test])
    warnings = list(features.attrs.get("evaluation_warnings", []))
    metrics = {
        "algorithm": "linear_regression",
        "r2": float(r2_score(y[test], predicted)) if len(test) >= 2 else None,
        "mae": float(mean_absolute_error(y[test], predicted)),
        "rmse": float(np.sqrt(mean_squared_error(y[test], predicted))),
        "split_method": split_method,
        "train_count": len(train),
        "test_count": len(test),
        "random_state": random_state,
        "train_indices": features.index[train].tolist(),
        "test_indices": features.index[test].tolist(),
        "warnings": warnings,
    }
    prediction = pd.DataFrame({"actual": y[test], "predicted": predicted}, index=features.index[test])
    importance = pd.DataFrame(
        {"feature": features.columns, "coefficient": np.asarray(estimator.coef_).reshape(-1)}
    )
    return {
        "model": TrainedRegressor(estimator, list(features.columns)),
        "prediction": prediction,
        "metrics": metrics,
        "importance": importance,
    }


@dataclass
class ARMAModel:
    """ARMA(p, q) 模型：截距、自回归系数、滑动平均系数，以及预测所需的历史。

    ``history`` 是训练段的观测序列，``residual_history`` 是同一段的残差；
    预测时把它们当作已知的过去，逐步滚动生成多步点预测。
    """

    intercept: float
    ar_parameters: np.ndarray
    ma_parameters: np.ndarray
    history: list[float]
    residual_history: list[float]

    def predict(self, values: int | pd.DataFrame | pd.Series = 1) -> np.ndarray:
        """预测未来 ``steps`` 步（参数传 DataFrame/Series 时按行数决定步数）。

        每步都用"截距 + AR 项 × 历史值 + MA 项 × 历史残差"计算，然后把预测值追加进历史、
        把该步残差记为 0（未来误差不可知，这是 ARMA 点预测的标准做法）。
        """
        steps = values if isinstance(values, int) else len(values)
        if steps < 1:
            raise ValueError("ARMA prediction needs at least one step")
        history = list(self.history)
        errors = list(self.residual_history)
        forecasts = []
        for _ in range(steps):
            forecast = self.intercept
            forecast += sum(value * history[-lag] for lag, value in enumerate(self.ar_parameters, 1))
            forecast += sum(value * errors[-lag] for lag, value in enumerate(self.ma_parameters, 1))
            forecasts.append(float(forecast))
            history.append(float(forecast))
            errors.append(0.0)
        return np.asarray(forecasts)


def _fit_arma(values: np.ndarray, p: int, q: int, iterations: int) -> tuple[np.ndarray, np.ndarray]:
    """用迭代最小二乘估计 ARMA 参数（条件平方和法的简化实现）。

    第 1 轮假设残差全为 0 得到初值，之后每轮用上一轮残差重建设计矩阵再求解；
    ``iterations`` 轮后返回最终系数与残差序列。相比 statsmodels 的实现，
    这里不提供统计显著性，只给出可用且可复现的点估计。
    """
    lag = max(p, q)
    residuals = np.zeros(len(values), dtype=float)
    beta = np.zeros(1 + p + q, dtype=float)
    for _ in range(iterations):
        rows, target = [], []
        # 设计矩阵每行 = [1, 过去 p 个观测, 过去 q 个残差]，目标 = 当前观测。
        for index in range(lag, len(values)):
            rows.append(
                [
                    1.0,
                    *[values[index - offset] for offset in range(1, p + 1)],
                    *[residuals[index - offset] for offset in range(1, q + 1)],
                ]
            )
            target.append(values[index])
        beta = np.linalg.lstsq(np.asarray(rows), np.asarray(target), rcond=None)[0]
        # 用新系数重算全部残差，供下一轮（以及最终返回值）使用。
        residuals[:] = 0.0
        design = np.asarray(rows)
        residuals[lag:] = np.asarray(target) - design @ beta
    return beta, residuals


def validate_arma(
    data: pd.DataFrame,
    column: str,
    p: int = 2,
    q: int = 1,
    test_size: float = 0.25,
    iterations: int = 5,
    time_column: str | None = None,
) -> dict[str, Any]:
    """ARMA 验证：前段训练、后段预测，指标与分类验证保持同一套字段风格。

    输入是原始序列（不是窗口特征），需要 ``boundary > max(p, q) + 2`` 才能估计参数，
    否则直接报错而不是给出退化的结果。``time_column`` 只用于排序，保证"后段"就是时间上的后段。
    """
    numeric_columns(data, [column])
    if time_column:
        if time_column not in data.columns:
            raise ValueError(f"Missing time column: {time_column}")
        ordered = data.sort_values(time_column, kind="stable")
    else:
        ordered = data
    values = ordered[column].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("ARMA requires finite values")
    boundary = int(len(values) * (1 - test_size))
    lag = max(p, q)
    if boundary <= lag + 2 or len(values) - boundary < 1:
        raise ValueError("ARMA needs more training rows than its maximum lag")
    beta, residuals = _fit_arma(values[:boundary], p, q, iterations)
    model = ARMAModel(
        float(beta[0]),
        np.asarray(beta[1 : 1 + p]),
        np.asarray(beta[1 + p :]),
        values[:boundary].tolist(),
        residuals.tolist(),
    )
    predicted = model.predict(len(values) - boundary)
    actual = values[boundary:]
    prediction = pd.DataFrame({"actual": actual, "predicted": predicted}, index=ordered.index[boundary:])
    metrics = {
        "algorithm": "arma",
        "order": [p, q],
        "r2": float(r2_score(actual, predicted)) if len(actual) >= 2 else None,
        "mae": float(mean_absolute_error(actual, predicted)),
        "rmse": float(np.sqrt(mean_squared_error(actual, predicted))),
        "train_count": boundary,
        "test_count": len(actual),
        "train_indices": ordered.index[:boundary].tolist(),
        "test_indices": ordered.index[boundary:].tolist(),
        "warnings": [],
    }
    return {"model": model, "prediction": prediction, "metrics": metrics}


@dataclass
class FittedAnomalyDetector:
    """训练好的无监督检测器：标准化器 + 估计器 + 阈值。

    ``score_samples`` 统一成"分数越大越异常"：隔离森林的 ``score_samples`` 本身是
    "越大越正常"，因此取负；KNN 用 k 近邻平均距离，天然是越大越异常。
    """

    method: str
    columns: list[str]
    scaler: StandardScaler
    estimator: Any
    threshold: float
    neighbors: int = 5

    def score_samples(self, data: pd.DataFrame) -> np.ndarray:
        """按训练时的列与尺度计算逐行异常分数。"""
        missing = set(self.columns) - set(data.columns)
        if missing:
            raise ValueError(f"Detection data is missing columns: {sorted(missing)}")
        values = self.scaler.transform(data[self.columns])
        if self.method == "isolation_forest":
            return -np.asarray(self.estimator.score_samples(values), dtype=float)
        distances = self.estimator.kneighbors(values, n_neighbors=self.neighbors)[0]
        return distances.mean(axis=1)

    def predict(self, data: pd.DataFrame) -> np.ndarray:
        """分数超过阈值即判为异常，返回布尔数组。"""
        return self.score_samples(data) >= self.threshold


def fit_anomaly_detector(
    data: pd.DataFrame,
    columns: list[str] | None = None,
    method: str = "knn",
    contamination: float = 0.05,
    neighbors: int = 5,
    random_state: int = 42,
    n_estimators: int = 100,
) -> dict[str, Any]:
    """拟合 KNN 或隔离森林检测器，并返回逐行分数、标记与统计。

    先做标准化（KNN 依赖距离，量纲不同会直接改变结果），再按 ``contamination``
    取分数分位数作为阈值——也就是说 **contamination 是假设而不是发现**：
    它决定"有多少比例被判异常"，报告时必须说明这一点。

    metrics 里固定附上"阈值在完整输入上拟合"的警告，提醒这不是留出评估。
    """
    cols = numeric_columns(data, columns)
    values = data[cols].to_numpy(dtype=float)
    if len(data) < 5 or not np.isfinite(values).all():
        raise ValueError("Anomaly detection needs at least five finite rows")
    scaler = StandardScaler().fit(data[cols])
    scaled = scaler.transform(data[cols])
    if method == "knn":
        if neighbors >= len(data):
            raise ValueError("neighbors must be smaller than the row count")
        estimator = NearestNeighbors(n_neighbors=neighbors).fit(scaled)
        scores = estimator.kneighbors(scaled, n_neighbors=neighbors)[0].mean(axis=1)
        model_neighbors = neighbors
    elif method == "isolation_forest":
        estimator = IsolationForest(
            n_estimators=n_estimators,
            contamination=contamination,
            random_state=random_state,
            n_jobs=1,
        ).fit(scaled)
        # 取负：把 sklearn 的"越大越正常"翻成统一的"越大越异常"。
        scores = -estimator.score_samples(scaled)
        model_neighbors = neighbors
    else:
        raise ValueError(f"Unknown anomaly detector: {method}")
    # 取 (1 - contamination) 分位数作阈值，使异常比例约等于给定污染率。
    threshold = float(np.quantile(scores, 1 - contamination))
    flags = scores >= threshold
    model = FittedAnomalyDetector(method, cols, scaler, estimator, threshold, model_neighbors)
    prediction = pd.DataFrame({"anomaly_score": scores, "is_anomaly": flags}, index=data.index)
    metrics = {
        "algorithm": method,
        "threshold": threshold,
        "contamination": contamination,
        "anomaly_count": int(flags.sum()),
        "anomaly_rate": float(flags.mean()),
        "sample_count": len(data),
        "warnings": ["Detector threshold was fitted on the complete input dataset."],
    }
    return {"model": model, "prediction": prediction, "metrics": metrics}


@dataclass
class PersistenceDetector:
    """规则型"持续越限"检测器：连续 ``min_consecutive`` 个点越限才算异常。

    这条规则用来抓卡死/保持值这类故障——单点尖峰不算，持续偏离才算。
    阈值应当来自工艺知识，而不是从数据里拟合出来。
    """

    column: str
    threshold: float
    direction: str
    min_consecutive: int
    group_column: str | None = None

    def _breach(self, values: pd.Series) -> pd.Series:
        """判断每个点是否越限：``above``/``below`` 看方向，``absolute`` 看绝对值。"""
        if self.direction == "above":
            return values > self.threshold
        if self.direction == "below":
            return values < self.threshold
        if self.direction == "absolute":
            return values.abs() > self.threshold
        raise ValueError(f"Unknown persistence direction: {self.direction}")

    def predict(self, data: pd.DataFrame) -> np.ndarray:
        """按组（可选）判定持续越限，返回布尔数组。

        实现方式是对越限指示做长度 ``min_consecutive`` 的滚动求和，
        求和等于窗口长度即说明这一段**全部**越限。开头不足窗口长度的位置补 False。
        """
        if self.column not in data.columns:
            raise ValueError(f"Missing persistence column: {self.column}")
        breach = self._breach(data[self.column])
        if self.group_column:
            if self.group_column not in data.columns:
                raise ValueError(f"Missing group column: {self.group_column}")
            flags = breach.groupby(data[self.group_column], sort=False, dropna=False).transform(
                lambda values: (
                    values.rolling(self.min_consecutive, min_periods=self.min_consecutive).sum()
                    >= self.min_consecutive
                )
            )
        else:
            flags = (
                breach.rolling(self.min_consecutive, min_periods=self.min_consecutive).sum()
                >= self.min_consecutive
            )
        return flags.fillna(False).to_numpy(dtype=bool)


def persistence_detection(
    data: pd.DataFrame,
    column: str,
    threshold: float,
    direction: str = "above",
    min_consecutive: int = 3,
    group_column: str | None = None,
) -> dict[str, Any]:
    """跑一次持续越限检测，输出 ``value`` / ``persistence_score`` / ``is_anomaly`` 三列。

    ``persistence_score`` 是"超出阈值多少"（方向相关的有符号量），
    便于在报告里说明越限程度，而不只是一个布尔标记。
    """
    numeric_columns(data, [column])
    if not np.isfinite(data[column].to_numpy(dtype=float)).all():
        raise ValueError("Persistence detection requires finite values")
    model = PersistenceDetector(column, threshold, direction, min_consecutive, group_column)
    flags = model.predict(data)
    values = data[column].to_numpy(dtype=float)
    if direction == "above":
        scores = values - threshold
    elif direction == "below":
        scores = threshold - values
    else:
        scores = np.abs(values) - threshold
    prediction = pd.DataFrame(
        {"value": values, "persistence_score": scores, "is_anomaly": flags}, index=data.index
    )
    metrics = {
        "algorithm": "persistence_detector",
        "threshold": threshold,
        "direction": direction,
        "min_consecutive": min_consecutive,
        "anomaly_count": int(flags.sum()),
        "anomaly_rate": float(flags.mean()),
        "sample_count": len(data),
        "warnings": [],
    }
    return {"model": model, "prediction": prediction, "metrics": metrics}
