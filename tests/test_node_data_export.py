"""点节点导出中间产物为 CSV：输入侧来自上游、输出侧来自本节点。"""

from __future__ import annotations

import io

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from fault_platform.api import create_app


@pytest.fixture
def executed(tmp_path):
    """跑一遍示例方案，返回 ``(client, pipeline_id)``——导出必须有真实产物才有意义。"""
    app = create_app(data_root=tmp_path)
    with TestClient(app) as client:
        pipeline = client.post("/api/control/create_example", json={}).json()["graph"]["id"]
        client.post("/api/control/execute_pipeline", json={"pipeline_id": pipeline})
        client.post(
            "/api/control/wait_for_pipeline",
            json={"pipeline_id": pipeline, "timeout_seconds": 180},
        )
        yield client, pipeline


def test_lists_inputs_and_outputs_of_a_node(executed):
    client, pipeline = executed
    spec = client.get("/api/node-data", params={"pipeline_id": pipeline, "node_id": "stat"}).json()
    sides = {(row["direction"], row["port"]) for row in spec["data"]}
    # 输入侧是上游喂进来的原始表，输出侧是这个组件自己的特征与标签。
    assert ("input", "dataset") in sides
    assert ("output", "features") in sides
    assert ("output", "labels") in sides
    features = next(row for row in spec["data"] if row["port"] == "features")
    assert features["kind"] == "table" and features["rows"] > 0 and features["available"] is True
    assert features["owner"] == "stat"
    labels = next(row for row in spec["data"] if row["port"] == "labels")
    assert labels["kind"] == "vector" and labels["columns"] == ["label"]


