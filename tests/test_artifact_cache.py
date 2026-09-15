"""P0 memory work: store by reference, preview without copying, bounded LRU cache."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from fault_platform.runtime import ExecutionContext
from fault_platform.workspace import (
    FaultWorkspace,
    MemoryArtifactStore,
    NodeStatus,
    PipelineStatus,
    WorkspaceManager,
    estimate_size,
    summarize,
)


def test_store_keeps_values_by_reference_and_copies_only_on_request():
    frame = pd.DataFrame({"a": np.arange(1000.0)})
    store = MemoryArtifactStore()
    reference = store.put(frame)
    assert store.peek(reference) is frame  # storing does not duplicate the payload
    shared = store.get(reference, copy=False)
    assert shared is frame
    isolated = store.get(reference)
    assert isolated is not frame and isolated.equals(frame)
    assert store.stats()["bytes"] >= estimate_size(frame)


def test_preview_does_not_duplicate_the_payload(pipeline, context):
    ws = FaultWorkspace("p1")
    frame = pd.DataFrame({"a": np.arange(5000.0), "b": np.arange(5000.0)})
    ws.store_outputs("node", {"dataset": frame})
    stored = ws.artifacts.peek(ws.node_results["node"]["dataset"])
    assert stored is frame
    preview = ws.get_node_result("node", limit=5)
    assert len(preview["outputs"]["dataset"]["preview"]) == 5
    assert ws.artifacts.peek(ws.node_results["node"]["dataset"]) is frame
    # Missing rates come from the preview rows instead of a full boolean frame.
    summary = summarize(frame, limit=5)
    assert summary["missing_rate_rows"] == 5
    assert summary["shape"] == [5000, 2]


def test_execution_inputs_stay_isolated_between_branches(pipeline, context):
    from fault_platform.runtime import ExecutionEngine

    ws = ExecutionEngine().execute(pipeline, context)
    stored = ws.get_output("source", "dataset", copy=False)
    first = ws.get_output("source", "dataset")
    first.iloc[0, first.columns.get_loc("temperature")] = -999
    assert stored.iloc[0, stored.columns.get_loc("temperature")] != -999
    assert ws.get_output("source", "dataset").iloc[0, 1] != -999


def test_budget_evicts_lru_unpinned_artifacts_and_invalidates_their_nodes():
    frame = pd.DataFrame({"a": np.zeros(10_000), "b": np.zeros(10_000)})
    size = estimate_size(frame)
    store = MemoryArtifactStore(max_bytes=int(size * 2.2))
    evicted: list[str] = []
    store.on_evict = evicted.append
    first = store.put(frame.copy(), owner="n0")
    second = store.put(frame.copy(), owner="n1")
    store.peek(first)  # most recently used
    third = store.put(frame.copy(), owner="n2")
    store.enforce_budget()
    assert evicted == [second]
    assert store.contains(first) and store.contains(third)
    stats = store.stats()
    assert stats["bytes"] <= int(size * 2.2) and stats["evictions"] == 1

    pinned = MemoryArtifactStore(max_bytes=1)
    pinned.put(frame.copy())
    pinned_reference = next(iter(pinned._entries))
    pinned.pin([pinned_reference])
    pinned.enforce_budget()
    assert pinned.contains(pinned_reference)  # pinned entries are never evicted


def test_eviction_is_suspended_while_a_dag_runs():
    store = MemoryArtifactStore(max_bytes=1)
    store.put(pd.DataFrame({"a": [1.0]}))
    store.suspend()
    store.enforce_budget()
    assert store.stats()["artifacts"] == 1 and store.stats()["suspended"] is True
    store.resume()
    assert store.stats()["artifacts"] == 0


def test_evicted_node_is_marked_for_recomputation():
    ws = FaultWorkspace("p1", artifact_cache_bytes=1)  # a 1-byte budget evicts everything
    ws.store_outputs("source", {"dataset": pd.DataFrame({"a": [1.0, 2.0]})})
    assert ws.node_results == {}
    assert ws.node_status["source"] == NodeStatus.PENDING
    assert any("evicted" in warning for warning in ws.warnings)


def test_checkpoint_shares_payloads_and_pins_them(pipeline, context):
    from fault_platform.runtime import ExecutionEngine

    ws = ExecutionEngine().execute(pipeline, context)
    manager = WorkspaceManager()
    manager.workspaces[ws.workspace_id] = ws
    before = ws.get_output("source", "dataset", copy=False)
    checkpoint = manager.save_checkpoint(ws.workspace_id, pipeline.serialize())
    assert checkpoint.artifact_refs and ws.artifacts.stats()["pinned"] >= 1
    assert len(checkpoint.snapshot) and "artifacts" not in checkpoint.snapshot

    ws.clear_node("source")  # a pinned payload survives reference removal
    restored, _ = manager.load_checkpoint(checkpoint.checkpoint_id)
    assert restored.get_output("source", "dataset", copy=False) is before
    assert restored.node_status["model"] == NodeStatus.SUCCESS

    manager.delete_checkpoint(checkpoint.checkpoint_id)
    assert ws.artifacts.stats()["pinned"] == 0


def test_limited_cache_keeps_runs_correct(tmp_path: Path, registry):
    from fault_platform.examples import create_dataset
    from fault_platform.graph import ComponentGraph
    from fault_platform.runtime import ExecutionEngine

    data_root = tmp_path / "data"
    create_dataset(data_root / "synthetic_equipment.csv")
    graph = ComponentGraph(registry, "cache", "cache_pipeline")
    graph.add_node("data.input", "source", {"path": "synthetic_equipment.csv"})
    graph.add_node(
        "feature.statistical",
        "stats",
        {"columns": ["vibration"], "group_column": "equipment", "label_column": "label", "window_size": 16},
    )
    graph.add_node("validation.random_forest", "model", {"n_estimators": 10, "split_method": "group"})
    graph.connect("source", "dataset", "stats", "dataset")
    graph.connect("stats", "features", "model", "features")
    graph.connect("stats", "labels", "model", "labels")

    workspace = FaultWorkspace("cache_pipeline", artifact_cache_bytes=1)
    engine = ExecutionEngine()
    engine.execute(graph, ExecutionContext(workspace, data_root))
    # The tiny budget is enforced between runs: nothing is retained, results are
    # released and the pipeline drops back to READY instead of claiming SUCCESS.
    assert workspace.artifacts.stats()["evictions"] > 0
    assert workspace.node_results == {}
    assert workspace.status == PipelineStatus.READY
    assert any(entry.node_id == "model" and entry.success for entry in workspace.history)

    # A second full run recomputes everything instead of failing on missing outputs.
    processed = len(workspace.history)
    engine.execute(graph, ExecutionContext(workspace, data_root))
    assert any(entry.node_id == "model" and entry.success for entry in workspace.history[processed:])


def test_generous_cache_keeps_results_available(tmp_path: Path, registry):
    from fault_platform.examples import create_dataset
    from fault_platform.graph import ComponentGraph
    from fault_platform.runtime import ExecutionEngine

    data_root = tmp_path / "data"
    create_dataset(data_root / "synthetic_equipment.csv")
    graph = ComponentGraph(registry, "cache", "cache_pipeline2")
    graph.add_node("data.input", "source", {"path": "synthetic_equipment.csv"})
    graph.add_node(
        "feature.statistical",
        "stats",
        {"columns": ["vibration"], "group_column": "equipment", "label_column": "label", "window_size": 16},
    )
    graph.add_node("validation.random_forest", "model", {"n_estimators": 10, "split_method": "group"})
    graph.connect("source", "dataset", "stats", "dataset")
    graph.connect("stats", "features", "model", "features")
    graph.connect("stats", "labels", "model", "labels")

    workspace = FaultWorkspace("cache_pipeline2", artifact_cache_bytes=64 * 1024 * 1024)
    ExecutionEngine().execute(graph, ExecutionContext(workspace, data_root))
    stats = workspace.artifacts.stats()
    assert stats["evictions"] == 0 and stats["bytes"] > 0
    assert workspace.get_output("model", "metrics")["accuracy"] > 0.8
    assert stats["bytes"] < 64 * 1024 * 1024


def test_budget_spills_to_disk_instead_of_dropping(tmp_path: Path):
    frame = pd.DataFrame({"a": np.arange(10_000.0), "b": np.zeros(10_000)})
    size = estimate_size(frame)
    store = MemoryArtifactStore(max_bytes=int(size * 1.5), spill_dir=tmp_path / "spill")
    references = [store.put(frame.copy(), owner=f"n{index}") for index in range(3)]
    store.enforce_budget()
    stats = store.stats()
    assert stats["evictions"] == 0, "spilling should take precedence over dropping"
    assert stats["spilled"] >= 1 and stats["disk_bytes"] > 0
    assert stats["bytes"] <= int(size * 1.5)
    spilling = [ref for ref, entry in store._entries.items() if entry.spilled]
    assert spilling
    restored = store.peek(spilling[0])
    assert restored.equals(frame)  # the payload is still available after spilling
    assert store.stats()["loads"] == 1
    assert all(store.contains(reference) for reference in references)


def test_pinned_artifacts_are_never_spilled(tmp_path: Path):
    frame = pd.DataFrame({"a": np.arange(10_000.0)})
    size = estimate_size(frame)
    store = MemoryArtifactStore(max_bytes=int(size * 1.5), spill_dir=tmp_path / "spill")
    pinned_reference = store.put(frame.copy())
    store.pin([pinned_reference])
    store.put(frame.copy())
    store.enforce_budget()
    entry = store._entries[pinned_reference]
    assert entry.pins == 1 and not entry.spilled


def test_spilled_results_stay_readable_and_delete_removes_files(tmp_path: Path):
    from fault_platform.examples import create_dataset
    from fault_platform.graph import ComponentGraph
    from fault_platform.runtime import ExecutionEngine

    registry = __import__("fault_platform.registry", fromlist=["default_registry"]).default_registry()
    data_root = tmp_path / "data"
    create_dataset(data_root / "synthetic_equipment.csv")
    graph = ComponentGraph(registry, "spill", "spill_pipeline")
    graph.add_node("data.input", "source", {"path": "synthetic_equipment.csv"})
    graph.add_node(
        "feature.statistical",
        "stats",
        {"columns": ["vibration"], "group_column": "equipment", "label_column": "label", "window_size": 16},
    )
    graph.add_node("validation.random_forest", "model", {"n_estimators": 10, "split_method": "group"})
    graph.connect("source", "dataset", "stats", "dataset")
    graph.connect("stats", "features", "model", "features")
    graph.connect("stats", "labels", "model", "labels")

    spill_dir = tmp_path / "spill"
    workspace = FaultWorkspace("spill_pipeline", artifact_cache_bytes=1, spill_dir=spill_dir)
    ExecutionEngine().execute(graph, ExecutionContext(workspace, data_root))
    stats = workspace.artifacts.stats()
    assert stats["evictions"] == 0 and stats["spills"] > 0
    assert workspace.status == PipelineStatus.SUCCESS  # spilling keeps results complete
    assert workspace.node_status["model"] == NodeStatus.SUCCESS
    assert workspace.get_output("model", "metrics")["accuracy"] > 0.8
    assert list(spill_dir.glob("*.pickle")), "spilled payloads should exist on disk"

    references = sorted(workspace.references())
    workspace.artifacts.delete(references[0])
    workspace.cleanup()
    assert not list(spill_dir.glob("*.pickle"))
