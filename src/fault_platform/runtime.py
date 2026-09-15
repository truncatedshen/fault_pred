"""Deterministic dependency-aware execution, independent of Agent frameworks.

运行时只做一件事：按拓扑序执行 DAG，并把每个节点的状态、产物与历史记进 workspace。
它不认识 MCP、HTTP 与前端；可观测性通过 :class:`ExecutionContext` 上的 ``on_event`` 回调
（可缺省）向外传递，因此"没有监听者"和"监听者崩了"都不会影响执行结果。

三条核心机制：

1. **指纹与增量**：每个节点的指纹由"组件配置 + 组件版本 + 各输入的指纹 + 外部资源指纹"
   哈希得到。指纹未变则直接复用上次产物（``cached=True``），改了参数只会重算它和下游。
2. **失败传播**：上游不是 SUCCESS 时下游标记 ``SKIPPED``（并记录缺失的上游），
   异常节点标记 ``FAILED`` 并保存类型、消息、栈、参数快照与输入预览，便于直接定位。
3. **取消**：通过 ``cancel_event`` 协作式取消，在每个节点开始前检查。
"""

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

from fault_platform.components.base import ComponentResult, DataType, InputPort
from fault_platform.graph import ComponentGraph
from fault_platform.streaming import StreamedDataset
from fault_platform.version import PLATFORM_VERSION, compatibility_error
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
    """一次执行的环境：workspace（产物与状态）、数据根目录、取消信号与事件回调。"""

    workspace: FaultWorkspace
    data_root: Path
    cancel_event: Event = field(default_factory=Event)
    #: Optional observability hook (the web event stream). Execution never depends on it.
    on_event: Callable[[str, dict[str, Any]], None] | None = None
    #: Suffixes a data source may use; components decide what they can actually read.
    data_suffixes: frozenset[str] = frozenset({".csv", ".parquet", ".pq"})

    def resolve_data_path(self, value: str) -> Path:
        """把组件里的相对路径解析成绝对路径，并做三重校验。

        1. 必须落在 ``data_root`` 内（拒绝绝对路径与 ``..`` 逃逸）；
        2. 文件必须存在；
        3. 后缀必须在允许的集合内（默认 CSV/Parquet）。

        这三条一起构成本地服务的文件访问边界：组件永远拿不到数据目录之外的文件。
        """
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
    """校验一个产物是否符合端口声明的类型契约。

    允许的唯一宽松之处：``StreamedDataset`` 也算合法的 ``Dataset``
    （它是惰性数据源，满足相同契约）；其余情况严格按类型判断，
    例如模型必须可 ``predict``、特征变换器必须可 ``transform``。
    """
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


def validate_input(value: Any, port: InputPort) -> None:
    """输入端口的值校验：满足声明的任一兼容类型即可。

    输出端口永远只有一个类型，所以这里只比 :func:`validate_value` 宽松一点点，
    但报错信息保持同样的形状（``Expected runtime value X, received Y``）。
    """
    for candidate in port.accepted_types:
        try:
            validate_value(value, candidate)
            return
        except ValueError:
            continue
    allowed = " or ".join(item.value for item in port.accepted_types)
    raise ValueError(f"Expected runtime value {allowed}, received {type(value).__name__}")


def emit(context: ExecutionContext, event_type: str, **data: Any) -> None:
    """把状态变化转发给观察者（网页 SSE）；观察者异常一律吞掉。

    可观测性是"尽力而为"的旁路：没有订阅者时什么都不做，
    订阅者抛异常也不会让一次正常执行失败。
    """
    if context.on_event is None:
        return
    try:
        context.on_event(event_type, data)
    except Exception:  # pragma: no cover - a broken listener must not fail execution
        pass


