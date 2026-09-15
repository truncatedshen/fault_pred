"""Control API integration, concurrency and actual MCP stdio handshake."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient

from fault_platform.api import create_app
from fault_platform.mcp_server import create_mcp_server
from fault_platform.service import PipelineService


@pytest.fixture
def service(tmp_path):
    result = PipelineService(tmp_path / "data", tmp_path / "saved")
    yield result
    result.close()


def call(client, operation, **arguments):
    return client.post("/api/control/" + operation, json=arguments)


def test_api_example_execution_and_xml(service):
    with TestClient(create_app(service=service)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/api/health").json()["components"] == 29
        example = call(client, "create_example").json()
        pipeline_id = example["pipeline_id"]
        assert call(client, "validate_pipeline", pipeline_id=pipeline_id).json()["valid"]
        start = call(client, "execute_pipeline", pipeline_id=pipeline_id).json()
        service.jobs[pipeline_id].result(timeout=30)
        status = call(client, "get_pipeline_status", pipeline_id=pipeline_id).json()
        assert status["status"] == "SUCCESS"
        result = call(client, "get_node_result", pipeline_id=pipeline_id, node_id="forest").json()
        assert result["outputs"]["metrics"]["value"]["accuracy"] > 0.8
        assert result["outputs"]["model"]["kind"] == "model"
        xml = call(client, "get_pipeline_xml", pipeline_id=pipeline_id).json()["xml"]
        imported = call(client, "load_pipeline", xml=xml).json()
        assert imported["pipeline_id"] != pipeline_id
        assert len(imported["graph"]["nodes"]) == len(example["graph"]["nodes"])
        saved = call(client, "save_pipeline", pipeline_id=pipeline_id).json()
        assert Path(saved["path"]).is_file()
        checkpoint = call(client, "save_checkpoint", pipeline_id=pipeline_id).json()["checkpoint"]
        call(
            client,
            "configure_component",
            pipeline_id=pipeline_id,
            node_id="forest",
            parameters={"n_estimators": 5},
        )
        stale = call(client, "get_node_result", pipeline_id=pipeline_id, node_id="forest").json()
        assert stale["status"] == "PENDING" and stale["outputs"] == {}
        restored = call(client, "load_checkpoint", checkpoint_id=checkpoint["checkpoint_id"]).json()
        assert restored["workspace_id"] == start["workspace_id"]
        assert call(client, "get_pipeline_status", pipeline_id=pipeline_id).json()["status"] == "SUCCESS"


def test_api_input_errors_upload_and_boundaries(service):
    with TestClient(create_app(service=service)) as client:
        pipeline = call(client, "create_pipeline").json()
        pid = pipeline["pipeline_id"]
        response = client.post(
            "/api/data/upload", files={"file": ("测量.csv", b"x,label\n1,0\n2,1\n", "text/csv")}
        )
        assert response.status_code == 200
        assert (service.data_root / response.json()["path"]).is_file()
        assert call(
            client,
            "add_component",
            pipeline_id=pid,
            component_type="data.input",
            parameters={"path": response.json()["path"]},
        ).json()["success"]
        assert not call(
            client, "configure_component", pipeline_id=pid, node_id="missing", parameters={}
        ).json()["success"]
        assert call(client, "save_pipeline", pipeline_id=pid, filename="../../escape.xml").status_code == 400
        assert (
            call(
                client, "replace_pipeline", pipeline_id=pid, graph=pipeline["graph"], expected_version=0
            ).status_code
            == 400
        )
        assert call(client, "list_components", limit="20").status_code == 400
        assert call(client, "create_pipeline", unknown=3).status_code == 400
        assert call(client, "_graph", pipeline_id=pid).status_code == 400
        assert (
            client.post(
                "/api/control/create_pipeline", json={}, headers={"Origin": "https://unrelated.example"}
            ).status_code
            == 403
        )


def test_parallel_edits_rejected_during_execution(service):
    pid = service.create_example()["pipeline_id"]
    released = Event()
    original = service._run

    def block(*args):
        released.wait(timeout=10)
        return original(*args)

    service._run = block
    service.execute_pipeline(pid)
    try:
        assert not service.dispatch(
            "configure_component",
            {
                "pipeline_id": pid,
                "node_id": "forest",
                "parameters": {"n_estimators": 5},
            },
        )["success"]
        assert not service.dispatch("execute_pipeline", {"pipeline_id": pid})["success"]
        service.cancel_pipeline(pid)
    finally:
        released.set()
    service.jobs[pid].result(timeout=30)
    assert service.get_pipeline_status(pid)["status"] == "CANCELLED"


async def test_mcp_tool_schemas():
    pytest.importorskip("mcp")
    server = create_mcp_server()
    tools = await server.list_tools()
    by_name = {tool.name: tool for tool in tools}
    assert "execute_pipeline" in by_name and "normalize" not in by_name
    schema = by_name["add_component"].inputSchema
    assert "pipeline_id" in schema["properties"]
    assert "component_type" in schema["required"]


@pytest.fixture
def live_server(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log = (tmp_path / "server.log").open("w")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "fault_platform",
            "serve",
            "--port",
            str(port),
            "--data-root",
            str(tmp_path / "data"),
            "--storage-root",
            str(tmp_path / "saved"),
        ],
        stdout=log,
        stderr=log,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(trust_env=False) as client:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Test API server exited")
                try:
                    if client.get(base_url + "/api/health").status_code == 200:
                        break
                except httpx.ConnectError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Test API server did not start")
        yield base_url
    finally:
        process.terminate()
        process.wait(timeout=15)
        log.close()


async def test_real_stdio_mcp_and_shared_http_graph(live_server):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "fault_platform", "mcp", "--url", live_server]
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("create_pipeline", {"name": "MCP integration"})
            payload = json.loads(result.content[0].text)
            assert payload["success"]
            pid = payload["pipeline_id"]
            added = await session.call_tool(
                "add_component",
                {
                    "pipeline_id": pid,
                    "component_type": "data.input",
                    "node_id": "via_mcp",
                    "parameters": {"path": "not-yet-uploaded.csv"},
                },
            )
            assert not added.isError
            async with httpx.AsyncClient(trust_env=False) as client:
                graph = (
                    await client.post(live_server + "/api/control/get_pipeline", json={"pipeline_id": pid})
                ).json()["graph"]
            assert graph["name"] == "MCP integration"
            assert graph["nodes"][0]["id"] == "via_mcp"
