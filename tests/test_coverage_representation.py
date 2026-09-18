"""窗口来源行的表示形式：区间 + 零拷贝，但不能丢掉语义、也不能踩 pandas 的坑。

背景（108k 行的实测）：pandas 每次 `__finalize__` 都会**深拷贝 attrs**，而窗口特征的 attrs 里
带着"每个窗口用了哪些原始行"。以前是逐行号列表，3580 个窗口一次深拷贝 3.1ms，`visual.overview`
在特征表上触发四百多次 → 2 秒；`feature.imputation` 因为先调 `select_dtypes`（同样 finalize）也要 2.3 秒。
改成区间 + 不可变类型后是 0.0003ms。这组测试守住两件容易做错的事：**深拷贝要零成本**、
**`==` 要返回布尔值**（裸 numpy 数组会让 `pd.concat` 直接抛 "truth value is ambiguous"）。
"""

from __future__ import annotations

import copy
import pickle

import numpy as np
import pandas as pd
import pytest

from fault_core import features


def windowed(rows: int = 120, window: int = 30, step: int = 15, index=None):
    rng = np.random.default_rng(2)
    frame = pd.DataFrame(
        {
            "equipment": ["EQ-0"] * rows,
            "time": np.arange(rows),
            "signal": rng.normal(size=rows),
            "label": np.arange(rows) % 2,
        }
    )
    if index is not None:
        frame.index = index
    return features.extract_features(
        frame,
        ["signal"],
        group_column="equipment",
        label_column="label",
        time_column="time",
        window_size=window,
        step=step,
        label_policy="last",
    )["features"]


def test_coverage_is_stored_as_cheap_immutable_ranges():
    table = windowed()
    coverage = table.attrs["source_rows_ranges"]
    assert isinstance(coverage, features.CoverageRanges)
    # 零拷贝：pandas 深拷贝 attrs 时不会再复制几万个行号对象。
    assert copy.deepcopy(coverage) is coverage
    # 但语义没变：展开后仍是"每个窗口用了哪些原始行"。
    expanded = features.expand_coverage(table.attrs)
    assert expanded[0] == list(range(30))
    assert expanded[1] == list(range(15, 45))
    assert len(expanded) == len(table)
    # 序列化（产物溢写 / 检查点）要能往返。
    assert pickle.loads(pickle.dumps(coverage)) == coverage


def test_ranges_survive_pandas_concat_and_equality():
    """两个特征表要能 `pd.concat`——裸 numpy 数组在这里会抛 ambiguous truth value。"""
    left = windowed()
    right = windowed()
    right.columns = [f"{name}__b" for name in right.columns]
    merged = pd.concat([left, right], axis=1)
    assert len(merged) == len(left)
    # 相等比较必须返回布尔值，而不是元素级数组。
    assert (left.attrs["source_rows_ranges"] == right.attrs["source_rows_ranges"]) is True
    assert (left.attrs["source_rows_ranges"] == features.CoverageRanges([[0, 1]])) is False
    assert features.provenance_matches(left.attrs, right.attrs)
    assert features.values_equal(np.asarray([1, 2]), np.asarray([1, 2])) is True
    assert features.values_equal(None, None) is True
    assert features.values_equal(None, [1]) is False


def test_merge_still_validates_provenance(registry, context):
    """`feature.merge` 走的就是 concat + provenance 比较，这条链必须活着。"""
    table = windowed()
    other = windowed()
    other.columns = [f"{name}__other" for name in other.columns]
    merged = registry.create("feature.merge").execute({"left": table, "right": other}, context).outputs
    assert len(merged["features"]) == len(table)
    # 换了来源的表不能合：窗口键一样、覆盖的行不一样 → provenance 不一致。
    # 这一份还刻意用不连续行号（于是退回"行号列表"表示），顺带验证两种表示可以互相比。
    shifted = windowed(index=pd.Index(np.arange(120) * 3, name="row"))
    assert shifted.index.equals(table.index)
    assert "source_rows_ranges" not in shifted.attrs
    with pytest.raises(ValueError, match="provenance"):
        registry.create("feature.merge").execute({"left": table, "right": shifted}, context)


def test_non_contiguous_index_falls_back_to_row_lists():
    """行号不是连续整数时不能压成区间——必须老老实实退回行号列表。"""
    index = pd.Index(np.arange(120) * 3, name="row")  # 故意不连续
    table = windowed(index=index)
    assert "source_rows_ranges" not in table.attrs
    assert "source_rows" in table.attrs
    assert table.attrs["source_rows"][0] == list(index[:30])


def test_statistical_features_still_land_in_the_table():
    """加速不能改变数值：`_stat` 换成查表后逐位比对几种特征。"""
    rng = np.random.default_rng(5)
    window = rng.normal(size=64)
    expected = {
        "mean": float(np.mean(window)),
        "std": float(np.std(window)),
        "rms": float(np.sqrt(np.mean(window**2))),
        "count": 64.0,
        "distinct_count": float(len(np.unique(window))),
        "range": float(np.ptp(window)),
        "iqr": float(np.quantile(window, 0.75) - np.quantile(window, 0.25)),
    }
    for name, value in expected.items():
        assert features._stat(window, name, 0.75) == pytest.approx(value)
    assert np.isfinite(features._stat(np.zeros(8), "crest_factor", 0.75))
    with pytest.raises(ValueError, match="Unknown statistical feature"):
        features._stat(window, "not_a_feature", 0.75)
