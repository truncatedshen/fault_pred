"""Deterministic dependency-aware execution, independent of Agent frameworks."""

from __future__ import annotations

import hashlib
import json
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any, Callable

import pandas as pd

from fault_platform.components.base import ComponentResult, DataType
from fault_platform.graph import ComponentGraph
from fault_platform.streaming import StreamedDataset
from fault_platform.workspace import (
    ExecutionHistory,
    FaultWorkspace,
    NodeStatus,
    PipelineStatus,
    now,
    summarize,
)


@dataclass
class ExecutionContext:
    workspace: FaultWorkspace
    data_root: Path
    cancel_event: Event = field(default_factory=Event)
    #: Optional observability hook (the web event stream). Execution never depends on it.
    on_event: Callable[[str, dict[str, Any]], None] | None = None
    #: Suffixes a data source may use; components decide what they can actually read.
    data_suffixes: frozenset[str] = frozenset({".csv", ".parquet", ".pq"})

    def resolve_data_path(self, value: str) -> Path:
        root = self.data_root.resolve()
        path = (root / value).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Data path must remain inside the configured data directory")
        if not path.is_file():
            raise ValueError(f"Data source does not exist: {value}")
        if path.suffix.lower() not in self.data_suffixes:
            raise ValueError(f"Unsupported data source type: {path.suffix or value}")
        return path


def validate_value(value: Any, data_type: DataType) -> None:
    if data_type in {DataType.DATASET, DataType.TIME_SERIES} and isinstance(value, StreamedDataset):
        return  # a lazy source still satisfies the Dataset contract
    if data_type in {
        DataType.DATASET,
        DataType.TIME_SERIES,
        DataType.FEATURE_DATASET,
        DataType.CORRELATION,
        DataType.PREDICTION,
        DataType.IMPORTANCE,
    }:
        valid = isinstance(value, pd.DataFrame)
    elif data_type == DataType.LABEL_VECTOR:
        valid = isinstance(value, pd.Series)
    elif data_type == DataType.MODEL:
        valid = callable(getattr(value, "predict", None))
    elif data_type == DataType.FEATURE_TRANSFORMER:
        valid = callable(getattr(value, "transform", None))
    else:
        valid = isinstance(value, dict)
    if not valid:
        raise ValueError(f"Expected runtime value {data_type}, received {type(value).__name__}")


def emit(context: ExecutionContext, event_type: str, **data: Any) -> None:
    """Forward a state transition to observers; observability must never break a run."""
    if context.on_event is None:
        return
    try:
        context.on_event(event_type, data)
    except Exception:  # pragma: no cover - a broken listener must not fail execution
        pass


