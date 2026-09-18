"""标识列（地址／编号）不能当物理量求均值，但"这一窗里出现过几种"是真实问题。

对应 `feature.statistical` 的 `distinct_count`：它把"错误落到多少个不同的 bank / col 上"
变成一个可算的量，而不是把地址当坐标算 mean/std。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fault_core import features


def address_frame() -> pd.DataFrame:
    """两个实体，各 24 行；标识列只在少数几个取值间跳，物理量连续。"""
    rows = []
    for entity, values in enumerate(([3, 3, 3, 3, 4, 4, 5, 5], [7, 7, 9, 9, 9, 9, 9, 9])):
        for window in range(3):
            for index, stack in enumerate(values):
                rows.append(
                    {
                        "entity": f"EQ-{entity}",
                        "time": window * 8 + index,
                        "stack": stack,
                        "col": (window * 4 + index) % 6,
                        "voltage": 1.0 + 0.1 * index,
                    }
                )
    return pd.DataFrame(rows)


def test_distinct_count_counts_categories_not_rows():
    extracted = features.extract_features(
        address_frame(),
        ["stack", "voltage"],
        group_column="entity",
        window_size=8,
        features=["count", "distinct_count"],
    )
    table = extracted["features"]
    assert len(table) == 6  # 两个实体 × 三个窗口
    # 行数永远是窗口长度；不同取值个数才是"窗口里有几类"。
    assert (table["stack__count"] == 8).all()
    # 实体 0 的窗口里有 {3,4,5} 三类，实体 1 的窗口里只有 {7,9} 两类。
    assert set(table["stack__distinct_count"]) == {3.0, 2.0}
    # 连续物理量的取值基本两两不同——这也说明 distinct_count 只该用在标识列上。
    assert (table["voltage__distinct_count"] == 8).all()


def test_distinct_count_is_the_count_form_of_duplicate_point_ratio():
    extracted = features.extract_features(
        address_frame(),
        ["stack"],
        group_column="entity",
        window_size=8,
        features=["count", "distinct_count", "duplicate_point_ratio"],
    )
    table = extracted["features"]
    derived = table["stack__count"] * (1.0 - table["stack__duplicate_point_ratio"])
    assert np.allclose(derived, table["stack__distinct_count"])


def test_distinct_count_survives_the_streaming_path(tmp_path):
    """流式与批量必须逐位一致——这条对新特征同样成立。"""
    path = tmp_path / "addresses.csv"
    address_frame().to_csv(path, index=False)
    options = dict(
        columns=["stack"],
        group_column="entity",
        window_size=8,
        features=["count", "distinct_count"],
    )
    batch = features.extract_features(pd.read_csv(path), **options)["features"]
    from fault_platform.streaming import StreamedDataset

    streamed = features.extract_features_stream(
        StreamedDataset(path=path, chunk_rows=100).chunks(), **options
    )["features"]
    assert np.allclose(batch.to_numpy(), streamed.to_numpy())
    assert list(batch.columns) == list(streamed.columns)


def test_schema_exposes_the_new_feature(registry):
    """组件 schema 是 Agent 唯一的"能填什么"来源，新特征必须出现在里面。"""
    parameter = next(
        item for item in registry.get("feature.statistical").parameter_schema if item.name == "features"
    )
    assert "distinct_count" in parameter.options
    assert len(features.STATISTICS) == len(parameter.options) == 36


@pytest.mark.parametrize("feature", ["distinct_count"])
def test_distinct_count_is_finite_on_a_constant_window(feature):
    frame = pd.DataFrame({"entity": ["A"] * 8, "time": range(8), "stack": [4] * 8, "voltage": [2.0] * 8})
    table = features.extract_features(
        frame, ["stack", "voltage"], group_column="entity", window_size=8, features=[feature]
    )["features"]
    assert np.isfinite(table.to_numpy()).all()
    assert (table["stack__distinct_count"] == 1).all()
