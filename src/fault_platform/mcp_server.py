"""MCP 1.x stdio bridge to the same HTTP service used by the visual editor.

这个文件**不实现任何业务逻辑**：它读取 :data:`fault_platform.service.CONTROL_OPERATIONS`
里的 38 个操作，为每一个动态生成一个 MCP 工具，工具调用被转发成
``POST /api/control/<name>``。因此三个入口（网页、MCP、Python API）改的是同一张图。

四个细节：

* 工具签名照抄服务方法的类型注解（``get_type_hints``），客户端因此能看到参数名与类型；
* 工具描述取 :data:`DESCRIPTIONS`，**每个操作都必须有一条**——没有描述时兜底生成的一句
  "Wait For Pipeline." 等于让 Agent 靠猜，``tests/test_mcp_bridge.py`` 会守住这条；
* **超时按工具区分**：``wait_for_pipeline`` 会阻塞到服务端等满 ``timeout_seconds``
  （没有在途任务时立即返回 ``started=false``，不再空等，见服务端 docstring），
  所以客户端超时必须跟着这个参数走（默认 300 s），其余操作 60 s。
  早期版本一律 60 s，于是真实数据上跑两分钟的任务会在第 60 秒被误报成"服务不可达"
  （实测 60.2 s 返回 CONTROL_API_UNAVAILABLE、服务端仍在 RUNNING，见
  ``scripts/mcp_wait_probe.py``）；Agent 若因此重启服务，内存里的图与产物会全部丢失；
* 传输层失败返回结构化错误（``error_code`` + ``summary`` + ``recommended_action``），
  并且**区分"超时"与"服务没起来"**，不再含糊地把两者说成一回事。
"""

from __future__ import annotations

import inspect
from typing import Any, get_type_hints

import httpx

from fault_platform.service import CONTROL_OPERATIONS, PipelineService

#: 普通操作的客户端超时；绝大多数控制操作在毫秒级返回。
DEFAULT_TIMEOUT_SECONDS = 60.0
#: 等待类工具的余地：服务端最多等 ``timeout_seconds``，客户端再多留一点。
WAIT_TIMEOUT_MARGIN_SECONDS = 30.0
#: 没传 ``timeout_seconds`` 时服务端会用这个默认值（与 PipelineService 保持一致）。
DEFAULT_WAIT_TIMEOUT_SECONDS = 300.0

DESCRIPTIONS = {
    "get_server_info": "Report version, data_root/storage_root, dataset and component counts, artifact-cache stats. Call it first whenever the environment is unclear.",
    "list_datasets": "List the readable CSV/Parquet files under data_root with byte sizes. Use it instead of guessing a data path.",
    "get_component_facets": "Catalogue navigation: categories, subcategories, tags, versions and compatibility ranges.",
    "list_components": "Find components by category, keyword, tags and port types; request detailed schemas selectively.",
    "search_components": "Search the component registry by keyword.",
    "retrieve_components": "Rank components for an intent with optional category and typed-port compatibility.",
    "get_component_schema": "Get exact parameter and typed port schemas for a component.",
    "create_pipeline": "Create an empty component graph. Returns the pipeline_id every other call needs.",
    "list_pipelines": "List the pipelines currently held in memory with ids and versions.",
    "get_pipeline": "Return one pipeline serialized graph (nodes, edges, positions). Call it once when the whole structure is needed.",
    "delete_pipeline": "Delete a pipeline with its workspaces, spilled files and checkpoints. Use it after abandoning a failed attempt.",
    "replace_pipeline": "Overwrite a graph with optimistic concurrency (expected_version); a stale version is refused instead of clobbering another editor.",
    "save_pipeline": "Save graph XML in the configured local pipeline directory.",
    "load_pipeline": "Import XML as a graph using registered components. Does not load runtime artifacts.",
    "get_pipeline_xml": "Return the pipeline XML document inline instead of writing a file.",
    "export_python": "Write the pipeline as a standalone runnable Python file (nodes, parameters and edges as code) that rebuilds and runs the same graph through the platform's Python API without the service. Returns the path; pass include_code=true to also get the source inline.",
    "create_example": "Create a synthetic equipment dataset and an example comparison graph.",
    "add_component": "Add one configured component instance to a graph.",
    "add_components": "Add many nodes in one call; prefer this over repeated add_component, and pass include_graph=false to keep the reply small. All or nothing: a rejected entry names its position and rolls the whole batch back, leaving the graph untouched.",
    "remove_component": "Remove a node together with the edges touching it.",
    "configure_component": "Update one node parameters with schema validation.",
    "configure_components": "Update many nodes parameters in one call. All or nothing: a rejected entry names its position and restores every node already updated in the same call.",
    "connect_components": "Connect typed ports; rejects cycles and multiple producers for one input.",
    "connect_many": "Connect many port pairs in one call; prefer this over repeated connect_components. All or nothing: a rejected connection names its position and removes the edges already made in the same call.",
    "disconnect_components": "Remove one connection by its endpoints (source node/port to target node/port).",
    "validate_pipeline": "Structural dry run: missing inputs, unset required parameters, type mismatches, cycles. Cheap; run it before every execution.",
    "execute_pipeline": "Start asynchronous DAG execution. mode=all|node|from, incremental=true reuses unchanged nodes. Then poll get_pipeline_status or wait with wait_for_pipeline.",
    "execute_node": "Run one node using valid upstream workspace outputs.",
    "execute_from_node": "Recompute a node and its descendants using valid upstream results.",
    "retry_node": "Retry a failed node and its descendants.",
    "cancel_pipeline": "Stop a running pipeline and keep the partial workspace.",
    "get_pipeline_status": "Overall status, per-node status, warnings and errors. This is the polling call while a run is in flight.",
    "wait_for_pipeline": "Block until the run reaches a terminal status (default timeout_seconds=300). Returns immediately with started=false if nothing is running (never started, or invalidated by a graph edit) instead of burning the timeout. On real datasets a long wait is not evidence that the service died.",
    "get_node_result": "Return bounded preview, metadata and artifact references, never a full dataset or model.",
    "get_pipeline_result": "Return bounded node summaries and metrics.",
    "get_history": "Per-node attempts with timings and the cached flag.",
    "save_checkpoint": "Create an independent in-memory snapshot of graph and workspace.",
    "load_checkpoint": "Restore both graph and workspace from an in-memory checkpoint.",
    "list_checkpoints": "List the checkpoints held in memory with their ids.",
}


