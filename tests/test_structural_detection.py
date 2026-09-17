"""批次 3（时序结构变化检测）+ 批次 4（无 torch 的其余缺口）的验收测试。

两个批次共同的风险是"看起来有结论、其实口径没说清"，所以测试重点不在"跑通"，而在：

1. **判定口径可核对**：在人造数据上必须抓到预先埋进去的变化，并且报出的
   ``scored_count``/阈值/变化点位置要对得上；
2. **不跨分组边界**：把阶跃正好放在两组的拼接处，按组检测**必须**不报警；
3. **诚实性**：算不出来的位置留空（NaN）而不是填 0；可选依赖缺失时给可执行的安装提示；
4. **可检索**：19 个新组件都要能被中文意图找到，否则对 Agent 等于不存在。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

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
def series_frame():
    """300 行、埋了五种变化的合成序列；组边界正好落在阶跃处（第 150 行）。"""
    rng = np.random.default_rng(3)
    count = 300
    steps = np.arange(count)
    shift = rng.normal(0, 1.0, count)
    shift[150:] += 5.0
    volatility = rng.normal(0, 1.0, count)
    volatility[150:] *= 4
    seasonal = 3 * np.sin(2 * np.pi * steps / 24) + rng.normal(0, 0.3, count)
    seasonal[200:] += 8
    autoregressive = np.zeros(count)
    autoregressive[0] = 1.0
    for index in range(1, count):
        autoregressive[index] = 0.7 * autoregressive[index - 1] + rng.normal(0, 0.1)
    autoregressive[200:] = rng.normal(0, 2.0, count - 200)
    spike = rng.normal(0, 1, count)
    spike[[30, 120, 250]] = 40.0
    return pd.DataFrame(
        {
            "shift": shift,
            "volatility": volatility,
            "seasonal": seasonal,
            "autoregressive": autoregressive,
            "spike": spike,
            "group": np.repeat(["a", "b"], count // 2),
        }
    )


# ------------------------------------------------------------- 批次 3：七个检测器


def test_level_shift_finds_the_planted_step(registry, series_frame, context):
    outputs = execute(
        registry,
        context,
        "validation.level_shift_detector",
        {"dataset": series_frame},
        column="shift",
        window=20,
        threshold=4.0,
    )
    metrics = outputs["metrics"]
    # 阶跃在第 150 行；窗口 20 → 可评分位置是 20..280，共 261 行。
    assert metrics["scored_count"] == 261
    assert metrics["anomaly_count"] > 0
    flagged = outputs["prediction"].index[outputs["prediction"]["is_anomaly"].to_numpy()]
    assert min(flagged) >= 140 and max(flagged) <= 165, (min(flagged), max(flagged))
    assert outputs["prediction"]["anomaly_score"].iloc[:20].isna().all()
    assert len(outputs["model"].predict(series_frame)) == len(series_frame)


def test_volatility_shift_score_is_in_z_units(registry, series_frame, context):
    outputs = execute(
        registry,
        context,
        "validation.volatility_shift_detector",
        {"dataset": series_frame},
        column="volatility",
        window=20,
        threshold=4.0,
    )
    # 标准差放大 4 倍 = 方差放大 16 倍 → 对数比 ≈ 2.77；原假设抽样标准差 sqrt(2/19) ≈ 0.324
    # → z ≈ 8.5。这也说明为什么分数要按原假设归一化：不归一化就没人知道 2.77 算不算大。
    peak = float(outputs["prediction"]["anomaly_score"].max())
    assert 7.0 < peak < 11.0, peak
    assert outputs["metrics"]["anomaly_count"] > 0


def test_seasonal_and_autoregression_detectors(registry, series_frame, context):
    seasonal = execute(
        registry,
        context,
        "validation.seasonal_detector",
        {"dataset": series_frame},
        column="seasonal",
        period=24,
        threshold=4.0,
    )
    # 参考段之外的季节剖面偏离必须被标出来，且强度要报出来。
    assert seasonal["metrics"]["anomaly_count"] > 0
    # seasonal_strength 只在参考段上算（那里剖面才成立），所以一次后期阶跃不会把它压低。
    assert 0.5 < seasonal["metrics"]["seasonal_strength"] <= 1.0
    assert seasonal["metrics"]["reference_rows"] == 150
    rows = series_frame.index[seasonal["prediction"]["is_anomaly"].to_numpy()]
    # 参考段里偶发的 4 倍稳健尺度越界本来就会发生（150 行里约 0.6 行）——所以判定不看
    # "有没有误报"，而看"变化之后是否整段被标出来"：第 200 行起的那 100 行必须几乎全中。
    assert (rows >= 200).sum() >= 90
    assert (rows < 150).sum() <= 5

    autoregression = execute(
        registry,
        context,
        "validation.autoregression_detector",
        {"dataset": series_frame},
        column="autoregressive",
        order=2,
        threshold=4.0,
    )
    assert autoregression["metrics"]["scored_count"] == 150
    assert autoregression["prediction"]["anomaly_score"].iloc[:150].isna().all()
    # 后半段被换成白噪声，AR 残差应当显著变大。
    assert autoregression["metrics"]["anomaly_count"] > 10


def test_esd_finds_exactly_the_planted_spikes(registry, series_frame, context):
    outputs = execute(
        registry,
        context,
        "validation.esd_detector",
        {"dataset": series_frame},
        column="spike",
        alpha=0.05,
    )
    assert outputs["metrics"]["outlier_count"] == 3
    assert outputs["metrics"]["outliers"] == [30, 120, 250]
    assert outputs["prediction"]["is_anomaly"].sum() == 3


def test_nsigma_modes_and_mean_drift_cusum(registry, series_frame, context):
    global_mode = execute(
        registry,
        context,
        "validation.nsigma_detector",
        {"dataset": series_frame},
        column="shift",
        sigma=3.0,
        mode="global",
    )
    rolling = execute(
        registry,
        context,
        "validation.nsigma_detector",
        {"dataset": series_frame},
        column="shift",
        sigma=3.0,
        mode="rolling",
        window=30,
    )
    # 全局口径的中心/尺度被整段阶跃拉偏，因此两种口径的结论本来就不一样——
    # 这正是必须把 mode 写进 metrics 的原因。
    assert global_mode["metrics"]["mode"] == "global"
    assert "center" in global_mode["metrics"]
    assert rolling["metrics"]["mode"] == "rolling"
    assert rolling["metrics"]["scored_count"] < len(series_frame)
    assert rolling["metrics"]["anomaly_count"] > 0

    drift = execute(
        registry,
        context,
        "validation.mean_drift_detector",
        {"dataset": series_frame},
        column="shift",
        reference_fraction=0.3,
        slack=0.5,
        decision=5.0,
    )
    assert drift["metrics"]["reference_rows"] == 90
    assert drift["metrics"]["first_alarm"] is not None
    assert drift["metrics"]["first_alarm_index"] == str(drift["metrics"]["first_alarm"])


def test_change_detectors_never_cross_group_boundaries(registry, series_frame, context):
    """阶跃正好落在 a 组 / b 组的拼接处：按组检测必须一次都不报。"""
    outputs = execute(
        registry,
        context,
        "validation.level_shift_detector",
        {"dataset": series_frame},
        column="shift",
        window=20,
        threshold=4.0,
        group_column="group",
    )
    assert outputs["metrics"]["group_column"] == "group"
    assert outputs["metrics"]["anomaly_count"] == 0
    # 每组 150 行、窗口 20 → 每组 111 个可评分位置，两组共 222。
    assert outputs["metrics"]["scored_count"] == 222


def test_change_detection_rejects_impossible_settings(registry, series_frame, context):
    with pytest.raises(ValueError):
        execute(
            registry,
            context,
            "validation.level_shift_detector",
            {"dataset": series_frame.iloc[:30]},
            column="shift",
            window=20,
        )
    with pytest.raises(ValueError):
        execute(
            registry,
            context,
            "validation.nsigma_detector",
            {"dataset": series_frame},
            column="shift",
            mode="group",
        )


# ------------------------------------------------------------- 批次 4：序列分析


def test_hp_filter_separates_trend_and_cycle(registry, context):
    steps = np.arange(400)
    rng = np.random.default_rng(7)
    frame = pd.DataFrame(
        {
            "trend": 0.05 * steps,
            "pure_cycle": 2 * np.sin(2 * np.pi * steps / 24) + rng.normal(0, 0.2, 400),
        }
    )
    trend = execute(registry, context, "explore.hp_filter", {"dataset": frame}, column="trend", lamb=1600.0)[
        "statistics"
    ]
    # 线性趋势应当被完整还原（斜率 0.05），周期项只剩噪声水平。
    assert trend["rows"][0]["trend_slope"] == pytest.approx(0.05, abs=0.005)
    assert trend["rows"][0]["cycle_share"] < 0.05
    assert trend["lambda"] == 1600.0
    assert "caveat" in trend

    cycle = execute(registry, context, "explore.hp_filter", {"dataset": frame}, column="pure_cycle")[
        "statistics"
    ]
    # 周期 24、λ=1600 时约 3/4 的方差归到周期项——不是"全部"，因为 λ 是个选择。
    assert 0.6 < cycle["rows"][0]["cycle_share"] < 0.9


def test_stationarity_separates_noise_from_a_random_walk(registry, context):
    rng = np.random.default_rng(7)
    walk = np.cumsum(rng.normal(0, 1, 400))
    frame = pd.DataFrame({"noise": rng.normal(0, 1, 400), "walk": walk})
    noise = execute(registry, context, "explore.stationarity", {"dataset": frame}, column="noise")[
        "statistics"
    ]
    random_walk = execute(registry, context, "explore.stationarity", {"dataset": frame}, column="walk")[
        "statistics"
    ]
    assert noise["rows"][0]["stationary_at_5pct"] is True
    assert random_walk["rows"][0]["stationary_at_5pct"] is False
    # 不报 p 值是有意的：只给统计量与三档渐近临界值。
    assert "p_value" not in noise["rows"][0]
    assert "caveat" in noise


def test_dtw_and_sbd_recover_a_known_shift(registry, context):
    steps = np.arange(300)
    reference = 3 * np.sin(2 * np.pi * steps / 24)
    rng = np.random.default_rng(7)
    frame = pd.DataFrame(
        {
            "reference": reference,
            "shifted": np.roll(reference, 5),
            "unrelated": rng.normal(0, 1, 300),
        }
    )
    similar = execute(
        registry,
        context,
        "explore.dtw",
        {"dataset": frame},
        first_column="reference",
        second_column="shifted",
    )["statistics"]
    different = execute(
        registry,
        context,
        "explore.dtw",
        {"dataset": frame},
        first_column="reference",
        second_column="unrelated",
    )["statistics"]
    assert similar["distance"] < different["distance"] / 10
    assert similar["path_length"] > 0

    sbd_similar = execute(
        registry,
        context,
        "explore.sbd",
        {"dataset": frame},
        first_column="reference",
        second_column="shifted",
    )["statistics"]
    sbd_different = execute(
        registry,
        context,
        "explore.sbd",
        {"dataset": frame},
        first_column="reference",
        second_column="unrelated",
    )["statistics"]
    # SBD 应当精确还原"整体平移 5 格"，而且距离有界（0 就是形状相同）。
    assert sbd_similar["best_lag"] == 5
    assert sbd_similar["distance"] < 0.05
    assert sbd_different["distance"] > 0.5


def test_slope_cosine_flags_opposite_movement(registry, context):
    steps = np.arange(300)
    frame = pd.DataFrame(
        {
            "up": 3 * np.sin(2 * np.pi * steps / 24),
            "down": -3 * np.sin(2 * np.pi * steps / 24),
        }
    )
    outputs = execute(
        registry,
        context,
        "explore.slope_cosine",
        {"dataset": frame},
        first_column="up",
        second_column="down",
        window=12,
    )["prediction"]
    assert set(outputs.columns) == {"up__slope", "down__slope", "cosine", "opposite"}
    assert float(outputs["cosine"].mean()) < -0.5
    assert bool(outputs["opposite"].any())
    assert len(outputs) == len(frame)


def test_seasonal_difference_and_wavelet_features(registry, context):
    steps = np.arange(240)
    frame = pd.DataFrame(
        {
            "level": 10 + 2 * np.sin(2 * np.pi * steps / 24),
            "signal": 2 * np.sin(2 * np.pi * steps / 24),
        }
    )
    differenced = execute(
        registry,
        context,
        "data.seasonal_difference",
        {"dataset": frame},
        columns=["level"],
        period=24,
        mode="difference",
    )["dataset"]
    assert "level__seasonal_diff_24" in differenced
    # 纯周期序列做同期差分后应当接近常数 0。
    assert differenced["level__seasonal_diff_24"].dropna().abs().max() < 1e-6
    # 每组前 period 行没有可比对象：默认保留为 NaN 并写警告，而不是悄悄删行。
    assert differenced["level__seasonal_diff_24"].isna().sum() == 24
    assert len(differenced) == len(frame)

    ratio = execute(
        registry,
        context,
        "data.seasonal_difference",
        {"dataset": frame},
        columns=["level"],
        period=24,
        mode="ratio",
        keep_original=False,
    )["dataset"]
    assert ratio["level__seasonal_ratio_24"].dropna().sub(1.0).abs().max() < 1e-9

    wavelet = execute(
        registry,
        context,
        "feature.wavelet",
        {"dataset": frame},
        columns=["signal"],
        window=32,
        levels=3,
    )["features"]
    assert len(wavelet) == len(frame)
    assert {
        "signal__wavelet_energy_l1",
        "signal__wavelet_energy_l2",
        "signal__wavelet_energy_l3",
        "signal__wavelet_dominant_level",
        "signal__wavelet_peak_count",
    } <= set(wavelet.columns)
    shares = wavelet[["signal__wavelet_energy_l1", "signal__wavelet_energy_l2", "signal__wavelet_energy_l3"]]
    assert np.allclose(shares.dropna().sum(axis=1), 1.0)
    assert wavelet["signal__wavelet_peak_count"].iloc[:31].isna().all()
    assert wavelet["signal__wavelet_peak_count"].dropna().mean() > 0


# ------------------------------------------------------------- 批次 4：模型


def test_exponential_smoothing_scores_and_predicts(registry, context):
    steps = np.arange(240)
    rng = np.random.default_rng(1)
    trend_only = pd.DataFrame({"signal": 3 + 0.08 * steps + rng.normal(0, 0.2, 240)})
    holt = execute(
        registry,
        context,
        "validation.exponential_smoothing",
        {"dataset": trend_only},
        column="signal",
        method="holt",
    )
    assert holt["metrics"]["r2"] > 0.9
    assert len(holt["model"].predict(3)) == 3
    assert holt["metrics"]["refit_on_full"] is True
    assert any("coefficients are inputs" in warning for warning in holt["metrics"]["warnings"])

    seasonal = pd.DataFrame({"signal": 10 + 2 * np.sin(2 * np.pi * steps / 24)})
    winters = execute(
        registry,
        context,
        "validation.exponential_smoothing",
        {"dataset": seasonal},
        column="signal",
        method="holt_winters",
        seasonal_periods=24,
    )
    assert winters["metrics"]["r2"] > 0.5
    # 乘法季节要求严格正基线，负值必须被拒绝而不是给出无意义的结果。
    with pytest.raises(ValueError):
        execute(
            registry,
            context,
            "validation.exponential_smoothing",
            {"dataset": pd.DataFrame({"signal": np.sin(np.arange(200.0))})},
            column="signal",
            method="holt_winters",
            seasonal_periods=24,
            seasonal="multiplicative",
        )


def test_arima_reports_an_actionable_missing_dependency(registry, context):
    frame = pd.DataFrame({"signal": np.sin(np.arange(200.0) / 5)})
    try:
        import statsmodels  # noqa: F401
    except ImportError:
        with pytest.raises(ValueError, match="statsmodels"):
            execute(registry, context, "validation.arima", {"dataset": frame}, column="signal")
        return
    outputs = execute(registry, context, "validation.arima", {"dataset": frame}, column="signal")
    assert outputs["metrics"]["order"] == [2, 1, 1]
    assert len(outputs["model"].predict(3)) == 3


def test_grid_search_returns_candidates_and_warns_about_optimism(registry, dataset, context):
    from fault_core import features

    extracted = features.extract_features(
        dataset, ["vibration", "temperature"], group_column="equipment", label_column="label"
    )
    outputs = execute(
        registry,
        context,
        "validation.grid_search",
        extracted,
        algorithm="random_forest",
        param_grid={"n_estimators": [20, 60], "max_depth": [3, None]},
        cv_method="group",
        cv_folds=3,
    )
    metrics = outputs["metrics"]
    assert metrics["candidate_count"] == 4
    assert len(metrics["top_candidates"]) == 4
    assert metrics["best_params"]
    assert any("optimistic" in warning for warning in metrics["warnings"])
    assert len(outputs["model"].predict(extracted["features"].iloc[:3])) == 3
    assert "importance" in outputs


def test_grid_search_rejects_unknown_parameters(registry, dataset, context):
    from fault_core import features

    extracted = features.extract_features(
        dataset, ["vibration", "temperature"], group_column="equipment", label_column="label"
    )
    with pytest.raises(ValueError, match="Unsupported parameter"):
        execute(
            registry,
            context,
            "validation.grid_search",
            extracted,
            algorithm="random_forest",
            param_grid={"bogus": [1]},
        )


def test_kmeans_clusters_and_one_class_svm_detects(registry, context):
    rng = np.random.default_rng(5)
    frame = pd.DataFrame(
        {
            "a": np.concatenate([rng.normal(0, 0.3, 150), rng.normal(6, 0.3, 50)]),
            "b": np.concatenate([rng.normal(0, 0.3, 150), rng.normal(6, 0.3, 50)]),
        }
    )
    clustering = execute(
        registry, context, "validation.kmeans", {"dataset": frame}, columns=["a", "b"], n_clusters=0
    )
    assert clustering["metrics"]["n_clusters"] == 2
    assert clustering["metrics"]["silhouette"] > 0.5
    assert sorted(clustering["metrics"]["cluster_sizes"]) == [50, 150]
    assert {"cluster", "distance_to_centre"} <= set(clustering["prediction"].columns)
    assert len(clustering["model"].predict(frame.iloc[:5])) == 5
    assert "severity" in clustering["metrics"]["warnings"][0]

    one_class = execute(
        registry, context, "validation.one_class_svm", {"dataset": frame}, columns=["a", "b"], nu=0.1
    )
    assert one_class["metrics"]["threshold"] == 0.0
    # nu 是训练时越界比例的上界，实际比例可以更低——两者都要在 metrics 里。
    assert one_class["metrics"]["anomaly_rate"] <= 0.2
    assert "support_vector_share" in one_class["metrics"]


# ------------------------------------------------------------- 检索可达性


@pytest.mark.parametrize(
    ("intent", "component_type"),
    [
        ("均值阶跃 突变检测", "validation.level_shift_detector"),
        ("波动率变化检测", "validation.volatility_shift_detector"),
        ("季节性检测", "validation.seasonal_detector"),
        ("自回归残差检测", "validation.autoregression_detector"),
        ("广义ESD离群点检验", "validation.esd_detector"),
        ("Nsigma算法", "validation.nsigma_detector"),
        ("平均值漂移检测", "validation.mean_drift_detector"),
        ("指数平滑预测", "validation.exponential_smoothing"),
        ("ARIMA", "validation.arima"),
        ("超参搜索", "validation.grid_search"),
        ("kmeans聚类", "validation.kmeans"),
        ("单类SVM检测", "validation.one_class_svm"),
        ("Hodrick Prescott过滤器", "explore.hp_filter"),
        ("平稳性检查", "explore.stationarity"),
        ("DTW相关分析", "explore.dtw"),
        ("SBD相关", "explore.sbd"),
        ("斜率与余弦夹角分析", "explore.slope_cosine"),
        ("同比口径", "data.seasonal_difference"),
        ("连续小波变换的山峰数", "feature.wavelet"),
    ],
)
def test_new_batch_components_are_discoverable(intent, component_type):
    registry = default_registry()
    found = {item["component_type"] for item in registry.retrieve(intent=intent, limit=5)}
    assert component_type in found, f"{intent!r} found {sorted(found)}"


def test_torch_dependent_components_are_deliberately_absent():
    """torch 依赖的组件按要求**不实现**：registry 里不该出现，避免"看起来有"的错觉。"""
    registry = default_registry()
    names = {item["component_type"] for item in registry.list(limit=200)}
    for forbidden in (
        "validation.lstm",
        "validation.autoencoder_detector",
        "validation.vae_detector",
        "validation.prophet",
        "validation.rocka",
    ):
        assert forbidden not in names


# ------------------------------------------------------------- 图内端到端


def test_structural_detectors_run_inside_a_real_graph(registry, dataset, context):
    graph = ComponentGraph(registry, "batch34", context.workspace.pipeline_id)
    graph.add_node("data.input", "source", {"path": "sample.csv"})
    graph.add_node(
        "feature.wavelet",
        "wavelet",
        {"columns": ["vibration"], "window": 8, "levels": 3, "group_column": "equipment"},
    )
    graph.add_node(
        "validation.nsigma_detector",
        "sigma",
        {"column": "vibration", "sigma": 3.0, "mode": "group", "group_column": "equipment"},
    )
    graph.add_node("explore.stationarity", "adf", {"column": "vibration"})
    for source, source_port, target, target_port in (
        ("source", "dataset", "wavelet", "dataset"),
        ("source", "dataset", "sigma", "dataset"),
        ("source", "dataset", "adf", "dataset"),
    ):
        graph.connect(source, source_port, target, target_port)
    workspace = ExecutionEngine().execute(graph, context)
    assert workspace.status == PipelineStatus.SUCCESS
    assert workspace.get_output("wavelet", "features").shape[0] == len(dataset)
    assert workspace.get_output("sigma", "metrics")["mode"] == "group"
    adf = workspace.get_output("adf", "statistics")["rows"][0]
    assert set(adf) >= {"statistic", "critical_5pct", "stationary_at_5pct"}
