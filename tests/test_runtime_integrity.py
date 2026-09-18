"""Regression checks for lineage, changing files, partial runs and warnings."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fault_core.features import expand_coverage, extract_features, merge_features
from fault_core.models import validate_model
from fault_core.preprocessing import scale
from fault_platform.runtime import ExecutionEngine
from fault_platform.workspace import NodeStatus, PipelineStatus, summarize


def test_labels_from_other_windows_are_rejected(dataset):
    a = extract_features(dataset, ["vibration"], group_column="equipment", label_column="label")
    b = extract_features(dataset.iloc[::2], ["vibration"], group_column="equipment", label_column="label")
    assert a["features"].index.equals(b["labels"].index)
    with pytest.raises(ValueError, match="provenance"):
        validate_model(a["features"], b["labels"], split_method="group")
    with pytest.raises(ValueError, match="provenance"):
        merge_features(a["features"], b["features"].rename(columns=lambda c: "b_" + c))


def test_temporal_split_purges_raw_window_overlap():
    rng = np.random.default_rng(4)
    data = pd.DataFrame({"signal": rng.normal(size=120), "label": np.arange(120) % 2})
    extracted = extract_features(
        data, ["signal"], label_column="label", label_policy="last", window_size=15, step=3
    )
    outputs = validate_model(**extracted, split_method="temporal", n_estimators=5)
    metrics = outputs["metrics"]
    frame = extracted["features"]
    # 覆盖信息现在是区间表示（`source_rows_ranges`）：pandas 每次 finalize 都会深拷贝 attrs，
    # 逐行号列表在大表上是秒级开销。语义完全一样，用官方访问器取回逐窗口行号。
    coverage = dict(zip(frame.index, expand_coverage(frame.attrs)))
    train = {r for key in metrics["train_indices"] for r in coverage[key]}
    test = {r for key in metrics["test_indices"] for r in coverage[key]}
    assert not train.intersection(test)
    assert metrics["train_count"] < int(len(frame) * 0.75)


def test_source_content_changes_invalidate_cached_results(pipeline, context):
    engine = ExecutionEngine()
    ws = engine.execute(pipeline, context)
    frame = pd.read_csv(context.data_root / "sample.csv")
    frame["temperature"] += 1
    frame.to_csv(context.data_root / "sample.csv", index=False)
    count = len(ws.history)
    engine.execute(pipeline, context)
    assert not any(h.cached for h in ws.history[count:])
    np.testing.assert_allclose(ws.get_output("source", "dataset").temperature, frame.temperature)


def test_single_node_rerun_invalidates_downstream(pipeline, context):
    engine = ExecutionEngine()
    ws = engine.execute(pipeline, context)
    engine.execute(pipeline, context, mode="node", node_id="filter", incremental=False)
    assert ws.node_status["filter"] == NodeStatus.SUCCESS
    assert ws.node_status["model"] == NodeStatus.PENDING
    assert "model" not in ws.node_results
    assert ws.status == PipelineStatus.READY


def test_full_data_fit_warning_and_preview_bounds(dataset):
    transformed = scale(dataset, ["vibration"], "zscore")
    extracted = extract_features(transformed, ["vibration"], group_column="equipment", label_column="label")
    result = validate_model(**extracted, split_method="group", n_estimators=5)
    assert "full dataset" in result["metrics"]["warnings"][0]
    preview = summarize(pd.DataFrame(np.ones((1000, 200))), limit=1000)
    assert len(preview["preview"]) == 100
    assert len(preview["columns"]) == 50
    assert preview["truncated"]
