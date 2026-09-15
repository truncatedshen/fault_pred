from __future__ import annotations

import numpy as np
import pandas as pd

from fault_core import features
from fault_platform.graph import ComponentGraph
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


def test_registry_exposes_complete_component_inventory(registry):
    assert len(registry.list(limit=100)) == 56
    expected = {
        "data.asset_key",
        "feature.imputation",
        "data.time_resample",
        "data.split",
        "data.neighbor_features",
        "data.imputation",
        "data.binarize",
        "explore.distribution",
        "explore.periodicity",
        "explore.concept_drift",
        "explore.cross_relation",
        "explore.anomaly",
        "visual.subplot",
        "visual.histogram",
        "visual.compare",
        "visual.anomaly",
        "visual.relationship",
        "feature.rolling_statistics",
        "feature.temporal",
        "feature.entropy",
        "validation.linear_regression",
        "validation.arma",
        "validation.knn_detector",
        "validation.isolation_forest_detector",
        "validation.persistence_detector",
        "validation.decision_tree",
        "validation.reservoir_classifier",
    }
    assert expected <= {item["component_type"] for item in registry.list(limit=100)}
    assert {item["component_type"] for item in registry.search("近似熵")} == {"feature.entropy"}
    assert {item["component_type"] for item in registry.search("互协方差")} == {"explore.cross_relation"}
    assert {item["component_type"] for item in registry.search("可预测性")} == {
        "validation.linear_regression",
        "validation.arma",
    }


def test_extended_data_components(registry, context):
    frame = pd.DataFrame(
        {
            "group": ["a"] * 4 + ["b"] * 4,
            "time": pd.date_range("2024-01-01", periods=4, freq="30s").tolist() * 2,
            "value": [1.0, np.nan, 3.0, 100.0, 2.0, 4.0, 6.0, 8.0],
            "other": [5.0] * 8,
            "empty": [np.nan] * 8,
        }
    )
    mean_filled = execute(
        registry,
        context,
        "data.imputation",
        {"dataset": frame},
        columns=["value"],
        method="mean",
        group_column="group",
    )["dataset"]
    assert mean_filled.loc[1, "value"] == (1 + 3 + 100) / 3
    interpolated = execute(
        registry,
        context,
        "data.imputation",
        {"dataset": frame},
        columns=["value"],
        method="interpolate",
        group_column="group",
    )["dataset"]
    assert interpolated.loc[1, "value"] == 2.0

    dropped = execute(
        registry,
        context,
        "data.column_operation",
        {"dataset": mean_filled},
        operation="drop_empty",
    )["dataset"]
    assert "empty" not in dropped
    constant_dropped = execute(
        registry,
        context,
        "data.column_operation",
        {"dataset": dropped},
        operation="drop_constant",
    )["dataset"]
    assert "other" not in constant_dropped
    trimmed = execute(
        registry,
        context,
        "data.column_operation",
        {"dataset": dropped},
        operation="trim_extrema",
        columns=["value"],
        extrema_count=1,
    )["dataset"]
    assert trimmed["value"].max() < 100

    resampled = execute(
        registry,
        context,
        "data.time_resample",
        {"dataset": mean_filled},
        time_column="time",
        frequency="1min",
        columns=["value"],
        group_column="group",
    )["dataset"]
    assert len(resampled) == 4
    neighbors = execute(
        registry,
        context,
        "data.neighbor_features",
        {"dataset": mean_filled},
        columns=["value"],
        offsets=[1, -1],
        group_column="group",
        time_column="time",
    )["dataset"]
    assert {"value_lag_1", "value_lead_1"} <= set(neighbors)
    assert pd.isna(neighbors.loc[4, "value_lag_1"])
    split = execute(
        registry,
        context,
        "data.split",
        {"dataset": mean_filled},
        method="group",
        group_column="group",
        test_size=0.5,
    )
    assert set(split["train"]["group"]).isdisjoint(split["test"]["group"])
    binary = execute(
        registry,
        context,
        "data.binarize",
        {"dataset": mean_filled},
        columns=["value"],
        threshold=5.0,
    )["dataset"]
    assert set(binary["value_binary"]) == {0, 1}