class ExecutionEngine:
    def validate(self, graph: ComponentGraph, context: ExecutionContext) -> list[str]:
        errors = graph.validate_graph()
        for node in graph.nodes.values():
            try:
                node.component.preflight(context)
            except (ValueError, TypeError) as exc:
                errors.append(f"{node.id}: {exc}")
        return errors

    def fingerprints(self, graph: ComponentGraph, context: ExecutionContext) -> dict[str, str]:
        result = {}
        for node_id in graph.topological_sort():
            node = graph.nodes[node_id]
            payload: dict[str, Any] = {
                "component": node.component.serialize(),
                "version": node.component.metadata.version,
                "inputs": sorted(
                    (e.target_port, e.source_node, e.source_port, result[e.source_node])
                    for e in graph.edges
                    if e.target_node == node_id
                ),
                "external": node.component.external_fingerprint(context),
            }
            result[node_id] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return result

    def execute(
        self,
        graph: ComponentGraph,
        context: ExecutionContext,
        mode: str = "all",
        node_id: str | None = None,
        incremental: bool = True,
    ) -> FaultWorkspace:
        ws = context.workspace
        if ws.pipeline_id != graph.pipeline_id:
            raise ValueError("Workspace belongs to another pipeline")
        if mode not in {"all", "node", "from"}:
            raise ValueError("Execution mode must be all, node or from")
        if mode != "all":
            graph.get_node(node_id or "")
        # Every upstream output is still needed by some pending node while the DAG
        # runs, so the cache budget applies between runs, not inside one.
        ws.artifacts.suspend()
        try:
            return self._execute_locked(graph, context, mode, node_id, incremental)
        finally:
            ws.artifacts.resume()

    def _execute_locked(
        self,
        graph: ComponentGraph,
        context: ExecutionContext,
        mode: str,
        node_id: str | None,
        incremental: bool,
    ) -> FaultWorkspace:
        ws = context.workspace
        with ws.lock:
            ws.status = PipelineStatus.VALIDATING
            ws.metadata["graph_version"] = graph.version
            ws.warnings = []
        emit(context, "pipeline_status", status=PipelineStatus.VALIDATING, workspace_id=ws.workspace_id)
        errors = self.validate(graph, context)
        if errors:
            with ws.lock:
                ws.status = PipelineStatus.FAILED
                ws.errors["_validation"] = {
                    "error_type": "ValidationError",
                    "error_message": "; ".join(errors),
                }
                ws.touch()
            emit(context, "pipeline_status", status=PipelineStatus.FAILED, errors=errors)
            return ws
        current = self.fingerprints(graph, context)
        # Clear stale outputs even outside a selected execution subset.
        for removed in set(ws.node_status) - set(graph.nodes):
            ws.clear_node(removed)
            ws.node_status.pop(removed, None)
        for node in graph.nodes:
            if ws.fingerprints.get(node) != current[node]:
                ws.clear_node(node)
        if mode == "all":
            selected = set(graph.nodes)
        elif mode == "node":
            selected = {node_id}
        else:
            selected = {node_id, *graph.descendants(node_id)}
        if not incremental:
            for node in selected:
                ws.clear_node(node)
                for downstream in graph.descendants(node):
                    ws.clear_node(downstream)
        ws.errors.pop("_validation", None)
        ws.status = PipelineStatus.READY
        ws.status = PipelineStatus.RUNNING
        emit(context, "pipeline_status", status=PipelineStatus.RUNNING, selected=sorted(selected))
        failed = False
        for current_id in graph.topological_sort():
            if current_id not in selected:
                continue
            if context.cancel_event.is_set():
                with ws.lock:
                    ws.status = PipelineStatus.CANCELLED
                    ws.touch()
                emit(context, "pipeline_status", status=PipelineStatus.CANCELLED)
                return ws
            node = graph.nodes[current_id]
            component = node.component
            before = ws.node_status.get(current_id, NodeStatus.PENDING)
            incoming = [e for e in graph.edges if e.target_node == current_id]
            unavailable = [
                e.source_node for e in incoming if ws.node_status.get(e.source_node) != NodeStatus.SUCCESS
            ]
            if unavailable:
                ws.node_status[current_id] = NodeStatus.SKIPPED
                emit(
                    context,
                    "node_status",
                    node_id=current_id,
                    status=NodeStatus.SKIPPED,
                    upstream=sorted(set(unavailable)),
                )
                ws.history.append(
                    ExecutionHistory(
                        now(),
                        current_id,
                        component.component_type,
                        before,
                        NodeStatus.SKIPPED,
                        0,
                        {},
                        {},
                        False,
                        {"error_message": f"Upstream results unavailable: {unavailable}"},
                    )
                )
                failed = True
                continue
            if incremental and ws.fingerprints.get(current_id) == current[current_id]:
                ws.node_status[current_id] = NodeStatus.SUCCESS
                ws.warnings.extend(ws.node_warnings.get(current_id, []))
                emit(context, "node_status", node_id=current_id, status=NodeStatus.SUCCESS, cached=True)
                emit(
                    context,
                    "history",
                    node_id=current_id,
                    status=NodeStatus.SUCCESS,
                    cached=True,
                    execution_time=0.0,
                    success=True,
                )
                ws.history.append(
                    ExecutionHistory(
                        now(),
                        current_id,
                        component.component_type,
                        before,
                        NodeStatus.SUCCESS,
                        0,
                        {},
                        {},
                        True,
                        cached=True,
                    )
                )
                continue
            inputs: dict[str, Any] = {}
            started = time.perf_counter()
            with ws.lock:
                ws.node_status[current_id] = NodeStatus.READY
                ws.node_status[current_id] = NodeStatus.RUNNING
            emit(context, "node_status", node_id=current_id, status=NodeStatus.RUNNING)
            try:
                for edge in incoming:
                    inputs[edge.target_port] = ws.get_output(edge.source_node, edge.source_port)
                for port in component.input_ports:
                    if port.name in inputs:
                        validate_value(inputs[port.name], port.data_type)
                        if isinstance(inputs[port.name], StreamedDataset) and not component.accepts_streaming:
                            raise ValueError(
                                f"{component.component_type} cannot consume streamed input; "
                                "insert data.materialize or turn streaming off on data.input"
                            )
                result = component.execute(inputs, context)
                if not isinstance(result, ComponentResult):
                    raise ValueError("Component must return ComponentResult")
                ports = {p.name: p for p in component.output_ports}
                if result.outputs.keys() - ports.keys():
                    raise ValueError("Component returned undeclared output ports")
                for port in component.output_ports:
                    if port.required and port.name not in result.outputs:
                        raise ValueError(f"Missing required output: {port.name}")
                    if port.name in result.outputs:
                        validate_value(result.outputs[port.name], port.data_type)
                with ws.lock:
                    ws.store_outputs(current_id, result.outputs)
                    ws.node_status[current_id] = NodeStatus.SUCCESS
                    ws.fingerprints[current_id] = current[current_id]
                    ws.warnings.extend(result.warnings)
                    ws.node_warnings[current_id] = list(result.warnings)
                error = None
                output_summary = {k: summarize(v, 3) for k, v in result.outputs.items()}
                emit(context, "node_status", node_id=current_id, status=NodeStatus.SUCCESS)
            except Exception as exc:
                error = {
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "stack": traceback.format_exc(),
                    "parameters": component.parameters.copy(),
                    "input_summary": {k: summarize(v, 3) for k, v in inputs.items()},
                }
                with ws.lock:
                    ws.clear_node(current_id)
                    ws.node_status[current_id] = NodeStatus.FAILED
                    ws.errors[current_id] = error
                output_summary = {}
                failed = True
                emit(
                    context,
                    "node_status",
                    node_id=current_id,
                    status=NodeStatus.FAILED,
                    error_type=error["error_type"],
                    error_message=error["error_message"],
                )
            emit(
                context,
                "history",
                node_id=current_id,
                status=ws.node_status[current_id],
                execution_time=round(time.perf_counter() - started, 6),
                cached=False,
                success=error is None,
                error_message=error["error_message"] if error else None,
            )
            with ws.lock:
                ws.history.append(
                    ExecutionHistory(
                        now(),
                        current_id,
                        component.component_type,
                        before,
                        ws.node_status[current_id],
                        time.perf_counter() - started,
                        {k: summarize(v, 3) for k, v in inputs.items()},
                        output_summary,
                        error is None,
                        error,
                    )
                )
                ws.touch()
        with ws.lock:
            if context.cancel_event.is_set():
                ws.status = PipelineStatus.CANCELLED
            elif failed or any(v in {NodeStatus.FAILED, NodeStatus.SKIPPED} for v in ws.node_status.values()):
                ws.status = PipelineStatus.FAILED
            elif all(ws.node_status.get(n) == NodeStatus.SUCCESS for n in graph.nodes):
                ws.status = PipelineStatus.SUCCESS
            else:
                ws.status = PipelineStatus.READY
            ws.warnings = list(dict.fromkeys(ws.warnings))
            ws.touch()
            final_status = ws.status
        emit(
            context,
            "pipeline_status",
            status=final_status,
            node_status={node: str(status) for node, status in ws.node_status.items()},
            warnings=list(ws.warnings),
        )
        return ws
