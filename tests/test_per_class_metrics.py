"""指标口径必须能被手算复核：留出集、宏平均（不加权）、逐类数值与混淆矩阵一一对应。

这条要求的来源很实际：使用者拿混淆矩阵去算召回率，发现和报告里的数字对不上——因为报告
用的是**加权**平均，多数类把少数类的结果盖住了。改为宏平均 + 逐类数值之后，报告里的每个
数字都可以用混淆矩阵重算出来；加权值如果需要，也能自己用逐类值与支持数合成。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sklearn.metrics import f1_score, precision_recall_fscore_support

from fault_core import features, model_selection, models


def imbalanced_frame(rows_per_group: int = 30, faulted_groups: int = 3, groups: int = 20) -> pd.DataFrame:
    """前 ``faulted_groups`` 台设备整段故障——正类稀少，加权与宏平均会明显不同。"""
    rng = np.random.default_rng(7)
    rows = groups * rows_per_group
    label = np.repeat([1] * faulted_groups + [0] * (groups - faulted_groups), rows_per_group)
    return pd.DataFrame(
        {
            "instance": np.repeat([f"EQ-{index:03d}" for index in range(groups)], rows_per_group),
            "fault": label,
            "sensor": rng.normal(label * 0.2, 1.0, rows),
        }
    )


def fitted(split_method: str = "group", **parameters):
    data = features.extract_features(
        imbalanced_frame(),
        ["sensor"],
        group_column="instance",
        label_column="fault",
        window_size=10,
        step=10,
    )
    return models.validate_model(**data, split_method=split_method, n_estimators=30, **parameters), data


def test_headline_metrics_are_macro_and_match_the_confusion_matrix() -> None:
    """报告里的精确率/召回率/F1 = 逐类值的算术平均，且逐类值就是混淆矩阵算出来的那个。"""
    out, data = fitted()
    metrics = out["metrics"]
    matrix = np.asarray(metrics["confusion_matrix"], dtype=float)
    classes = [str(name) for name in metrics["classes"]]
    assert metrics["averaging"] == "macro"

    for position, name in enumerate(classes):
        row_total = matrix[position, :].sum()  # 该类真实出现次数 = 支持数
        column_total = matrix[:, position].sum()  # 该类被预测次数
        assert metrics["per_class_support"][name] == int(row_total) == metrics["test_class_counts"][name]
        expected_recall = matrix[position, position] / row_total if row_total else 0.0
        expected_precision = matrix[position, position] / column_total if column_total else 0.0
        assert metrics["per_class_recall"][name] == pytest.approx(expected_recall)
        assert metrics["per_class_precision"][name] == pytest.approx(expected_precision)
        if expected_precision + expected_recall:
            expected_f1 = 2 * expected_precision * expected_recall / (expected_precision + expected_recall)
        else:
            expected_f1 = 0.0
        assert metrics["per_class_f1"][name] == pytest.approx(expected_f1)

    for key in ("precision", "recall", "f1"):
        per_class = [metrics[f"per_class_{key}"][name] for name in classes]
        assert metrics[key] == pytest.approx(float(np.mean(per_class)))


def test_macro_metrics_are_not_the_weighted_ones() -> None:
    """加权值确实被换掉了：多数类占九成时两者会明显不同，宏平均不会被多数类盖住。"""
    out, _ = fitted()
    metrics = out["metrics"]
    prediction = out["prediction"]
    y_true = prediction["actual"].to_numpy()
    y_pred = prediction["predicted"].to_numpy()
    weighted = f1_score(y_true, y_pred, average="weighted", zero_division=0)
    macro = f1_score(y_true, y_pred, average="macro", zero_division=0)
    assert metrics["f1"] == pytest.approx(macro)
    # 正类稀少（本例约 15%）时两者本就不该相等——相等说明还是加权口径。
    assert abs(weighted - macro) > 1e-9
    # 与 sklearn 的逐类值逐个对齐（口径与第三方实现一致，不是自创定义）。
    per_class = precision_recall_fscore_support(y_true, y_pred, average=None, zero_division=0)
    for index, name in enumerate([str(name) for name in metrics["classes"]]):
        assert metrics["per_class_f1"][name] == pytest.approx(float(per_class[2][index]))


def test_missing_class_in_the_holdout_is_zeroed_and_flagged() -> None:
    """测试集里没有的类别记 0 并给警告——不参与宏平均等于把"没测到"伪装成"测得好"。"""
    rows_per_group, groups = 30, 12
    frame = imbalanced_frame(rows_per_group=rows_per_group, faulted_groups=1, groups=groups)
    data = features.extract_features(
        frame, ["sensor"], group_column="instance", label_column="fault", window_size=10, step=10
    )
    # temporal：故障只在前两台设备，末段留出集必然一个正类都没有。
    metrics = models.validate_model(**data, split_method="temporal", test_size=0.25, n_estimators=20)[
        "metrics"
    ]
    assert metrics["per_class_support"]["1"] == 0
    assert metrics["per_class_recall"]["1"] == 0.0
    assert metrics["precision"] == pytest.approx(metrics["per_class_precision"]["0"] / 2)
    assert any("no positive (1) rows" in note for note in metrics["warnings"])


def test_compare_table_carries_accuracy_and_the_macro_metrics(registry, context) -> None:
    """`validation.compare` 的对比表要带上同一批标量：accuracy + 宏平均 + 漏报率。"""
    data = features.extract_features(
        imbalanced_frame(),
        ["sensor"],
        group_column="instance",
        label_column="fault",
        window_size=10,
        step=10,
    )
    # 同一份数据、同一个 random_state → 同一个留出集，compare 才允许比较（否则它自己会拒绝）。
    first = models.validate_model(**data, algorithm="random_forest", n_estimators=30, split_method="group")[
        "metrics"
    ]
    second = models.validate_model(**data, algorithm="decision_tree", split_method="group")["metrics"]
    assert first["test_indices"] == second["test_indices"]
    component = registry.create("validation.compare")
    rows = component.execute({"first": first, "second": second}, context).outputs["comparison"]["rows"]
    assert [row["algorithm"] for row in rows] == ["random_forest", "decision_tree"]
    for row, metrics in zip(rows, (first, second)):
        assert row["accuracy"] == metrics["accuracy"]
        assert row["precision"] == metrics["precision"]
        assert row["recall"] == metrics["recall"]
        assert row["f1"] == metrics["f1"]
        assert row["miss_rate"] == metrics["miss_rate"]
    # 旧的假指标（只有 scalars、没有 per_class_*）也要能进来，缺字段给 None 而不是 KeyError。
    legacy = {"algorithm": "rf", "accuracy": 0.8, "test_indices": first["test_indices"]}
    assert (
        component.execute({"first": first, "second": legacy}, context).outputs["comparison"]["rows"][1][
            "precision"
        ]
        is None
    )


def test_grid_search_default_scoring_is_unweighted() -> None:
    """调参目标同样不加权：默认从 f1_weighted 改为 f1_macro。"""
    data = features.extract_features(
        imbalanced_frame(),
        ["sensor"],
        group_column="instance",
        label_column="fault",
        window_size=10,
        step=10,
    )
    result = model_selection.grid_search(
        data["features"],
        data["labels"],
        algorithm="random_forest",
        param_grid={"n_estimators": [10]},
        top_k=1,
    )
    assert result["metrics"]["scoring"] == "f1_macro"