def test_exploration_and_visual_components(registry, dataset, context):
    distribution = execute(
        registry,
        context,
        "explore.distribution",
        {"dataset": dataset},
        columns=["vibration"],
        bins=8,
    )["statistics"]
    assert distribution["rows"][0]["column"] == "vibration"
    periodicity = execute(
        registry,
        context,
        "explore.periodicity",
        {"dataset": dataset.iloc[:32]},
        columns=["vibration"],
        max_lag=8,
    )["statistics"]
    assert len(periodicity["series"][0]["x"]) == 8
    drift = execute(
        registry,
        context,
        "explore.concept_drift",
        {
            "reference": dataset.iloc[:320],
            "current": dataset.iloc[320:].assign(vibration=lambda x: x.vibration + 5),
        },
        columns=["vibration"],
    )["statistics"]
    assert drift["rows"][0]["drift"]
    relation = execute(
        registry,
        context,
        "explore.cross_relation",
        {"dataset": dataset},
        first_column="vibration",
        second_column="temperature",
        max_lag=3,
    )["matrix"]
    assert list(relation.index) == list(range(-3, 4))
    anomalies = execute(
        registry,
        context,
        "explore.anomaly",
        {"dataset": dataset},
        columns=["vibration"],
        method="dynamic_threshold",
        window=8,
    )["prediction"]
    assert {"is_anomaly", "vibration__score"} <= set(anomalies)
    smoothed = execute(
        registry,
        context,
        "explore.anomaly",
        {"dataset": dataset},
        columns=["vibration"],
        method="hyperbolic_smoothing",
        window=8,
        group_column="equipment",
        time_column="time",
    )["prediction"]
    assert "vibration__smoothed" in smoothed

    visual_cases = [
        (
            "visual.subplot",
            {"dataset": dataset},
            {"x": "time", "value_columns": ["vibration", "temperature"]},
        ),
        ("visual.histogram", {"dataset": dataset}, {"columns": ["vibration"], "bins": 10}),
        (
            "visual.compare",
            {"first": dataset.iloc[:20], "second": dataset.iloc[20:40]},
            {"columns": ["vibration"], "x_column": "time"},
        ),
        (
            "visual.anomaly",
            {"dataset": dataset, "prediction": anomalies},
            {"x": "time", "y": "vibration", "anomaly_column": "is_anomaly"},
        ),
        ("visual.relationship", {"dataset": dataset}, {"columns": ["vibration", "temperature"]}),
    ]
    for component_type, inputs, parameters in visual_cases:
        plot = execute(registry, context, component_type, inputs, **parameters)["plot"]
        assert plot["kind"] and "series" in plot


def test_sequence_feature_components(registry, dataset, context):
    rolling = execute(
        registry,
        context,
        "feature.rolling_statistics",
        {"dataset": dataset},
        columns=["vibration"],
        method="mean",
        window=4,
        group_column="equipment",
        time_column="time",
    )["features"]
    assert rolling.index.equals(dataset.index) and np.isfinite(rolling.to_numpy()).all()
    repeated = execute(
        registry,
        context,
        "feature.rolling_statistics",
        {"dataset": pd.DataFrame({"value": [1.0, 2.0, 2.0]})},
        columns=["value"],
        method="max_repeat",
        window=3,
    )["features"]
    assert repeated.iloc[-1, 0] == 1
    temporal = execute(
        registry,
        context,
        "feature.temporal",
        {"dataset": dataset},
        columns=["vibration"],
        method="second_difference",
        group_column="equipment",
        time_column="time",
    )["features"]
    assert temporal.iloc[0, 0] == 0
    simple = pd.DataFrame({"value": [1.0, 2.0, 4.0]})
    second = execute(
        registry,
        context,
        "feature.temporal",
        {"dataset": simple},
        columns=["value"],
        method="second_difference",
    )["features"]
    assert second.iloc[:, 0].tolist() == [0.0, 0.0, 1.0]
    autocorrelation = execute(
        registry,
        context,
        "feature.temporal",
        {"dataset": dataset.iloc[:16]},
        columns=["vibration"],
        method="autocorrelation",
        lag=1,
        window=6,
    )["features"]
    assert np.isfinite(autocorrelation.to_numpy()).all()
    entropy = execute(
        registry,
        context,
        "feature.entropy",
        {"dataset": dataset},
        columns=["vibration"],
        group_column="equipment",
        label_column="label",
    )
    assert entropy["features"].shape == (40, 2)
    assert entropy["features"].index.equals(entropy["labels"].index)


