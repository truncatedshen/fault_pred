"""MCP 1.x stdio bridge to the same HTTP service used by the visual editor."""

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
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("Install the MCP extra: pip install '.[mcp]'") from exc
    server = FastMCP("Fault Prediction Platform")

    def register(name: str) -> None:
        method = getattr(PipelineService, name)
        hints = get_type_hints(method)
        signature = inspect.signature(method)
        parameters = [
            p.replace(annotation=hints.get(p.name, p.annotation))
            for p in signature.parameters.values()
            if p.name != "self"
        ]

        async def proxy(**arguments: Any) -> dict[str, Any]:
            try:
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
    create_mcp_server(base_url).run(transport="stdio")
