"""疏密不均的数据 + 按时间切窗：窗口不会空，但会"很薄"。

平台的锚点是**组内真实采样**（`begin = seconds[start]`），所以按时间切窗**永远不会**切出空窗口；
稀疏期真正的病是"一个窗口只覆盖一两条采样"——那里的 std/skewness 没有信息。
默认只计数 + 警告（不改变样本数），设了 `min_window_rows` 才丢弃。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fault_core import features, sequence_features
from fault_platform.registry import default_registry


def gapped_frame(has_gap: bool = True) -> pd.DataFrame:
    """一段稠密数据 +（可选）一个孤立采样 + 远处的一小段数据。"""
    times = list(range(0, 3600, 20))
    if has_gap:
        times += [20000] + [40000, 40020, 40040, 40060]
    else:
        times += list(range(3600, 7600, 20))
    return pd.DataFrame(
        {
            "eq": ["A"] * len(times),
            "t": times,
            "v": np.arange(len(times), dtype=float),
            "label": [0] * len(times),
        }
    )


def extract(frame, **overrides):
    options = dict(
        columns=["v"],
        group_column="eq",
        time_column="t",
        label_column="label",
        window_span="2h",
        step_span="30m",
        label_policy="last",
    )
    options.update(overrides)
    return features.extract_features(frame, **options)


def test_windows_are_never_empty_even_with_a_huge_gap():
    outputs = extract(gapped_frame())
    coverage = features.expand_coverage(outputs["features"].attrs)
    assert coverage  # 有窗口
    assert min(len(rows) for rows in coverage) >= 1  # 锚点即采样，所以永不为空
    # 锚点写进 window_id 里，可以一眼看出这个窗口是从哪一刻开始的。
    assert all(key.split("_t")[1].isdigit() for key in outputs["features"].index)


def test_thin_windows_are_counted_and_warned_about_by_default():
    outputs = extract(gapped_frame())
    attrs = outputs["features"].attrs
    counts = [len(rows) for rows in features.expand_coverage(attrs)]
    assert min(counts) == 1  # 那个孤立采样自成一个窗口
    assert attrs["window_thin_windows"] == 1
    assert attrs["window_dropped_thin"] == 0  # 默认不丢样本
    assert attrs["window_min_rows"] is None
    assert any("只有不到 3 行" in note for note in attrs["warnings"])


def test_min_window_rows_drops_them_instead_of_just_complaining():
    outputs = extract(gapped_frame(), min_window_rows=3)
    attrs = outputs["features"].attrs
    counts = [len(rows) for rows in features.expand_coverage(attrs)]
    assert min(counts) >= 3
    assert attrs["window_min_rows"] == 3
    assert attrs["window_dropped_thin"] == 1
    assert attrs["window_thin_windows"] == 0
    assert any("少于 min_window_rows=3" in note for note in attrs["warnings"])


def test_dense_data_stays_quiet():
    outputs = extract(gapped_frame(has_gap=False))
    attrs = outputs["features"].attrs
    assert attrs["window_thin_windows"] == 0
    assert not any("只有不到" in note for note in attrs.get("warnings", []))
    # 稠密时步长说了算：相邻锚点间隔就是请求的 30 分钟。
    gaps = np.diff([int(key.split("_t")[1]) for key in outputs["features"].index])
    assert (gaps == 1800).all()


def test_min_window_rows_rejects_nonsense_values():
    for bad in (-1, 2.5, True):
        with pytest.raises(ValueError, match="min_window_rows"):
            extract(gapped_frame(), min_window_rows=bad)


def test_every_window_producer_exposes_min_window_rows():
    registry = default_registry()
    for name in ("feature.statistical", "feature.fitting", "feature.spectral", "feature.entropy"):
        parameter = next(
            item for item in registry.get(name).parameter_schema if item.name == "min_window_rows"
        )
        assert parameter.type == "integer" and parameter.default == 0 and parameter.min == 0


def test_spectral_and_entropy_accept_it_too():
    frame = gapped_frame()
    frame["label"] = 0
    spectral = features.spectral(
        frame,
        ["v"],
        sampling_rate=1.0,
        group_column="eq",
        time_column="t",
        label_column="label",
        window_size=64,
        step=32,
        min_window_rows=3,
    )
    assert spectral["features"].attrs["window_min_rows"] == 3
    result = sequence_features.entropy_features(
        frame,
        ["v"],
        group_column="eq",
        time_column="t",
        label_column="label",
        window_size=64,
        step=32,
        min_window_rows=3,
    )
    assert result["features"].attrs["window_min_rows"] == 3


def test_streaming_accepts_the_same_knob(tmp_path):
    """流式只支持按行切窗，薄窗口通常不出现；但参数必须被接受且口径一致。"""
    from fault_platform.streaming import StreamedDataset

    frame = gapped_frame()
    path = tmp_path / "gapped.csv"
    frame.to_csv(path, index=False)
    outputs = features.extract_features_stream(
        StreamedDataset(path=path, chunk_rows=200).chunks(),
        ["v"],
        group_column="eq",
        time_column="t",
        label_column="label",
        window_size=64,
        step=32,
        min_window_rows=3,
    )
    assert outputs["features"].attrs["window_min_rows"] == 3
    assert outputs["features"].attrs["window_dropped_thin"] == 0  # 按行切窗不会产生薄窗口
