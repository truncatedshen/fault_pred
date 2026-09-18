"""切分必须保证故障样本落在训练集**和**测试集里——否则指标是假象。

真实数据里故障不是均匀撒开的：HBM 原始 ECC 日志里 334 次 UER 只落在 9 台服务器上，其中
只有 2 台能切出合格窗口。按服务器随机整组留出时，测试集 155 个窗口里 **0 个正类**，
``accuracy=1.0``、``balanced_accuracy=1.0`` 全是没有信息量的满分。这一层就是钉住
"整组留出要按类别分层"与"时间切点可以在目标比例附近移动"这两条。

代价也写在这里：分层整组留出**不是**均匀随机抽组，而是为了可评估性刻意分层的；切点移动
会让留出集不再正好是 ``test_size``。两种情况都会在指标里留痕（``split_note`` 与 warnings）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fault_core import features, models


def equipment_frame(
    groups: int, rows_per_group: int, faulted: list[int], asset: str = "DC-1", prefix: str = "SRV"
) -> pd.DataFrame:
    """``groups`` 台设备各 ``rows_per_group`` 行；``faulted`` 里的设备整段故障。"""
    rng = np.random.default_rng(5)
    rows = groups * rows_per_group
    label = np.zeros(rows, dtype=int)
    for index in faulted:
        label[index * rows_per_group : (index + 1) * rows_per_group] = 1
    return pd.DataFrame(
        {
            "instance": np.repeat([f"{prefix}-{index:03d}" for index in range(groups)], rows_per_group),
            "datacenter": asset,
            "t": np.tile(np.arange(rows_per_group), groups),
            "fault": label,
            "sensor": rng.normal(label * 3.0, 0.5, rows),
        }
    )


def extracted(frame: pd.DataFrame, **kwargs) -> dict:
    return features.extract_features(
        frame,
        ["sensor"],
        group_column="instance",
        label_column="fault",
        window_size=10,
        step=10,
        **kwargs,
    )


def fault_counts(metrics: dict) -> tuple[int, int]:
    return metrics["train_class_counts"].get("1", 0), metrics["test_class_counts"].get("1", 0)


def side_values(data: dict, key: str, metrics: dict, side: str) -> set:
    """按 index 标签取回 ``attrs[key]`` —— 指标里给的是索引标签，不是行号。"""
    values = np.asarray(data["features"].attrs[key])
    positions = data["features"].index.get_indexer(metrics[f"{side}_indices"])
    return set(values[positions])


def test_group_split_puts_faults_on_both_sides() -> None:
    """只有少数几台设备故障时，整组留出仍然要让两侧都有正类。"""
    frame = equipment_frame(groups=9, rows_per_group=40, faulted=[2, 7])
    data = extracted(frame)
    metrics = models.validate_model(**data, split_method="group", test_size=0.25, n_estimators=20)["metrics"]
    train_faults, test_faults = fault_counts(metrics)
    assert train_faults > 0 and test_faults > 0
    # 整组留出：两侧的设备不重叠，且测试集规模贴近 test_size。
    assert not side_values(data, "groups", metrics, "train") & side_values(data, "groups", metrics, "test")
    assert 0.15 <= metrics["test_count"] / len(data["features"]) <= 0.40


def test_group_split_is_reproducible_and_seed_sensitive() -> None:
    """同种子逐位可复现；换种子要能给出不同的划分（否则 ``random_state`` 就是摆设）。"""
    data = extracted(equipment_frame(groups=9, rows_per_group=40, faulted=[2, 7]))
    partitions = {}
    for seed in range(6):
        metrics = models.validate_model(**data, split_method="group", random_state=seed, n_estimators=10)[
            "metrics"
        ]
        repeat = models.validate_model(**data, split_method="group", random_state=seed, n_estimators=10)[
            "metrics"
        ]
        assert metrics["test_indices"] == repeat["test_indices"]
        assert all(count > 0 for count in fault_counts(metrics))
        partitions[seed] = frozenset(metrics["test_indices"])
    assert len(set(partitions.values())) > 1


def test_asset_split_puts_faults_on_both_sides() -> None:
    """按资产留出同样分层：留出的数据中心一台故障都没有，等于没评估。"""
    frames = [
        equipment_frame(3, 40, faulted=[0], asset="DC-1", prefix="A"),
        equipment_frame(3, 40, faulted=[2], asset="DC-2", prefix="B"),
        equipment_frame(3, 40, faulted=[], asset="DC-3", prefix="C"),
        equipment_frame(3, 40, faulted=[], asset="DC-4", prefix="D"),
    ]
    frame = pd.concat(frames, ignore_index=True)
    data = extracted(frame, asset_column="datacenter")
    metrics = models.validate_model(**data, split_method="asset", test_size=0.25, n_estimators=20)["metrics"]
    train_faults, test_faults = fault_counts(metrics)
    assert train_faults > 0 and test_faults > 0
    assert not side_values(data, "assets", metrics, "train") & side_values(data, "assets", metrics, "test")


def test_a_single_faulted_group_stays_in_training_and_is_called_out() -> None:
    """只有一台设备发生过故障时无法两全：它必须留在训练集，测试集没有正类要明说。"""
    data = extracted(equipment_frame(groups=8, rows_per_group=40, faulted=[3]))
    metrics = models.validate_model(**data, split_method="group", test_size=0.25, n_estimators=20)["metrics"]
    train_faults, test_faults = fault_counts(metrics)
    assert train_faults > 0 and test_faults == 0
    assert "SRV-003" in side_values(data, "groups", metrics, "train")
    assert any("Holdout split has no 1 rows" in note for note in metrics["warnings"])
    assert any("no positive (1) rows" in note for note in metrics["warnings"])


def test_temporal_split_moves_the_boundary_so_the_holdout_has_faults() -> None:
    """时间切点按时间顺序不能重排，但可以挪：末段全是正常样本时往前挪到有故障的位置。"""
    rows = 400
    # 故障在第一段（前 100 行）与中段（第 150–260 行），末尾 140 行全正常：
    # 目标切点在 300（第 30 个窗口），那里切出来的测试集一个正类都没有。
    label = np.zeros(rows, dtype=int)
    label[:100] = 1
    label[150:260] = 1
    frame = pd.DataFrame(
        {
            "instance": "EQ-001",
            "t": np.arange(rows),
            "fault": label,
            "sensor": np.random.default_rng(9).normal(0, 1, rows),
        }
    )
    data = extracted(frame)
    metrics = models.validate_model(**data, split_method="temporal", test_size=0.25, n_estimators=20)[
        "metrics"
    ]
    train_faults, test_faults = fault_counts(metrics)
    assert train_faults > 0 and test_faults > 0
    assert metrics["split_note"] and "boundary moved" in metrics["split_note"]
    # 训练仍然全部在测试之前：挪的是切点，不是顺序。
    train_positions = set(data["features"].index.get_indexer(metrics["train_indices"]))
    test_positions = set(data["features"].index.get_indexer(metrics["test_indices"]))
    boundary = max(train_positions) + 1
    assert train_positions == set(range(boundary))
    assert test_positions == set(range(boundary, len(data["features"])))
    assert boundary in set(range(11, 40))


def test_temporal_split_keeps_the_target_when_no_cut_can_help() -> None:
    """挪到 50%～150% 之外才能凑齐两类时，宁可保留目标切点，并把这件事写进 split_note。"""
    rows = 240
    label = np.zeros(rows, dtype=int)
    label[:40] = 1  # 故障只在最前面：把切点挪到 40 行以内已经不是 25% 留出了
    frame = pd.DataFrame(
        {
            "instance": "EQ-001",
            "t": np.arange(rows),
            "fault": label,
            "sensor": np.random.default_rng(13).normal(0, 1, rows),
        }
    )
    data = extracted(frame)
    metrics = models.validate_model(**data, split_method="temporal", test_size=0.25, n_estimators=20)[
        "metrics"
    ]
    # 保留目标切点：24 个窗口里后 6 个是测试集，正好是 test_size=0.25。
    assert len(data["features"]) == 24
    assert metrics["test_count"] == 6
    assert fault_counts(metrics)[1] == 0
    assert metrics["split_note"] and "no cut point" in metrics["split_note"]
    assert any("no positive (1) rows" in note for note in metrics["warnings"])


def test_stratified_split_is_untouched_by_the_class_aware_path() -> None:
    """按行随机分层的场景本来就有两类，仍然走 sklearn 的 stratify，不引入整组语义。"""
    rng = np.random.default_rng(2)
    rows = 120
    # 前半故障、后半正常：每个窗口的标签都是纯的（strict 不接受混合标签的窗口）。
    label = np.zeros(rows, dtype=int)
    label[:60] = 1
    frame = pd.DataFrame({"instance": "EQ-001", "t": np.arange(rows), "fault": label})
    frame["sensor"] = rng.normal(label * 2.0, 0.5, rows)
    data = features.extract_features(
        frame, ["sensor"], group_column="instance", label_column="fault", window_size=6, step=6
    )
    with pytest.raises(ValueError, match="Repeated equipment groups require group or temporal split"):
        models.validate_model(**data, split_method="stratified", n_estimators=10)
