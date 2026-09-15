"""Feature scoring and selection that preserves row and window provenance.

四种打分方式：``variance``（无监督，方差越小越没用）、``correlation``（无监督，保留相关
成对特征中方差更大的那个）、``mutual_information`` 与 ``model``（需要标签，在有监督的
全部行上拟合）。后两者会通过 :func:`fault_core.preprocessing.mark_fitted` 标记为探索性，
因为它们的排序"看过"后面用于验证的行，不能当作已验证的证据。

所有分支都只做**列**选择：``.loc[:, kept]`` 保留原索引与 ``attrs``，
这样选完之后仍能与其它特征分支按 provenance 合并。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.feature_selection import mutual_info_classif

from fault_core.preprocessing import mark_fitted

SELECTION_METHODS = ("variance", "correlation", "mutual_information", "model")
#: 需要标签的方法：只有这两种会触碰 label 向量。
SUPERVISED_METHODS = ("mutual_information", "model")


def _check(data: pd.DataFrame, labels: pd.Series | None, method: str, top_k: int) -> None:
    """参数与数据的前置校验：方法名、top_k、索引唯一性、有限性，以及有监督方法所需的标签。"""
    if method not in SELECTION_METHODS:
        raise ValueError(f"Unknown selection method: {method}")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 0:
        raise ValueError("top_k must be zero or a positive integer")
    if data.shape[1] == 0 or not data.index.is_unique:
        raise ValueError("Feature selection needs at least one uniquely indexed feature")
    if not np.isfinite(data.to_numpy(dtype=float, copy=False)).all():
        raise ValueError("Feature selection requires finite numeric features")
    if method not in SUPERVISED_METHODS:
        return
    if labels is None:
        raise ValueError(f"{method} selection requires a label vector")
    if not data.index.equals(labels.index):
        raise ValueError("Feature and label indices must be aligned")
    if labels.isna().any():
        raise ValueError("Labels contain missing values")
    if labels.nunique() < 2:
        raise ValueError("Supervised selection needs at least two classes")


def _scores(data: pd.DataFrame, method: str, labels: pd.Series | None, random_state: int) -> pd.Series:
    """按方法算每个特征的分数，返回以特征名为索引的 Series。

    ``correlation`` 的分数是"与其它列的最大绝对相关"，取对角线为 0 后再求行最大，
    因此分数高意味着与别的特征冗余。
    """
    if method == "mutual_information":
        values = mutual_info_classif(data, labels, discrete_features=False, random_state=random_state)
    elif method == "model":
        forest = RandomForestClassifier(n_estimators=200, random_state=random_state, n_jobs=1)
        values = forest.fit(data, labels).feature_importances_
    elif method == "variance":
        values = data.var(ddof=0).to_numpy()
    else:
        correlations = data.corr().abs().to_numpy()
        np.fill_diagonal(correlations, 0.0)
        values = np.nanmax(np.nan_to_num(correlations, nan=0.0), axis=1)
    return pd.Series(np.asarray(values, dtype=float), index=data.columns, name="score")


def _ranked(data: pd.DataFrame, scores: pd.Series, top_k: int) -> list[str]:
    """按分数降序取前 ``top_k`` 个特征名；``top_k=0`` 表示不截断，仅排序。"""
    order = scores.sort_values(ascending=False, kind="stable")
    if top_k:
        return list(order.index[:top_k])
    return list(order.index)


def select_features(
    data: pd.DataFrame,
    method: str = "variance",
    labels: pd.Series | None = None,
    top_k: int = 0,
    threshold: float | None = None,
    random_state: int = 42,
) -> dict[str, Any]:
    """Rank features and drop uninformative or redundant ones.

    ``variance`` and ``correlation`` are unsupervised. ``mutual_information`` and
    ``model`` consume aligned labels and are therefore marked as exploratory: the
    ranking is fitted on every row, including rows later used for validation.
    """
    _check(data, labels, method, top_k)
    scores = _scores(data, method, labels, random_state)
    if method == "correlation":
        cut = 0.95 if threshold is None else threshold
        if not 0 < cut <= 1:
            raise ValueError("Correlation threshold must be between 0 and 1")
        variance = data.var(ddof=0)
        # Constant columns carry no information and have undefined correlation.
        candidates = [
            c for c in variance.sort_values(ascending=False, kind="stable").index if variance[c] > 0
        ]
        correlations = data[candidates].corr().abs()
        # Keep the higher-variance member of each redundant pair, which is the more stable one.
        # 逐个与已保留特征比较：与任何一个的相关性超过阈值就丢弃，得到近似"去冗余"的贪心解。
        kept: list[str] = []
        for feature in candidates:
            if all(correlations.loc[feature, other] <= cut for other in kept):
                kept.append(feature)
        kept = [c for c in data.columns if c in set(kept)]
    else:
        # variance 的默认阈值是 0（去掉常量列）；有监督方法默认不卡分数（-inf），只按 top_k 截断。
        cut = (0.0 if method == "variance" else -np.inf) if threshold is None else threshold
        ranked = _ranked(data, scores, top_k)
        kept = [c for c in data.columns if c in set(ranked) and scores[c] > cut]
    if not kept:
        raise ValueError("Selection removed every feature; relax top_k or the threshold")
    result = data.loc[:, kept].copy()
    result.attrs = {
        **data.attrs,
        "selected_features": list(kept),
        "dropped_features": [c for c in data.columns if c not in set(kept)],
        "selection_method": method,
    }
    ranked_scores = scores.to_frame()
    ranked_scores["selected"] = ranked_scores.index.isin(kept)
    ranked_scores = ranked_scores.sort_values("score", ascending=False, kind="stable").reset_index(
        names="feature"
    )
    return {"features": mark_fitted(result, f"selection.{method}"), "scores": ranked_scores}
