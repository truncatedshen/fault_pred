"""正负样本构成必须一路可见：数据概览里有比例，验证指标里训练/测试各自有比例。

这一层存在的理由很实际——`test_count=228` 与 `test_count=228、其中故障 13 行` 是两回事，
而只看前者的报告会让"永远判正常"的模型看起来是满分。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fault_core import features, models, visualization


def frame_with_rare_faults(rows: int = 200, fault_rows: int = 8) -> pd.DataFrame:
    """一批数据，正类只占 4%——就是让人误读 accuracy 的那种分布。"""
    rng = np.random.default_rng(3)
    label = np.zeros(rows, dtype=int)
    label[:fault_rows] = 1
    return pd.DataFrame(
        {
            "instance": np.repeat([f"EQ-{index:03d}" for index in range(rows // 20)], 20),
            "time_s": np.tile(np.arange(20), rows // 20),
            "fault": label,
            "sensor": rng.normal(label * 2.0, 0.5, rows),
        }
    )


def test_overview_reports_label_balance_from_a_column():
    payload = visualization.overview(frame_with_rare_faults(), label_column="fault")
    balance = payload["label_distribution"]
    assert balance["source"] == "fault"
    assert balance["counts"] == {"0": 192, "1": 8}
    assert balance["rates"]["1"] == pytest.approx(0.04)
    # 正类沿用平台约定：二分类取排序后的最后一个类别。
    assert balance["positive_class"] == "1"
    assert balance["positive_count"] == 8
    assert sum(balance["rates"].values()) == pytest.approx(1.0)
    # 4% 的正类必须换来一条能直接写进报告的结论，而不是只留个数字。
    assert any("imbalanced" in note for note in balance["findings"])
    assert "balanced_accuracy" in " ".join(balance["findings"])
    assert payload["findings"] == balance["findings"]


def test_overview_accepts_a_label_vector_and_stays_silent_without_one():
    frame = frame_with_rare_faults()
    assert visualization.overview(frame)["label_distribution"] is None
    from_vector = visualization.overview(frame, labels=frame["fault"])
    assert from_vector["label_distribution"]["counts"] == {"0": 192, "1": 8}
    assert from_vector["label_column"] == "<labels input>"


def test_overview_rejects_an_unknown_label_column():
    # 写错列必须报错：静默跳过会让人以为"标签是均衡的"。
    with pytest.raises(ValueError, match="label_column"):
        visualization.overview(frame_with_rare_faults(), label_column="missing")


def test_missing_labels_are_counted_not_hidden():
    frame = frame_with_rare_faults()
    frame.loc[:9, "fault"] = np.nan
    balance = visualization.overview(frame, label_column="fault")["label_distribution"]
    assert balance["rows"] == 200
    assert balance["total"] == 190
    assert balance["missing"] == 10
    assert any("no label" in note for note in balance["findings"])


def test_streamed_overview_matches_the_batch_numbers():
    frame = frame_with_rare_faults()
    chunks = [chunk for _, chunk in frame.groupby(np.arange(len(frame)) // 64)]
    streamed = visualization.overview_stream(iter(chunks), label_column="fault")
    batch = visualization.overview(frame, label_column="fault")
    assert streamed["label_distribution"]["counts"] == batch["label_distribution"]["counts"]
    assert streamed["label_distribution"]["rates"] == batch["label_distribution"]["rates"]
    assert streamed["label_distribution"]["findings"] == batch["label_distribution"]["findings"]
    assert streamed["findings"] == batch["findings"]


def test_metrics_carry_train_and_test_class_rates():
    # 一半实例是故障：group 切分要把两类都留在训练集里，否则平台会（正确地）拒绝训练。
    frame = frame_with_rare_faults(rows=400, fault_rows=200)
    extracted = features.extract_features(
        frame, ["sensor"], group_column="instance", label_column="fault", window_size=8, step=8
    )
    metrics = models.validate_model(**extracted, split_method="group", test_size=0.25, n_estimators=20)[
        "metrics"
    ]
    for side in ("train", "test"):
        counts = metrics[f"{side}_class_counts"]
        rates = metrics[f"{side}_class_rates"]
        assert set(counts) == set(rates)
        assert sum(rates.values()) == pytest.approx(1.0)
        for name, count in counts.items():
            assert rates[name] == pytest.approx(count / metrics[f"{side}_count"])
    assert metrics["positive_class"] == "1"


def test_empty_holdout_class_is_called_out_in_warnings():
    """训练集里两类都在、测试集里只有一类时，指标必须自己说明它没测到另一类。"""
    rng = np.random.default_rng(11)
    rows, groups, per_group = 12, 12, 20
    frame = pd.DataFrame(
        {
            "instance": np.repeat([f"EQ-{group:03d}" for group in range(groups)], per_group),
            "time_s": np.tile(np.arange(per_group), groups),
            # 只有前两台设备发生故障：留出这两台就可能让测试集一个正类都没有。
            "fault": np.repeat([1, 1] + [0] * (groups - 2), per_group),
            "sensor": rng.normal(0, 1, rows * per_group),
        }
    )
    extracted = features.extract_features(
        frame, ["sensor"], group_column="instance", label_column="fault", window_size=10, step=10
    )
    metrics = models.validate_model(**extracted, split_method="temporal", test_size=0.5, n_estimators=20)[
        "metrics"
    ]
    assert set(metrics["train_class_counts"]) == {"0", "1"}
    assert set(metrics["test_class_counts"]) == {"0"}
    assert any("Holdout split has no 1 rows" in note for note in metrics["warnings"])
    assert any("no positive (1) rows" in note for note in metrics["warnings"])


def test_overview_component_carries_labels_port_into_the_report(registry, context, dataset):
    component = registry.create("visual.overview", parameters={})
    ports = {port.name: port for port in component.input_ports}
    assert ports["labels"].required is False
    assert ports["labels"].accepted_types == ("LabelVector",)
    result = component.execute({"dataset": dataset, "labels": dataset["label"]}, context)
    assert result.outputs["overview"]["label_distribution"]["counts"] == {
        "0": int((dataset["label"] == 0).sum()),
        "1": int((dataset["label"] == 1).sum()),
    }


def test_overview_component_reads_the_label_column_parameter(registry, context, dataset):
    component = registry.create("visual.overview", parameters={"label_column": "label"})
    result = component.execute({"dataset": dataset}, context)
    assert result.outputs["overview"]["label_distribution"]["total"] == len(dataset)


def test_pipeline_shows_class_balance_on_a_feature_branch(pipeline, registry):
    """特征分支上接 `labels` 端口：中间产物的正负比例必须看得见。"""
    pipeline.add_node("visual.overview", "balance")
    pipeline.connect("features", "features", "balance", "dataset")
    pipeline.connect("features", "labels", "balance", "labels")
    assert pipeline.validate_graph() == []


def test_overview_on_a_feature_branch_reports_the_window_label_balance(pipeline, context):
    """接上 `labels` 之后，概览报的是**窗口标签**的比例，与数据集行数无关。"""
    from fault_platform.runtime import ExecutionEngine

    pipeline.add_node("visual.overview", "balance")
    pipeline.connect("features", "features", "balance", "dataset")
    pipeline.connect("features", "labels", "balance", "labels")
    workspace = ExecutionEngine().execute(pipeline, context)
    labels = workspace.get_output("features", "labels")
    overview = workspace.get_output("balance", "overview")
    assert overview["row_count"] == len(labels)
    assert overview["label_distribution"]["total"] == len(labels)
    assert sum(overview["label_distribution"]["counts"].values()) == len(labels)
