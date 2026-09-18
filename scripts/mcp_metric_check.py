"""通过 MCP 桥复核指标口径：留出集、宏平均（不加权）、逐类数值与混淆矩阵逐项对应。

    .\\.venv\\Scripts\\python.exe scripts\\mcp_metric_check.py --url http://127.0.0.1:8765

用 test_hbm_raw/data/prepared_hbm_rows.csv（正类稀少，加权与宏平均会明显不同）。
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
    assert result.content, f"{tool} returned no content"
    return json.loads(result.content[0].text)


def check_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """把"报告的指标能不能用混淆矩阵重算出来"逐项验一遍。"""
    assert metrics["averaging"] == "macro", metrics["averaging"]
    matrix = metrics["confusion_matrix"]
    classes = [str(name) for name in metrics["classes"]]
    for position, name in enumerate(classes):
        row_total = sum(matrix[position])
        column_total = sum(row[position] for row in matrix)
        assert metrics["per_class_support"][name] == row_total == metrics["test_class_counts"][name]
        recall = matrix[position][position] / row_total if row_total else 0.0
        precision = matrix[position][position] / column_total if column_total else 0.0
        assert abs(metrics["per_class_recall"][name] - recall) < 1e-12, (name, recall)
        assert abs(metrics["per_class_precision"][name] - precision) < 1e-12, (name, precision)
        f1 = 0.0 if not (precision + recall) else 2 * precision * recall / (precision + recall)
        assert abs(metrics["per_class_f1"][name] - f1) < 1e-12, (name, f1)
    for key in ("precision", "recall", "f1"):
        mean = sum(metrics[f"per_class_{key}"][name] for name in classes) / len(classes)
        assert abs(metrics[key] - mean) < 1e-12, (key, metrics[key], mean)
    return {
        "averaging": metrics["averaging"],
        "accuracy": round(metrics["accuracy"], 4),
        "balanced_accuracy": round(metrics["balanced_accuracy"], 4),
        "precision_macro": round(metrics["precision"], 4),
        "recall_macro": round(metrics["recall"], 4),
        "f1_macro": round(metrics["f1"], 4),
        "per_class_support": metrics["per_class_support"],
        "per_class_precision": {k: round(v, 4) for k, v in metrics["per_class_precision"].items()},
        "per_class_recall": {k: round(v, 4) for k, v in metrics["per_class_recall"].items()},
        "per_class_f1": {k: round(v, 4) for k, v in metrics["per_class_f1"].items()},
        "confusion_matrix": matrix,
        "train_count": metrics["train_count"],
        "test_count": metrics["test_count"],
    }


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
            pipeline = await call(session, "create_pipeline", name="metric-check")
            pipeline_id = pipeline["pipeline_id"]
            await call(
                session,
                "add_components",
                pipeline_id=pipeline_id,
                include_graph=False,
                components=[
                    {
                        "component_type": "data.input",
                        "node_id": "src",
                        "parameters": {"path": "prepared_hbm_rows.csv"},
                    },
                    {"component_type": "feature.statistical", "node_id": "win", "parameters": WINDOW},
                    {
                        "component_type": "validation.random_forest",
                        "node_id": "forest",
                        "parameters": {"n_estimators": 40, "split_method": "group", "test_size": 0.25},
                    },
                ],
            )
            await call(
                session,
                "connect_many",
                pipeline_id=pipeline_id,
                include_graph=False,
                connections=[
                    {
                        "source_node": "src",
                        "source_port": "dataset",
                        "target_node": "win",
                        "target_port": "dataset",
                    },
                    {
                        "source_node": "win",
                        "source_port": "features",
                        "target_node": "forest",
                        "target_port": "features",
                    },
                    {
                        "source_node": "win",
                        "source_port": "labels",
                        "target_node": "forest",
                        "target_port": "labels",
                    },
                ],
            )
            problems = await call(session, "validate_pipeline", pipeline_id=pipeline_id)
            assert not problems.get("problems"), problems
            await call(session, "execute_pipeline", pipeline_id=pipeline_id)
            status = await call(session, "wait_for_pipeline", pipeline_id=pipeline_id, timeout_seconds=300)
            assert status["status"] == "SUCCESS", status
            result = await call(session, "get_pipeline_result", pipeline_id=pipeline_id, limit=5)
            outputs = result["result_summary"]["forest"]["outputs"]
            metrics = outputs["metrics"]["value"]
            print(json.dumps(check_metrics(metrics), ensure_ascii=False, indent=2))
            # 指标是留出集的：predict 端口只覆盖测试行。
            assert outputs["prediction"]["shape"][0] == metrics["test_count"]
            await call(session, "delete_pipeline", pipeline_id=pipeline_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
