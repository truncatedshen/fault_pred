"""Shared control API: both the web editor and MCP call these operations."""

from __future__ import annotations

import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from threading import Event, RLock
from typing import Any
from uuid import uuid4

from pydantic import ValidationError, validate_call

from fault_platform.events import EventBus, Subscription
from fault_platform.examples import create_dataset, example_graph
from fault_platform.graph import ComponentGraph
from fault_platform.registry import ComponentRegistry, default_registry
from fault_platform.runtime import ExecutionContext, ExecutionEngine
from fault_platform.workspace import FaultWorkspace, PipelineStatus, WorkspaceManager, json_safe
from fault_platform.xml_io import XMLParser, XMLSerializer

CONTROL_OPERATIONS = (
    "create_pipeline",
    "list_pipelines",
    "get_pipeline",
    "delete_pipeline",
    "replace_pipeline",
    "load_pipeline",
    "save_pipeline",
    "get_server_info",
    "list_datasets",
    "list_components",
    "search_components",
    "get_component_schema",
    "add_component",
    "add_components",
    "remove_component",
    "configure_component",
    "configure_components",
    "connect_components",
    "connect_many",
    "disconnect_components",
    "validate_pipeline",
    "execute_pipeline",
    "execute_node",
    "execute_from_node",
    "retry_node",
    "cancel_pipeline",
    "get_pipeline_status",
    "wait_for_pipeline",
    "get_node_result",
    "get_pipeline_result",
    "get_pipeline_xml",
    "save_checkpoint",
    "load_checkpoint",
    "list_checkpoints",
    "get_history",
    "create_example",
)

#: Operations that block on purpose; they must not hold the service lock.
BLOCKING_OPERATIONS = frozenset({"wait_for_pipeline"})


