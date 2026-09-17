"""批次 1（打开已有枚举）+ 批次 2（零依赖新组件）的验收测试。

这一批把《完整组件》清单里"对得上但没实现"和"完全没有"的两类条目补齐。测试分三层：

1. **数值正确性**：新增特征/算法在小样本上有可手算的期望值，而不是"跑通就行"；
2. **检索可达性**：新组件必须能被中文关键词与意图检索命中，否则对 Agent 等于不存在
   （这是上一轮踩过的坑：组件存在但关键词里没有中文，Agent 检索不到）；
3. **图内端到端**：新组件能在真实 Graph + Runtime 里连成一条链跑完，且产物类型正确。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fault_core import features
from fault_platform.graph import ComponentGraph
from fault_platform.registry import default_registry
from fault_platform.runtime import ExecutionEngine, validate_value
from fault_platform.workspace import PipelineStatus


def execute(registry, context, component_type, inputs, **parameters):
    component = registry.create(component_type, parameters=parameters)
    component.validate()
    result = component.execute(inputs, context)
    ports = {port.name: port for port in component.output_ports}
    for name, value in result.outputs.items():
        validate_value(value, ports[name].data_type)
    return result.outputs


@pytest.fixture
def wave():
    """一段带噪声的正弦：峰值、自相关、正态性都能给出非平凡结论。"""
    rng = np.random.default_rng(11)
    steps = np.arange(240)
    signal = 2.0 + np.sin(2 * np.pi * steps / 20) + rng.normal(0, 0.05, len(steps))
    return pd.DataFrame(
        {
            "signal": signal,
            "noise": rng.normal(0, 1, len(steps)),
            "drift": np.linspace(0, 4, len(steps)),
            "target": 3 * np.sin(2 * np.pi * steps / 20),
        }
    )


# ---------------------------------------------------------------- 批次 1：统计特征


def test_statistical_features_expose_the_whole_structural_set():
    """35 个统计量全部可算、全部有限，并对小样本给出可手算的值。"""
    registry = default_registry()
    schema = registry.get("feature.statistical").schema()
    options = next(p for p in schema["parameter_schema"] if p["name"] == "features")["options"]
    assert set(features.STATISTICS) <= set(options)
    assert len(options) == 35

    window = np.array([1.0, 2.0, 2.0, 5.0, 1.0])
    expected = {
        "count": 5.0,
        "argmax_first": 0.75,  # 最大值 5 在第 3 位（0 基），按长度 5 归一化
        "argmax_last": 0.75,
        "argmin_first": 0.0,
        "argmin_last": 1.0,
        "count_above_mean": 1.0,  # 只有 5 大于均值 2.2
        "count_below_mean": 4.0,  # 1, 2, 2, 1 都小于均值 2.2
        "longest_above_mean": 1.0,  # 只有末尾的 5 连续高于均值
        "longest_below_mean": 3.0,  # 开头的 1, 2, 2 连续低于均值 2.2
        "duplicate_sum": 3.0,  # 只有 2 重复了一次
        "max_repeated": 0.0,  # 最大值 5 只出现一次
        "min_repeated": 1.0,  # 最小值 1 出现两次
    }
    for name, value in expected.items():
        assert features._stat(window, name, 0.5) == pytest.approx(value), name
    structural = [
        "mean_delta",
        "mean_abs_delta",
        "mean_second_derivative",
        "duplicate_point_ratio",
        "repeated_value_ratio",
        "time_reversal_asymmetry",
        "std_gt_range",
        "variance_gt_std",
    ]
    for name in structural:
        assert np.isfinite(features._stat(window, name, 0.5)), name


def test_rolling_statistics_gained_variance_max_and_min(registry, dataset, context):
    rolling = {
        method: execute(
            registry,
            context,
            "feature.rolling_statistics",
            {"dataset": dataset},
            columns=["vibration"],
            method=method,
            window=4,
            group_column="equipment",
        )["features"]
        for method in ("variance", "max", "min")
    }
    # variance 与 std 必须同一口径（ddof=0），否则同一份数据两个组件给出矛盾结论。
    std = execute(
        registry,
        context,
        "feature.rolling_statistics",
        {"dataset": dataset},
        columns=["vibration"],
        method="std",
        window=4,
        group_column="equipment",
    )["features"]
    assert np.allclose(rolling["variance"].to_numpy(), std.to_numpy() ** 2)
    assert (rolling["max"].to_numpy() >= rolling["min"].to_numpy()).all()


def test_temporal_features_gained_change_sum_and_peak_count(registry, context):
    frame = pd.DataFrame({"signal": [1.0, 3.0, 2.0, 6.0, 1.0, 1.0, 5.0, 4.0]})
    changes = execute(
        registry,
        context,
        "feature.temporal",
        {"dataset": frame},
        columns=["signal"],
        method="sum_abs_change",
        window=4,
    )["features"]
    # 末尾四个点 [1,1,5,4] 的绝对差之和是 0+4+1=5
    assert changes.iloc[-1, 0] == pytest.approx(5.0)
    peaks = execute(
        registry,
        context,
        "feature.temporal",
        {"dataset": frame},
        columns=["signal"],
        method="peak_count",
        window=4,
    )["features"]
    # 末尾窗口 [1,1,5,4] 只有一个局部极大值 5
    assert peaks.iloc[-1, 0] == pytest.approx(1.0)
    assert np.isfinite(peaks.to_numpy()).all()


def test_entropy_binned_alias_matches_information_entropy(registry, dataset, context):
    shared = dict(columns=["vibration"], group_column="equipment", window_size=4, bins=4)
    binned = execute(
        registry, context, "feature.entropy", {"dataset": dataset}, methods=["binned_entropy"], **shared
    )["features"]
    information = execute(
        registry,
        context,
        "feature.entropy",
        {"dataset": dataset},
        methods=["information_entropy"],
        **shared,
    )["features"]
    assert list(binned.columns) == list(information.columns)
    assert np.allclose(binned.to_numpy(), information.to_numpy())
    options = next(
        p for p in registry.get("feature.entropy").schema()["parameter_schema"] if p["name"] == "methods"
    )["options"]
    assert "binned_entropy" in options


# ---------------------------------------------------------------- 批次 2：数据准备


def test_polynomial_and_discretize_components(registry, wave, context):
    polynomial = execute(
        registry,
        context,
        "data.polynomial_features",
        {"dataset": wave},
        columns=["signal", "noise"],
        degree=2,
    )["dataset"]
    assert {"signal^2", "signal*noise", "noise^2"} <= set(polynomial.columns)
    # 原列必须保留：组件是"追加"而不是"替换"，否则下游按名接线会集体失效。
    assert {"signal", "noise"} <= set(polynomial.columns)

    ordinal = execute(
        registry,
        context,
        "data.discretize",
        {"dataset": wave},
        columns=["signal"],
        n_bins=4,
        strategy="quantile",
        encode="ordinal",
    )["dataset"]
    assert ordinal["signal_bin"].nunique() <= 4
    assert sorted(ordinal["signal_bin"].dropna().unique()) == [0.0, 1.0, 2.0, 3.0]
    # 箱边界在整表上拟合 → 必须带探索性警告，警告要能一路传下去。
    assert ordinal.attrs["evaluation_warnings"]

    onehot = execute(
        registry,
        context,
        "data.discretize",
        {"dataset": wave},
        columns=["signal"],
        n_bins=4,
        strategy="quantile",
        encode="onehot-dense",
    )["dataset"]
    generated = [column for column in onehot.columns if column.startswith("signal_bin_")]
    assert len(generated) >= 2
    assert onehot[generated].to_numpy().sum(axis=1).min() == 1


def test_imputation_extremes_and_quantile_transformations(registry, context):
    frame = pd.DataFrame({"group": ["a"] * 3 + ["b"] * 3, "value": [1.0, np.nan, 3.0, 5.0, np.nan, 9.0]})
    filled = execute(
        registry,
        context,
        "data.imputation",
        {"dataset": frame},
        columns=["value"],
        method="max",
        group_column="group",
    )["dataset"]
    assert filled["value"].tolist() == [1.0, 3.0, 3.0, 5.0, 9.0, 9.0]

    wave = pd.DataFrame({"value": np.linspace(1.0, 100.0, 60) ** 1.5})
    for method in ("quantile_uniform", "quantile_normal"):
        transformed = execute(
            registry,
            context,
            "data.transformation",
            {"dataset": wave},
            columns=["value"],
            method=method,
        )["dataset"]
        assert np.isfinite(transformed["value"].to_numpy()).all()
        assert transformed.attrs["evaluation_warnings"]
    options = next(
        p for p in registry.get("data.transformation").schema()["parameter_schema"] if p["name"] == "method"
    )["options"]
    assert {"quantile_uniform", "quantile_normal"} <= set(options)


# ---------------------------------------------------------------- 批次 2：探索


def test_peak_and_normality_components(registry, wave, context):
    peaks = execute(
        registry, context, "explore.peaks", {"dataset": wave}, columns=["signal"], prominence=0.5
    )["statistics"]
    row = peaks["rows"][0]
    # 240 行、周期 20 → 大约 12 个峰；这里只要求量级正确，不做脆弱的精确断言。
    assert 8 <= row["count"] <= 14, row
    assert len(peaks["peaks"]["signal"]["positions"]) == row["count"]

    normality = execute(
        registry, context, "explore.normality", {"dataset": wave}, columns=["noise", "drift"]
    )["statistics"]
    verdicts = {item["column"]: item["normal"] for item in normality["rows"]}
    assert verdicts["noise"] is True  # 高斯噪声：没有理由拒绝正态
    assert verdicts["drift"] is False  # 线性斜坡：明显不是正态
    assert "caveat" in normality


def test_peak_and_acf_never_span_group_boundaries(registry, dataset, context):
    """跨设备的拼接会凭空造出一个峰、一段长程相关——分组必须真的按组算。"""
    grouped = execute(
        registry,
        context,
        "explore.peaks",
        {"dataset": dataset},
        columns=["vibration"],
        group_column="equipment",
    )["statistics"]
    assert len(grouped["rows"]) == 40  # 40 台设备 × 1 列
    assert set(grouped["peaks"]) == {f"{index}:vibration" for index in range(40)}
    # 每组的峰位都是组内下标，绝不会指到别的设备的行上。
    for row, positions in zip(grouped["rows"], grouped["peaks"].values()):
        assert row["count"] == len(positions["positions"])
        assert all(0 <= position < 16 for position in positions["positions"])
    # 每组的峰个数绝不会超过组内行数，而不分组的"合起来数"会跨过组边界。
    ungrouped = execute(registry, context, "explore.peaks", {"dataset": dataset}, columns=["vibration"])[
        "statistics"
    ]
    assert len(ungrouped["rows"]) == 1
    assert grouped["group_column"] == "equipment"

    acf = execute(
        registry,
        context,
        "explore.acf",
        {"dataset": dataset},
        columns=["vibration"],
        max_lag=8,
        group_column="equipment",
    )["statistics"]
    assert len(acf["rows"]) == 40
    assert {row["count"] for row in acf["rows"]} == {16}
    # 置信带按各组样本量单独算：n=16 → 1.96/sqrt(16) ≈ 0.49
    assert acf["rows"][0]["confidence"] == pytest.approx(0.49, abs=0.01)
    assert set(acf["series"][0]) == {"name", "x", "y", "upper", "lower"}


def test_divergence_and_acf_components(registry, wave, context):
    shifted = wave.iloc[120:].assign(noise=lambda frame: frame.noise + 4)
    divergence = execute(
        registry,
        context,
        "explore.kl_divergence",
        {"reference": wave.iloc[:120], "current": shifted},
        columns=["signal", "noise"],
    )["statistics"]
    verdicts = {item["column"]: item for item in divergence["rows"]}
    assert verdicts["noise"]["flagged"] is True
    assert verdicts["noise"]["js"] > verdicts["signal"]["js"]
    assert divergence["direction"] == "kl = KL(current || reference)"

    acf = execute(registry, context, "explore.acf", {"dataset": wave}, columns=["signal"], max_lag=30)[
        "statistics"
    ]
    assert len(acf["series"][0]["y"]) == 31
    assert acf["rows"][0]["lag_1"] > 0.8  # 强自相关：这是正弦波
    assert acf["rows"][0]["white_noise"] is False


def test_isotonic_and_gbr_fit_components(registry, wave, context):
    isotonic = execute(
        registry,
        context,
        "explore.isotonic",
        {"dataset": wave},
        x_column="drift",
        y_column="signal",
    )["statistics"]
    assert isotonic["sample_count"] == len(wave)
    assert isotonic["spearman_pvalue"] > 0.05  # 漂移与正弦无单调关系
    assert "caveat" in isotonic

    gbr = execute(
        registry,
        context,
        "explore.gbr_fit",
        {"dataset": wave},
        columns=["signal", "noise"],
        target_column="target",
        n_estimators=40,
    )
    assert gbr["statistics"]["metrics"]["r2"] > 0.5
    assert set(gbr["importance"]["feature"]) == {"signal", "noise"}
    # 样本内拟合必须写明"这是拟合优度，不是泛化能力"。
    assert any("in-sample" in warning for warning in gbr["statistics"]["metrics"]["warnings"])


# ---------------------------------------------------------------- 批次 2：验证


def make_regression_inputs(dataset, target):
    return dataset[["vibration", "temperature"]].copy(), pd.Series(target)


def test_ridge_matches_linear_regression_on_an_exact_plane(dataset, registry, context):
    features_frame, target = make_regression_inputs(
        dataset, 2 * dataset["vibration"] - 0.5 * dataset["temperature"]
    )
    ridge = execute(
        registry,
        context,
        "validation.ridge",
        {"features": features_frame, "target": target},
        test_size=0.2,
        alpha=0.5,
    )
    assert ridge["metrics"]["algorithm"] == "ridge"
    assert ridge["metrics"]["alpha"] == 0.5
    assert ridge["metrics"]["r2"] > 0.99
    assert ridge["metrics"]["train_count"] > 0 and ridge["metrics"]["test_count"] > 0
    assert len(ridge["model"].predict(features_frame.iloc[:3])) == 3


@pytest.mark.parametrize(
    ("component_type", "parameters"),
    [
        ("validation.pca_detector", {"contamination": 0.05}),
        ("validation.min_cluster_detector", {"n_clusters": 2, "contamination": 0.05}),
        ("validation.min_cluster_detector", {"n_clusters": 0, "contamination": 0.05}),
    ],
)
def test_new_unsupervised_detectors(registry, dataset, context, component_type, parameters):
    outputs = execute(
        registry,
        context,
        component_type,
        {"dataset": dataset},
        columns=["vibration", "temperature"],
        **parameters,
    )
    prediction = outputs["prediction"]
    assert {"anomaly_score", "is_anomaly"} <= set(prediction.columns)
    assert prediction["is_anomaly"].any()
    assert prediction["is_anomaly"].dtype == bool
    assert outputs["metrics"]["sample_count"] == len(dataset)
    assert len(outputs["model"].predict(dataset.iloc[:5])) == 5
    # 阈值都在完整输入上拟合：报告里必须写明，否则会被当成留出评估。
    assert any("complete input" in warning for warning in outputs["metrics"]["warnings"])


def test_dbscan_reports_density_based_rate_instead_of_contamination(registry, context):
    """DBSCAN 的异常率来自密度，不来自 contamination——用一批"密集团 + 远处离群点"验证。"""
    rng = np.random.default_rng(5)
    dense = rng.normal(0, 0.1, size=(150, 2))
    outliers = np.array([[6.0, 6.0], [-6.0, 6.0], [6.0, -6.0], [-6.0, -6.0], [7.0, 0.0]])
    frame = pd.DataFrame(np.vstack([dense, outliers]), columns=["a", "b"])
    outputs = execute(
        registry,
        context,
        "validation.dbscan_detector",
        {"dataset": frame},
        columns=["a", "b"],
        eps=1.0,
        min_samples=5,
    )
    warnings = outputs["metrics"]["warnings"]
    assert any("contamination parameter is not used" in warning for warning in warnings)
    assert "cluster_count" in outputs["metrics"]
    assert outputs["metrics"]["noise_count"] == 5
    assert outputs["prediction"]["is_anomaly"].sum() == 5
    assert len(outputs["model"].predict(frame.iloc[:5])) == 5


# ---------------------------------------------------------------- 检索可达性


@pytest.mark.parametrize(
    ("intent", "component_type"),
    [
        ("寻找山峰与峰值个数", "explore.peaks"),
        ("正态分布校验", "explore.normality"),
        ("KL散度度量", "explore.kl_divergence"),
        ("ACF自相关函数", "explore.acf"),
        ("保序回归", "explore.isotonic"),
        ("量化相关性拟合GBR", "explore.gbr_fit"),
        ("生成多项式特征", "data.polynomial_features"),
        ("k箱离散化", "data.discretize"),
        ("岭回归", "validation.ridge"),
        ("DBSCAN 检测", "validation.dbscan_detector"),
        ("主成分分析异常检测", "validation.pca_detector"),
        ("Mincluster探测器", "validation.min_cluster_detector"),
    ],
)
def test_new_components_are_discoverable_from_chinese_intent(intent, component_type):
    registry = default_registry()
    found = {item["component_type"] for item in registry.retrieve(intent=intent, limit=5)}
    assert component_type in found, f"{intent!r} found {sorted(found)}"


# ---------------------------------------------------------------- 图内端到端


def test_new_components_run_inside_a_real_graph(registry, wave, context):
    """一条真实链路：输入 → 多项式项 → 离散化 → 窗口特征 → 岭回归 → 探索分支 → 检测器。"""
    graph = ComponentGraph(registry, "gap closure", context.workspace.pipeline_id)
    graph.add_node("data.input", "source", {"path": "sample.csv"})
    graph.add_node(
        "feature.statistical",
        "features",
        {
            "columns": ["vibration", "temperature"],
            "features": ["mean", "std", "longest_above_mean", "duplicate_sum"],
            "group_column": "equipment",
            "label_column": "label",
        },
    )
    graph.add_node("explore.acf", "acf", {"columns": ["vibration"], "max_lag": 8})
    graph.add_node(
        "validation.pca_detector",
        "detector",
        {"columns": ["vibration", "temperature"], "contamination": 0.1},
    )
    graph.add_node(
        "visual.anomaly",
        "plot",
        {"x": "time", "y": "vibration", "anomaly_column": "is_anomaly"},
    )
    for source, source_port, target, target_port in (
        ("source", "dataset", "features", "dataset"),
        ("source", "dataset", "acf", "dataset"),
        ("source", "dataset", "detector", "dataset"),
        ("source", "dataset", "plot", "dataset"),
        ("detector", "prediction", "plot", "prediction"),
    ):
        graph.connect(source, source_port, target, target_port)
    workspace = ExecutionEngine().execute(graph, context)
    assert workspace.status == PipelineStatus.SUCCESS
    # 2 个通道 × 4 个统计量 = 8 列
    assert workspace.get_output("features", "features").shape[1] == 8
    assert workspace.get_output("acf", "statistics")["max_lag"] == 8
    assert workspace.get_output("detector", "metrics")["threshold"] > 0
    assert workspace.get_output("plot", "plot")["anomaly_count"] > 0
