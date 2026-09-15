"""Command line entry points for headless execution, web and MCP.

四个子命令：

* ``serve``：启动本地服务（网页设计器 + HTTP 控制 API + SSE）；
* ``demo``：生成合成数据与示例方案，执行一次并把结果写到 ``demo_result.json``；
* ``run``：无界面执行一份方案 XML，打印 JSON 摘要（适合脚本化取指标）；
* ``mcp``：把 MCP stdio bridge 接到正在运行的服务上。

``run``/``demo`` 失败时会以退出码 1 结束，方便 CI 与部署脚本判断。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fault_platform.examples import create_dataset, example_graph
from fault_platform.registry import default_registry
from fault_platform.runtime import ExecutionContext, ExecutionEngine
from fault_platform.workspace import FaultWorkspace, PipelineStatus, json_safe
from fault_platform.xml_io import XMLParser, XMLSerializer


def main() -> None:
    """解析命令行参数并分发到对应子命令。"""
    parser = argparse.ArgumentParser(description="Fault Prediction Component Platform")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Start the local visual designer")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--data-root", default="examples/data")
    serve.add_argument("--storage-root", default=".fault-platform/pipelines")
    serve.add_argument(
        "--artifact-cache-mb",
        type=int,
        default=None,
        help="Bound the in-memory artifact cache; unpinned outputs are spilled or evicted LRU-wise",
    )
    serve.add_argument(
        "--artifact-spill-dir",
        default=".fault-platform/artifact-spill",
        help="Directory for artifacts spilled out of the cache budget (empty disables spilling)",
    )
    demo = commands.add_parser("demo", help="Generate and execute a synthetic pipeline")
    demo.add_argument("--output", default="examples")
    demo.add_argument("--xgboost", action="store_true")
    run = commands.add_parser("run", help="Execute pipeline XML without UI/Agent")
    run.add_argument("xml")
    run.add_argument("--data-root", default="examples/data")
    mcp = commands.add_parser("mcp", help="MCP stdio bridge to the running local service")
    mcp.add_argument("--url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        from fault_platform.api import create_app

        # 只绑定回环地址：这是一个面向本机单用户的服务，没有鉴权与多用户隔离。
        uvicorn.run(
            create_app(
                args.data_root,
                args.storage_root,
                artifact_cache_mb=args.artifact_cache_mb,
                artifact_spill_dir=args.artifact_spill_dir or None,
            ),
            host="127.0.0.1",
            port=args.port,
        )
        return
    if args.command == "mcp":
        from fault_platform.mcp_server import run as run_mcp

        run_mcp(args.url)
        return
    registry = default_registry()
    if args.command == "demo":
        # demo 既是演示也是冒烟测试：先造数据、写 XML，再执行并导出结果。
        destination = Path(args.output).resolve()
        data_root = destination / "data"
        create_dataset(data_root / "synthetic_equipment.csv")
        graph = example_graph(registry, include_xgboost=args.xgboost)
        XMLSerializer().save(graph, destination / "example_pipeline.xml")
    else:
        data_root = Path(args.data_root).resolve()
        # require_complete=True：XML 里缺必填参数或端口没接好会直接失败，而不是跑到一半才报错。
        graph = XMLParser(registry).load(args.xml, require_complete=True)
    ws = ExecutionEngine().execute(graph, ExecutionContext(FaultWorkspace(graph.pipeline_id), data_root))
    # 只抽取各节点的 metrics 端口（没有该端口的节点自然跳过），避免把大表打进终端。
    metrics = {
        n: ws.get_output(n, "metrics") for n, outputs in ws.node_results.items() if "metrics" in outputs
    }
    result = {**ws.get_summary(), "metrics": json_safe(metrics)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "demo":
        (destination / "demo_result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if ws.status != PipelineStatus.SUCCESS:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
