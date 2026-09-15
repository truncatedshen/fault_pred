"""长跑探针：用真实 stdio 桥调用 wait_for_pipeline，验证它不会把长跑误报成服务不可达。

背景：桥曾经对所有工具固定 60 秒客户端超时，而 ``wait_for_pipeline`` 服务端默认等 300 秒。
在 3W 这份真实数据上（489,456 行，8091 个窗口，统计+拟合+频域 → merge → 插补 → RF-300）
一轮要跑 ~150 秒，于是第 60 秒就会得到
``{"success": false, "error_code": "CONTROL_API_UNAVAILABLE", "summary": "Start fault-platform serve ..."}``
——而服务端仍在 RUNNING。Agent 若相信这句话去重启服务，内存里的图与产物会全部丢失。

用法（需要 ``test_3w/data/3w_events.parquet``，约 3 MB）：

    .\\.venv\\Scripts\\python.exe scripts\\mcp_wait_probe.py

期望输出：``wait_for_pipeline returned after ~150 s: {"success": true, ... "status": "SUCCESS"}``。
"""

import asyncio
import json
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
SENSORS = ["P-MON-CKP", "P-TPT", "T-TPT"]
WINDOW = {
    "columns": SENSORS,
    "group_column": "instance",
    "label_column": "fault",
    "time_column": "time_s",
    "window_size": 180,
    "step": 60,
    "label_policy": "mode",
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def control(base: str, operation: str, **payload: object) -> dict:
    return httpx.post(f"{base}/api/control/{operation}", json=payload, timeout=180).json()


def build_pipeline(base: str) -> str:
    pipeline_id = control(base, "create_pipeline", name="wait probe")["pipeline_id"]
    control(
        base,
        "add_components",
        pipeline_id=pipeline_id,
        components=[
            {"component_type": "data.input", "node_id": "src", "parameters": {"path": "3w_events.parquet"}},
            {"component_type": "feature.statistical", "node_id": "stats", "parameters": dict(WINDOW)},
            {"component_type": "feature.fitting", "node_id": "fit", "parameters": dict(WINDOW)},
            {
                "component_type": "feature.spectral",
                "node_id": "spec",
                "parameters": dict(WINDOW, sampling_rate=1.0),
            },
            {"component_type": "feature.merge", "node_id": "merge", "parameters": {}},
            {"component_type": "feature.imputation", "node_id": "fill", "parameters": {"method": "median"}},
            {
                "component_type": "validation.random_forest",
                "node_id": "rf",
                "parameters": {"n_estimators": 300, "split_method": "group"},
            },
        ],
    )
    control(
        base,
        "connect_many",
        pipeline_id=pipeline_id,
        connections=[
            {
                "source_node": "src",
                "source_port": "dataset",
                "target_node": "stats",
                "target_port": "dataset",
            },
            {"source_node": "src", "source_port": "dataset", "target_node": "fit", "target_port": "dataset"},
            {"source_node": "src", "source_port": "dataset", "target_node": "spec", "target_port": "dataset"},
            {
                "source_node": "stats",
                "source_port": "features",
                "target_node": "merge",
                "target_port": "left",
            },
            {"source_node": "fit", "source_port": "features", "target_node": "merge", "target_port": "right"},
            {
                "source_node": "merge",
                "source_port": "features",
                "target_node": "fill",
                "target_port": "features",
            },
            {
                "source_node": "fill",
                "source_port": "features",
                "target_node": "rf",
                "target_port": "features",
            },
            {"source_node": "stats", "source_port": "labels", "target_node": "rf", "target_port": "labels"},
        ],
    )
    valid = control(base, "validate_pipeline", pipeline_id=pipeline_id)["valid"]
    print("graph valid:", valid)
    return pipeline_id


async def probe(base: str, pipeline_id: str) -> None:
    params = StdioServerParameters(command=str(PYTHON), args=["-m", "fault_platform", "mcp", "--url", base])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            started = json.loads(
                (await session.call_tool("execute_pipeline", {"pipeline_id": pipeline_id})).content[0].text
            )
            print("execute_pipeline ->", started.get("status"))
            wait_started = time.perf_counter()
            waited = json.loads(
                (await session.call_tool("wait_for_pipeline", {"pipeline_id": pipeline_id})).content[0].text
            )
            print("wait_for_pipeline returned after %.1f s" % (time.perf_counter() - wait_started))
            print(
                "  ",
                json.dumps(
                    {k: waited.get(k) for k in ("success", "status", "error_code")}, ensure_ascii=False
                ),
            )
            status = control(base, "get_pipeline_status", pipeline_id=pipeline_id)["status"]
            print("real status from the service:", status)
            if waited.get("success") and waited.get("status") == "SUCCESS":
                print("PASS: the blocking wait survived a long run")
            else:
                print("FAIL: the bridge misreported a long run")


def main() -> None:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    tmp = Path(tempfile.mkdtemp())
    data_root = tmp / "data"
    data_root.mkdir(parents=True)
    shutil.copy(ROOT / "test_3w" / "data" / "3w_events.parquet", data_root / "3w_events.parquet")
    server = subprocess.Popen(
        [str(PYTHON), "-m", "fault_platform", "serve", "--port", str(port), "--data-root", str(data_root)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(120):
            try:
                if httpx.get(f"{base}/api/health", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        asyncio.run(probe(base, build_pipeline(base)))
    finally:
        server.terminate()


if __name__ == "__main__":
    main()