class ExecutionEngine:
    def validate(self, graph: ComponentGraph, context: ExecutionContext) -> list[str]:
        """执行前的完整校验：图结构 + 组件兼容性 + 各组件的外部资源预检。

        返回**错误字符串列表**（空列表表示通过）而不是抛异常，
        这样调用方可以一次性把全部问题展示给用户或 Agent。
        """
        errors = graph.validate_graph()
        for node in graph.nodes.values():
            # 兼容性：组件声明的 fault-platform 版本区间必须包含当前平台版本。
            unsatisfied = compatibility_error(node.component.metadata.compatibility, PLATFORM_VERSION)
            if unsatisfied:
                errors.append(f"{node.id}: {node.component.component_type} {unsatisfied}")
            try:
                node.component.preflight(context)
            except (ValueError, TypeError) as exc:
                # preflight 用于检查外部资源（数据文件是否存在等），属于"结果已知的失败"。
                errors.append(f"{node.id}: {exc}")
        return errors

    def fingerprints(self, graph: ComponentGraph, context: ExecutionContext) -> dict[str, str]:
        """按拓扑序计算每个节点的指纹（SHA-256）。

        指纹内容：组件序列化结果（含参数）、组件版本、**每个输入的（端口, 上游节点, 上游端口,
        上游指纹）四元组**、以及外部资源指纹。因为输入里嵌了上游指纹，
        改动任一上游参数都会让整条下游链的指纹变化——这正是增量执行的基础。
        """
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
        """执行入口：``mode`` 决定跑全图、单节点还是"从某节点起"。

        ``incremental=True`` 时复用指纹未变的节点产物；``False`` 时强制重算选中范围及其下游。
        执行期间**暂停**产物缓存的淘汰：DAG 运行中每个上游产物都可能还被 pending 节点需要，
        淘汰会让正常执行失败。预算因此在两次运行之间生效。
        """
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
        """真正的执行主体：校验 → 计算指纹 → 清理失效产物 → 按拓扑序逐节点执行 → 定终态。"""
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
        # 清掉"图里已经不存在"的节点状态：编辑图（删节点）后不留幽灵结果。
        for removed in set(ws.node_status) - set(graph.nodes):
            ws.clear_node(removed)
            ws.node_status.pop(removed, None)
        for node in graph.nodes:
            # 指纹变了（改了参数/上游变了/外部文件变了）就清空该节点产物，等待重算。
            if ws.fingerprints.get(node) != current[node]:
                ws.clear_node(node)
        if mode == "all":
            selected = set(graph.nodes)
        elif mode == "node":
            selected = {node_id}
        else:
            selected = {node_id, *graph.descendants(node_id)}
        if not incremental:
            # 强制模式：选中范围及其下游全部重算，其它分支仍可复用。
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
                # 协作式取消：只保证"当前节点跑完之后不再继续"，不会中断正在执行的组件。
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
                # 上游没成功（失败或跳过）时下游无法计算，标记 SKIPPED 并记录缺失的上游节点。
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
                # 缓存命中：直接把节点标为成功并回放上次的警告，不重新计算。
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
                    # get_output 默认返回隔离副本语义的产物引用；上游必须是 SUCCESS 才有值。
                    inputs[edge.target_port] = ws.get_output(edge.source_node, edge.source_port)
                for port in component.input_ports:
                    if port.name in inputs:
                        validate_input(inputs[port.name], port)
                        if isinstance(inputs[port.name], StreamedDataset) and not component.accepts_streaming:
                            # 惰性数据集只能喂给声明支持流式的组件，否则提示先物化。
                            raise ValueError(
                                f"{component.component_type} cannot consume streamed input; "
                                "insert data.materialize or turn streaming off on data.input"
                            )
                result = component.execute(inputs, context)
                if not isinstance(result, ComponentResult):
                    raise ValueError("Component must return ComponentResult")
                ports = {p.name: p for p in component.output_ports}
                if result.outputs.keys() - ports.keys():
                    # 组件返回了未声明的端口：属于实现错误，立即失败而不是悄悄忽略。
                    raise ValueError("Component returned undeclared output ports")
                for port in component.output_ports:
                    if port.required and port.name not in result.outputs:
                        raise ValueError(f"Missing required output: {port.name}")
                    if port.name in result.outputs:
                        validate_value(result.outputs[port.name], port.data_type)
                with ws.lock:
                    ws.store_outputs(current_id, result.outputs)
                    ws.node_status[current_id] = NodeStatus.SUCCESS
                    # 只有成功才写入指纹：失败节点下次仍会被重算。
                    ws.fingerprints[current_id] = current[current_id]
                    ws.warnings.extend(result.warnings)
                    ws.node_warnings[current_id] = list(result.warnings)
                error = None
                output_summary = {k: summarize(v, 3) for k, v in result.outputs.items()}
                emit(context, "node_status", node_id=current_id, status=NodeStatus.SUCCESS)
            except Exception as exc:
                # 失败信息要能自证：异常类型 + 消息 + 栈 + 当时的参数 + 输入预览（前几行）。
                # 报告里常见的定位线索（"这一列恒为 0"）就来自 input_summary。
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
                # 历史按节点逐条记录：状态迁移、耗时、输入/输出摘要、是否缓存。
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
            # 终态优先级：取消 > 有失败/跳过 > 全部成功 > 其它（只跑了子集，回到 READY）。
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
