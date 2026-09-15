"""End-to-end MCP smoke test against a running Fault Prediction service.

It launches the same stdio bridge that Codex is configured with, then discovers
components, builds a graph, validates it, executes it and inspects the results
through MCP tools only - no direct HTTP or Python API access.

    .\\.venv\\Scripts\\python.exe scripts\\mcp_smoke.py --url http://127.0.0.1:8765

The synthetic dataset is not a physical vibration signal, so spectral values here
only prove that the pipeline and parameter plumbing work.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SPECTRAL_WINDOW = {
    "columns": ["vibration"],
    "sampling_rate": 64.0,
    "group_column": "equipment",
    "label_column": "label",
    "window_size": 16,
    "features": ["dominant_frequency", "spectral_rms"],
}


def check(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def call(session: ClientSession, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await session.call_tool(tool, arguments)
    check(result.content, f"{tool} returned no content")
    return json.loads(result.content[0].text)


async def poll(session: ClientSession, pipeline_id: str, report: dict[str, Any], attempts: int = 240) -> str:
    terminal = {"SUCCESS", "FAILED", "CANCELLED"}
    for _ in range(attempts):
        status = (await call(session, "get_pipeline_status", pipeline_id=pipeline_id))["status"]
        if status in terminal:
            return status
        await asyncio.sleep(0.5)
    raise AssertionError(f"pipeline {pipeline_id} did not finish; report={report}")


def configured_parameters(config: Path, server: str, base_url: str) -> tuple[StdioServerParameters, str]:
    """Read the stdio server exactly as a Codex client would launch it."""
    entry = tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"][server]
    arguments = [
        base_url if argument == "http://127.0.0.1:8765" else argument for argument in entry.get("args", [])
    ]
    parameters = StdioServerParameters(command=entry["command"], args=arguments, env=entry.get("env") or None)
    return parameters, f"{config}::{server}"


async def run(base_url: str, parameters: StdioServerParameters, source: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "url": base_url,
        "bridge_command": parameters.command,
        "bridge_args": list(parameters.args),
        "bridge_source": source,
    }
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = sorted(tool.name for tool in (await session.list_tools()).tools)
            required = {
                "create_pipeline",
                "add_components",
                "connect_many",
                "execute_pipeline",
                "wait_for_pipeline",
            }
            check(required.issubset(tools), f"missing tools: {sorted(required - set(tools))}")
            check("feature.spectral" not in tools, "components must not be exposed as tools")
            report["tools"] = len(tools)

            found = await call(session, "list_components", category="feature", include_schema=False, limit=20)
            if not found.get("success"):
                raise AssertionError(f"bridge cannot reach the service: {found}")
            names = [component["component_type"] for component in found["components"]]
            check("feature.spectral" in names, f"feature lookup missed spectral: {names}")
            schema = await call(session, "get_component_schema", component_type="feature.spectral")
            spectral = schema["component"]
            sampling = next(p for p in spectral["parameter_schema"] if p["name"] == "sampling_rate")
            check(sampling["required"], "sampling_rate should be required")
            report["discovery"] = {"feature_components": len(names), "sampling_rate": sampling["type"]}

            # Make the smoke test self-sufficient on a fresh machine: this writes the
            # synthetic dataset into the service data root before any pipeline reads it.
            example = await call(session, "create_example")
            check(example.get("success"), f"create_example failed: {example}")
            dataset_path = next(
                node["parameters"]["path"]
                for node in example["graph"]["nodes"]
                if node["type"] == "data.input"
            )
            report["example_dataset"] = dataset_path

            created = await call(session, "create_pipeline", name="MCP 冒烟测试")
            check(created.get("success"), f"create_pipeline failed: {created}")
            pipeline_id = created["pipeline_id"]
            report["pipeline_id"] = pipeline_id

            for component_type, node_id, parameters in (
                ("data.input", "source", {"path": "synthetic_equipment.csv"}),
                (
                    "feature.statistical",
                    "stats",
                    {
                        "columns": ["vibration", "temperature"],
                        "group_column": "equipment",
                        "label_column": "label",
                        "window_size": 16,
                        "features": ["mean", "std", "rms"],
                    },
                ),
                ("feature.spectral", "spectral", SPECTRAL_WINDOW),
                ("feature.merge", "merge", {}),
                ("validation.random_forest", "model", {"n_estimators": 100, "split_method": "group"}),
            ):
                added = await call(
                    session,
                    "add_component",
                    pipeline_id=pipeline_id,
                    component_type=component_type,
                    node_id=node_id,
                    parameters=parameters,
                )
                check(added.get("success"), f"add_component {component_type} failed: {added}")

            configured = await call(
                session,
                "configure_component",
                pipeline_id=pipeline_id,
                node_id="model",
                parameters={"n_estimators": 50},
            )
            check(configured.get("success"), f"configure_component failed: {configured}")

            edges = (
                ("source", "dataset", "stats", "dataset"),
                ("source", "dataset", "spectral", "dataset"),
                ("stats", "features", "merge", "left"),
                ("spectral", "features", "merge", "right"),
                ("merge", "features", "model", "features"),
                ("stats", "labels", "model", "labels"),
            )
            for source_node, source_port, target_node, target_port in edges:
                connected = await call(
                    session,
                    "connect_components",
                    pipeline_id=pipeline_id,
                    source_node=source_node,
                    source_port=source_port,
                    target_node=target_node,
                    target_port=target_port,
                )
                check(connected.get("success"), f"connect {source_node}.{source_port} failed: {connected}")
            report["edges"] = len(edges)

            rejected = await call(
                session,
                "connect_components",
                pipeline_id=pipeline_id,
                source_node="source",
                source_port="dataset",
                target_node="model",
                target_port="features",
            )
            check(not rejected.get("success"), "typed ports accepted Dataset -> FeatureDataset")
            report["rejected_connection"] = {
                "error_code": rejected.get("error_code"),
                "summary": rejected.get("summary"),
            }

            validation = await call(session, "validate_pipeline", pipeline_id=pipeline_id)
            check(validation.get("success") is not False, f"validate_pipeline failed: {validation}")

            started = await call(session, "execute_pipeline", pipeline_id=pipeline_id)
            check(started.get("success"), f"execute_pipeline failed: {started}")
            status = await poll(session, pipeline_id, report)
            check(status == "SUCCESS", f"pipeline finished with {status}")
            report["status"] = status

            metrics = await call(
                session, "get_node_result", pipeline_id=pipeline_id, node_id="model", limit=5
            )
            check(metrics.get("success"), f"get_node_result failed: {metrics}")
            accuracy = metrics["outputs"]["metrics"]["value"]["accuracy"]
            importance = metrics["outputs"]["importance"]["preview"][0]
            report["metrics"] = {
                "accuracy": round(accuracy, 4),
                "top_feature": importance["feature"],
                "test_count": metrics["outputs"]["metrics"]["value"]["test_count"],
            }

            spectral_output = await call(
                session, "get_node_result", pipeline_id=pipeline_id, node_id="spectral", limit=2
            )
            spectral_columns = spectral_output["outputs"]["features"]["columns"]
            check(
                any("dominant_frequency" in column for column in spectral_columns),
                f"spectral node produced unexpected columns: {spectral_columns}",
            )
            report["spectral_columns"] = len(spectral_columns)

            summary = await call(session, "get_pipeline_result", pipeline_id=pipeline_id)
            check(summary.get("success"), f"get_pipeline_result failed: {summary}")
            history = await call(session, "get_history", pipeline_id=pipeline_id, limit=50)
            report["history_entries"] = len(history["history"])

            xml = await call(session, "get_pipeline_xml", pipeline_id=pipeline_id)
            check(
                "feature.spectral" in xml["xml"] and pipeline_id in xml["xml"], "exported XML is incomplete"
            )
            report["xml_characters"] = len(xml["xml"])

            checkpoint = await call(session, "save_checkpoint", pipeline_id=pipeline_id)
            check(checkpoint.get("success"), f"save_checkpoint failed: {checkpoint}")
            checkpoint_id = checkpoint["checkpoint"]["checkpoint_id"]
            checkpoints = await call(session, "list_checkpoints", pipeline_id=pipeline_id)
            check(checkpoints["checkpoints"], "checkpoint list is empty")
            restored = await call(session, "load_checkpoint", checkpoint_id=checkpoint_id)
            check(restored.get("success"), f"load_checkpoint failed: {restored}")
            check(restored["graph"]["nodes"], "restored checkpoint has no graph")
            report["checkpoint"] = {
                "checkpoint_id": checkpoint_id,
                "completed_nodes": checkpoint["checkpoint"]["completed_nodes"],
                "listed": len(checkpoints["checkpoints"]),
            }
    report["success"] = True
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP end-to-end smoke test")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument(
        "--from-config",
        action="store_true",
        help="Launch the stdio bridge exactly as the Codex MCP configuration does",
    )
    parser.add_argument("--config", default=str(Path.home() / ".codex" / "config.toml"))
    parser.add_argument("--server", default="fault-prediction")
    arguments = parser.parse_args()
    if arguments.from_config:
        parameters, source = configured_parameters(Path(arguments.config), arguments.server, arguments.url)
    else:
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "fault_platform", "mcp", "--url", arguments.url]
        )
        source = "command line default"
    report = asyncio.run(run(arguments.url, parameters, source))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
