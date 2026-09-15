"""P2 streaming: chunked input, identical features, explicit materialisation."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fault_core import features
from fault_core.visualization import overview, overview_stream
from fault_platform.runtime import ExecutionContext, ExecutionEngine, validate_value
from fault_platform.streaming import StreamedDataset
from fault_platform.workspace import FaultWorkspace, NodeStatus, PipelineStatus


def grouped_frame(equipment: int = 30, rows: int = 64, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    total = equipment * rows
    return pd.DataFrame(
        {
            "equipment": np.repeat(np.arange(equipment), rows),
            "time": np.tile(np.arange(rows), equipment),
            "label": np.repeat(np.arange(equipment) % 3, rows),
            "vibration": rng.normal(size=total),
            "temperature": rng.normal(size=total) + 20,
        }
    )


def streamed(tmp_path, frame: pd.DataFrame, chunk_rows: int = 137) -> StreamedDataset:
    path = tmp_path / "data.csv"
    frame.to_csv(path, index=False)
    return StreamedDataset(path=path, chunk_rows=chunk_rows)


def test_streamed_dataset_chunks_keep_global_row_identity(tmp_path):
    frame = grouped_frame(equipment=4, rows=50)
    stream = streamed(tmp_path, frame, chunk_rows=37)
    chunks = list(stream.chunks())
    assert [len(chunk) for chunk in chunks] == [37, 37, 37, 37, 37, 15]
    combined = pd.concat(chunks)
    assert list(combined.index) == list(range(len(frame)))  # provenance-compatible identity
    pd.testing.assert_frame_equal(stream.materialize(), frame)
    assert (
        validate_value(
            stream, __import__("fault_platform.components.base", fromlist=["DataType"]).DataType.DATASET
        )
        is None
    )


@pytest.mark.parametrize("kind", ["statistical", "fitting", "spectral"])
def test_streaming_features_match_batch_features(tmp_path, kind):
    path = tmp_path / "data.csv"
    grouped_frame().to_csv(path, index=False)
    # Compare against the same bytes the stream reads, so streaming is the only variable.
    frame = pd.read_csv(path)
    chunks = StreamedDataset(path=path, chunk_rows=137).chunks()
    common = {
        "columns": ["vibration", "temperature"],
        "group_column": "equipment",
        "label_column": "label",
        "time_column": "time",
        "window_size": 16,
    }
    if kind == "spectral":
        common = {"columns": ["vibration"], **{k: v for k, v in common.items() if k != "columns"}}
        extra = {"sampling_rate": 64.0, "features": ["dominant_frequency", "spectral_rms"]}
        batch = features.spectral(frame, **common, **extra)
        stream = features.spectral_stream(chunks, **common, **extra)
    else:
        extra = (
            {"features": ["mean", "std", "rms", "kurtosis"]}
            if kind == "statistical"
            else {"fitting_method": "linear"}
        )
        batch = features.extract_features(frame, kind=kind, **common, **extra)
        stream = features.extract_features_stream(chunks, kind=kind, **common, **extra)

    assert batch["features"].index.equals(stream["features"].index)
    np.testing.assert_allclose(batch["features"].to_numpy(), stream["features"].to_numpy(), atol=1e-12)
    assert batch["labels"].equals(stream["labels"])
    assert "source_rows_ranges" in stream["features"].attrs  # ranges, not per-row labels
    assert features.provenance_matches(batch["features"].attrs, stream["features"].attrs)
    np.testing.assert_array_equal(
        features.expand_coverage(batch["features"].attrs),
        features.expand_coverage(stream["features"].attrs),
    )
    for key in ("groups", "grouped", "window_size", "step", "overlapping"):
        assert batch["features"].attrs[key] == stream["features"].attrs[key]
    assert stream["features"].attrs["streaming"] is True
    assert stream["features"].attrs["streamed_rows"] == len(frame)


def test_streaming_requires_contiguous_groups(tmp_path):
    frame = grouped_frame(equipment=10, rows=32)
    shuffled = frame.iloc[np.random.default_rng(1).permutation(len(frame))]
    stream = streamed(tmp_path, shuffled)
    with pytest.raises(ValueError, match="contiguous"):
        features.extract_features_stream(
            stream.chunks(), ["vibration"], group_column="equipment", label_column="label", window_size=8
        )


def test_streaming_overview_matches_batch_counts(tmp_path):
    frame = grouped_frame(equipment=12, rows=40)
    stream = streamed(tmp_path, frame, chunk_rows=101)
    streamed_spec = overview_stream(stream.chunks(), time_column="time")
    batch_spec = overview(frame, time_column="time")
    assert streamed_spec["row_count"] == batch_spec["row_count"]
    assert streamed_spec["column_names"] == batch_spec["column_names"]
    assert streamed_spec["unique_count"] == batch_spec["unique_count"]
    assert streamed_spec["time_range"] == batch_spec["time_range"]
    for column, rate in batch_spec["missing_rate"].items():
        assert streamed_spec["missing_rate"][column] == pytest.approx(rate)


def test_streaming_pipeline_matches_materialised_pipeline(tmp_path, registry):
    from fault_platform.graph import ComponentGraph

    frame = grouped_frame(equipment=40)
    path = tmp_path / "data.csv"
    frame.to_csv(path, index=False)

    def build(node_id: str, streaming: bool):
        graph = ComponentGraph(registry, node_id, node_id)
        graph.add_node(
            "data.input", "source", {"path": "data.csv", "streaming": streaming, "chunk_rows": 211}
        )
        graph.add_node(
            "feature.statistical",
            "stats",
            {
                "columns": ["vibration", "temperature"],
                "group_column": "equipment",
                "label_column": "label",
                "window_size": 16,
                "features": ["mean", "std", "rms"],
            },
        )
        graph.add_node("validation.random_forest", "model", {"n_estimators": 20, "split_method": "group"})
        graph.connect("source", "dataset", "stats", "dataset")
        graph.connect("stats", "features", "model", "features")
        graph.connect("stats", "labels", "model", "labels")
        return graph

    engine = ExecutionEngine()
    materialised = FaultWorkspace("batch")
    engine.execute(build("batch", False), ExecutionContext(materialised, tmp_path))
    chunked = FaultWorkspace("stream")
    engine.execute(build("stream", True), ExecutionContext(chunked, tmp_path))

    for node in ("source", "stats", "model"):
        assert chunked.node_status[node] == NodeStatus.SUCCESS
    assert chunked.status == PipelineStatus.SUCCESS
    left = materialised.get_output("stats", "features")
    right = chunked.get_output("stats", "features")
    assert left.index.equals(right.index)
    np.testing.assert_allclose(left.to_numpy(), right.to_numpy())
    assert features.provenance_matches(left.attrs, right.attrs)
    assert (
        chunked.get_output("model", "metrics")["accuracy"]
        == materialised.get_output("model", "metrics")["accuracy"]
    )
    assert chunked.get_output("stats", "features").attrs["streamed_rows"] == len(frame)


def test_components_without_streaming_support_fail_with_a_clear_hint(tmp_path, registry):
    frame = grouped_frame(equipment=6, rows=32)
    frame.to_csv(tmp_path / "data.csv", index=False)
    graph = __import__("fault_platform.graph", fromlist=["ComponentGraph"]).ComponentGraph(
        registry, "guard", "guard_pipeline"
    )
    graph.add_node("data.input", "source", {"path": "data.csv", "streaming": True})
    graph.add_node("explore.correlation", "corr", {"columns": ["vibration"]})
    graph.connect("source", "dataset", "corr", "dataset")
    workspace = FaultWorkspace("guard_pipeline")
    ExecutionEngine().execute(graph, ExecutionContext(workspace, tmp_path))
    assert workspace.node_status["corr"] == NodeStatus.FAILED
    message = workspace.errors["corr"]["error_message"]
    assert "streamed input" in message and "data.materialize" in message


def test_materialize_component_restores_global_operations(tmp_path, registry):
    frame = grouped_frame(equipment=6, rows=32)
    path = tmp_path / "data.csv"
    frame.to_csv(path, index=False)
    graph = __import__("fault_platform.graph", fromlist=["ComponentGraph"]).ComponentGraph(
        registry, "materialise", "materialise_pipeline"
    )
    graph.add_node("data.input", "source", {"path": "data.csv", "streaming": True})
    graph.add_node("data.materialize", "load")
    graph.add_node("explore.correlation", "corr", {"columns": ["vibration", "temperature"]})
    graph.connect("source", "dataset", "load", "dataset")
    graph.connect("load", "dataset", "corr", "dataset")
    workspace = FaultWorkspace("materialise_pipeline")
    ExecutionEngine().execute(graph, ExecutionContext(workspace, tmp_path))
    assert workspace.status == PipelineStatus.SUCCESS
    assert any("Materialised" in warning for warning in workspace.warnings)
    assert workspace.get_output("load", "dataset").shape[0] == len(frame)
