"""行级编码 → 窗口聚合：这条链以前是断的，现在能跑通。

背景：`feature.categorical` 的输出只剩编码列（实体/时间/标签都被丢掉），而窗口组件要按这些列
取数；`feature.merge` 也合不了（行数与来源对不上）。修法有两半：窗口组件的输入端口接受
`FeatureDataset`，并且分类组件能把需要的列用 `keep_columns` 原样带过去。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fault_core import features
from fault_platform.components.base import DataType
from fault_platform.graph import ComponentGraph
from fault_platform.registry import default_registry
from fault_platform.runtime import ExecutionContext, ExecutionEngine
from fault_platform.workspace import FaultWorkspace


def address_frame(windows: int = 5) -> pd.DataFrame:
    """两个实体、各若干窗口；stack 的类别构成在窗口之间不同。

    默认 5 个窗口 × 2 个实体 = 10 个窗口，够验证组件的最低样本数（8）。
    """
    rows = []
    for entity, stacks in (("EQ-A", [3, 3, 4, 4, 4, 5, 5, 5]), ("EQ-B", [7, 7, 7, 9, 9, 9, 9, 9])):
        for window in range(windows):
            # 标签在窗口内恒定（否则 label_policy=strict 会（正确地）拒绝这次运行）。
            for index, stack in enumerate(stacks):
                rows.append(
                    {
                        "entity": entity,
                        "time": window * 8 + index,
                        "label": int(window >= windows - 2),
                        "stack": stack,
                        "voltage": 10.0 + index + window,
                    }
                )
    return pd.DataFrame(rows)


@pytest.fixture
def context(tmp_path):
    address_frame().to_csv(tmp_path / "addresses.csv", index=False)
    return tmp_path


def build(context, *, keep=None, features_list=None):
    registry = default_registry()
    graph = ComponentGraph(registry, "encoded-then-windowed")
    graph.add_node("data.input", "source", {"path": "addresses.csv"})
    graph.add_node(
        "feature.categorical",
        "encode",
        {"columns": ["stack"], "method": "onehot", "keep_columns": keep or []},
    )
    graph.add_node(
        "feature.statistical",
        "windows",
        {
            # 独热列名由 pandas 生成：整数取值就是 `stack_3`（不是 `stack_3.0`）。
            "columns": ["stack_3", "stack_4", "stack_5", "stack_7", "stack_9"],
            "features": features_list or ["mean", "count"],
            "group_column": "entity",
            "time_column": "time",
            "label_column": "label",
            "window_size": 8,
        },
    )
    graph.connect("source", "dataset", "encode", "dataset")
    graph.connect("encode", "features", "windows", "dataset")
    return graph


def run(graph, context):
    workspace = FaultWorkspace(graph.pipeline_id)
    return ExecutionEngine().execute(graph, ExecutionContext(workspace, context))


def test_window_components_accept_feature_datasets():
    registry = default_registry()
    for name in ("feature.statistical", "feature.fitting", "feature.spectral", "feature.entropy"):
        port = registry.get(name).input_ports[0]
        assert port.accepted_types == (DataType.DATASET, DataType.FEATURE_DATASET), name
    # 数据转换类与质量预检不放宽：它们只在原始表上有意义。
    assert registry.get("data.quality").input_ports[0].accepted_types == (DataType.DATASET,)
    assert registry.get("feature.select").input_ports[0].accepted_types == (DataType.DATASET,)


def test_onehot_then_windowed_mean_is_the_category_share(context):
    graph = build(context, keep=["entity", "time", "label"])
    assert graph.validate_graph() == []
    workspace = run(graph, context)
    assert workspace.status == "SUCCESS"
    table = workspace.get_output("windows", "features")
    assert len(table) == 10  # 2 个实体 × 5 个窗口
    # 实体 A 的窗口里 3 出现 2 次、4 出现 3 次、5 出现 3 次——独热均值就是"窗口内的类别占比"。
    first = table.iloc[0]
    assert first["stack_3__mean"] == pytest.approx(2 / 8)
    assert first["stack_4__mean"] == pytest.approx(3 / 8)
    assert first["stack_5__mean"] == pytest.approx(3 / 8)
    assert first["stack_7__mean"] == pytest.approx(0.0)
    assert first["stack_3__count"] == 8
    # 标签按窗口对齐：每个实体最后两个窗口为 1。
    assert list(workspace.get_output("windows", "labels")) == [0, 0, 0, 1, 1, 0, 0, 0, 1, 1]


def test_kept_columns_are_warned_about_everywhere(context):
    graph = build(context, keep=["entity", "time", "label"])
    workspace = run(graph, context)
    # 节点级警告出在"做了这件事"的那个节点上（编码节点）。
    assert any("keep_columns" in note for note in (workspace.node_warnings.get("encode") or []))
    # 警告也要进窗口特征的 attrs，才有机会一路传到模型 metrics。
    attrs = workspace.get_output("windows", "features").attrs
    assert any("keep_columns" in note for note in attrs.get("evaluation_warnings", []))


def test_carried_columns_can_feed_a_model_without_leaking(context):
    """整条链：行级独热 → 窗口聚合 → 模型；带过来的列不许变成特征。"""
    graph = build(context, keep=["entity", "time", "label"])
    graph.add_node("validation.decision_tree", "model", {"split_method": "group", "random_state": 0})
    graph.connect("windows", "features", "model", "features")
    graph.connect("windows", "labels", "model", "labels")
    workspace = run(graph, context)
    assert workspace.status == "SUCCESS"
    columns = list(workspace.get_output("windows", "features").columns)
    assert "entity" not in columns and "label" not in columns and "time" not in columns
    assert all(name.endswith(("__mean", "__count")) for name in columns)


def test_missing_and_colliding_keep_columns_are_rejected(context):
    # 组件抛的错由引擎记在节点上（不是向上抛），所以断言节点的 error_message。
    workspace = run(build(context, keep=["entity", "nope"]), context)
    assert workspace.status == "FAILED"
    assert "keep_columns not found" in workspace.errors["encode"]["error_message"]

    # 冲突检测：源表里真的存在一个与编码列同名的列。
    frame = address_frame().assign(stack_3=1.0)
    encoded = pd.DataFrame({"stack_3": [1.0] * len(frame)}, index=frame.index)
    with pytest.raises(ValueError, match="collide with encoded columns"):
        features.carry_columns(encoded, frame, ["stack_3"])
    with pytest.raises(ValueError, match="share the source row index"):
        features.carry_columns(encoded.iloc[:5], frame, ["entity"])


def test_without_keep_columns_the_failure_says_which_column_is_missing(context):
    """不带走实体列时，窗口组件应当报"找不到列"，而不是给出一个错误的结果。"""
    workspace = run(build(context, keep=[]), context)
    assert workspace.status == "FAILED"
    assert "entity" in workspace.errors["windows"]["error_message"]


def test_carry_columns_is_a_no_op_without_columns():
    frame = address_frame()
    encoded = pd.DataFrame({"stack_3": np.zeros(len(frame))}, index=frame.index)
    assert features.carry_columns(encoded, frame, []) is encoded
    carried = features.carry_columns(encoded, frame, ["entity", "label"])
    assert list(carried.columns) == ["stack_3", "entity", "label"]
    assert carried["entity"].tolist() == frame["entity"].tolist()
