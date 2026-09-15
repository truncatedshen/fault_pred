"""MCP 1.x stdio bridge to the same HTTP service used by the visual editor.

这个文件**不实现任何业务逻辑**：它读取 :data:`fault_platform.service.CONTROL_OPERATIONS`
里的 38 个操作，为每一个动态生成一个 MCP 工具，工具调用被转发成
``POST /api/control/<name>``。因此三个入口（网页、MCP、Python API）改的是同一张图。

两个细节：

* 工具签名照抄服务方法的类型注解（``get_type_hints``），客户端因此能看到参数名与类型；
* 工具描述取 :data:`DESCRIPTIONS`，没有列出的操作按函数名生成一句兜底说明——
  新增操作时应当同时补一条描述，否则 Agent 只能靠名字猜。
服务不可达时返回 ``error_code=CONTROL_API_UNAVAILABLE`` 并提示先启动服务，而不是抛栈。
"""

from __future__ import annotations

import inspect
from typing import Any, get_type_hints

import httpx

from fault_platform.service import CONTROL_OPERATIONS, PipelineService

DESCRIPTIONS = {
    "create_pipeline": "Create an empty component graph.",
    "load_pipeline": "Import XML as a graph using registered components. Does not load runtime artifacts.",
    "save_pipeline": "Save graph XML in the configured local pipeline directory.",
    "list_components": "Find components by category, keyword, tags and port types; request detailed schemas selectively.",
    "search_components": "Search the component registry by keyword.",
    "retrieve_components": "Rank components for an intent with optional category and typed-port compatibility.",
    "get_component_facets": "Catalogue navigation: categories, subcategories, tags, versions and compatibility ranges.",
    "get_component_schema": "Get exact parameter and typed port schemas for a component.",
    "add_component": "Add a configured component instance to a graph.",
    "configure_component": "Update component parameters with schema validation.",
    "connect_components": "Connect typed ports; rejects cycles and multiple producers for one input.",
    "execute_pipeline": "Start asynchronous DAG execution. Poll get_pipeline_status until completion.",
    "execute_node": "Run one node using valid upstream workspace outputs.",
    "execute_from_node": "Recompute a node and its descendants using valid upstream results.",
    "retry_node": "Retry a failed node and its descendants.",
    "get_node_result": "Return bounded preview, metadata and artifact references, never a full dataset or model.",
    "get_pipeline_result": "Return bounded node summaries and metrics.",
    "save_checkpoint": "Create an independent in-memory snapshot of graph and workspace.",
    "load_checkpoint": "Restore both graph and workspace from an in-memory checkpoint.",
    "create_example": "Create a synthetic equipment dataset and an example comparison graph.",
}


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

        async def proxy(**arguments: Any) -> dict[str, Any]:
            """转发一次调用；网络/解析失败统一转成结构化错误对象。"""
            try:
                # trust_env=False：忽略系统代理，避免本机回环地址被代理劫持。
                async with httpx.AsyncClient(base_url=base_url, timeout=60, trust_env=False) as client:
                    response = await client.post(f"/api/control/{name}", json=arguments)
                    return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                return {
                    "success": False,
                    "error_code": "CONTROL_API_UNAVAILABLE",
                    "summary": f"Start fault-platform serve at {base_url}: {exc}",
                }

        proxy.__name__ = name
        proxy.__doc__ = DESCRIPTIONS.get(name, name.replace("_", " ").capitalize() + ".")
        proxy.__signature__ = signature.replace(parameters=parameters, return_annotation=dict[str, Any])
        proxy.__annotations__ = {k: v for k, v in hints.items() if k != "self"}
        server.tool(name=name, description=proxy.__doc__)(proxy)

    for operation in CONTROL_OPERATIONS:
        register(operation)
    return server


def run(base_url: str = "http://127.0.0.1:8765") -> None:
    """以 stdio 传输启动 bridge（客户端按这个进程的标准输入输出通信）。"""
    create_mcp_server(base_url).run(transport="stdio")
