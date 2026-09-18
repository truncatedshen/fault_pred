"""逐列特征配置：同一个组件里，不同列可以算不同的东西。

动机（用户的"初衷"）：地址/编号列只能取 `distinct_count`，物理量列才该求均值与标准差。
以前 `features` 是全局的，只能"建两三条分支再合并"，既费节点，又会在 `feature.merge` 上撞到
"来源必须完全一致"。现在一个节点就能表达，`column_features` 覆盖到的列用它的清单，
没覆盖的列继续用全局 `features`。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fault_core import features, sequence_features
from fault_platform.registry import default_registry


def mixed_frame(rows: int = 32) -> pd.DataFrame:
    """一列物理量、一列地址、一列档位：三列该看的东西完全不同。"""
    rng = np.random.default_rng(3)
    return pd.DataFrame(
        {
            "equipment": np.repeat([0, 1], rows // 2),
            "time": np.tile(np.arange(rows // 2), 2),
            "vibration": rng.normal(size=rows),
            "stack": np.tile([3, 3, 4, 4], rows // 4),
        }
    )


def test_statistical_features_can_differ_per_column():
    """一个节点里：vibration 求均值/标准差，stack 只数"有几种取值"。"""
    table = features.extract_features(
        mixed_frame(),
        ["vibration", "stack"],
        group_column="equipment",
        window_size=16,
        features=["mean", "std"],
        column_features={"stack": "distinct_count"},
    )["features"]
    assert set(table.columns) == {"vibration__mean", "vibration__std", "stack__distinct_count"}
    # stack 的取值是 3/4 两种，且窗口内各 8 个——逐列配置拿到的就是"有几类"。
    assert table["stack__distinct_count"].eq(2.0).all()


def test_columns_not_listed_keep_the_global_features():
    table = features.extract_features(
        mixed_frame(),
        ["vibration", "stack"],
        group_column="equipment",
        window_size=16,
        features=["mean"],
        column_features={"stack": ["distinct_count", "max_repeated"]},
    )["features"]
    assert set(table.columns) == {"vibration__mean", "stack__distinct_count", "stack__max_repeated"}


def test_a_string_value_is_the_same_as_a_single_item_list():
    options = dict(group_column="equipment", window_size=16, features=["mean"])
    as_string = features.extract_features(
        mixed_frame(), ["vibration", "stack"], column_features={"stack": "distinct_count"}, **options
    )["features"]
    as_list = features.extract_features(
        mixed_frame(), ["vibration", "stack"], column_features={"stack": ["distinct_count"]}, **options
    )["features"]
    pd.testing.assert_frame_equal(as_string, as_list)


def test_streaming_matches_batch_with_per_column_features():
    import tempfile
    from pathlib import Path

    from fault_platform.streaming import StreamedDataset

    frame = mixed_frame(rows=128)
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "mixed.csv"
        frame.to_csv(path, index=False)
        options = dict(
            columns=["vibration", "stack"],
            group_column="equipment",
            window_size=16,
            features=["mean"],
            column_features={"stack": "distinct_count"},
        )
        batch = features.extract_features(pd.read_csv(path), **options)["features"]
        streamed = features.extract_features_stream(
            StreamedDataset(path=path, chunk_rows=100).chunks(), **options
        )["features"]
    assert list(batch.columns) == list(streamed.columns)
    np.testing.assert_allclose(batch.to_numpy(), streamed.to_numpy())


def test_spectral_entropy_and_sequence_components_accept_it_too():
    rng = np.random.default_rng(5)
    rows = 64
    frame = pd.DataFrame(
        {
            "equipment": [0] * rows,
            "time": np.arange(rows),
            "vibration": np.sin(2 * np.pi * 4 * np.arange(rows) / rows),
            "temperature": rng.normal(size=rows),
        }
    )
    spectral = features.spectral(
        frame,
        ["vibration", "temperature"],
        sampling_rate=64.0,
        group_column="equipment",
        time_column="time",
        window_size=64,
        features=["dominant_frequency"],
        column_features={"temperature": ["spectral_rms"]},
    )["features"]
    assert set(spectral.columns) == {"vibration__dominant_frequency", "temperature__spectral_rms"}

    entropy = sequence_features.entropy_features(
        frame,
        ["vibration", "temperature"],
        group_column="equipment",
        window_size=32,
        methods=["approximate_entropy"],
        column_features={"temperature": "information_entropy"},
    )["features"]
    assert set(entropy.columns) == {
        "vibration__approximate_entropy",
        "temperature__information_entropy",
    }

    rolling = sequence_features.rolling_statistics(
        frame,
        ["vibration", "temperature"],
        method="mean",
        window=3,
        group_column="equipment",
        column_features={"temperature": "std"},
    )
    assert list(rolling.columns) == ["vibration__rolling_mean_3", "temperature__rolling_std_3"]

    temporal = sequence_features.temporal_features(
        frame,
        ["vibration", "temperature"],
        method="first_difference",
        group_column="equipment",
        column_features={"temperature": "peak_count"},
    )
    assert list(temporal.columns) == ["vibration__first_difference", "temperature__peak_count"]


def test_unknown_column_empty_list_and_unknown_feature_are_rejected():
    options = dict(group_column="equipment", window_size=16, features=["mean"])
    with pytest.raises(ValueError, match="which is not in columns"):
        features.extract_features(
            mixed_frame(), ["vibration"], column_features={"stack": "distinct_count"}, **options
        )
    with pytest.raises(ValueError, match="is empty"):
        features.extract_features(mixed_frame(), ["vibration"], column_features={"vibration": []}, **options)
    with pytest.raises(ValueError, match="unknown features"):
        features.extract_features(
            mixed_frame(), ["vibration"], column_features={"vibration": ["nope"]}, **options
        )


def test_single_method_components_reject_more_than_one_method():
    frame = mixed_frame()
    with pytest.raises(ValueError, match="exactly one method"):
        sequence_features.rolling_statistics(
            frame,
            ["vibration"],
            method="mean",
            group_column="equipment",
            column_features={"vibration": ["mean", "std"]},
        )
    with pytest.raises(ValueError, match="unknown methods"):
        sequence_features.temporal_features(
            frame, ["vibration"], column_features={"vibration": "nope"}, group_column="equipment"
        )


def test_fitting_branch_has_no_feature_list_and_says_so():
    with pytest.raises(ValueError, match="fitting branch has no feature list"):
        features.extract_features(
            mixed_frame(),
            ["vibration"],
            kind="fitting",
            group_column="equipment",
            window_size=16,
            column_features={"vibration": "mean"},
        )


def test_component_schemas_expose_column_features():
    registry = default_registry()
    for name in (
        "feature.statistical",
        "feature.spectral",
        "feature.entropy",
        "feature.rolling_statistics",
        "feature.temporal",
    ):
        parameter = next(
            (item for item in registry.get(name).parameter_schema if item.name == "column_features"),
            None,
        )
        assert parameter is not None, name
        assert parameter.type == "object"  # 前端按 JSON 文本框渲染，Agent 直接填 JSON
        assert parameter.default == {}
    # fitting 没有特征清单，所以刻意没有这个参数。
    assert not any(
        item.name == "column_features" for item in registry.get("feature.fitting").parameter_schema
    )


def test_component_runs_the_per_column_plan_end_to_end(registry, context, tmp_path):
    """走组件而不是直接调函数：Agent 通过 MCP 填的就是这份 JSON。"""
    frame = mixed_frame(rows=64)
    frame.to_csv(tmp_path / "mixed.csv", index=False)
    component = registry.create(
        "feature.statistical",
        parameters={
            "columns": ["vibration", "stack"],
            "group_column": "equipment",
            "window_size": 16,
            "features": ["mean", "std"],
            "column_features": {"stack": "distinct_count"},
        },
    )
    result = component.execute({"dataset": pd.read_csv(tmp_path / "mixed.csv")}, context)
    assert set(result.outputs["features"].columns) == {
        "vibration__mean",
        "vibration__std",
        "stack__distinct_count",
    }