def test_new_validation_components(registry, dataset, context):
    extracted = features.extract_features(
        dataset,
        ["vibration", "temperature"],
        group_column="equipment",
        label_column="label",
    )
    for component_type in ("validation.decision_tree", "validation.reservoir_classifier"):
        outputs = execute(
            registry,
            context,
            component_type,
            extracted,
            split_method="group",
        )
        assert outputs["metrics"]["test_count"] > 0
        assert len(outputs["model"].predict(extracted["features"].iloc[:3])) == 3

    row_features = dataset[["vibration", "temperature"]].copy()
    target = pd.Series(
        2 * row_features["vibration"] - 0.5 * row_features["temperature"],
        index=row_features.index,
        name="target",
    )
    regression = execute(
        registry,
        context,
        "validation.linear_regression",
        {"features": row_features, "target": target},
        test_size=0.2,
    )
    assert regression["metrics"]["r2"] > 0.99

    rng = np.random.default_rng(7)
    values = [0.0, 0.0]
    for noise in rng.normal(0, 0.05, 120):
        values.append(0.7 * values[-1] - 0.2 * values[-2] + noise)
    arma_data = pd.DataFrame({"time": np.arange(len(values)), "signal": values})
    arma = execute(
        registry,
        context,
        "validation.arma",
        {"dataset": arma_data},
        column="signal",
        p=2,
        q=1,
        time_column="time",
    )
    assert arma["metrics"]["test_count"] > 0
    assert len(arma["model"].predict(3)) == 3

    for component_type in ("validation.knn_detector", "validation.isolation_forest_detector"):
        detection = execute(
            registry,
            context,
            component_type,
            {"dataset": dataset},
            columns=["vibration", "temperature"],
            contamination=0.05,
        )
        assert detection["prediction"]["is_anomaly"].any()
        assert len(detection["model"].predict(dataset.iloc[:3])) == 3

    persistence = execute(
        registry,
        context,
        "validation.persistence_detector",
        {"dataset": pd.DataFrame({"signal": [0, 2, 2, 2, 0]})},
        column="signal",
        threshold=1,
        min_consecutive=2,
    )
    assert persistence["prediction"]["is_anomaly"].tolist() == [False, False, True, True, False]


def test_extended_components_execute_through_graph_runtime(registry, context):
    graph = ComponentGraph(registry, "extended runtime", context.workspace.pipeline_id)
    graph.add_node("data.input", "source", {"path": "sample.csv"})
    graph.add_node(
        "feature.rolling_statistics",
        "rolling",
        {
            "columns": ["vibration", "temperature"],
            "method": "mean",
            "window": 4,
            "group_column": "equipment",
            "time_column": "time",
        },
    )
    graph.add_node("data.labels", "labels", {"column": "label"})
    graph.add_node("validation.decision_tree", "tree", {"split_method": "group"})
    graph.add_node(
        "validation.knn_detector",
        "detector",
        {"columns": ["vibration", "temperature"], "neighbors": 5},
    )
    graph.add_node(
        "visual.anomaly",
        "plot",
        {"x": "time", "y": "vibration", "anomaly_column": "is_anomaly"},
    )
    for source, source_port, target, target_port in (
        ("source", "dataset", "rolling", "dataset"),
        ("source", "dataset", "labels", "dataset"),
        ("rolling", "features", "tree", "features"),
        ("labels", "labels", "tree", "labels"),
        ("source", "dataset", "detector", "dataset"),
        ("source", "dataset", "plot", "dataset"),
        ("detector", "prediction", "plot", "prediction"),
    ):
        graph.connect(source, source_port, target, target_port)
    workspace = ExecutionEngine().execute(graph, context)
    assert workspace.status == PipelineStatus.SUCCESS
    assert workspace.get_output("tree", "metrics")["test_count"] > 0
    assert workspace.get_output("plot", "plot")["anomaly_count"] > 0
