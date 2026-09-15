import numpy as np
import pandas as pd
import pytest

from fault_core import features, reduction, selection
from fault_platform.components.base import DataType
from fault_platform.graph import ComponentGraph
from fault_platform.runtime import ExecutionContext, ExecutionEngine, validate_value
from fault_platform.workspace import FaultWorkspace, NodeStatus, PipelineStatus

SPECTRAL_COLUMNS = ["dominant_frequency", "dominant_amplitude", "spectral_rms", "band_energy_ratio"]


def tone_frame(sampling_rate: float = 1024.0, windows: int = 8, size: int = 256, harmonic: bool = False):
    """Each equipment group is one aligned 64 Hz window; harmonics are optional."""
    time = np.arange(windows * size) / sampling_rate
    signal = np.sin(2 * np.pi * 64 * time)
    if harmonic:
        signal = signal + 0.4 * np.sin(2 * np.pi * 192 * time)
    return pd.DataFrame(
        {
            "equipment": np.repeat(np.arange(windows), size),
            "time": time,
            "vibration": signal,
            "label": np.repeat(np.arange(windows) % 2, size),
        }
    )


def spectrum_of(frame: pd.DataFrame, **overrides):
    parameters = {
        "columns": ["vibration"],
        "sampling_rate": 1024.0,
        "group_column": "equipment",
        "label_column": "label",
        "window_size": 256,
        "features": SPECTRAL_COLUMNS,
        "band_edges": [0.25, 0.5],
    }
    return features.spectral(frame, **{**parameters, **overrides})


def test_spectral_features_recover_a_known_tone():
    result = spectrum_of(tone_frame())
    frame = result["features"]
    assert frame.index.name == "window_id" and len(frame) == 8
    np.testing.assert_allclose(frame["vibration__dominant_frequency"], 64.0)
    np.testing.assert_allclose(frame["vibration__dominant_amplitude"], 1.0, atol=1e-3)
    np.testing.assert_allclose(frame["vibration__spectral_rms"], 1 / np.sqrt(2), atol=1e-3)
    bands = frame[[c for c in frame.columns if c.startswith("vibration__band_energy_ratio_")]]
    assert bands.shape[1] == 3
    np.testing.assert_allclose(bands.sum(axis=1), 1.0)
    assert frame.index.equals(result["labels"].index)
    assert frame.attrs["source_rows"][0] == list(range(256))
    assert frame.attrs["grouped"] and frame.attrs["window_size"] == 256


def test_spectral_features_separate_harmonic_energy():
    plain = spectrum_of(tone_frame(), features=["harmonic_ratio", "spectral_centroid", "spectral_spread"])
    rich = spectrum_of(
        tone_frame(harmonic=True), features=["harmonic_ratio", "spectral_centroid", "spectral_spread"]
    )
    assert plain["features"]["vibration__harmonic_ratio"].max() < 1e-6
    assert rich["features"]["vibration__harmonic_ratio"].min() > 0.05
    assert (
        rich["features"]["vibration__spectral_centroid"].iloc[0]
        > plain["features"]["vibration__spectral_centroid"].iloc[0]
    )
    assert rich["features"]["vibration__spectral_spread"].iloc[0] > 20


def test_spectral_parameter_validation(registry, dataset):
    component = registry.create("feature.spectral", parameters={"columns": ["vibration"]})
    with pytest.raises(ValueError, match="sampling_rate"):
        component.validate()
    parameters = {
        "columns": ["vibration"],
        "sampling_rate": 16.0,
        "group_column": "equipment",
        "window_size": 8,
    }
    with pytest.raises(ValueError, match="positive"):
        features.spectral(dataset, **{**parameters, "sampling_rate": 0.0})
    with pytest.raises(ValueError, match="Band edges"):
        features.spectral(dataset, **{**parameters, "band_edges": [0.6, 0.3]})
    with pytest.raises(ValueError, match="Unknown spectral features"):
        features.spectral(dataset, **{**parameters, "features": ["bogus"]})
    with pytest.raises(ValueError, match="at least"):
        features.spectral(dataset, **{**parameters, "window_size": 7})
    # A constant channel is a normal condition in real data: the default policy keeps
    # the rows aligned and reports NaNs instead of failing the whole run.
    flat = features.spectral(
        dataset.assign(flat=2.0),
        columns=["flat"],
        sampling_rate=16.0,
        group_column="equipment",
        window_size=8,
    )
    assert flat["features"]["flat__dominant_frequency"].isna().all()
    assert any("flat" in warning for warning in flat["warnings"])
    with pytest.raises(ValueError, match="Flat \\(constant\\) window"):
        features.spectral(
            dataset.assign(flat=2.0),
            columns=["flat"],
            sampling_rate=16.0,
            group_column="equipment",
            window_size=8,
            flat_policy="error",
        )
    with pytest.raises(ValueError, match="flat_policy"):
        features.spectral(
            dataset.assign(flat=2.0),
            columns=["flat"],
            sampling_rate=16.0,
            group_column="equipment",
            window_size=8,
            flat_policy="guess",
        )


def test_spectral_component_runs_through_the_registry(registry, dataset, context):
    component = registry.create(
        "feature.spectral",
        parameters={
            "columns": ["vibration"],
            "sampling_rate": 16.0,
            "group_column": "equipment",
            "label_column": "label",
            "window_size": 8,
            "features": ["dominant_frequency", "spectral_rms"],
            "band_edges": [0.5],
        },
    )
    component.validate()
    outputs = component.execute({"dataset": dataset}, context).outputs
    validate_value(outputs["features"], DataType.FEATURE_DATASET)
    assert len(outputs["features"]) == 80
    assert len(outputs["labels"]) == 80
    assert list(outputs["features"].columns) == [
        "vibration__dominant_frequency",
        "vibration__spectral_rms",
    ]


