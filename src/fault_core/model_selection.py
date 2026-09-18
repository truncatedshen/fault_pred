"""Hyper-parameter search with an honest account of what the score means.

清单里的"自动机器学习"目前只做到了"模型对比"（``validation.compare``），缺的是**超参搜索**。
这个模块补上它，但刻意把两件事分清楚：

* ``best_score`` 是**交叉验证**分数，不是留出分数。它比单次留出稳，但仍然是在同一份数据上
  选出来的参数，对"这个模型到底多好"的回答偏乐观；
* 因此返回值里另外给出 ``search_method``、折数、每折的样本量与候选数量，让人能判断
  "选出来的参数是不是在少量样本上赢了一点点"。``warnings`` 里也会明确写这句。

交叉验证的切分方式与验证器保持一致：``stratified``（行独立）、``group``（按实例整组留出）、
``temporal``（按行序切尾段）。重叠窗口必须用后两者——随机 K 折会让相邻窗口一个进训练、
一个进验证，把答案泄进去。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor, RandomForestClassifier
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.model_selection import GridSearchCV, GroupKFold, KFold, StratifiedKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.svm import SVC
from sklearn.tree import DecisionTreeClassifier

from fault_core.models import TrainedClassifier

#: 支持的算法：分类器与回归器分开列，避免把回归器接到标签端口上。
CLASSIFIERS = ("random_forest", "decision_tree", "svm", "logistic")
REGRESSORS = ("ridge", "gradient_boosting")
ALGORITHMS = (*CLASSIFIERS, *REGRESSORS)


def _estimator(algorithm: str, random_state: int) -> Any:
    """按名字构造估计器；SVM/logistic 内部带标准化，保证缩放只在训练折上拟合。"""
    if algorithm == "random_forest":
        return RandomForestClassifier(random_state=random_state, n_jobs=1)
    if algorithm == "decision_tree":
        return DecisionTreeClassifier(random_state=random_state)
    if algorithm == "svm":
        return make_pipeline(StandardScaler(), SVC(random_state=random_state))
    if algorithm == "logistic":
        return make_pipeline(StandardScaler(), LogisticRegression(random_state=random_state))
    if algorithm == "ridge":
        return Ridge(random_state=random_state)
    if algorithm == "gradient_boosting":
        return GradientBoostingRegressor(random_state=random_state)
    raise ValueError(f"Unknown search algorithm: {algorithm}")


def _parameter_names(algorithm: str) -> list[str]:
    """各算法可搜索的参数名，用于在报错里提示正确的写法。"""
    return {
        "random_forest": ["n_estimators", "max_depth", "min_samples_leaf", "max_features"],
        "decision_tree": ["criterion", "max_depth", "min_samples_leaf"],
        "svm": ["svc__C", "svc__gamma", "svc__kernel"],
        "logistic": ["logisticregression__C"],
        "ridge": ["alpha", "fit_intercept"],
        "gradient_boosting": ["n_estimators", "learning_rate", "max_depth"],
    }[algorithm]


def _cross_validator(
    method: str, folds: int, features: pd.DataFrame, target: pd.Series, classification: bool
) -> Any:
    """按 ``method`` 造一个交叉验证切分器；组切分要求上游窗口组件给了 ``group_column``。"""
    if method == "group":
        groups = features.attrs.get("groups")
        if not features.attrs.get("grouped") or groups is None or len(groups) != len(features):
            raise ValueError("cv_method=group requires feature extraction with group_column")
        unique = len(set(groups))
        if unique < 2:
            raise ValueError(f"Group cross-validation needs at least two groups, found {unique}")
        return GroupKFold(n_splits=min(folds, unique))
    if method == "temporal":
        return KFold(n_splits=folds, shuffle=False)
    if method == "stratified":
        if not classification:
            return KFold(n_splits=folds, shuffle=True, random_state=0)
        counts = target.value_counts()
        smallest = int(counts.min()) if len(counts) else 0
        if smallest < folds:
            # 折数不能超过最小类别的样本数，否则某一折里会缺类，网格搜索会直接报错。
            folds = max(2, smallest)
            if folds < 2:
                raise ValueError("Stratified search needs at least two samples per class")
        return StratifiedKFold(n_splits=folds, shuffle=True, random_state=0)
    raise ValueError(f"Unknown cross-validation method: {method}")


def grid_search(
    features: pd.DataFrame,
    target: pd.Series,
    algorithm: str = "random_forest",
    param_grid: dict[str, list[Any]] | None = None,
    cv_method: str = "stratified",
    cv_folds: int = 3,
    scoring: str = "",
    top_k: int = 5,
) -> dict[str, Any]:
    """在小网格上做交叉验证搜索，返回最优估计器、最优参数与**全部**候选的分数。

    ``param_grid`` 必须是"参数名 → 候选值列表"的小字典（例如
    ``{"n_estimators": [100, 300], "max_depth": [4, 8]}``）。**故意不提供大网格**：
    候选数量会在返回值里给出，超过几十个就应该先想清楚"为什么要搜这些"，而不是让
    服务替你跑一整天。分类任务的 ``scoring`` 默认 ``f1_macro``——**不做加权**：加权 F1 在
    故障稀少时由多数类主导，会把"一个故障都没抓到"的配置排到第一；宏平均让每个类等权。
    回归默认 ``r2``。（想要旧口径可以显式传 ``scoring="f1_weighted"``。）

    ``top_k`` 控制返回多少个候选的分数，用来判断"第一名是不是只赢了第二名一点点"。
    """
    if algorithm not in ALGORITHMS:
        raise ValueError(f"Unknown search algorithm: {algorithm}")
    classification = algorithm in CLASSIFIERS
    if not features.index.is_unique or not target.index.is_unique or not features.index.equals(target.index):
        raise ValueError("Feature and target indices must be unique and exactly aligned")
    if len(features) < 8 or features.shape[1] == 0:
        raise ValueError("Grid search needs at least eight samples and one feature")
    values = features.to_numpy(dtype=float, copy=False)
    if not np.isfinite(values).all():
        raise ValueError("Grid search requires finite feature values; impute missing features first")
    if target.isna().any():
        raise ValueError("Grid search requires a target without missing values")
    grid = param_grid or {}
    if not isinstance(grid, dict) or not grid:
        raise ValueError(
            f"param_grid cannot be empty; searchable parameters for {algorithm}: "
            f"{_parameter_names(algorithm)}"
        )
    allowed = set(_parameter_names(algorithm))
    unknown = sorted(set(grid) - allowed)
    if unknown:
        raise ValueError(f"Unsupported parameter(s) for {algorithm}: {unknown}. Supported: {sorted(allowed)}")
    candidates = int(np.prod([len(values_) for values_ in grid.values()]))
    # 分类任务先把标签编码成整数：估计器、分层折数与编码器三者用同一套映射，
    # 返回的模型再把它翻回原始类别名（与其它验证器一致）。
    encoder = LabelEncoder().fit(target) if classification else None
    encoded = encoder.transform(target) if encoder is not None else target.to_numpy(dtype=float)
    estimator = _estimator(algorithm, 0)
    cross_validator = _cross_validator(cv_method, int(cv_folds), features, target, classification)
    folds = int(getattr(cross_validator, "get_n_splits")())
    chosen_scoring = scoring or ("f1_macro" if classification else "r2")
    search = GridSearchCV(
        estimator,
        grid,
        cv=cross_validator,
        scoring=chosen_scoring,
        refit=True,
        n_jobs=1,
        error_score="raise",
    )
    # GroupKFold 需要在 fit 时显式给出分组列，否则它会在运行时才抱怨 groups 为 None。
    if cv_method == "group":
        search.fit(features, encoded, groups=list(features.attrs["groups"]))
    else:
        search.fit(features, encoded)
    best_estimator = search.best_estimator_
    ranking = pd.DataFrame(search.cv_results_)
    ranking = ranking.sort_values("rank_test_score", kind="stable").head(max(1, top_k))
    candidates_table = pd.DataFrame(
        {
            "rank": ranking["rank_test_score"].astype(int),
            "mean_test_score": ranking["mean_test_score"].astype(float).round(6),
            "std_test_score": ranking["std_test_score"].astype(float).round(6),
            "params": [str(item) for item in ranking["params"]],
        }
    )
    importance = _importance(best_estimator, list(features.columns))
    spread = (
        float(candidates_table["mean_test_score"].iloc[0] - candidates_table["mean_test_score"].iloc[-1])
        if len(candidates_table) > 1
        else 0.0
    )
    metrics = {
        "algorithm": algorithm,
        "search_method": f"{cv_method}_cv",
        "scoring": chosen_scoring,
        "cv_folds": folds,
        "candidate_count": candidates,
        "best_params": {key: str(value) for key, value in search.best_params_.items()},
        "best_score": float(search.best_score_),
        "top_score_spread": spread,
        "sample_count": int(len(features)),
        "feature_count": int(features.shape[1]),
        "test_count": 0,
        "test_indices": [],
        "warnings": [
            "best_score is a cross-validation score from the same data the parameters were "
            "chosen on; it is optimistic and is not a holdout result.",
        ]
        + (
            ["Overlapping windows make random folds leak; use cv_method=group or temporal."]
            if features.attrs.get("overlapping")
            else []
        ),
    }
    outputs: dict[str, Any] = {
        "model": (
            TrainedClassifier(best_estimator, encoder, list(features.columns))
            if encoder is not None
            else best_estimator
        ),
        "metrics": metrics,
        "candidates": candidates_table,
    }
    if importance is not None:
        outputs["importance"] = importance
    return outputs


def _importance(estimator: Any, columns: list[str]) -> pd.DataFrame | None:
    """从最优估计器里抽特征重要性；没有可解释权重的算法返回 ``None``。"""
    final = estimator
    if hasattr(estimator, "steps"):
        final = estimator.steps[-1][1]
    if hasattr(final, "feature_importances_"):
        return pd.DataFrame(
            {"feature": columns, "importance": np.asarray(final.feature_importances_, dtype=float)}
        )
    if hasattr(final, "coef_"):
        return pd.DataFrame(
            {"feature": columns, "coefficient": np.asarray(final.coef_, dtype=float).reshape(-1)}
        )
    return None
