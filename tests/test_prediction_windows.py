"""预测型窗口：按时间切窗 + 未来视野标签（"用最近 7 天预测未来会不会故障"）。

这一层是检测与预测的分界线：检测问"现在是不是故障"，预测问"接下来这段时间会不会坏"。
因此标签必须来自窗口**之后**的数据，而且要有两个防泄漏的边界：

* 窗口自身的区间里已经故障的样本不属于预测任务（默认丢弃并计数）；
* 视野超出可用数据的样本不能标 0（"没看到故障"不等于"没有故障"），一律丢弃并计数。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fault_core import features

HOUR = 3600.0


def synthetic(days: int = 10, fault_start_hour: int | None = 168) -> pd.DataFrame:
    """每小时一行、共 ``days`` 天；``fault_start_hour`` 之后标签为 1。"""
    rows = days * 24
    hours = np.arange(rows)
    fault = np.zeros(rows, dtype=int)
    if fault_start_hour is not None:
        fault[hours >= fault_start_hour] = 1
    return pd.DataFrame(
        {
            "asset": "W-1",
            "t": hours * HOUR,
            "v": np.linspace(1.0, 2.0, rows) + np.sin(hours / 6.0),
            "fault": fault,
        }
    )


def episodic(days: int = 60, onset_days: tuple[int, ...] = (20, 40, 55), hours: int = 12) -> pd.DataFrame:
    """多事件长序列：真实预测任务里必须有好几个故障事件，才能既有正样本又有大量负样本。"""
    rows = days * 24
    hours_axis = np.arange(rows)
    fault = np.zeros(rows, dtype=int)
    for day in onset_days:
        start = day * 24
        fault[start : start + hours] = 1
    return pd.DataFrame(
        {
            "asset": "W-1",
            "t": hours_axis * HOUR,
            "v": np.linspace(1.0, 3.0, rows) + np.sin(hours_axis / 5.0),
            "fault": fault,
        }
    )


def test_horizon_labels_mark_what_the_future_holds() -> None:
    """2 天窗口 + 2 天视野：命中故障起始的那几个窗口标 1，自身已故障的窗口被丢弃。"""
    out = features.extract_features(
        synthetic(),
        ["v"],
        group_column="asset",
        label_column="fault",
        time_column="t",
        window_span="2d",
        step_span="1d",
        prediction_horizon="2d",
        label_policy="horizon",
    )
    features_frame, labels = out["features"], out["labels"]
    # 起点 0..5 天的六个窗口：6 天与 7 天起点落在故障区间内，按 drop 丢掉并计数。
    assert list(features_frame.index) == [
        "g0_t0",
        "g0_t86400",
        "g0_t172800",
        "g0_t259200",
        "g0_t345600",
        "g0_t432000",
    ]
    assert labels.tolist() == [0, 0, 0, 1, 1, 1]
    attrs = features_frame.attrs
    assert attrs["horizon_dropped_current_fault"] == 2
    assert attrs["horizon_dropped_unknown_future"] == 0
    assert attrs["window_span_seconds"] == 2 * 24 * HOUR
    assert attrs["prediction_horizon_seconds"] == 2 * 24 * HOUR
    assert attrs["overlapping"] is True  # 步长 < 跨度：验证器必须拒绝随机切分
    assert any("current_fault_policy" in notice for notice in attrs["warnings"])


def test_prediction_gap_pushes_the_horizon_away() -> None:
    """间隔带把视野整体推后：贴着故障起始的那个窗口因此不再算"预测到"。"""
    out = features.extract_features(
        synthetic(),
        ["v"],
        group_column="asset",
        label_column="fault",
        time_column="t",
        window_span="2d",
        step_span="1d",
        prediction_horizon="2d",
        prediction_gap="1d",
        label_policy="horizon",
    )
    labels = out["labels"]
    attrs = out["features"].attrs
    # 间隔带把视野整体推后：起点 2/3/4 天的窗口视野覆盖第 7 天，起点 0/1 天看不到。
    assert labels.tolist() == [0, 0, 1, 1, 1]
    # 起点 5 天的窗口视野要到第 10 天之后，数据不够，丢弃而不是标 0。
    assert attrs["horizon_dropped_unknown_future"] == 1
    assert attrs["horizon_dropped_current_fault"] == 2


def test_unknown_future_is_dropped_not_labelled_zero() -> None:
    """数据末尾那些"看不见未来"的窗口必须丢掉，而不是当成正常样本。"""
    out = features.extract_features(
        synthetic(),
        ["v"],
        group_column="asset",
        label_column="fault",
        time_column="t",
        window_span="2d",
        step_span="1d",
        prediction_horizon="7d",
        label_policy="horizon",
    )
    attrs = out["features"].attrs
    # 视野要 7 天：只有起点 0 天的窗口能完整看到未来，2..6 天起的五个窗口都缺未来数据。
    assert out["labels"].tolist() == [1]
    assert attrs["horizon_dropped_unknown_future"] == 5
    assert attrs["horizon_dropped_current_fault"] == 2
    assert any("超出了可用数据" in notice for notice in attrs["warnings"])


def test_time_windows_work_without_prediction_labels() -> None:
    """不预测的时候也可以按时间切窗：标签仍按窗口内聚合，只是行数不再固定。"""
    frame = synthetic(days=6, fault_start_hour=None)
    regular = features.extract_features(
        frame,
        ["v"],
        group_column="asset",
        label_column="fault",
        time_column="t",
        window_span="1d",
        label_policy="mode",
    )
    # 6 天数据、1 天窗口、不重叠：窗口起点为 0..5 天；末尾那个窗口的视野之外没有数据，
    # 但取窗口内标签不需要未来，所以不会被丢。
    assert len(regular["features"]) == 5
    assert set(regular["labels"]) == {0}

    # 采样不规则（每小时一条，但后半段只留每 3 小时一条）：窗口行数随之不同。
    uneven = frame[frame.index % 3 == 0].reset_index(drop=True)
    uneven["t"] = frame["t"].iloc[::3].to_numpy()
    sparse = features.extract_features(
        uneven,
        ["v"],
        group_column="asset",
        label_column="fault",
        time_column="t",
        window_span="1d",
        label_policy="mode",
    )
    counts = [len(rows) for rows in sparse["features"].attrs["source_rows"]]
    assert len(set(counts)) == 1  # 每 3 小时一条时仍然是等间隔
    assert len(sparse["features"]) == 5


def test_prediction_pipeline_runs_end_to_end(tmp_path) -> None:
    """整条链路：时间窗口 + 未来视野标签 → 随机森林，图能建、能校验、能跑出指标。"""
    from fault_platform.graph import ComponentGraph
    from fault_platform.registry import default_registry
    from fault_platform.runtime import ExecutionContext, ExecutionEngine
    from fault_platform.workspace import FaultWorkspace, PipelineStatus

    csv = tmp_path / "onset.csv"
    episodic().to_csv(csv, index=False)
    graph = ComponentGraph(default_registry(), "预测", "prediction_pipeline")
    graph.add_node("data.input", "src", {"path": "onset.csv"})
    graph.add_node(
        "feature.statistical",
        "stats",
        {
            "columns": ["v"],
            "group_column": "asset",
            "label_column": "fault",
            "time_column": "t",
            "window_span": "2d",
            "step_span": "1d",
            "prediction_horizon": "2d",
            "label_policy": "horizon",
            "features": ["mean", "std", "rms"],
        },
    )
    # 时间窗口步长小于跨度 → 窗口重叠，验证器因此只接受 group/asset/temporal 切分。
    graph.add_node("validation.random_forest", "forest", {"n_estimators": 25, "split_method": "temporal"})
    graph.connect("src", "dataset", "stats", "dataset")
    graph.connect("stats", "features", "forest", "features")
    graph.connect("stats", "labels", "forest", "labels")
    assert graph.validate_graph() == []

    ws = ExecutionEngine().execute(graph, ExecutionContext(FaultWorkspace("prediction_pipeline"), tmp_path))
    assert ws.status == PipelineStatus.SUCCESS
    labels = ws.get_output("stats", "labels")
    assert set(labels.unique()) == {0, 1}  # 预测目标里正负类都在
    metrics = ws.get_output("forest", "metrics")
    assert "accuracy" in metrics
    features_frame = ws.get_output("stats", "features")
    assert features_frame.attrs["window_span_seconds"] == 2 * 24 * HOUR
    assert features_frame.attrs["prediction_horizon_seconds"] == 2 * 24 * HOUR
    assert features_frame.attrs["horizon_dropped_current_fault"] > 0  # 事件窗口交给检测任务
    assert len(features_frame) >= 8


def test_prediction_parameters_are_validated_loudly() -> None:
    """参数组合写错时必须报错，而不是静默退化成别的窗口语义。"""
    base = {"columns": ["v"], "group_column": "asset", "label_column": "fault", "time_column": "t"}
    frame = synthetic()
    with pytest.raises(ValueError, match="cannot both be set"):
        features.extract_features(frame, window_span="7d", window_size=10, **base)
    with pytest.raises(ValueError, match="step_span needs window_span"):
        features.extract_features(frame, step_span="1d", **base)
    with pytest.raises(ValueError, match="need label_policy=horizon"):
        features.extract_features(frame, window_span="2d", prediction_horizon="2d", **base)
    with pytest.raises(ValueError, match="needs time_column"):
        features.extract_features(
            frame,
            columns=["v"],
            group_column="asset",
            label_column="fault",
            window_span="2d",
            prediction_horizon="2d",
            label_policy="horizon",
        )
    with pytest.raises(ValueError, match="needs window_span"):
        features.extract_features(frame, prediction_horizon="2d", label_policy="horizon", **base)
    with pytest.raises(ValueError, match="must look like"):
        features.extract_features(frame, window_span="seven days", **base)
    with pytest.raises(ValueError, match="whole group and its future"):
        features.extract_features_stream(
            [frame],
            columns=["v"],
            group_column="asset",
            label_column="fault",
            time_column="t",
            window_span="2d",
            prediction_horizon="2d",
            label_policy="horizon",
        )


def test_string_labels_and_normal_label() -> None:
    """标签是字符串时也能用：``normal_label`` 按标签列的真实类型对齐。"""
    frame = synthetic(days=10)
    frame["state"] = np.where(frame["fault"] == 1, "fault", "ok")
    out = features.extract_features(
        frame,
        ["v"],
        group_column="asset",
        label_column="state",
        time_column="t",
        window_span="2d",
        step_span="1d",
        prediction_horizon="2d",
        label_policy="horizon",
        normal_label="ok",
    )
    labels = out["labels"].tolist()
    assert set(labels) <= {0, 1}
    assert 1 in labels


def test_feature_row_count_follows_the_window_and_step() -> None:
    """窗口大小与步长决定特征行数——不是"每个采样点一行"。

    这条钉住的是一个容易读错的语义：输入是逐行采样，输出是逐窗口；
    每组窗口数约为"组内时长 / 步长"（最后一个不完整的窗口丢弃）。
    """
    frame = synthetic(days=10, fault_start_hour=None)  # 240 行
    last_hour = len(frame) - 1
    for step_hours in (24, 48, 120):
        span_hours = 48
        want = len([h for h in range(0, len(frame), step_hours) if h + span_hours <= last_hour])
        out = features.extract_features(
            frame,
            ["v"],
            group_column="asset",
            label_column="fault",
            time_column="t",
            window_span="2d",
            step_span=f"{step_hours}h",
            label_policy="mode",
        )
        rows = out["features"].shape[0]
        assert rows == want, f"step {step_hours}h: got {rows}, want {want}"
        # 行数远小于输入行数，且与输入行数无关：240 行输入最多产出 8 行特征。
        assert rows <= 8 < len(frame)
        assert len(out["labels"]) == rows
