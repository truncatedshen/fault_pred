"""通过 MCP 桥复核本轮改动：窗口默认口径 + 按类别分层的切分。

    .\\.venv\\Scripts\\python.exe scripts\\mcp_split_check.py --url http://127.0.0.1:8765

走的全是 Agent 能看到的工具（schema → 批量建图 → 校验 → 执行 → 读结果），
不碰 HTTP API，也不碰 Python API。数据用 test_hbm_raw/data/prepared_hbm_rows.csv：
这份数据正好是"故障只集中在少数实体"的例子，最能说明切分要按类别分层。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]

DATA_PATH = "prepared_hbm_rows.csv"
WINDOW = {
    "columns": ["is_ce", "is_ueo", "col", "row"],
    "group_column": "entity",
    "asset_column": "asset",
    "label_column": "label",
    "time_column": "time",
    "window_span": "2h",
    "step_span": "2h",
    "prediction_horizon": "7d",
    "label_policy": "horizon",
}


async def call(session: ClientSession, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await session.call_tool(tool, arguments)
    if not result.content:
        raise AssertionError(f"{tool} returned no content")
    return json.loads(result.content[0].text)


async def run_split(session: ClientSession, split_method: str) -> dict[str, Any]:
    pipeline = await call(session, "create_pipeline", name=f"split-{split_method}")
    pipeline_id = pipeline["pipeline_id"]
    added = await call(
        session,
        "add_components",
        pipeline_id=pipeline_id,
        include_graph=False,
        components=[
            {"component_type": "data.input", "node_id": "src", "parameters": {"path": DATA_PATH}},
            {"component_type": "feature.statistical", "node_id": "win", "parameters": WINDOW},
            {
                "component_type": "validation.random_forest",
                "node_id": "forest",
                "parameters": {"n_estimators": 40, "split_method": split_method, "test_size": 0.25},
            },
        ],
    )
    assert [entry["node_id"] for entry in added["added"]] == ["src", "win", "forest"], added
    await call(
        session,
        "connect_many",
        pipeline_id=pipeline_id,
        include_graph=False,
        connections=[
            {"source_node": "src", "source_port": "dataset", "target_node": "win", "target_port": "dataset"},
            {
                "source_node": "win",
                "source_port": "features",
                "target_node": "forest",
                "target_port": "features",
            },
            {"source_node": "win", "source_port": "labels", "target_node": "forest", "target_port": "labels"},
        ],
    )
    problems = await call(session, "validate_pipeline", pipeline_id=pipeline_id)
    assert not problems.get("problems"), problems
    await call(session, "execute_pipeline", pipeline_id=pipeline_id)
    status = await call(session, "wait_for_pipeline", pipeline_id=pipeline_id, timeout_seconds=300)
    assert status["status"] == "SUCCESS", status
    result = await call(session, "get_pipeline_result", pipeline_id=pipeline_id, limit=5)
    forest_outputs = result["result_summary"]["forest"]["outputs"]
    metrics = forest_outputs["metrics"]["value"]
    # 指标是**留出集**上的：prediction 端口只覆盖测试行，行数必须等于 test_count。
    # 这一条断言把"验证器输出的分数来自哪一侧"钉在回归测试里，而不是靠读代码猜。
    assert forest_outputs["prediction"]["shape"][0] == metrics["test_count"], forest_outputs["prediction"]
    window_attrs = result["result_summary"]["win"]["outputs"]["features"]
    report = {
        "split_method": split_method,
        "windows": window_attrs["shape"][0],
        "prediction_rows": forest_outputs["prediction"]["shape"][0],
        "train_count": metrics["train_count"],
        "train_class_counts": metrics["train_class_counts"],
        "test_count": metrics["test_count"],
        "test_class_counts": metrics["test_class_counts"],
        "split_note": metrics["split_note"],
        "accuracy": metrics["accuracy"],
        "balanced_accuracy": metrics["balanced_accuracy"],
        "warnings": metrics["warnings"],
    }
    await call(session, "delete_pipeline", pipeline_id=pipeline_id)
    return report


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    arguments = parser.parse_args()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "fault_platform", "mcp", "--url", arguments.url],
        cwd=str(ROOT),
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            schema = await call(session, "get_component_schema", component_type="feature.statistical")
            parameters_schema = schema["component"]["parameter_schema"]
            window = next(p for p in parameters_schema if p["name"] == "current_fault_policy")
            print("current_fault_policy default :", window["default"], "| options:", window["options"])
            assert window["default"] == "positive", window
            for split_method in ("group", "asset", "temporal"):
                report = await run_split(session, split_method)
                print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