def test_score_select_supervised_and_unsupervised(registry, dataset, context):
    frame = dataset.assign(constant=1.0, duplicate=dataset["vibration"] * 2)
    columns = ["vibration", "temperature", "constant", "duplicate"]
    variance = registry.create("feature.score_select", parameters={"method": "variance"})
    outputs = variance.execute({"features": frame[columns]}, context).outputs
    assert list(outputs["features"].columns) == ["vibration", "temperature", "duplicate"]
    assert outputs["features"].attrs["dropped_features"] == ["constant"]
    assert list(outputs["scores"].columns) == ["feature", "score", "selected"]
    assert outputs["scores"]["selected"].sum() == 3
    correlated = registry.create("feature.score_select", parameters={"method": "correlation"})
    selected = correlated.execute({"features": frame[columns]}, context).outputs["features"]
    assert list(selected.columns) == ["temperature", "duplicate"]
    with pytest.raises(ValueError, match="requires a label vector"):
        registry.create("feature.score_select", parameters={"method": "model"}).execute(
            {"features": frame[columns]}, context
        )
    with pytest.raises(ValueError, match="Unknown selection method"):
        selection.select_features(frame[columns], method="magic")


def test_score_select_keeps_provenance_and_can_feed_a_model(registry, dataset, context):
    extracted = features.extract_features(
        dataset, ["vibration", "temperature"], group_column="equipment", label_column="label", window_size=8
    )
    component = registry.create(
        "feature.score_select", parameters={"method": "mutual_information", "top_k": 4}
    )
    outputs = component.execute(
        {"features": extracted["features"], "labels": extracted["labels"]}, context
    ).outputs
    assert len(outputs["features"].columns) == 4
    for key in ("source_rows", "groups", "source_path", "source_id"):
        assert outputs["features"].attrs.get(key) == extracted["features"].attrs.get(key)
    assert any("leakage" in warning for warning in outputs["features"].attrs["evaluation_warnings"])
    model = registry.create(
        "validation.random_forest", parameters={"split_method": "group", "n_estimators": 10}
    )
    metrics = model.execute(
        {"features": outputs["features"], "labels": extracted["labels"]}, context
    ).outputs["metrics"]
    assert metrics["accuracy"] > 0.8


def test_pca_projection_preserves_rows_and_merges(registry, dataset, context):
    extracted = features.extract_features(
        dataset, ["vibration", "temperature"], group_column="equipment", label_column="label", window_size=8
    )
    outputs = (
        registry.create("feature.pca", parameters={"n_components": 2})
        .execute({"features": extracted["features"]}, context)
        .outputs
    )
    projected = outputs["features"]
    assert list(projected.columns) == ["pc_1", "pc_2"]
    assert projected.index.equals(extracted["features"].index)
    assert projected.attrs["source_rows"] == extracted["features"].attrs["source_rows"]
    rows = outputs["variance"]["rows"]
    assert rows[-1]["cumulative"] == pytest.approx(sum(projected.attrs["pca_explained_variance_ratio"]))
    assert 0 < rows[-1]["cumulative"] <= 1
    assert outputs["variance"]["source_columns"] == [
        "vibration__mean",
        "vibration__std",
        "vibration__rms",
        "temperature__mean",
        "temperature__std",
        "temperature__rms",
    ]
    merged = features.merge_features(extracted["features"], projected)
    assert merged.shape[1] == extracted["features"].shape[1] + 2
    with pytest.raises(ValueError, match="cannot exceed"):
        reduction.pca(extracted["features"], n_components=99)


def test_window_features_and_spectra_compose_in_one_graph(registry, context):
    graph = ComponentGraph(registry, "Extended", "extended_pipeline")
    graph.add_node("data.input", "source", {"path": "sample.csv"})
    graph.add_node(
        "feature.statistical",
        "stats",
        {
            "columns": ["vibration", "temperature"],
            "group_column": "equipment",
            "label_column": "label",
            "window_size": 8,
        },
    )
    graph.add_node(
        "feature.spectral",
        "spectrum",
        {
            "columns": ["vibration"],
            "sampling_rate": 16.0,
            "group_column": "equipment",
            "label_column": "label",
            "window_size": 8,
            "features": ["dominant_frequency", "spectral_rms", "band_energy_ratio"],
            "band_edges": [0.5],
        },
    )
    graph.add_node("feature.merge", "merge")
    graph.add_node("feature.score_select", "select", {"method": "variance"})
    graph.add_node("validation.random_forest", "model", {"n_estimators": 10, "split_method": "group"})
    graph.connect("source", "dataset", "stats", "dataset")
    graph.connect("source", "dataset", "spectrum", "dataset")
    graph.connect("stats", "features", "merge", "left")
    graph.connect("spectrum", "features", "merge", "right")
    graph.connect("merge", "features", "select", "features")
    graph.connect("select", "features", "model", "features")
    graph.connect("stats", "labels", "model", "labels")
    workspace = ExecutionEngine().execute(
        graph, ExecutionContext(FaultWorkspace("extended_pipeline"), context.data_root)
    )
    assert workspace.status == PipelineStatus.SUCCESS
    assert workspace.node_status["model"] == NodeStatus.SUCCESS
    assert workspace.get_output("model", "metrics")["accuracy"] > 0.8
    assert len(workspace.get_output("select", "features").columns) > 6