def test_csv_export_keeps_the_window_key_index(executed):
    client, pipeline = executed
    response = client.get(
        "/api/node-data/csv",
        params={"pipeline_id": pipeline, "node_id": "stat", "direction": "output", "port": "features"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    # 特征表的行索引是窗口键：它是"这行来自哪段数据"的唯一线索，必须留在文件里。
    frame = pd.read_csv(io.StringIO(response.text.lstrip("\ufeff")))
    assert list(frame.columns)[0] == "window_id"
    assert frame["window_id"].iloc[0].startswith("g")
    assert int(response.headers["x-rows"]) == len(frame) == int(response.headers["x-total-rows"])
    assert response.headers["x-truncated"] == "false"


def test_input_side_exports_what_the_component_received(executed):
    client, pipeline = executed
    output = pd.read_csv(
        io.StringIO(
            client.get(
                "/api/node-data/csv",
                params={
                    "pipeline_id": pipeline,
                    "node_id": "stat",
                    "direction": "output",
                    "port": "features",
                },
            ).text.lstrip("\ufeff")
        )
    )
    upstream = pd.read_csv(
        io.StringIO(
            client.get(
                "/api/node-data/csv",
                params={"pipeline_id": pipeline, "node_id": "stat", "direction": "input"},
            ).text.lstrip("\ufeff")
        )
    )
    # 输入侧就是上游那 5760 行原始表：行数对不上说明"导出的不是它吃的那份"。
    assert len(upstream) == 5760
    assert len(output) < len(upstream)
    assert {"vibration", "temperature"}.issubset(upstream.columns)


def test_truncation_is_reported_not_hidden(executed):
    client, pipeline = executed
    response = client.get(
        "/api/node-data/csv",
        params={"pipeline_id": pipeline, "node_id": "source", "direction": "output", "max_rows": 5},
    )
    assert response.status_code == 200
    assert response.headers["x-rows"] == "5"
    assert int(response.headers["x-total-rows"]) == 5760
    assert response.headers["x-truncated"] == "true"
    assert len(pd.read_csv(io.StringIO(response.text.lstrip("\ufeff")))) == 5


def test_max_rows_zero_means_everything(executed):
    client, pipeline = executed
    response = client.get(
        "/api/node-data/csv",
        params={"pipeline_id": pipeline, "node_id": "source", "direction": "output", "max_rows": 0},
    )
    assert response.headers["x-truncated"] == "false"
    assert int(response.headers["x-rows"]) == int(response.headers["x-total-rows"]) == 5760


def test_errors_name_the_real_problem(tmp_path):
    app = create_app(data_root=tmp_path)
    with TestClient(app) as client:
        pipeline = client.post("/api/control/create_example", json={}).json()["graph"]["id"]
        # 没跑过就没有产物，错误要说清是"还没执行"，而不是"没有数据"。
        before = client.get("/api/node-data", params={"pipeline_id": pipeline, "node_id": "stat"})
        assert before.status_code == 400
        assert "has not been executed" in before.json()["summary"]

        client.post("/api/control/execute_pipeline", json={"pipeline_id": pipeline})
        client.post(
            "/api/control/wait_for_pipeline",
            json={"pipeline_id": pipeline, "timeout_seconds": 180},
        )
        unknown = client.get("/api/node-data", params={"pipeline_id": pipeline, "node_id": "nope"})
        assert unknown.status_code == 400 and "Unknown node" in unknown.json()["summary"]

        # metrics 是 dict 不是表：要说"这个端口导不出来"，并列出可以导的端口。
        not_table = client.get(
            "/api/node-data/csv",
            params={"pipeline_id": pipeline, "node_id": "forest", "direction": "output", "port": "metrics"},
        )
        assert not_table.status_code == 400
        assert "not an exportable table" in not_table.json()["summary"]
        assert "prediction" in not_table.json()["summary"]


def test_editing_the_graph_blocks_stale_exports(executed):
    client, pipeline = executed
    client.post(
        "/api/control/configure_component",
        json={"pipeline_id": pipeline, "node_id": "filter", "parameters": {"value": 0.1}},
    )
    # 图改了、结果没重算：宁可拒绝，也不能把上一版图算出的表当成当前的发出去。
    blocked = client.get("/api/node-data", params={"pipeline_id": pipeline, "node_id": "stat"})
    assert blocked.status_code == 400
    assert "Graph changed" in blocked.json()["summary"]


def test_export_endpoints_are_not_control_operations():
    """导出是给人点文件接口，不该悄悄多出第 40 个 MCP 工具。"""
    from fault_platform.service import CONTROL_OPERATIONS

    assert "export_node_data" not in CONTROL_OPERATIONS
    assert "list_node_data" not in CONTROL_OPERATIONS
    assert len(CONTROL_OPERATIONS) == 39


def test_streamed_artifacts_say_why_they_cannot_be_exported(tmp_path):
    """流式产物是按块读的，导出等于物化——要说清这一点，而不是假装"没有数据"。"""
    frame = pd.DataFrame(
        {
            "equipment": [0] * 40 + [1] * 40,
            "time": list(range(40)) * 2,
            "label": [0] * 40 + [1] * 40,
            "vibration": [float(index) for index in range(80)],
        }
    )
    frame.to_csv(tmp_path / "streamed.csv", index=False)
    app = create_app(data_root=tmp_path)
    with TestClient(app) as client:
        pipeline = client.post("/api/control/create_pipeline", json={"name": "stream"}).json()["pipeline_id"]
        client.post(
            "/api/control/add_components",
            json={
                "pipeline_id": pipeline,
                "components": [
                    {
                        "node_id": "source",
                        "component_type": "data.input",
                        "parameters": {"path": "streamed.csv", "streaming": True, "chunk_rows": 128},
                    },
                    {
                        "node_id": "overview",
                        "component_type": "visual.overview",
                        "parameters": {"time_column": "time"},
                    },
                ],
            },
        )
        client.post(
            "/api/control/connect_many",
            json={
                "pipeline_id": pipeline,
                "connections": [
                    {
                        "source_node": "source",
                        "source_port": "dataset",
                        "target_node": "overview",
                        "target_port": "dataset",
                    },
                ],
            },
        )
        started = client.post("/api/control/execute_pipeline", json={"pipeline_id": pipeline})
        assert started.json()["success"], started.json()
        done = client.post(
            "/api/control/wait_for_pipeline",
            json={"pipeline_id": pipeline, "timeout_seconds": 60},
        ).json()
        assert done["status"] == "SUCCESS", done
        spec = client.get("/api/node-data", params={"pipeline_id": pipeline, "node_id": "overview"}).json()
        row = next(item for item in spec["data"] if item["kind"] == "streamed")
        assert row["available"] is False
        assert "data.materialize" in row["reason"]
        blocked = client.get(
            "/api/node-data/csv",
            params={"pipeline_id": pipeline, "node_id": "overview", "direction": "input"},
        )
        assert blocked.status_code == 400
        assert "data.materialize" in blocked.json()["summary"]
