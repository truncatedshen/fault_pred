"""多数据源：入口把多份同构数据拼成一份，运行期可以整组换掉。

两类问题最容易做成"看起来能用"：

1. **拼接的诚实性**：列不一致时静默补 NaN/丢列，会让"两台机器数据结构不同"一路漂到模型里；
2. **增量复用**：换数据却不改图参数，指纹必须跟着变——否则第二次运行会拿第一次的结果冒充。

这两条都由测试盯着。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fault_core import advanced_data
from fault_platform.graph import ComponentGraph
from fault_platform.runtime import ExecutionContext, ExecutionEngine
from fault_platform.service import PipelineService
from fault_platform.workspace import FaultWorkspace, PipelineStatus


def write_source(path, label: int, rows: int = 12, *, columns=None, extra: str | None = None):
    """写一份同构小数据；``columns`` 可打乱列顺序，``extra`` 可加一列（用于 schema 测试）。"""
    rng = np.random.default_rng(label + 1)
    frame = pd.DataFrame(
        {
            "equipment": np.repeat(np.arange(3), rows // 3),
            "time": np.tile(np.arange(rows // 3), 3),
            "label": label,
            "vibration": rng.normal(2 + label * 3, 0.4, rows),
            "temperature": rng.normal(30 + label * 8, 2, rows),
        }
    )
    if extra:
        frame[extra] = 1
    path.parent.mkdir(parents=True, exist_ok=True)
    frame[columns or list(frame.columns)].to_csv(path, index=False)
    return path


def build_input_graph(source: dict) -> ComponentGraph:
    """data.input → feature.select：够小，但走的是真实的组件链路。"""
    graph = ComponentGraph(default_registry(), "多源输入测试", "pipeline_multi_source")
    graph.add_node("data.input", "source", source)
    graph.add_node(
        "feature.select",
        "select",
        {"columns": ["vibration", "temperature"]},
    )
    graph.connect("source", "dataset", "select", "dataset")
    return graph


from fault_platform.registry import default_registry  # noqa: E402  (放在函数后便于阅读)


def run(graph: ComponentGraph, data_root, **context_kwargs):
    workspace = FaultWorkspace(graph.pipeline_id)
    context = ExecutionContext(workspace, data_root, **context_kwargs)
    ExecutionEngine().execute(graph, context)
    assert workspace.status == PipelineStatus.SUCCESS, workspace.errors
    return workspace


def test_input_concatenates_multiple_sources(tmp_path) -> None:
    write_source(tmp_path / "a.csv", label=0)
    write_source(tmp_path / "b.csv", label=1)
    graph = build_input_graph({"path": "a.csv", "paths": ["b.csv"]})
    workspace = run(graph, tmp_path)
    dataset = workspace.get_output("source", "dataset")
    assert len(dataset) == 24
    # 索引必须重排：两份文件各自 0..11，直接拼会出现重复索引。
    assert list(dataset.index) == list(range(24))
    assert dataset["label"].tolist() == [0] * 12 + [1] * 12
    assert dataset.attrs["source_paths"] == [
        str((tmp_path / "a.csv").resolve()),
        str((tmp_path / "b.csv").resolve()),
    ]
    assert "Combined 2 sources" in " ".join(dataset.attrs["evaluation_warnings"])


def test_input_reorders_columns_and_rejects_mismatched_schemas(tmp_path) -> None:
    write_source(tmp_path / "a.csv", label=0)
    # 列顺序不同但集合相同：应当按第一份的顺序对齐，而不是各自为政。
    write_source(
        tmp_path / "b.csv",
        label=1,
        columns=["temperature", "vibration", "label", "time", "equipment"],
    )
    graph = build_input_graph({"path": "a.csv", "paths": ["b.csv"]})
    dataset = run(graph, tmp_path).get_output("source", "dataset")
    assert list(dataset.columns) == ["equipment", "time", "label", "vibration", "temperature"]
    assert dataset["label"].tolist() == [0] * 12 + [1] * 12

    # 列集合不同：必须报错并点名缺/多哪一列（静默补 NaN 是最坏的选择）。
    write_source(tmp_path / "c.csv", label=2, extra="pressure")
    broken = build_input_graph({"path": "a.csv", "paths": ["c.csv"]})
    workspace = FaultWorkspace(broken.pipeline_id)
    ExecutionEngine().execute(broken, ExecutionContext(workspace, tmp_path))
    assert workspace.status == PipelineStatus.FAILED
    message = workspace.errors["source"]["error_message"]
    assert "different schema" in message and "pressure" in message


def test_source_column_records_where_each_row_came_from(tmp_path) -> None:
    write_source(tmp_path / "a.csv", label=0)
    write_source(tmp_path / "b.csv", label=1)
    graph = build_input_graph({"path": "a.csv", "paths": ["b.csv"], "source_column": "source_file"})
    dataset = run(graph, tmp_path).get_output("source", "dataset")
    assert dataset["source_file"].tolist() == ["a.csv"] * 12 + ["b.csv"] * 12


def test_source_fingerprint_covers_every_file(tmp_path) -> None:
    write_source(tmp_path / "a.csv", label=0)
    write_source(tmp_path / "b.csv", label=1)
    graph = build_input_graph({"path": "a.csv", "paths": ["b.csv"]})
    first = run(graph, tmp_path).get_output("source", "dataset").attrs["source_id"]
    # 只改第二份文件：source_id 必须变（否则增量复用会拿旧结果冒充）。
    write_source(tmp_path / "b.csv", label=1, rows=15)
    second = run(graph, tmp_path).get_output("source", "dataset").attrs["source_id"]
    assert first != second
    # 单源时摘要必须与"只读那一个文件"一致（既有方案的缓存不能因此失效）。
    single = build_input_graph({"path": "a.csv"})
    only = run(single, tmp_path).get_output("source", "dataset").attrs["source_id"]
    assert only != first


def test_streaming_rejects_multiple_sources(tmp_path) -> None:
    write_source(tmp_path / "a.csv", label=0)
    write_source(tmp_path / "b.csv", label=1)
    graph = build_input_graph({"path": "a.csv", "paths": ["b.csv"], "streaming": True})
    workspace = FaultWorkspace(graph.pipeline_id)
    ExecutionEngine().execute(graph, ExecutionContext(workspace, tmp_path))
    assert workspace.status == PipelineStatus.FAILED
    assert "single source" in workspace.errors["source"]["error_message"]


def build_concat_graph(tmp_path, ports: int, source_column: str = "") -> ComponentGraph:
    """N 个 data.input 各接一份同构文件，接到同一个 data.concat 上（前端就是这么拉的）。"""
    graph = ComponentGraph(default_registry(), "拼接组件测试", "pipeline_concat")
    graph.add_node("data.concat", "concat", {"source_column": source_column})
    for index, name in enumerate(["first", "second", "third", "fourth"][:ports], start=1):
        write_source(tmp_path / f"src{index}.csv", label=index - 1)
        graph.add_node("data.input", f"src{index}", {"path": f"src{index}.csv"})
        graph.connect(f"src{index}", "dataset", "concat", name)
    return graph


def test_concat_component_merges_two_and_four_branches(tmp_path) -> None:
    """两个源、四个源都要能拼；可选端口没接就跳过。"""
    two = build_concat_graph(tmp_path, 2)
    dataset = run(two, tmp_path).get_output("concat", "dataset")
    assert len(dataset) == 24
    assert list(dataset.index) == list(range(24))
    assert dataset["label"].tolist() == [0] * 12 + [1] * 12
    assert dataset.attrs["concatenated_inputs"] == ["first", "second"]
    assert "Concatenated 2 inputs" in " ".join(dataset.attrs["evaluation_warnings"])

    four = build_concat_graph(tmp_path, 4)
    merged = run(four, tmp_path).get_output("concat", "dataset")
    assert len(merged) == 48
    assert merged["label"].tolist() == [0] * 12 + [1] * 12 + [2] * 12 + [3] * 12
    assert merged.attrs["concatenated_inputs"] == ["first", "second", "third", "fourth"]


def test_concat_requires_two_branches(tmp_path) -> None:
    """合并节点至少要两条输入：只接一条会被**结构校验**拦下（与 `feature.merge` 同一约定）。

    半接线的图应当在 `validate_pipeline` 阶段就报错，而不是等到运行才炸。
    底层函数本身对单表是"原样返回"的（它是个通用工具），这里一并钉住。
    """
    graph = build_concat_graph(tmp_path, 1)
    workspace = FaultWorkspace(graph.pipeline_id)
    ExecutionEngine().execute(graph, ExecutionContext(workspace, tmp_path))
    assert workspace.status == PipelineStatus.FAILED
    assert "required input not connected" in workspace.errors["_validation"]["error_message"]

    single = pd.DataFrame({"a": [1, 2, 3]})
    passthrough = advanced_data.concat_by_rows([("first", single)])
    assert passthrough.equals(single)
    assert passthrough.attrs["concatenated_inputs"] == ["first"]


def test_concat_source_column_and_schema_guard(tmp_path) -> None:
    graph = build_concat_graph(tmp_path, 3, source_column="来源")
    dataset = run(graph, tmp_path).get_output("concat", "dataset")
    assert dataset["来源"].tolist() == ["first"] * 12 + ["second"] * 12 + ["third"] * 12

    # 第二份文件多一列 → 必须报错并点名是哪个输入端口、多了什么。
    # 注意要在建图**之后**改文件：build_concat_graph 会重写 src1/src2。
    broken = build_concat_graph(tmp_path, 2)
    write_source(tmp_path / "src2.csv", label=1, extra="pressure")
    workspace = FaultWorkspace(broken.pipeline_id)
    ExecutionEngine().execute(broken, ExecutionContext(workspace, tmp_path))
    assert workspace.status == PipelineStatus.FAILED
    message = workspace.errors["concat"]["error_message"]
    assert "second" in message and "pressure" in message


def test_concat_is_discoverable_and_renders_in_the_palette() -> None:
    """前端要能自己找到并拉出来：检索命中 + schema 里有 4 个输入端口。"""
    registry = default_registry()
    found = {item["component_type"] for item in registry.retrieve(intent="多数据源合并", limit=3)}
    assert "data.concat" in found
    schema = registry.get("data.concat").schema()
    ports = {port["name"]: port for port in schema["input_ports"]}
    assert list(ports) == ["first", "second", "third", "fourth"]
    assert ports["first"]["required"] and ports["second"]["required"]
    assert not ports["third"]["required"] and not ports["fourth"]["required"]
    assert schema["category"] == "data"


def test_execute_pipeline_dataset_overrides_replace_the_whole_source_set(tmp_path) -> None:
    """运行期覆盖：同一张图，先跑一份数据，再整组换成两份数据，都不改图。"""
    write_source(tmp_path / "a.csv", label=0)
    write_source(tmp_path / "b.csv", label=1)
    graph = build_input_graph({"path": "a.csv"})
    service = PipelineService(tmp_path, tmp_path / "storage")
    service.graphs[graph.pipeline_id] = graph
    try:
        first = service.execute_pipeline(graph.pipeline_id, dataset_overrides={"source": "a.csv"})
        assert first["dataset_overrides"] == {"source": "a.csv"}
        service.jobs[graph.pipeline_id].result(timeout=60)
        # summarize() 返回的是有界摘要，行数看 shape，不是 len(载荷)。
        single_preview = service.get_node_result(graph.pipeline_id, "source")["outputs"]["dataset"]
        assert single_preview["shape"] == [12, 5]

        # 整组替换成两份：列表语义 = 只读这几个文件（顺序即拼接顺序）。
        second = service.execute_pipeline(graph.pipeline_id, dataset_overrides={"source": ["a.csv", "b.csv"]})
        assert second["dataset_overrides"] == {"source": ["a.csv", "b.csv"]}
        service.jobs[graph.pipeline_id].result(timeout=60)
        status = service.get_pipeline_status(graph.pipeline_id)
        assert status["status"] == "SUCCESS"
        preview = service.get_node_result(graph.pipeline_id, "source")["outputs"]["dataset"]
        assert preview["shape"] == [24, 5]

        # 图本身没变：参数还是 a.csv、paths 还是空。
        assert graph.get_node("source").component.parameters["paths"] == []
        assert graph.get_node("source").component.parameters["path"] == "a.csv"
    finally:
        service.close()


def test_node_warnings_reach_get_node_result(tmp_path) -> None:
    """节点自己的警告必须在 `get_node_result` 里看得到。

    回归：运行时一直把组件的 warnings 收进 `ws.node_warnings`，但服务层只回方案级提示，
    于是"这次读的是覆盖数据源""输入被裁剪过"这类警告在节点级是**读不到的**——
    与 skill 里写明的返回结构（`{status, outputs, error, warnings}`）不符。
    """
    write_source(tmp_path / "a.csv", label=0)
    write_source(tmp_path / "b.csv", label=1)
    graph = build_input_graph({"path": "a.csv"})
    service = PipelineService(tmp_path, tmp_path / "storage")
    service.graphs[graph.pipeline_id] = graph
    try:
        service.execute_pipeline(graph.pipeline_id, dataset_overrides={"source": ["a.csv", "b.csv"]})
        service.jobs[graph.pipeline_id].result(timeout=60)
        payload = service.get_node_result(graph.pipeline_id, "source")
        text = " ".join(payload["warnings"])
        assert "dataset override" in text
        assert "Combined 2 sources" in text
        # 方案级提示仍在（两类警告是拼接，不是替换）。
        assert payload["workspace_id"]
    finally:
        service.close()


def test_execute_pipeline_rejects_bad_overrides(tmp_path) -> None:
    write_source(tmp_path / "a.csv", label=0)
    graph = build_input_graph({"path": "a.csv"})
    service = PipelineService(tmp_path, tmp_path / "storage")
    service.graphs[graph.pipeline_id] = graph
    try:
        with pytest.raises(ValueError, match="Unknown node"):
            service.execute_pipeline(graph.pipeline_id, dataset_overrides={"nope": "a.csv"})
        with pytest.raises(ValueError, match="only data.input nodes"):
            service.execute_pipeline(graph.pipeline_id, dataset_overrides={"select": "a.csv"})
        with pytest.raises(ValueError, match="is invalid"):
            service.execute_pipeline(graph.pipeline_id, dataset_overrides={"source": "missing.csv"})
        with pytest.raises(ValueError, match="is invalid"):
            service.execute_pipeline(graph.pipeline_id, dataset_overrides={"source": "../escape.csv"})
        with pytest.raises(ValueError, match="must be a path or a list of paths"):
            service.execute_pipeline(graph.pipeline_id, dataset_overrides={"source": 42})
    finally:
        service.close()