class PipelineService:
    def __init__(
        self,
        data_root: str | Path,
        storage_root: str | Path | None = None,
        registry: ComponentRegistry | None = None,
        artifact_cache_mb: int | None = None,
        artifact_spill_dir: str | Path | None = None,
    ) -> None:
        self.data_root = Path(data_root).resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.storage_root = Path(storage_root or self.data_root / ".fault-platform").resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.registry = registry or default_registry()
        self.graphs: dict[str, ComponentGraph] = {}
        cache_bytes = None if artifact_cache_mb is None else int(artifact_cache_mb) * 1024 * 1024
        if cache_bytes is None or artifact_spill_dir is None:
            spill_root = None
        else:
            # Session-scoped directory: stale files from a previous process are not reused.
            spill_root = Path(artifact_spill_dir).resolve() / f"session-{uuid4().hex[:8]}"
        self.workspaces = WorkspaceManager(cache_bytes, spill_root)
        self.latest: dict[str, str] = {}
        self.engine = ExecutionEngine()
        self.lock = RLock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fault-runtime")
        self.jobs: dict[str, Future] = {}
        self.events: dict[str, Event] = {}
        self.bus = EventBus()
        self.operations = {
            name: validate_call(getattr(self, name), config={"strict": True}) for name in CONTROL_OPERATIONS
        }

    def close(self) -> None:
        for event in self.events.values():
            event.set()
        self.bus.close()
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.workspaces.cleanup()

    def dispatch(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Bounded success/error observations; never return raw artifacts."""
        try:
            if operation not in self.operations:
                raise ValueError(f"Unknown operation: {operation}")
            if operation in BLOCKING_OPERATIONS:
                # wait_for_pipeline sleeps; holding the global lock would freeze the service.
                result = self.operations[operation](**arguments)
            else:
                with self.lock:
                    result = self.operations[operation](**arguments)
            success = True
            if operation == "validate_pipeline":
                success = result["valid"]
            return {
                "success": success,
                "summary": operation.replace("_", " "),
                **result,
                "warnings": result.get("warnings", []),
            }
        except (ValueError, KeyError, TypeError, OSError, ValidationError) as exc:
            return {
                "success": False,
                "error_code": type(exc).__name__,
                "summary": str(exc)[:3000],
                "recommended_action": "Check parameters, ports and current run status.",
            }

    def _graph(self, pipeline_id: str, editable: bool = False) -> ComponentGraph:
        if pipeline_id not in self.graphs:
            raise ValueError(f"Unknown pipeline: {pipeline_id}")
        if editable and pipeline_id in self.jobs and not self.jobs[pipeline_id].done():
            raise ValueError("Pipeline is running; wait or cancel before editing")
        return self.graphs[pipeline_id]

    def _workspace(self, pipeline_id: str, workspace_id: str | None = None) -> FaultWorkspace:
        self._graph(pipeline_id)
        key = workspace_id or self.latest.get(pipeline_id)
        if not key:
            raise ValueError("Pipeline has not been executed")
        return self.workspaces.get_workspace(key, pipeline_id)

    def _invalidate(self, pipeline_id: str) -> None:
        """Edited graphs must not continue advertising old results as current."""
        for ws in self.workspaces.workspaces.values():
            if ws.pipeline_id == pipeline_id:
                ws.status = PipelineStatus.CREATED
                ws.metadata["graph_changed"] = True
                ws.touch()

    def _changed(self, pipeline_id: str, operation: str) -> None:
        """Announce a graph revision so open designers can refresh."""
        graph = self.graphs.get(pipeline_id)
        self.bus.publish(
            pipeline_id,
            "graph_changed",
            version=graph.version if graph else 0,
            operation=operation,
            name=graph.name if graph else "",
            nodes=len(graph.nodes) if graph else 0,
        )

    def create_pipeline(self, name: str = "未命名方案") -> dict[str, Any]:
        graph = ComponentGraph(self.registry, name)
        self.graphs[graph.pipeline_id] = graph
        self._changed(graph.pipeline_id, "create_pipeline")
        return {"pipeline_id": graph.pipeline_id, "graph": graph.serialize()}

    def list_pipelines(self) -> dict[str, Any]:
        return {
            "pipelines": [
                {"id": g.pipeline_id, "name": g.name, "nodes": len(g.nodes)} for g in self.graphs.values()
            ]
        }

    def get_pipeline(self, pipeline_id: str) -> dict[str, Any]:
        return {"pipeline_id": pipeline_id, "graph": self._graph(pipeline_id).serialize()}

    def _revision(self, pipeline_id: str, include_graph: bool) -> dict[str, Any]:
        """Small acknowledgement for edits: counts and version, optionally the whole graph."""
        graph = self.graphs[pipeline_id]
        payload: dict[str, Any] = {
            "pipeline_id": pipeline_id,
            "version": graph.version,
            "node_count": len(graph.nodes),
            "edge_count": len(graph.edges),
        }
        if include_graph:
            payload["graph"] = graph.serialize()
        return payload

    def delete_pipeline(self, pipeline_id: str) -> dict[str, Any]:
        """Remove a pipeline, its workspaces, spilled files and checkpoints."""
        self._graph(pipeline_id)
        job = self.jobs.get(pipeline_id)
        if job is not None and not job.done():
            raise ValueError("Pipeline is running; cancel it before deleting")
        self.graphs.pop(pipeline_id, None)
        self.jobs.pop(pipeline_id, None)
        self.events.pop(pipeline_id, None)
        self.latest.pop(pipeline_id, None)
        removed = self.workspaces.delete_by_pipeline(pipeline_id)
        self.bus.publish(pipeline_id, "pipeline_deleted")
        return {"pipeline_id": pipeline_id, "deleted": True, "workspaces_removed": removed}

    def get_server_info(self) -> dict[str, Any]:
        """Where the service reads data from and how much it is holding."""
        files = sorted(
            path.relative_to(self.data_root).as_posix()
            for path in self.data_root.glob("**/*")
            if path.is_file() and path.suffix.lower() in {".csv", ".parquet", ".pq"}
        )
        return {
            "version": "0.1.0",
            "python": sys.version.split()[0],
            "data_root": str(self.data_root),
            "storage_root": str(self.storage_root),
            "data_file_count": len(files),
            "data_files": files[:50],
            "components": len(self.registry.list(limit=500)),
            "pipelines": len(self.graphs),
            "artifact_cache": self.workspaces.cache_stats(),
        }

    def list_datasets(self) -> dict[str, Any]:
        return {
            "data_root": str(self.data_root),
            "datasets": [
                {"path": path.relative_to(self.data_root).as_posix(), "bytes": path.stat().st_size}
                for path in sorted(self.data_root.glob("**/*"))
                if path.is_file() and path.suffix.lower() in {".csv", ".parquet", ".pq"}
            ][:200],
        }

    def replace_pipeline(
        self, pipeline_id: str, graph: dict[str, Any], expected_version: int | None = None
    ) -> dict[str, Any]:
        previous = self._graph(pipeline_id, editable=True)
        if expected_version is not None and expected_version != previous.version:
            raise ValueError("Graph changed in another client; reload before editing")
        if graph.get("id") != pipeline_id:
            raise ValueError("Pipeline ID cannot change")
        candidate = ComponentGraph.deserialize(graph, self.registry)
        candidate.version = previous.version + 1
        self.graphs[pipeline_id] = candidate
        self._invalidate(pipeline_id)
        self._changed(pipeline_id, "replace_pipeline")
        return self.get_pipeline(pipeline_id)

    def load_pipeline(self, xml: str) -> dict[str, Any]:
        graph = XMLParser(self.registry).loads(xml)
        # Import is a new document, preserving an existing open pipeline.
        if graph.pipeline_id in self.graphs:
            graph.pipeline_id = f"pipeline_{uuid4().hex[:12]}"
        self.graphs[graph.pipeline_id] = graph
        self._changed(graph.pipeline_id, "load_pipeline")
        return {"pipeline_id": graph.pipeline_id, "graph": graph.serialize()}

    def save_pipeline(self, pipeline_id: str, filename: str | None = None) -> dict[str, Any]:
        graph = self._graph(pipeline_id)
        filename = filename or f"{pipeline_id}.xml"
        destination = (self.storage_root / filename).resolve()
        if not destination.is_relative_to(self.storage_root) or destination.suffix.lower() != ".xml":
            raise ValueError("Save path must be an XML file within the pipeline storage directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        XMLSerializer().save(graph, temporary)
        temporary.replace(destination)
        return {"pipeline_id": pipeline_id, "path": str(destination)}

    def list_components(
        self,
        category: str | None = None,
        query: str = "",
        tags: list[str] | None = None,
        input_type: str | None = None,
        output_type: str | None = None,
        limit: int = 100,
        include_schema: bool = True,
    ) -> dict[str, Any]:
        return {
            "components": self.registry.list(
                category, query, tags, input_type, output_type, limit, include_schema
            )
        }

    def search_components(self, query: str, limit: int = 20) -> dict[str, Any]:
        return self.list_components(query=query, limit=limit, include_schema=False)

    def get_component_schema(self, component_type: str) -> dict[str, Any]:
        return {"component": self.registry.get(component_type).schema()}

    def add_component(
        self,
        pipeline_id: str,
        component_type: str,
        node_id: str | None = None,
        parameters: dict[str, Any] | None = None,
        position: dict[str, float] | None = None,
        include_graph: bool = True,
    ) -> dict[str, Any]:
        graph = self._graph(pipeline_id, editable=True)
        node = graph.add_node(component_type, node_id, parameters, position)
        self._invalidate(pipeline_id)
        self._changed(pipeline_id, "add_component")
        payload = self._revision(pipeline_id, include_graph)
        payload["node"] = (
            node.serialize() if include_graph else {"id": node.id, "type": node.component.component_type}
        )
        return payload

    def add_components(
        self, pipeline_id: str, components: list[dict[str, Any]], include_graph: bool = False
    ) -> dict[str, Any]:
        """Add many components in one call.

        Each entry: ``{"component_type": ..., "node_id": ..., "parameters": {...}, "position": {...}}``.
        Returns counts by default; pass ``include_graph=true`` for the full graph.
        """
        if not components:
            raise ValueError("components must contain at least one entry")
        graph = self._graph(pipeline_id, editable=True)
        added = []
        for spec in components:
            if "component_type" not in spec:
                raise ValueError("Every component entry needs component_type")
            node = graph.add_node(
                spec["component_type"], spec.get("node_id"), spec.get("parameters"), spec.get("position")
            )
            added.append({"node_id": node.id, "component_type": node.component.component_type})
        self._invalidate(pipeline_id)
        self._changed(pipeline_id, "add_components")
        return {**self._revision(pipeline_id, include_graph), "added": added, "added_count": len(added)}

    def remove_component(self, pipeline_id: str, node_id: str, include_graph: bool = True) -> dict[str, Any]:
        self._graph(pipeline_id, editable=True).remove_node(node_id)
        self._invalidate(pipeline_id)
        self._changed(pipeline_id, "remove_component")
        return self._revision(pipeline_id, include_graph)

    def configure_component(
        self, pipeline_id: str, node_id: str, parameters: dict[str, Any], include_graph: bool = True
    ) -> dict[str, Any]:
        self._graph(pipeline_id, editable=True).configure(node_id, parameters)
        self._invalidate(pipeline_id)
        self._changed(pipeline_id, "configure_component")
        return self._revision(pipeline_id, include_graph)

    def connect_components(
        self,
        pipeline_id: str,
        source_node: str,
        source_port: str,
        target_node: str,
        target_port: str,
        include_graph: bool = True,
    ) -> dict[str, Any]:
        self._graph(pipeline_id, editable=True).connect(source_node, source_port, target_node, target_port)
        self._invalidate(pipeline_id)
        self._changed(pipeline_id, "connect_components")
        return self._revision(pipeline_id, include_graph)

    def connect_many(
        self, pipeline_id: str, connections: list[dict[str, Any]], include_graph: bool = False
    ) -> dict[str, Any]:
        """Wire many ports in one call.

        Each entry: ``{"source_node": ..., "source_port": ..., "target_node": ..., "target_port": ...}``.
        """
        if not connections:
            raise ValueError("connections must contain at least one entry")
        graph = self._graph(pipeline_id, editable=True)
        for edge in connections:
            graph.connect(edge["source_node"], edge["source_port"], edge["target_node"], edge["target_port"])
        self._invalidate(pipeline_id)
        self._changed(pipeline_id, "connect_many")
        return {**self._revision(pipeline_id, include_graph), "connected_count": len(connections)}

    def disconnect_components(
        self,
        pipeline_id: str,
        source_node: str,
        source_port: str,
        target_node: str,
        target_port: str,
        include_graph: bool = True,
    ) -> dict[str, Any]:
        self._graph(pipeline_id, editable=True).disconnect(source_node, source_port, target_node, target_port)
        self._invalidate(pipeline_id)
        self._changed(pipeline_id, "disconnect_components")
        return self._revision(pipeline_id, include_graph)

    def configure_components(
        self, pipeline_id: str, updates: list[dict[str, Any]], include_graph: bool = False
    ) -> dict[str, Any]:
        """Update several nodes' parameters in one call.

        Each entry: ``{"node_id": ..., "parameters": {...}}``.
        """
        if not updates:
            raise ValueError("updates must contain at least one entry")
        graph = self._graph(pipeline_id, editable=True)
        for update in updates:
            graph.configure(update["node_id"], update["parameters"])
        self._invalidate(pipeline_id)
        self._changed(pipeline_id, "configure_components")
        return {**self._revision(pipeline_id, include_graph), "updated_count": len(updates)}

    def validate_pipeline(self, pipeline_id: str) -> dict[str, Any]:
        graph = self._graph(pipeline_id)
        context = ExecutionContext(FaultWorkspace(pipeline_id), self.data_root)
        errors = self.engine.validate(graph, context)
        return {"pipeline_id": pipeline_id, "valid": not errors, "errors": errors}

    def _run(
        self,
        graph: ComponentGraph,
        context: ExecutionContext,
        mode: str,
        node_id: str | None,
        incremental: bool,
    ) -> None:
        try:
            self.engine.execute(graph, context, mode, node_id, incremental)
            context.workspace.metadata.pop("graph_changed", None)
        except Exception as exc:
            with context.workspace.lock:
                context.workspace.status = PipelineStatus.FAILED
                context.workspace.errors["_runtime"] = {
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
                context.workspace.touch()
            self.bus.publish(
                context.workspace.pipeline_id,
                "pipeline_status",
                status=PipelineStatus.FAILED,
                errors=[str(exc)],
            )
        finally:
            if self.latest.get(context.workspace.pipeline_id) == context.workspace.workspace_id:
                self.bus.publish(
                    context.workspace.pipeline_id,
                    "finished",
                    status=str(context.workspace.status),
                    workspace_id=context.workspace.workspace_id,
                )

    def execute_pipeline(
        self,
        pipeline_id: str,
        workspace_id: str | None = None,
        incremental: bool = True,
        mode: str = "all",
        node_id: str | None = None,
    ) -> dict[str, Any]:
        graph = self._graph(pipeline_id, editable=True)
        if mode not in {"all", "node", "from"}:
            raise ValueError("Invalid execution mode")
        if mode != "all":
            graph.get_node(node_id or "")
        errors = self.validate_pipeline(pipeline_id)["errors"]
        if errors:
            raise ValueError("; ".join(errors))
        key = workspace_id or self.latest.get(pipeline_id)
        ws = self._workspace(pipeline_id, key) if key else self.workspaces.create_workspace(pipeline_id)
        self.latest[pipeline_id] = ws.workspace_id
        event = Event()
        self.events[pipeline_id] = event
        ws.status = PipelineStatus.RUNNING
        self.bus.publish(
            pipeline_id,
            "run_started",
            workspace_id=ws.workspace_id,
            mode=mode,
            node_id=node_id,
            incremental=incremental,
        )
        self.jobs[pipeline_id] = self.pool.submit(
            self._run,
            graph.clone(),
            ExecutionContext(
                ws,
                self.data_root,
                event,
                on_event=lambda event_type, data: self.bus.publish(pipeline_id, event_type, **data),
            ),
            mode,
            node_id,
            incremental,
        )
        return {"pipeline_id": pipeline_id, "workspace_id": ws.workspace_id, "status": "RUNNING"}

    def execute_node(self, pipeline_id: str, node_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        return self.execute_pipeline(pipeline_id, workspace_id, False, "node", node_id)

    def execute_from_node(
        self, pipeline_id: str, node_id: str, workspace_id: str | None = None
    ) -> dict[str, Any]:
        return self.execute_pipeline(pipeline_id, workspace_id, False, "from", node_id)

    def retry_node(self, pipeline_id: str, node_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        return self.execute_from_node(pipeline_id, node_id, workspace_id)

    def cancel_pipeline(self, pipeline_id: str) -> dict[str, Any]:
        self._graph(pipeline_id)
        if pipeline_id in self.events:
            self.events[pipeline_id].set()
        return {
            "pipeline_id": pipeline_id,
            "summary": "Cancellation requested; finishes current component first",
        }

    def get_pipeline_status(self, pipeline_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        self._graph(pipeline_id)
        if not workspace_id and pipeline_id not in self.latest:
            return {"pipeline_id": pipeline_id, "status": "CREATED", "node_status": {}}
        workspace = self._workspace(pipeline_id, workspace_id)
        summary = workspace.get_summary()
        summary["warnings"] = [
            *summary.get("warnings", []),
            *self._workspace_warnings(pipeline_id, workspace_id),
        ]
        return summary

    def _workspace_warnings(self, pipeline_id: str, workspace_id: str | None) -> list[str]:
        """Flag a stale workspace instead of silently reporting old results."""
        latest = self.latest.get(pipeline_id)
        if workspace_id and latest and workspace_id != latest:
            return [f"Reading workspace {workspace_id}, which is not the pipeline's latest ({latest})."]
        return []

    def wait_for_pipeline(
        self,
        pipeline_id: str,
        workspace_id: str | None = None,
        timeout_seconds: float = 300.0,
        poll_seconds: float = 0.5,
    ) -> dict[str, Any]:
        """Block until the run reaches a terminal state; returns the status summary.

        ``timed_out`` is set when the deadline passes so an agent can decide to keep
        waiting, cancel, or inspect partial results.
        """
        self._graph(pipeline_id)
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        interval = max(0.05, min(float(poll_seconds), 5.0))
        while True:
            summary = self.get_pipeline_status(pipeline_id, workspace_id)
            if summary.get("status") in {"SUCCESS", "FAILED", "CANCELLED"}:
                return {**summary, "timed_out": False}
            if time.monotonic() >= deadline:
                return {
                    **summary,
                    "timed_out": True,
                    "timeout_seconds": timeout_seconds,
                }
            time.sleep(interval)

    def get_node_result(
        self,
        pipeline_id: str,
        node_id: str,
        workspace_id: str | None = None,
        limit: int = 20,
        include_indices: bool = False,
    ) -> dict[str, Any]:
        self._graph(pipeline_id).get_node(node_id)
        ws = self._workspace(pipeline_id, workspace_id)
        if ws.metadata.get("graph_changed"):
            return {
                "pipeline_id": pipeline_id,
                "workspace_id": ws.workspace_id,
                "node_id": node_id,
                "status": "PENDING",
                "outputs": {},
                "warnings": ["Graph changed; run to refresh results."],
            }
        return {
            "pipeline_id": pipeline_id,
            "workspace_id": ws.workspace_id,
            **ws.get_node_result(node_id, limit, include_indices),
            "warnings": self._workspace_warnings(pipeline_id, workspace_id),
        }

    def get_pipeline_result(
        self,
        pipeline_id: str,
        workspace_id: str | None = None,
        limit: int = 10,
        include_indices: bool = False,
    ) -> dict[str, Any]:
        graph = self._graph(pipeline_id)
        ws = self._workspace(pipeline_id, workspace_id)
        nodes = list(graph.nodes)[-max(1, min(limit, 50)) :]
        summary = ws.get_summary()
        return {
            **summary,
            "artifact_cache": ws.cache_stats(),
            "result_summary": {
                n: self.get_node_result(pipeline_id, n, ws.workspace_id, 5, include_indices) for n in nodes
            },
            "truncated_nodes": len(graph.nodes) > len(nodes),
            "warnings": [
                *summary.get("warnings", []),
                *self._workspace_warnings(pipeline_id, workspace_id),
            ],
        }

    def get_pipeline_xml(self, pipeline_id: str) -> dict[str, Any]:
        return {"pipeline_id": pipeline_id, "xml": XMLSerializer().dumps(self._graph(pipeline_id))}

    def get_history(
        self, pipeline_id: str, workspace_id: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        ws = self._workspace(pipeline_id, workspace_id)
        with ws.lock:
            return {"history": [json_safe(asdict(h)) for h in ws.history[-max(1, min(limit, 200)) :]]}

    def subscribe_events(
        self, pipeline_id: str | None = None, last_event_id: int = 0
    ) -> tuple[Subscription, list[Any]]:
        """Open an observation channel; unknown pipelines are rejected up front."""
        if pipeline_id is not None:
            self._graph(pipeline_id)
        return self.bus.subscribe(pipeline_id, last_event_id)

    def unsubscribe_events(self, subscription: Subscription) -> None:
        self.bus.unsubscribe(subscription)

    def save_checkpoint(self, pipeline_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        graph = self._graph(pipeline_id, editable=True)
        ws = self._workspace(pipeline_id, workspace_id)
        if ws.metadata.get("graph_changed"):
            raise ValueError("Execute edited graph before checkpointing")
        checkpoint = self.workspaces.save_checkpoint(ws.workspace_id, graph.serialize())
        self.bus.publish(pipeline_id, "checkpoint", checkpoint_id=checkpoint.checkpoint_id, action="save")
        return {"checkpoint": checkpoint.summary()}

    def load_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        if checkpoint_id not in self.workspaces.checkpoints:
            raise ValueError("Unknown checkpoint")
        cp = self.workspaces.checkpoints[checkpoint_id]
        self._graph(cp.pipeline_id, editable=True)
        ws, value = self.workspaces.load_checkpoint(checkpoint_id)
        graph = ComponentGraph.deserialize(value, self.registry)
        # A restore is a new graph revision for optimistic concurrency.
        graph.version = self.graphs[graph.pipeline_id].version + 1
        self.graphs[graph.pipeline_id] = graph
        self.latest[graph.pipeline_id] = ws.workspace_id
        self._changed(graph.pipeline_id, "load_checkpoint")
        return {"pipeline_id": graph.pipeline_id, "workspace_id": ws.workspace_id, "graph": graph.serialize()}

    def list_checkpoints(self, pipeline_id: str) -> dict[str, Any]:
        self._graph(pipeline_id)
        return {
            "checkpoints": [
                cp.summary() for cp in self.workspaces.checkpoints.values() if cp.pipeline_id == pipeline_id
            ]
        }

    def create_example(self, include_xgboost: bool = False) -> dict[str, Any]:
        path = create_dataset(self.data_root / "synthetic_equipment.csv")
        graph = example_graph(self.registry, path.relative_to(self.data_root).as_posix(), include_xgboost)
        self.graphs[graph.pipeline_id] = graph
        return {"pipeline_id": graph.pipeline_id, "graph": graph.serialize()}
