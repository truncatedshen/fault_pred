"""Shared control API: both the web editor and MCP call these operations.

这是平台的"控制面"：网页设计器与 MCP bridge 都通过 HTTP 调用这里的方法，
因此两边改的是同一张图、同一份运行状态（这也是"人机协同编辑"能成立的原因）。

三个贯穿全文件的约定：

1. **单一入口与统一契约**。所有操作都在 :data:`CONTROL_OPERATIONS` 中登记，
   用 ``validate_call`` 做严格参数校验，并被 :meth:`PipelineService.dispatch` 包成
   ``{"success": bool, ...}`` 的观测结果——失败也返回结构化错误，而不是抛栈给客户端。
2. **一把全局锁**。除 :data:`BLOCKING_OPERATIONS`（只有 ``wait_for_pipeline``，它会 sleep）
   之外，所有操作都在同一把可重入锁下执行，保证图编辑与状态读取不会看到中间态。
3. **编辑即失效**。任何图编辑都会把该方案的 workspace 标记为"图已变更"、
   状态退回 CREATED，并广播 ``graph_changed`` 事件，让打开的网页立刻刷新——
   宁可从零重算，也不让旧结果冒充当前结果。
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from threading import Event, RLock
from typing import Any
from uuid import uuid4

from pydantic import ValidationError, validate_call

from fault_platform.events import EventBus, Subscription
from fault_platform.examples import create_dataset, example_graph
from fault_platform.graph import ComponentGraph, Connection
from fault_platform.registry import ComponentRegistry, default_registry
from fault_platform.runtime import ExecutionContext, ExecutionEngine
from fault_platform.version import PLATFORM_VERSION
from fault_platform.workspace import FaultWorkspace, PipelineStatus, WorkspaceManager, json_safe
from fault_platform.xml_io import XMLParser, XMLSerializer

#: 全部控制操作；顺序即工具列表顺序，MCP bridge 会为每一个生成同名工具。
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
    "retrieve_components",
    "get_component_facets",
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
        """初始化服务：数据目录、存储目录、组件注册表、workspace 管理器与事件总线。

        * ``data_root``：所有数据路径的沙箱根目录（组件只能读它里面的文件）；
        * ``storage_root``：XML 与运行期文件的存放处，默认 ``<data_root>/.fault-platform``；
        * ``artifact_cache_mb`` + ``artifact_spill_dir``：产物缓存预算与溢写目录。
          溢写目录带 ``session-<随机后缀>``，**上一次进程遗留的文件不会被复用**；
        * 线程池固定 2 个工作线程：不同方案可以并行跑，同一方案仍由任务的先后顺序约束。
        """
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
        """停止服务：唤醒等待中的执行、关闭事件总线、等待线程池收尾并释放全部产物。"""
        for event in self.events.values():
            event.set()
        self.bus.close()
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.workspaces.cleanup()

    def dispatch(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """所有控制调用的统一入口：校验、加锁、执行，并把结果包成有界观测。

        返回约定：成功时 ``success=True`` 并展开操作自身的返回值；
        失败时 ``success=False`` 加 ``error_code``/``summary``/``recommended_action``。
        业务上"校验不通过"（``validate_pipeline`` 的 ``valid=False``）也算 success=False，
        因为它同样表示"这次请求没有达到预期效果"。
        任何路径都不会把原始产物（大表、模型）直接返回——产物只以摘要与引用出现。
        """
        try:
            if operation not in self.operations:
                raise ValueError(f"Unknown operation: {operation}")
            if operation in BLOCKING_OPERATIONS:
                # wait_for_pipeline 会 sleep：持有全局锁会让其它请求（包括网页刷新）全部卡住。
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
            # 只捕获"可预期的失败"：参数错误、缺对象、校验失败、文件问题。
            # 真正的 bug（如 AttributeError）仍然抛出，避免被伪装成普通错误信息。
            return {
                "success": False,
                "error_code": type(exc).__name__,
                "summary": str(exc)[:3000],
                "recommended_action": "Check parameters, ports and current run status.",
            }

    def _graph(self, pipeline_id: str, editable: bool = False) -> ComponentGraph:
        """取图；``editable=True`` 时禁止在运行中编辑（避免改到正在执行的图）。"""
        if pipeline_id not in self.graphs:
            raise ValueError(f"Unknown pipeline: {pipeline_id}")
        if editable and pipeline_id in self.jobs and not self.jobs[pipeline_id].done():
            raise ValueError("Pipeline is running; wait or cancel before editing")
        return self.graphs[pipeline_id]

    def _workspace(self, pipeline_id: str, workspace_id: str | None = None) -> FaultWorkspace:
        """取 workspace：显式给 id 就用它，否则用该方案最近一次运行的 workspace。"""
        self._graph(pipeline_id)
        key = workspace_id or self.latest.get(pipeline_id)
        if not key:
            raise ValueError("Pipeline has not been executed")
        return self.workspaces.get_workspace(key, pipeline_id)

    def _invalidate(self, pipeline_id: str) -> None:
        """图被编辑后让旧结果失效：状态退回 CREATED 并标记 ``graph_changed``。

        标记的作用是让读取接口返回 ``PENDING`` + "Graph changed; run to refresh results."，
        而不是把上一版图算出的结果当成当前结果。
        """
        for ws in self.workspaces.workspaces.values():
            if ws.pipeline_id == pipeline_id:
                ws.status = PipelineStatus.CREATED
                ws.metadata["graph_changed"] = True
                ws.touch()

    def _changed(self, pipeline_id: str, operation: str) -> None:
        """广播 ``graph_changed`` 事件，让打开的网页实时刷新图。"""
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
        """新建一张空图，返回其 id 与序列化内容。"""
        graph = ComponentGraph(self.registry, name)
        self.graphs[graph.pipeline_id] = graph
        self._changed(graph.pipeline_id, "create_pipeline")
        return {"pipeline_id": graph.pipeline_id, "graph": graph.serialize()}

    def list_pipelines(self) -> dict[str, Any]:
        """列出当前进程里的全部方案（只给 id/名称/节点数，不返回图本体）。"""
        return {
            "pipelines": [
                {"id": g.pipeline_id, "name": g.name, "nodes": len(g.nodes)} for g in self.graphs.values()
            ]
        }

    def get_pipeline(self, pipeline_id: str) -> dict[str, Any]:
        """取某个方案的完整序列化图。需要结构信息时调用它一次，而不是逐次编辑都回吐整图。"""
        return {"pipeline_id": pipeline_id, "graph": self._graph(pipeline_id).serialize()}

    def _revision(self, pipeline_id: str, include_graph: bool) -> dict[str, Any]:
        """编辑操作的轻量回执：版本号与节点/边计数；``include_graph=True`` 时才附整图。"""
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

    @staticmethod
    def _describe_entry(entry: Any, keys: tuple[str, ...]) -> str:
        """给批量报错用的条目摘要；条目根本不是字典时也不能再炸一次。"""
        if not isinstance(entry, dict):
            return repr(entry)[:80]
        return ", ".join(f"{key}={entry.get(key)!r}" for key in keys)

    @staticmethod
    def _rollback_nodes(graph: ComponentGraph, node_ids: list[str], version_before: int) -> None:
        """回滚一次批量新增：删掉已加的节点，并把版本号退回调用前。

        版本号也要退：否则一次被拒的调用会留下"版本涨了但内容没变"的状态，
        让持有旧 ``expected_version`` 的客户端凭空收到并发冲突。
        """
        for node_id in reversed(node_ids):
            if node_id in graph.nodes:
                graph.remove_node(node_id)
        graph.version = version_before

    def delete_pipeline(self, pipeline_id: str) -> dict[str, Any]:
        """删除方案及其全部运行痕迹：workspace、产物、溢写文件与检查点。"""
        self._graph(pipeline_id)
        job = self.jobs.get(pipeline_id)
        if job is not None and not job.done():
            # 运行中删除会让执行线程操作已释放的对象，因此要求先取消。
            raise ValueError("Pipeline is running; cancel it before deleting")
        self.graphs.pop(pipeline_id, None)
        self.jobs.pop(pipeline_id, None)
        self.events.pop(pipeline_id, None)
        self.latest.pop(pipeline_id, None)
        removed = self.workspaces.delete_by_pipeline(pipeline_id)
        self.bus.publish(pipeline_id, "pipeline_deleted")
        return {"pipeline_id": pipeline_id, "deleted": True, "workspaces_removed": removed}

    def get_server_info(self) -> dict[str, Any]:
        """服务自述：版本、Python 版本、数据目录与存储目录、数据文件数、组件数、缓存统计。

        Agent 用它回答"服务在哪读数据、现在占了多少内存"——这两件事以前只能靠读进程命令行猜。
        """
        files = sorted(
            path.relative_to(self.data_root).as_posix()
            for path in self.data_root.glob("**/*")
            if path.is_file() and path.suffix.lower() in {".csv", ".parquet", ".pq"}
        )
        return {
            "version": PLATFORM_VERSION,
            "python": sys.version.split()[0],
            "data_root": str(self.data_root),
            "storage_root": str(self.storage_root),
            "data_file_count": len(files),
            "data_files": files[:50],
            "components": len(self.registry),
            "pipelines": len(self.graphs),
            "artifact_cache": self.workspaces.cache_stats(),
        }

    def list_datasets(self) -> dict[str, Any]:
        """列出 ``data_root`` 下可读的数据文件（CSV/Parquet）及字节大小，最多 200 条。"""
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
        """整体替换一张图（前端"保存"与批量改图的落点）。

        ``expected_version`` 提供乐观并发控制：与当前版本不一致就拒绝，
        避免两个客户端（人 + Agent）互相覆盖对方的编辑。
        新图的版本号取"旧版本 + 1"，因此替换也算一次修订。
        """
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
        """从 XML 文本导入一张图；若 id 与已有方案冲突则分配新 id，保证是"新建"而非覆盖。"""
        graph = XMLParser(self.registry).loads(xml)
        # 导入语义是"新增文档"，不能悄悄覆盖已经打开的方案。
        if graph.pipeline_id in self.graphs:
            graph.pipeline_id = f"pipeline_{uuid4().hex[:12]}"
        self.graphs[graph.pipeline_id] = graph
        self._changed(graph.pipeline_id, "load_pipeline")
        return {"pipeline_id": graph.pipeline_id, "graph": graph.serialize()}

    def save_pipeline(self, pipeline_id: str, filename: str | None = None) -> dict[str, Any]:
        """把图保存成 XML。

        路径被限制在 ``storage_root`` 内且后缀必须是 ``.xml``；
        写入采用"先写临时文件再原子替换"，避免写到一半被读到半个文件。
        """
        graph = self._graph(pipeline_id)
        filename = filename or f"{pipeline_id}.xml"
        destination = (self.storage_root / filename).resolve()
        if not destination.is_relative_to(self.storage_root) or destination.suffix.lower() != ".xml":
            raise ValueError("Save path must be an XML file within the pipeline storage directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # 先写 .tmp 再 replace：崩溃时最多留下临时文件，不会损坏已保存的方案。
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
        limit: int = 20,
        include_schema: bool = False,
        offset: int = 0,
        subcategory: str | None = None,
        version: str | None = None,
        compatibility: str | None = None,
    ) -> dict[str, Any]:
        """分页列出组件；返回 ``total``/``returned``/``has_more`` 支持翻页与"还有多少"判断。

        ``limit`` 上限 500，``include_schema=False``（默认）不返回参数表——浏览目录时不需要它，
        需要时再对少数组件调 ``get_component_schema``。
        """
        filters = {
            "category": category,
            "query": query,
            "tags": tags,
            "input_type": input_type,
            "output_type": output_type,
            "subcategory": subcategory,
            "version": version,
            "compatibility": compatibility,
        }
        total = self.registry.count(**filters)
        bounded_limit = max(0, min(limit, 500))
        bounded_offset = max(0, offset)
        return {
            "components": self.registry.list(
                limit=bounded_limit,
                include_schema=include_schema,
                offset=bounded_offset,
                **filters,
            ),
            "total": total,
            "returned": min(bounded_limit, max(0, total - bounded_offset)),
            "offset": bounded_offset,
            "limit": bounded_limit,
            "has_more": bounded_offset + bounded_limit < total,
        }

    def search_components(
        self,
        query: str,
        category: str | None = None,
        tags: list[str] | None = None,
        input_type: str | None = None,
        output_type: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """关键词检索组件（``list_components`` 的便捷包装，等价于 ``query=...``）。"""
        return self.list_components(
            category=category,
            query=query,
            tags=tags,
            input_type=input_type,
            output_type=output_type,
            limit=limit,
            include_schema=False,
        )

    def retrieve_components(
        self,
        intent: str,
        category: str | None = None,
        tags: list[str] | None = None,
        input_type: str | None = None,
        output_type: str | None = None,
        source_component_type: str | None = None,
        target_component_type: str | None = None,
        limit: int = 10,
        include_schema: bool = False,
    ) -> dict[str, Any]:
        """按自然语言意图检索组件，返回带 ``score`` 与 ``match_reasons`` 的排序结果。

        ``source_component_type``/``target_component_type`` 是"往已有图里插组件"的关键：
        指定后只返回"能把上游输出接进来、且输出能被下游接收"的类型。
        """
        components = self.registry.retrieve(
            intent=intent,
            category=category,
            tags=tags,
            input_type=input_type,
            output_type=output_type,
            source_component_type=source_component_type,
            target_component_type=target_component_type,
            limit=limit,
            include_schema=include_schema,
        )
        return {
            "components": components,
            "returned": len(components),
            "limit": max(0, min(limit, 50)),
        }

    def get_component_schema(self, component_type: str) -> dict[str, Any]:
        """取单个组件的完整 schema（端口类型、参数定义、实现类）。"""
        return {"component": self.registry.get(component_type).schema()}

    def get_component_facets(self) -> dict[str, Any]:
        """目录导航：分类/子分类/标签/版本/兼容区间的计数分布，外加组件总数与平台版本。

        组件规模变大后，Agent 应先看 facets 决定范围，再对少量组件拉 schema。
        """
        return {
            "total": self.registry.count(),
            "platform_version": PLATFORM_VERSION,
            "facets": self.registry.facets(),
        }

    def add_component(
        self,
        pipeline_id: str,
        component_type: str,
        node_id: str | None = None,
        parameters: dict[str, Any] | None = None,
        position: dict[str, float] | None = None,
        include_graph: bool = True,
    ) -> dict[str, Any]:
        """添加一个组件节点；默认返回整图（``include_graph=false`` 时只回执计数）。"""
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

        中文说明：批量新增是"用几次调用搭好一张图"的关键——默认不回吐整图，只返回版本号、
        节点/边计数与新增节点列表。**要么全部成功，要么一条都不落**：任何一条被拒都会把已加
        的节点回滚干净（版本号一起退回），并在报错里点名是第几条、哪个组件，以及"什么都没加"。
        """
        if not components:
            raise ValueError("components must contain at least one entry")
        graph = self._graph(pipeline_id, editable=True)
        version_before = graph.version
        added: list[dict[str, Any]] = []
        for position, spec in enumerate(components, start=1):
            detail = self._describe_entry(spec, ("component_type", "node_id"))
            try:
                node = graph.add_node(
                    spec["component_type"], spec.get("node_id"), spec.get("parameters"), spec.get("position")
                )
            except (KeyError, TypeError, ValueError) as exc:
                self._rollback_nodes(graph, [entry["node_id"] for entry in added], version_before)
                raise ValueError(
                    f"add_components rejected entry {position} of {len(components)} ({detail}): {exc}. "
                    "Nothing was added: the graph is exactly as it was before the call."
                ) from exc
            added.append({"node_id": node.id, "component_type": node.component.component_type})
        self._invalidate(pipeline_id)
        self._changed(pipeline_id, "add_components")
        return {**self._revision(pipeline_id, include_graph), "added": added, "added_count": len(added)}

    def remove_component(self, pipeline_id: str, node_id: str, include_graph: bool = True) -> dict[str, Any]:
        """删除节点及其相关边。"""
        self._graph(pipeline_id, editable=True).remove_node(node_id)
        self._invalidate(pipeline_id)
        self._changed(pipeline_id, "remove_component")
        return self._revision(pipeline_id, include_graph)

    def configure_component(
        self, pipeline_id: str, node_id: str, parameters: dict[str, Any], include_graph: bool = True
    ) -> dict[str, Any]:
        """修改单个节点的参数（参数会按 schema 校验，失败则整体回滚）。"""
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
        """连接两个端口；类型不匹配、目标输入已占用或成环都会被拒绝。"""
        self._graph(pipeline_id, editable=True).connect(source_node, source_port, target_node, target_port)
        self._invalidate(pipeline_id)
        self._changed(pipeline_id, "connect_components")
        return self._revision(pipeline_id, include_graph)

    def connect_many(
        self, pipeline_id: str, connections: list[dict[str, Any]], include_graph: bool = False
    ) -> dict[str, Any]:
        """Wire many ports in one call.

        Each entry: ``{"source_node": ..., "source_port": ..., "target_node": ..., "target_port": ...}``.

        中文说明：批量连线同样默认不回吐整图，只返回连线数与回执。
        **要么全部成功，要么一条都不连**：任何一条被拒都会把已经连上的边删掉，
        并在报错里点名是第几条、哪条边。
        """
        if not connections:
            raise ValueError("connections must contain at least one entry")
        graph = self._graph(pipeline_id, editable=True)
        version_before = graph.version
        applied: list[Connection] = []
        for position, edge in enumerate(connections, start=1):
            detail = self._describe_entry(edge, ("source_node", "source_port", "target_node", "target_port"))
            try:
                applied.append(
                    graph.connect(
                        edge["source_node"], edge["source_port"], edge["target_node"], edge["target_port"]
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                for done in reversed(applied):
                    graph.disconnect(done.source_node, done.source_port, done.target_node, done.target_port)
                graph.version = version_before
                raise ValueError(
                    f"connect_many rejected entry {position} of {len(connections)} ({detail}): {exc}. "
                    "No connections were made."
                ) from exc
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
        """断开一条连线。"""
        self._graph(pipeline_id, editable=True).disconnect(source_node, source_port, target_node, target_port)
        self._invalidate(pipeline_id)
        self._changed(pipeline_id, "disconnect_components")
        return self._revision(pipeline_id, include_graph)

    def configure_components(
        self, pipeline_id: str, updates: list[dict[str, Any]], include_graph: bool = False
    ) -> dict[str, Any]:
        """Update several nodes parameters in one call.

        Each entry: ``{"node_id": ..., "parameters": {...}}``.

        中文说明：修改后重新执行时，只有"参数变了的节点及其下游"会重算（指纹机制），
        因此改一个阈值不需要重跑整张图。**要么全部成功，要么一条都不改**：任何一条被拒
        都会把已经改过的节点恢复回原参数，并在报错里点名是第几条、哪个节点。
        """
        if not updates:
            raise ValueError("updates must contain at least one entry")
        graph = self._graph(pipeline_id, editable=True)
        version_before = graph.version
        applied: list[tuple[str, dict[str, Any]]] = []
        for position, update in enumerate(updates, start=1):
            detail = self._describe_entry(update, ("node_id",))
            try:
                node_id = update["node_id"]
                previous = deepcopy(graph.get_node(node_id).component.parameters)
                graph.configure(node_id, update["parameters"])
            except (KeyError, TypeError, ValueError) as exc:
                for applied_id, previous_parameters in reversed(applied):
                    graph.configure(applied_id, previous_parameters)
                graph.version = version_before
                raise ValueError(
                    f"configure_components rejected entry {position} of {len(updates)} ({detail}): {exc}. "
                    "Nothing was changed: every node keeps its previous parameters."
                ) from exc
            applied.append((node_id, previous))
        self._invalidate(pipeline_id)
        self._changed(pipeline_id, "configure_components")
        return {**self._revision(pipeline_id, include_graph), "updated_count": len(updates)}

    def validate_pipeline(self, pipeline_id: str) -> dict[str, Any]:
        """只做结构校验，不执行任何计算：返回 ``{"valid": bool, "errors": [...]}``。

        校验包括图结构（端口/必填输入/环/重复连接）、组件兼容性与组件自身的外部资源预检。
        先用它把关，比让一次真实执行失败要便宜得多。
        """
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
        """线程池里的执行体：跑引擎、清"图已变更"标记、异常兜底并广播结束事件。"""
        try:
            self.engine.execute(graph, context, mode, node_id, incremental)
            # 执行成功说明"当前图"与"当前结果"重新一致，可以撤掉待刷新标记。
            context.workspace.metadata.pop("graph_changed", None)
        except Exception as exc:
            # 运行时自身出错也要落到 workspace 上；否则状态会永远停在 RUNNING，
            # 等待方只能一直等到超时。
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
            # 只有"这次运行的 workspace 仍是该方案的最新"时才广播 finished，
            # 避免并发运行互相覆盖事件语义。
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
        """启动一次异步执行，立即返回 ``RUNNING`` 与 ``workspace_id``。

        要点：

        * 执行前先 ``validate_pipeline``，有错直接拒绝，不占用线程池；
        * 提交给线程池的是图的**克隆**，因此运行期间前端/Agent 的编辑不会影响这次执行；
        * ``mode``：``all`` 全图、``node`` 单节点、``from`` 该节点及其全部下游；
        * 引擎的事件回调接进事件总线，网页因此能看到实时节点状态与耗时。
        """
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
        """只执行一个节点（强制重算，不走增量复用）。"""
        return self.execute_pipeline(pipeline_id, workspace_id, False, "node", node_id)

    def execute_from_node(
        self, pipeline_id: str, node_id: str, workspace_id: str | None = None
    ) -> dict[str, Any]:
        """从某节点开始重算（含其全部下游），上游产物复用。"""
        return self.execute_pipeline(pipeline_id, workspace_id, False, "from", node_id)

    def retry_node(self, pipeline_id: str, node_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        """重试失败节点：等价于 :meth:`execute_from_node`（修好参数后用它最省时间）。"""
        return self.execute_from_node(pipeline_id, node_id, workspace_id)

    def cancel_pipeline(self, pipeline_id: str) -> dict[str, Any]:
        """请求取消：置位取消事件，引擎在**下一个节点开始前**退出本次运行。"""
        self._graph(pipeline_id)
        if pipeline_id in self.events:
            self.events[pipeline_id].set()
        return {
            "pipeline_id": pipeline_id,
            "summary": "Cancellation requested; finishes current component first",
        }

    def get_pipeline_status(self, pipeline_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        """查询运行状态：整体状态、逐节点状态、警告与错误；从未运行过时返回 ``CREATED``。"""
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
        """显式提醒"你读到的不是最新一次运行"，而不是默默返回旧结果。"""
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

        中文说明：这是唯一会阻塞的操作，因此**不持有全局锁**（见模块文档）。
        轮询间隔限制在 0.05~5 秒：太密空转，太稀会让短任务白等。
        超时不是错误——返回 ``timed_out=True`` 与当前状态，让调用方自行决定继续等、
        取消，还是先看部分结果。
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
        """取单个节点的结果摘要（各端口产物的 ``kind``/预览/引用 + 错误 + 警告）。

        图被编辑过但结果还没刷新时，直接返回 ``PENDING`` 与提示，避免误读旧结果；
        ``include_indices=False``（默认）会把长索引数组折叠成计数。
        """
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
        """取方案汇总：状态、逐节点摘要（默认最后 10 个节点）、缓存统计与警告。

        ``truncated_nodes=True`` 表示还有节点没列出来，需要时再用 ``get_node_result`` 单独取。
        """
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
        """直接把图导出成 XML 文本（不落盘），便于外部系统取用。"""
        return {"pipeline_id": pipeline_id, "xml": XMLSerializer().dumps(self._graph(pipeline_id))}

    def get_history(
        self, pipeline_id: str, workspace_id: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        """取执行历史（默认最近 100 条、上限 200）：状态迁移、耗时、输入/输出摘要。"""
        ws = self._workspace(pipeline_id, workspace_id)
        with ws.lock:
            return {"history": [json_safe(asdict(h)) for h in ws.history[-max(1, min(limit, 200)) :]]}

    def subscribe_events(
        self, pipeline_id: str | None = None, last_event_id: int = 0
    ) -> tuple[Subscription, list[Any]]:
        """订阅事件流，返回 ``(订阅对象, 需要补发的历史事件)``；未知方案先报错。

        HTTP 层用它实现 SSE：先补发 ``last_event_id`` 之后的事件，再持续读取新事件。
        """
        if pipeline_id is not None:
            self._graph(pipeline_id)
        return self.bus.subscribe(pipeline_id, last_event_id)

    def unsubscribe_events(self, subscription: Subscription) -> None:
        """取消订阅并关闭其队列（浏览器断开连接时调用）。"""
        self.bus.unsubscribe(subscription)

    def save_checkpoint(self, pipeline_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        """保存检查点（图 + workspace 快照；产物按引用固定共享，不复制数据）。"""
        graph = self._graph(pipeline_id, editable=True)
        ws = self._workspace(pipeline_id, workspace_id)
        if ws.metadata.get("graph_changed"):
            # 图变了但没重跑：快照与图已经不一致，保存下来只会误导。
            raise ValueError("Execute edited graph before checkpointing")
        checkpoint = self.workspaces.save_checkpoint(ws.workspace_id, graph.serialize())
        self.bus.publish(pipeline_id, "checkpoint", checkpoint_id=checkpoint.checkpoint_id, action="save")
        return {"checkpoint": checkpoint.summary()}

    def load_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        """恢复检查点：同时还原图与 workspace，并把这次恢复当作一次新的图修订。"""
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
        """列出某个方案的检查点摘要（不含图与快照本体）。"""
        self._graph(pipeline_id)
        return {
            "checkpoints": [
                cp.summary() for cp in self.workspaces.checkpoints.values() if cp.pipeline_id == pipeline_id
            ]
        }

    def create_example(self, include_xgboost: bool = False) -> dict[str, Any]:
        """生成合成数据与完整示例方案（数据文件已存在时直接复用，绝不覆盖用户文件）。"""
        path = create_dataset(self.data_root / "synthetic_equipment.csv")
        graph = example_graph(self.registry, path.relative_to(self.data_root).as_posix(), include_xgboost)
        self.graphs[graph.pipeline_id] = graph
        return {"pipeline_id": graph.pipeline_id, "graph": graph.serialize()}