def call_timeout_seconds(name: str, arguments: dict[str, Any]) -> float:
    """按工具算客户端超时：等待类工具跟着它自己的 ``timeout_seconds`` 走。

    这是 P0 修复的核心：``wait_for_pipeline`` 服务端默认等 300 s，客户端却固定 60 s，
    于是真实数据上的长跑会在第 60 秒被判成"服务不可达"。参数缺失或非法时退回默认值，
    真正非法时服务端自己会报参数错误。
    """
    if name != "wait_for_pipeline":
        return DEFAULT_TIMEOUT_SECONDS
    requested = arguments.get("timeout_seconds")
    try:
        wait = float(requested) if requested is not None else DEFAULT_WAIT_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        wait = DEFAULT_WAIT_TIMEOUT_SECONDS
    return max(DEFAULT_TIMEOUT_SECONDS, wait + WAIT_TIMEOUT_MARGIN_SECONDS)


def _proxy_for(name: str, base_url: str):
    """构造一个转发工具函数（独立出来，便于测试传输层失败的处理）。"""

    async def proxy(**arguments: Any) -> dict[str, Any]:
        """转发一次调用；传输层失败统一转成结构化错误对象。"""
        timeout = call_timeout_seconds(name, arguments)
        try:
            # trust_env=False：忽略系统代理，避免本机回环地址被代理劫持。
            async with httpx.AsyncClient(base_url=base_url, timeout=timeout, trust_env=False) as client:
                response = await client.post(f"/api/control/{name}", json=arguments)
        except httpx.TimeoutException as exc:
            # 超时 ≠ 服务没起来：服务端很可能还在算，绝不能诱导 Agent 去重启服务。
            return {
                "success": False,
                "error_code": "CONTROL_API_TIMEOUT",
                "summary": f"{name} waited longer than {timeout:.0f}s: {type(exc).__name__}",
                "recommended_action": (
                    "The run may still be alive: check get_pipeline_status before assuming failure, "
                    "and do not restart the service - in-memory graphs and artifacts would be lost."
                ),
            }
        except (httpx.HTTPError, ValueError) as exc:
            return {
                "success": False,
                "error_code": "CONTROL_API_UNAVAILABLE",
                "summary": f"{base_url} is unreachable: {type(exc).__name__}: {exc}",
                "recommended_action": (
                    "Start the service (python -m fault_platform serve --port 8765) and retry."
                ),
            }
        try:
            return response.json()
        except ValueError:
            return {
                "success": False,
                "error_code": "CONTROL_API_BAD_RESPONSE",
                "summary": f"HTTP {response.status_code} with a non-JSON body: {response.text[:200]}",
                "recommended_action": "Report this body to the operator; a non-JSON reply is a platform bug.",
            }

    return proxy


def create_mcp_server(base_url: str = "http://127.0.0.1:8765"):
    """构造 FastMCP 服务器，并为每个控制操作注册一个转发工具。"""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        # 缺少可选依赖时给出可直接执行的安装命令，而不是裸 ImportError。
        raise RuntimeError("Install the MCP extra: pip install '.[mcp]'") from exc
    server = FastMCP("Fault Prediction Platform")

    def register(name: str) -> None:
        """把 ``PipelineService.<name>`` 注册成一个同名 MCP 工具。"""
        method = getattr(PipelineService, name)
        hints = get_type_hints(method)
        signature = inspect.signature(method)
        # 去掉 self 并把字符串注解替换成真实类型：客户端据此生成参数表单。
        parameters = [
            p.replace(annotation=hints.get(p.name, p.annotation))
            for p in signature.parameters.values()
            if p.name != "self"
        ]
        proxy = _proxy_for(name, base_url)
        proxy.__name__ = name
        proxy.__doc__ = DESCRIPTIONS[name]
        proxy.__signature__ = signature.replace(parameters=parameters, return_annotation=dict[str, Any])
        proxy.__annotations__ = {k: v for k, v in hints.items() if k != "self"}
        server.tool(name=name, description=proxy.__doc__)(proxy)

    for operation in CONTROL_OPERATIONS:
        register(operation)
    return server


def run(base_url: str = "http://127.0.0.1:8765") -> None:
    """以 stdio 传输启动 bridge（客户端按这个进程的标准输入输出通信）。"""
    create_mcp_server(base_url).run(transport="stdio")
