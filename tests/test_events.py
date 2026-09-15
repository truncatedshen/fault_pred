"""Live-sync plumbing: bounded event bus, runtime transitions and the SSE endpoint."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from fault_platform.events import MAX_QUEUE, EventBus
from fault_platform.examples import create_dataset
from fault_platform.service import PipelineService


def test_event_bus_filters_replays_and_never_blocks_a_slow_reader():
    bus = EventBus(replay=5)
    pipeline, other = bus.subscribe("p1"), bus.subscribe("p2")
    everywhere, _ = bus.subscribe()
    bus.publish("p1", "graph_changed", version=2)
    bus.publish("p2", "graph_changed", version=9)
    assert [event.type for event in pipeline[1]] == []
    assert pipeline[0].get(timeout=1).data["version"] == 2
    assert other[0].get(timeout=1).data["version"] == 9
    assert everywhere.get(timeout=1).pipeline_id == "p1"
    assert bus.subscriber_count == 3

    late, replay = bus.subscribe("p1", last_event_id="0")
    assert [event.data["version"] for event in replay] == [2]

    # A browser reconnecting after a server restart carries an id from the old process.
    stale, stale_replay = bus.subscribe("p1", last_event_id="deadbeef-99")
    assert [event.data["version"] for event in stale_replay] == [2]
    assert bus.frame_id(replay[0]) == f"{bus.boot_id}-{replay[0].event_id}"
    resumed, resumed_replay = bus.subscribe("p1", last_event_id=f"{bus.boot_id}-1")
    assert resumed_replay == []
    bus.unsubscribe(stale)
    bus.unsubscribe(resumed)

    for index in range(MAX_QUEUE + 25):
        bus.publish("p1", "node_status", node_id=f"n{index}")
    assert late.queue.qsize() <= MAX_QUEUE  # oldest frames are dropped, publishing never blocks

    bus.unsubscribe(late)
    assert bus.subscriber_count == 3
    bus.close()
    assert bus.subscriber_count == 0


def test_service_publishes_edits_and_execution_transitions(tmp_path: Path):
    data_root = tmp_path / "data"
    create_dataset(data_root / "synthetic_equipment.csv")
    service = PipelineService(data_root, tmp_path / "saved")
    try:
        pipeline_id = service.create_pipeline("事件测试")["pipeline_id"]
        subscription, replay = service.subscribe_events(pipeline_id)
        assert replay[0].type == "graph_changed"

        service.add_component(pipeline_id, "data.input", "source", {"path": "synthetic_equipment.csv"})
        service.add_component(
            pipeline_id,
            "feature.statistical",
            "stats",
            {
                "columns": ["vibration"],
                "group_column": "equipment",
                "label_column": "label",
                "window_size": 16,
            },
        )
        service.add_component(
            pipeline_id, "validation.random_forest", "model", {"n_estimators": 30, "split_method": "group"}
        )
        service.connect_components(pipeline_id, "source", "dataset", "stats", "dataset")
        service.connect_components(pipeline_id, "stats", "features", "model", "features")
        service.connect_components(pipeline_id, "stats", "labels", "model", "labels")

        edits = []
        while True:
            event = subscription.get(timeout=1)
            if event is None:
                break
            edits.append(event)
        assert [event.data["operation"] for event in edits] == [
            "add_component",
            "add_component",
            "add_component",
            "connect_components",
            "connect_components",
            "connect_components",
        ]
        assert edits[-1].data["version"] == service.get_pipeline(pipeline_id)["graph"]["version"]

        service.execute_pipeline(pipeline_id)
        seen: list[tuple[str, str | None, str]] = []
        deadline = time.time() + 60
        while time.time() < deadline:
            event = subscription.get(timeout=1)
            if event is None:
                continue
            seen.append((event.type, event.data.get("node_id"), str(event.data.get("status"))))
            if event.type == "pipeline_status" and event.data["status"] == "SUCCESS":
                break
        assert ("run_started", None, "None") in seen
        assert ("pipeline_status", None, "RUNNING") in seen
        assert ("node_status", "source", "RUNNING") in seen
        assert ("node_status", "model", "SUCCESS") in seen
        assert ("history", "source", "SUCCESS") in seen
        assert seen[-1] == ("pipeline_status", None, "SUCCESS")

        history = [event for event in service.bus.recent(pipeline_id, limit=50) if event.type == "history"]
        assert {event.data["node_id"] for event in history} == {"source", "stats", "model"}
        assert all(event.data["success"] for event in history)

        checkpoint_id = service.save_checkpoint(pipeline_id)["checkpoint"]["checkpoint_id"]
        assert service.bus.recent(pipeline_id, limit=1)[0].data["checkpoint_id"] == checkpoint_id
    finally:
        service.close()


@pytest.fixture
def live_server(tmp_path: Path):
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


def test_events_endpoint_streams_graph_changes(live_server):
    timeout = httpx.Timeout(10.0, read=20.0)
    with httpx.Client(trust_env=False, timeout=timeout) as control:
        created = control.post(live_server + "/api/control/create_pipeline", json={"name": "SSE"}).json()
        pipeline_id = created["pipeline_id"]
        unknown = control.get(live_server + "/api/events", params={"pipeline_id": "pipeline_missing"})
        assert unknown.status_code == 404
        with httpx.stream(
            "GET", live_server + "/api/events", params={"pipeline_id": pipeline_id}, timeout=timeout
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            lines = response.iter_lines()
            assert next(lines).startswith(": connected")
            control.post(
                live_server + "/api/control/add_component",
                json={
                    "pipeline_id": pipeline_id,
                    "component_type": "data.input",
                    "node_id": "source",
                    "parameters": {"path": "synthetic_equipment.csv"},
                },
            )
            frames: list[dict] = []
            for line in lines:
                if line.startswith("data: "):
                    frames.append(json.loads(line[6:]))
                if any(frame.get("operation") == "add_component" for frame in frames):
                    break
                if len(frames) > 40:
                    break
            assert frames, "no SSE frames were received"
            changed = frames[-1]
            assert changed["type"] == "graph_changed"
            assert changed["pipeline_id"] == pipeline_id
            assert changed["operation"] == "add_component"
            assert changed["version"] == 2
            assert [frame["operation"] for frame in frames[:2]] == ["create_pipeline", "add_component"]
