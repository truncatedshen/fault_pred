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


class _NeverFinishes:
    """一个永远不结束的任务替身，用来确定性地测超时分支。"""

    def done(self) -> bool:
        return False


def test_wait_returns_immediately_when_nothing_is_in_flight(service):
    """没在跑就别等：漏看 `execute_pipeline` 的返回值不该换来一次满超时的白等。"""
    pipeline_id = service.create_pipeline("idle")["pipeline_id"]
    build_graph(service, pipeline_id)

    # 1) 从未启动过：立刻返回并说清原因，而不是等满 60 秒。
    started = time.perf_counter()
    idle = service.wait_for_pipeline(pipeline_id, timeout_seconds=60, poll_seconds=0.05)
    assert time.perf_counter() - started < 1.0
    assert idle["timed_out"] is False and idle["started"] is False
    assert idle["status"] == "CREATED"
    assert any("Nothing is running" in note for note in idle["warnings"])

    # 2) 正常跑一次：仍然阻塞到终态。
    service.execute_pipeline(pipeline_id)
    done = service.wait_for_pipeline(pipeline_id, timeout_seconds=60, poll_seconds=0.05)
    assert done["status"] == "SUCCESS"
    assert done["timed_out"] is False and done["started"] is True
    assert not any("Nothing is running" in note for note in done["warnings"])

    # 3) 跑完再改图 → 结果失效回到 CREATED：同样不该等。
    service.configure_component(pipeline_id, "source", {"path": "synthetic_equipment.csv", "max_rows": 100})
    started = time.perf_counter()
    stale = service.wait_for_pipeline(pipeline_id, timeout_seconds=60, poll_seconds=0.05)
    assert time.perf_counter() - started < 1.0
    assert stale["timed_out"] is False and stale["started"] is False
    assert any("Nothing is running" in note for note in stale["warnings"])


def test_wait_still_times_out_while_a_job_is_in_flight(service):
    """真的有在途任务时，超时语义不能被我改坏。"""
    pipeline_id = service.create_pipeline("busy")["pipeline_id"]
    build_graph(service, pipeline_id)
    service.jobs[pipeline_id] = _NeverFinishes()
    started = time.perf_counter()
    summary = service.wait_for_pipeline(pipeline_id, timeout_seconds=0.3, poll_seconds=0.05)
    assert time.perf_counter() - started >= 0.3
    assert summary["timed_out"] is True and summary["started"] is True
    assert summary["timeout_seconds"] == 0.3


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


def test_bulk_edits_are_all_or_nothing(service: PipelineService) -> None:
    """批量操作要么全成、要么全不成：一次被拒的调用必须留下原样的图。

    这条曾经是真的缺陷：add_components 半途失败会留下已加的节点，报错也不说第几条，
    于是调用方以为整批都没进去。
    """
    pipeline_id = service.create_pipeline("atomic")["pipeline_id"]
    version_before = service.get_pipeline(pipeline_id)["graph"]["version"]

    with pytest.raises(ValueError) as add_error:
        service.add_components(
            pipeline_id,
            [
                {"component_type": "data.input", "node_id": "source", "parameters": {"path": "x.csv"}},
                {
                    "component_type": "feature.statistical",
                    "node_id": "stats",
                    "parameters": {"columns": ["v"]},
                },
                # data.quality 不接受 label_policy：第三条被拒，前两条必须回滚
                {
                    "component_type": "data.quality",
                    "node_id": "quality",
                    "parameters": {"label_policy": "mode"},
                },
            ],
        )
    message = str(add_error.value)
    assert "entry 3 of 3" in message
    assert "data.quality" in message
    assert "Nothing was added" in message
    graph = service.get_pipeline(pipeline_id)["graph"]
    assert graph["nodes"] == []
    assert graph["version"] == version_before  # 版本号也不许漂：否则客户端会凭空遇到并发冲突

    service.add_components(
        pipeline_id,
        [
            {"component_type": "data.input", "node_id": "source", "parameters": {"path": "x.csv"}},
            {"component_type": "feature.statistical", "node_id": "stats", "parameters": {"columns": ["v"]}},
        ],
    )
    with pytest.raises(ValueError) as configure_error:
        service.configure_components(
            pipeline_id,
            [
                {"node_id": "stats", "parameters": {"label_policy": "mode"}},
                {"node_id": "missing", "parameters": {}},
            ],
        )
    assert "entry 2 of 2" in str(configure_error.value)
    nodes = {node["id"]: node["parameters"] for node in service.get_pipeline(pipeline_id)["graph"]["nodes"]}
    assert nodes["stats"]["label_policy"] == "strict"  # 第一条也要还原

    with pytest.raises(ValueError) as connect_error:
        service.connect_many(
            pipeline_id,
            [
                {
                    "source_node": "source",
                    "source_port": "dataset",
                    "target_node": "stats",
                    "target_port": "dataset",
                },
                # 同一个输入口连第二次：第二条被拒，第一条必须撤掉
                {
                    "source_node": "source",
                    "source_port": "dataset",
                    "target_node": "stats",
                    "target_port": "dataset",
                },
            ],
        )
    assert "entry 2 of 2" in str(connect_error.value)
    assert service.get_pipeline(pipeline_id)["graph"]["edges"] == []
