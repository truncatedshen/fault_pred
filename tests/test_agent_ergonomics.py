"""MCP ergonomics: bulk edits, token-light acknowledgements, compact results, lifecycle."""

from __future__ import annotations

import time

import pytest

from fault_platform.service import CONTROL_OPERATIONS, PipelineService
from fault_platform.workspace import summarize


@pytest.fixture
def service(tmp_path):
    from fault_platform.examples import create_dataset

    data_root = tmp_path / "data"
    create_dataset(data_root / "synthetic_equipment.csv")
    instance = PipelineService(data_root, tmp_path / "saved")
    try:
        yield instance
    finally:
        instance.close()


def build_graph(service, pipeline_id: str, include_graph: bool = False) -> dict:
    """One create + one bulk add + one bulk connect, the way an agent should assemble."""
    added = service.add_components(
        pipeline_id,
        [
            {
                "component_type": "data.input",
                "node_id": "source",
                "parameters": {"path": "synthetic_equipment.csv"},
            },
            {
                "component_type": "feature.statistical",
                "node_id": "stats",
                "parameters": {
                    "columns": ["vibration"],
                    "group_column": "equipment",
                    "label_column": "label",
                    "window_size": 16,
                },
            },
            {
                "component_type": "validation.random_forest",
                "node_id": "model",
                "parameters": {"n_estimators": 10, "split_method": "group"},
            },
        ],
        include_graph=include_graph,
    )
    assert added["added_count"] == 3
    return service.connect_many(
        pipeline_id,
        [
            {
                "source_node": "source",
                "source_port": "dataset",
                "target_node": "stats",
                "target_port": "dataset",
            },
            {
                "source_node": "stats",
                "source_port": "features",
                "target_node": "model",
                "target_port": "features",
            },
            {
                "source_node": "stats",
                "source_port": "labels",
                "target_node": "model",
                "target_port": "labels",
            },
        ],
        include_graph=include_graph,
    )


def test_bulk_edits_and_token_light_acknowledgements(service):
    pipeline_id = service.create_pipeline("bulk")["pipeline_id"]
    added = service.add_components(
        pipeline_id,
        [{"component_type": "data.input", "node_id": "a"}, {"component_type": "data.input", "node_id": "b"}],
    )
    assert "graph" not in added and added["node_count"] == 2 and added["edge_count"] == 0
    assert [entry["node_id"] for entry in added["added"]] == ["a", "b"]

    with_graph = service.add_components(
        pipeline_id, [{"component_type": "data.input", "node_id": "c"}], include_graph=True
    )
    assert "graph" in with_graph and len(with_graph["graph"]["nodes"]) == 3

    single = service.add_component(pipeline_id, "data.input", "d", include_graph=False)
    assert "graph" not in single and single["node"]["id"] == "d"
    assert "graph" in service.add_component(pipeline_id, "data.input", "e")

    configured = service.configure_components(
        pipeline_id, [{"node_id": "a", "parameters": {"path": "synthetic_equipment.csv"}}]
    )
    assert "graph" not in configured and configured["updated_count"] == 1
    compact_configure = service.configure_component(
        pipeline_id, "b", {"path": "synthetic_equipment.csv"}, include_graph=False
    )
    assert "graph" not in compact_configure


def test_result_summaries_drop_index_arrays(service):
    pipeline_id = service.create_pipeline("compact")["pipeline_id"]
    build_graph(service, pipeline_id)
    service.execute_pipeline(pipeline_id)
    status = service.wait_for_pipeline(pipeline_id, timeout_seconds=120)
    assert status["status"] == "SUCCESS"

    compact = service.get_node_result(pipeline_id, "model")
    metrics = compact["outputs"]["metrics"]["value"]
    assert "train_indices" not in metrics and "test_indices" not in metrics
    assert metrics["train_indices_count"] == metrics["train_count"]
    assert metrics["accuracy"] > 0.8  # the numbers that matter survive
    assert "confusion_matrix" in metrics

    expanded = service.get_node_result(pipeline_id, "model", include_indices=True)
    assert expanded["outputs"]["metrics"]["value"]["train_indices"]["count"] == metrics["train_count"]


