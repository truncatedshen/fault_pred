"""Regression, ARMA forecasting and unsupervised fault detectors.

三类"非分类器"的验证后端：

* ``validate_linear_regression``：在特征表上做回归，输出 R²/MAE/RMSE 与系数；
* ``validate_arma``：对单条时间序列做 ARMA 预测（后段留出，只前向预测）；
* ``fit_anomaly_detector`` / ``persistence_detection`` / ``fit_dbscan_detector`` /
  ``fit_pca_detector`` / ``fit_min_cluster_detector``：无监督检测，输出逐行分数与标记。
  这几个检测器的阈值来源互不相同（分位数、eps、簇距离），各自的 metrics 会把
  "这个阈值是怎么来的"写清楚，而不是让使用者以为它们可以互换。

与前两类不同，无监督检测没有"留出集"的概念：阈值在**全部输入**上拟合，
因此 metrics 里固定带一条警告，提醒使用者这只是描述性结果。
所有模型类都实现 ``predict``，会被统一包装成 ``Model`` 产物（见 fault_platform.workspace.summarize）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sklearn.cluster import DBSCAN, MiniBatchKMeans
from sklearn.decomposition import PCA
from sklearn.ensemble import IsolationForest
from sklearn.linear_model import LinearRegression, Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score, silhouette_score
from sklearn.model_selection import GroupShuffleSplit, train_test_split
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import StandardScaler
from sklearn.svm import OneClassSVM

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


def _regression_report(
    features: pd.DataFrame,
    target: pd.Series,
    estimator: Any,
    algorithm: str,
    split_method: str,
    test_size: float,
    random_state: int,
    extra_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """回归验证器的公共流程：对齐校验 → 切分 → 训练 → 留出指标 → 四件套。

    线性回归与岭回归只在估计器上不同，其余（重叠窗口复查、指标字段、系数表）必须完全一致，
    否则"换个模型再对比"这件事就失去意义，所以把流程收敛在这里。
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
    estimator.fit(features.iloc[train], y[train])
    predicted = estimator.predict(features.iloc[test])
    warnings = list(features.attrs.get("evaluation_warnings", []))
    metrics = {
        "algorithm": algorithm,
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
    if extra_metrics:
        metrics.update(extra_metrics)
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
    return _regression_report(
        features,
        target,
        LinearRegression(fit_intercept=fit_intercept, positive=positive),
        "linear_regression",
        split_method,
        test_size,
        random_state,
    )


def validate_ridge(
    features: pd.DataFrame,
    target: pd.Series,
    split_method: str = "random",
    test_size: float = 0.25,
    random_state: int = 42,
    alpha: float = 1.0,
    fit_intercept: bool = True,
) -> dict[str, Any]:
    """岭回归验证：与线性回归同一条流程，只是加了 L2 惩罚 ``alpha``。

    用途是"特征之间高度相关"的场合（窗口统计量几乎总是互相相关）：普通最小二乘此时系数
    会剧烈摆动甚至符号翻转，岭回归把系数压向 0 来换稳定性。代价是**系数不再是可解释的
    边际效应**，只能当"哪些特征被用到"的线索；``alpha`` 越大收缩越强，``alpha=0``
    等价于线性回归。指标字段与线性回归完全一致，方便用 ``validation.compare`` 并列。
    """
    return _regression_report(
        features,
        target,
        Ridge(alpha=alpha, fit_intercept=fit_intercept),
        "ridge",
        split_method,
        test_size,
        random_state,
        extra_metrics={"alpha": float(alpha)},
    )


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


@dataclass
class FittedUnsupervisedDetector:
    """DBSCAN / PCA / 簇距离三种检测器的统一形态。

    与 :class:`FittedAnomalyDetector` 一样约定"分数越大越异常"，但**阈值的来源不同**：
    DBSCAN 用 ``eps``（密度定义），PCA 与簇距离用 ``contamination`` 分位数（预算假设）。
    把来源写进 ``details``，是为了让报告能说清"这个异常率是被谁决定的"。
    """

    method: str
    columns: list[str]
    scaler: StandardScaler
    estimator: Any
    threshold: float
    details: dict[str, Any] = field(default_factory=dict)

    def _scaled(self, data: pd.DataFrame) -> np.ndarray:
        """按训练时的列与尺度取值；缺列立刻报错，绝不按位置猜列。"""
        missing = set(self.columns) - set(data.columns)
        if missing:
            raise ValueError(f"Detection data is missing columns: {sorted(missing)}")
        values = data[self.columns].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError("Detection data must be finite numeric values")
        return self.scaler.transform(values)

    def score_samples(self, data: pd.DataFrame) -> np.ndarray:
        """逐行异常分数；三种方法共用"越大越异常"的约定。"""
        values = self._scaled(data)
        if self.method == "dbscan":
            # 到最近核心样本的距离：大于 eps 就意味着它落在任何簇之外（噪声点）。
            return self.estimator.kneighbors(values, n_neighbors=1)[0].ravel()
        if self.method == "pca":
            # 重构误差：用前若干主成分把它压回去，压不回来的部分就是异常。
            reconstructed = self.estimator.inverse_transform(self.estimator.transform(values))
            return np.mean((values - reconstructed) ** 2, axis=1)
        if self.method == "min_cluster":
            # 到最近簇心的距离：离所有正常簇都远的点没有归属。
            return self.estimator.transform(values).min(axis=1)
        raise ValueError(f"Unknown unsupervised detector: {self.method}")

    def predict(self, data: pd.DataFrame) -> np.ndarray:
        """分数超过阈值即判异常（DBSCAN 下阈值就是 ``eps``）。"""
        return self.score_samples(data) > self.threshold


def _detector_inputs(
    data: pd.DataFrame, columns: list[str] | None, method: str
) -> tuple[list[str], StandardScaler, np.ndarray]:
    """三类检测器共用的输入校验与标准化：列存在、行数足够、取值有限。"""
    cols = numeric_columns(data, columns)
    values = data[cols].to_numpy(dtype=float)
    if len(data) < 5 or not np.isfinite(values).all():
        raise ValueError(f"{method} detection needs at least five finite rows")
    # 在 ndarray 上拟合：推理时同样传 ndarray，避免 sklearn 的"特征名不一致"警告。
    scaler = StandardScaler().fit(values)
    return cols, scaler, scaler.transform(values)


def _finish_detector(
    method: str,
    data: pd.DataFrame,
    cols: list[str],
    scaler: StandardScaler,
    estimator: Any,
    scores: np.ndarray,
    threshold: float,
    details: dict[str, Any],
) -> dict[str, Any]:
    """把分数、阈值与附加信息打包成与其他检测器一致的返回值。"""
    flags = scores > threshold
    model = FittedUnsupervisedDetector(method, cols, scaler, estimator, float(threshold), details)
    prediction = pd.DataFrame({"anomaly_score": scores, "is_anomaly": flags}, index=data.index)
    metrics = {
        "algorithm": method,
        "threshold": float(threshold),
        "anomaly_count": int(flags.sum()),
        "anomaly_rate": float(flags.mean()),
        "sample_count": len(data),
        **details,
        "warnings": [
            "Detector threshold was fitted on the complete input dataset.",
            *(
                ["Anomaly rate comes from eps/min_samples, the contamination parameter is not used."]
                if method == "dbscan"
                else []
            ),
        ],
    }
    return {"model": model, "prediction": prediction, "metrics": metrics}


def fit_dbscan_detector(
    data: pd.DataFrame,
    columns: list[str] | None = None,
    eps: float = 1.0,
    min_samples: int = 5,
    metric: str = "euclidean",
) -> dict[str, Any]:
    """DBSCAN 检测：把"落在任何簇之外"的点当作异常。

    **异常率由 ``eps`` 与 ``min_samples`` 决定，不来自 ``contamination``**——这与 KNN /
    隔离森林那套"给定污染率"的检测器是不同的假设：这里异常是数据里"密度不够"的客观结果，
    而不是一个预算。分数统一取"到最近核心样本的距离"，因此可以排序、可以画图。
    找不到任何核心样本会直接报错（数据整体太稀疏），而不是返回一堆看似正常的噪声标记。
    """
    cols, scaler, scaled = _detector_inputs(data, columns, "DBSCAN")
    estimator = DBSCAN(eps=eps, min_samples=min_samples, metric=metric).fit(scaled)
    core = estimator.components_
    if len(core) == 0:
        raise ValueError("DBSCAN found no core samples; increase eps or lower min_samples for this data")
    neighbors = NearestNeighbors(n_neighbors=1, metric=metric).fit(core)
    scores = neighbors.kneighbors(scaled, n_neighbors=1)[0].ravel()
    labels = estimator.labels_
    clusters = int(len(set(labels.tolist()) - {-1}))
    details = {
        "eps": float(eps),
        "min_samples": int(min_samples),
        "metric": metric,
        "cluster_count": clusters,
        "core_sample_count": int(len(core)),
        "noise_count": int(np.sum(labels == -1)),
    }
    # 存 NearestNeighbors 而不是 DBSCAN 本身：DBSCAN 没有 kneighbors，无法给新数据打分。
    return _finish_detector("dbscan", data, cols, scaler, neighbors, scores, float(eps), details)


def fit_pca_detector(
    data: pd.DataFrame,
    columns: list[str] | None = None,
    n_components: int = 0,
    contamination: float = 0.05,
    random_state: int = 42,
) -> dict[str, Any]:
    """PCA 检测：用前若干主成分重构每一行，**重构误差大**的行判为异常。

    ``n_components=0`` 表示自动取"能解释 95% 方差"的主成分数（``svd_solver="full"``，
    结果确定、不随机）；给正数则固定主成分数。``contamination`` 决定被判异常的比例，
    是**假设**而不是发现——报告里必须一起写出来。重构误差是"这条记录有多少信息无法由
    正常模式解释"，对多通道同时偏置这类故障比单变量阈值敏感。
    """
    if isinstance(n_components, bool) or not isinstance(n_components, int) or n_components < 0:
        raise ValueError("n_components must be an integer >= 0 (0 = keep 95% variance)")
    if not 0 < contamination < 1:
        raise ValueError("contamination must be between 0 and 1")
    cols, scaler, scaled = _detector_inputs(data, columns, "PCA")
    estimator = PCA(n_components=n_components or 0.95, svd_solver="full", random_state=random_state)
    estimator.fit(scaled)
    reconstructed = estimator.inverse_transform(estimator.transform(scaled))
    scores = np.mean((scaled - reconstructed) ** 2, axis=1)
    threshold = float(np.quantile(scores, 1 - contamination))
    details = {
        "contamination": float(contamination),
        "n_components": int(estimator.n_components_),
        "explained_variance_ratio_sum": float(np.sum(estimator.explained_variance_ratio_)),
        "input_columns": int(len(cols)),
    }
    return _finish_detector("pca", data, cols, scaler, estimator, scores, threshold, details)


def _fit_kmeans_model(
    scaled: np.ndarray,
    n_clusters: int,
    max_clusters: int,
    batch_size: int,
    random_state: int,
    silhouette_sample: int,
) -> tuple[Any, dict[str, Any]]:
    """拟合 MiniBatchKMeans；``n_clusters=0`` 时用轮廓系数自动选簇数。

    "自动给出合理聚类"的落点就是这里：在 ``2..max_clusters`` 上逐个试，挑轮廓系数最高的。
    轮廓系数只在 ``silhouette_sample`` 行子样本上算——它是 O(n²) 的，全量计算在大表上
    会直接卡死，而子样本足以比较不同 k 的高低。
    """
    best_k, best_score = int(n_clusters), None
    if n_clusters == 0:
        sample = scaled[:silhouette_sample]
        upper = min(max_clusters, max(2, int(np.sqrt(len(scaled)))))
        best_k, best_score = 2, -1.0
        for candidate in range(2, upper + 1):
            if candidate >= len(sample):
                break
            trial = MiniBatchKMeans(
                n_clusters=candidate, batch_size=batch_size, n_init=3, random_state=random_state
            ).fit(sample)
            if len(set(trial.labels_.tolist())) < 2:
                continue
            score = float(silhouette_score(sample, trial.labels_))
            if score > best_score:
                best_k, best_score = candidate, score
    if best_k >= len(scaled):
        raise ValueError("n_clusters must be smaller than the row count")
    estimator = MiniBatchKMeans(
        n_clusters=best_k, batch_size=batch_size, n_init=3, random_state=random_state
    ).fit(scaled)
    return estimator, {
        "n_clusters": int(best_k),
        "auto_selected": n_clusters == 0,
        "silhouette": None if best_score is None else float(best_score),
        "cluster_sizes": np.bincount(estimator.labels_, minlength=best_k).tolist(),
        "inertia": float(estimator.inertia_),
    }


def fit_min_cluster_detector(
    data: pd.DataFrame,
    columns: list[str] | None = None,
    n_clusters: int = 0,
    contamination: float = 0.05,
    max_clusters: int = 8,
    batch_size: int = 1024,
    random_state: int = 42,
    silhouette_sample: int = 5000,
) -> dict[str, Any]:
    """簇距离检测：先用 MiniBatchKMeans 找正常工况簇，再按"离最近簇心的距离"判异常。

    ``n_clusters=0`` 时**自动选簇数**：在 2..``max_clusters`` 内用轮廓系数挑选（轮廓系数
    最多在 ``silhouette_sample`` 行子样本上计算，避免大数据集上爆炸）。这条路径对应
    组件清单里的"自动给出合理聚类"。自动选出的簇数与轮廓系数都会写进 metrics——
    轮廓系数低（例如 < 0.2）说明数据本来就没有清晰簇结构，这时结论应当谨慎引用。

    与 PCA 检测器一样，``contamination`` 是异常预算假设，不是发现。
    """
    if isinstance(n_clusters, bool) or not isinstance(n_clusters, int) or n_clusters < 0:
        raise ValueError("n_clusters must be an integer >= 0 (0 = choose automatically)")
    if not 0 < contamination < 1:
        raise ValueError("contamination must be between 0 and 1")
    if max_clusters < 2:
        raise ValueError("max_clusters must be at least 2")
    cols, scaler, scaled = _detector_inputs(data, columns, "MinCluster")
    estimator, cluster_details = _fit_kmeans_model(
        scaled, n_clusters, max_clusters, batch_size, random_state, silhouette_sample
    )
    scores = estimator.transform(scaled).min(axis=1)
    threshold = float(np.quantile(scores, 1 - contamination))
    details = {"contamination": float(contamination), **cluster_details}
    return _finish_detector("min_cluster", data, cols, scaler, estimator, scores, threshold, details)


@dataclass
class FittedClusterModel:
    """训练好的聚类模型：列模式 + 标准化器 + 簇心，``predict`` 返回簇编号。

    单独一个类而不是复用检测器，是因为语义不同：检测器回答"这条记录异常吗"，
    聚类回答"这条记录属于哪一个工况簇"。塞进同一个返回结构会让人误以为"簇编号"就是
    "异常等级"。
    """

    columns: list[str]
    scaler: StandardScaler
    estimator: Any

    def _scaled(self, data: pd.DataFrame) -> np.ndarray:
        """按训练时的列与尺度取值；缺列立刻报错。"""
        missing = set(self.columns) - set(data.columns)
        if missing:
            raise ValueError(f"Clustering data is missing columns: {sorted(missing)}")
        values = data[self.columns].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError("Clustering data must be finite numeric values")
        return self.scaler.transform(values)

    def predict(self, data: pd.DataFrame) -> np.ndarray:
        """返回每一行所属的簇编号（0 起）。"""
        return np.asarray(self.estimator.predict(self._scaled(data)), dtype=int)

    def transform(self, data: pd.DataFrame) -> np.ndarray:
        """返回到每个簇心的距离矩阵（行 = 样本，列 = 簇）。"""
        return np.asarray(self.estimator.transform(self._scaled(data)), dtype=float)


def fit_kmeans(
    data: pd.DataFrame,
    columns: list[str] | None = None,
    n_clusters: int = 0,
    max_clusters: int = 8,
    batch_size: int = 1024,
    random_state: int = 42,
    silhouette_sample: int = 5000,
) -> dict[str, Any]:
    """KMeans 聚类：把记录分到若干工况簇，输出簇编号与到最近簇心的距离。

    ``n_clusters=0`` 用轮廓系数在 ``2..max_clusters`` 内自动选簇数。**这不是异常检测**：
    它不给"正常/异常"的判决、不做阈值——什么算异常要你自己在簇与距离上定义。
    返回的 ``silhouette`` 是"簇结构是否真实存在"的证据：低于约 0.2 时簇边界基本是硬切的，
    报告里不能只说"分成了 3 类"。
    """
    if isinstance(n_clusters, bool) or not isinstance(n_clusters, int) or n_clusters < 0:
        raise ValueError("n_clusters must be an integer >= 0 (0 = choose automatically)")
    if max_clusters < 2:
        raise ValueError("max_clusters must be at least 2")
    cols, scaler, scaled = _detector_inputs(data, columns, "KMeans")
    estimator, details = _fit_kmeans_model(
        scaled, n_clusters, max_clusters, batch_size, random_state, silhouette_sample
    )
    labels = estimator.predict(scaled)
    distances = estimator.transform(scaled).min(axis=1)
    model = FittedClusterModel(cols, scaler, estimator)
    prediction = pd.DataFrame(
        {"cluster": labels.astype(int), "distance_to_centre": distances}, index=data.index
    )
    metrics = {
        "algorithm": "kmeans",
        "sample_count": len(data),
        **details,
        "warnings": [
            "Cluster ids are labels, not severity: this is a descriptive grouping on the "
            "complete input, not a validated anomaly detector.",
        ],
    }
    return {"model": model, "prediction": prediction, "metrics": metrics}


def fit_one_class_svm(
    data: pd.DataFrame,
    columns: list[str] | None = None,
    nu: float = 0.05,
    kernel: str = "rbf",
    gamma: str = "scale",
) -> dict[str, Any]:
    """单类 SVM（novelty detection）：只学"正常长什么样"，边界之外判为异常。

    与二分类 SVM 的区别是**不需要标签**，适合"故障样本几乎没有、只有正常运行数据"的早期
    场景。``nu`` 是训练时落在边界外的样本比例**上界**（也是支持向量比例的下界），
    实际异常比例通常低于它——两个数都会写进 metrics，不能把 ``nu`` 当异常率。

    标准化在完整输入上拟合（属于描述性筛查），因此带探索性警告；核宽 ``gamma`` 对结果
    影响很大，换一份数据就该重新看一次分数分布。
    """
    if not 0 < nu <= 1:
        raise ValueError("nu must be in (0, 1]")
    if kernel not in {"rbf", "linear", "poly", "sigmoid"}:
        raise ValueError(f"Unknown kernel: {kernel}")
    cols, scaler, scaled = _detector_inputs(data, columns, "One-class SVM")
    estimator = OneClassSVM(nu=nu, kernel=kernel, gamma=gamma).fit(scaled)
    decision = np.asarray(estimator.decision_function(scaled), dtype=float)
    # 统一口径"越大越异常"：决策函数是"越大越正常"，取负号即可，阈值恰好是 0。
    scores = -decision
    flags = decision < 0
    model = FittedUnsupervisedDetector("one_class_svm", cols, scaler, estimator, 0.0, {"nu": float(nu)})
    prediction = pd.DataFrame({"anomaly_score": scores, "is_anomaly": flags}, index=data.index)
    metrics = {
        "algorithm": "one_class_svm",
        "nu": float(nu),
        "kernel": kernel,
        "gamma": gamma,
        "threshold": 0.0,
        "support_vector_count": int(len(estimator.support_)),
        "support_vector_share": float(len(estimator.support_) / len(data)),
        "anomaly_count": int(flags.sum()),
        "anomaly_rate": float(flags.mean()),
        "sample_count": len(data),
        "warnings": [
            "Fit on the complete input: this is a descriptive screen, not a validated detector.",
            "nu bounds the training outlier fraction; the observed rate can be lower and is not "
            "an estimate of the real fault rate.",
        ],
    }
    return {"model": model, "prediction": prediction, "metrics": metrics}