def test_wait_for_pipeline_and_stale_workspace_warning(service):
    pipeline_id = service.create_pipeline("wait")["pipeline_id"]
    build_graph(service, pipeline_id)
    started = time.perf_counter()
    service.execute_pipeline(pipeline_id)
    summary = service.wait_for_pipeline(pipeline_id, timeout_seconds=120, poll_seconds=0.05)
    assert summary["status"] == "SUCCESS" and summary["timed_out"] is False
    assert time.perf_counter() - started < 120

    # A second run into a fresh workspace makes the first one stale; reading it must say so.
    first_workspace = summary["workspace_id"]
    second = service.workspaces.create_workspace(pipeline_id)
    service.execute_pipeline(pipeline_id, workspace_id=second.workspace_id)
    service.wait_for_pipeline(pipeline_id, workspace_id=second.workspace_id, timeout_seconds=120)
    stale = service.get_pipeline_status(pipeline_id, workspace_id=first_workspace)
    assert any("not the pipeline's latest" in warning for warning in stale["warnings"])
    current = service.get_pipeline_status(pipeline_id, workspace_id=second.workspace_id)
    assert not any("not the pipeline's latest" in warning for warning in current["warnings"])

    unknown = service.dispatch(
        "get_pipeline_status", {"pipeline_id": pipeline_id, "workspace_id": "ws_missing"}
    )
    assert unknown["success"] is False and "Unknown workspace" in unknown["summary"]
    assert service.dispatch("wait_for_pipeline", {"pipeline_id": "pipeline_missing"})["success"] is False


def test_server_info_and_dataset_listing(service):
    info = service.get_server_info()
    assert info["data_root"].endswith("data")
    assert info["components"] >= 29
    assert "artifact_cache" in info and info["artifact_cache"]["max_bytes"] is None
    listing = service.list_datasets()
    assert any(item["path"].endswith(".csv") for item in listing["datasets"])


def test_delete_pipeline_frees_graph_workspace_and_cache(service, tmp_path):
    pipeline_id = service.create_pipeline("temporary")["pipeline_id"]
    build_graph(service, pipeline_id, include_graph=True)
    service.execute_pipeline(pipeline_id)
    service.wait_for_pipeline(pipeline_id, timeout_seconds=120)
    workspace_id = service.get_pipeline_status(pipeline_id)["workspace_id"]
    assert workspace_id in service.workspaces.workspaces

    result = service.delete_pipeline(pipeline_id)
    assert result["deleted"] and result["workspaces_removed"] == 1
    assert pipeline_id not in service.graphs
    assert workspace_id not in service.workspaces.workspaces
    with pytest.raises(ValueError, match="Unknown pipeline"):
        service.get_pipeline(pipeline_id)
    assert pipeline_id not in [entry["id"] for entry in service.list_pipelines()["pipelines"]]


def test_compact_mapping_keeps_short_lists_intact():
    summary = summarize({"steps": [1, 2, 3], "quality": {"notes": ["a"]}}, limit=20)
    assert summary["value"]["steps"] == [1, 2, 3]
    assert summary["value"]["quality"]["notes"] == ["a"]
    big = summarize({"labels": list(range(50)), "train_indices": list(range(50))})
    assert big["value"]["labels"]["count"] == 50
    assert big["value"]["train_indices_count"] == 50


def test_every_operation_is_reachable_through_dispatch(service):
    assert len(CONTROL_OPERATIONS) == len(set(CONTROL_OPERATIONS))
    info = service.dispatch("get_server_info", {})
    assert info["success"] and info["data_root"].endswith("data")
    unknown = service.dispatch("no_such_operation", {})
    assert unknown["success"] is False and "Unknown operation" in unknown["summary"]
    bad = service.dispatch("add_components", {"pipeline_id": "missing", "components": []})
    assert bad["success"] is False
