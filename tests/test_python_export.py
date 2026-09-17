"""导出的 Python 文件必须"真的能跑、且与服务里的图是同一张图"。

这个功能最容易做成"看起来导出了、跑不起来"或"跑起来了、但不是那张图"，所以测试盯三件事：

1. **保真**：脚本重建的图与源图逐节点、逐参数、逐连线一致（用 XML 往返比对）；
2. **可执行**：脚本能在子进程里独立跑完（不经过服务），节点全 SUCCESS；
3. **边界**：空方案不导出、路径逃逸与错后缀被拒、未完成的图能导出但要报出缺什么。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fault_platform.api import create_app
from fault_platform.examples import create_dataset
from fault_platform.graph import ComponentGraph
from fault_platform.python_export import render_python, slugify
from fault_platform.registry import default_registry
from fault_platform.service import PipelineService
from fault_platform.xml_io import XMLParser


def build_small_graph(registry) -> ComponentGraph:
    """一张够小又够完整的图：输入 → 窗口统计特征 → 随机森林。"""
    graph = ComponentGraph(registry, "导出保真测试", "pipeline_export_test")
    graph.add_node("data.input", "source", {"path": "sample.csv"}, {"x": 40, "y": 60})
    graph.add_node(
        "feature.statistical",
        "stat",
        {
            "columns": ["vibration", "temperature"],
            "features": ["mean", "std", "rms"],
            "group_column": "equipment",
            "label_column": "label",
            "time_column": "time",
            "window_size": 8,
            "step": 8,
            "label_policy": "mode",
        },
        {"x": 300, "y": 60},
    )
    graph.add_node(
        "validation.random_forest",
        "forest",
        {"split_method": "group", "n_estimators": 20},
        {"x": 560, "y": 60},
    )
    graph.connect("source", "dataset", "stat", "dataset")
    graph.connect("stat", "features", "forest", "features")
    graph.connect("stat", "labels", "forest", "labels")
    return graph


def graph_fingerprint(graph: ComponentGraph) -> dict:
    """把"图长什么样"压成可比对的结构：节点（含参数与位置）+ 边。"""
    serialized = graph.serialize()
    return {
        "id": serialized["id"],
        "name": serialized["name"],
        "nodes": [
            {
                "id": node["id"],
                "type": node["type"],
                "parameters": node["parameters"],
                "position": node["position"],
            }
            for node in serialized["nodes"]
        ],
        "edges": [
            [edge["source_node"], edge["source_port"], edge["target_node"], edge["target_port"]]
            for edge in serialized["edges"]
        ],
    }


def test_render_embeds_every_node_edge_and_parameter() -> None:
    graph = build_small_graph(default_registry())
    source = render_python(graph, data_root="examples/data")
    compile(source, "exported.py", "exec")
    for node in graph.nodes.values():
        assert node.id in source
        assert node.component.component_type in source
    assert "'window_size': 8" in source
    assert "'label_policy': 'mode'" in source
    assert "graph.connect(source_node, source_port, target_node, target_port)" in source
    assert "'sample.csv'" in source
    # 位置也要带过去，否则重新导入后布局会散掉。
    assert "{'x': 560, 'y': 60}" in source


def run_script(arguments: list[str]) -> subprocess.CompletedProcess:
    """在子进程里跑导出的脚本。

    Windows 上子进程的默认编码是 GBK，而脚本会打印中文（例如数据目录不存在的提示），
    不显式指定 UTF-8 会得到 UnicodeDecodeError —— 这是我第一次跑测试时踩到的。
    """
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    return subprocess.run(
        [sys.executable, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        env=environment,
    )


def test_render_refuses_an_empty_pipeline_and_slugifies_names() -> None:
    with pytest.raises(ValueError, match="nothing to export"):
        render_python(ComponentGraph(default_registry(), "空图", "pipeline_empty"))
    # 纯中文名拿不到 ASCII 主干 → 回退到 pipeline_id；混排名保留 ASCII 部分。
    assert slugify("行级故障预测", "pipeline_x") == "pipeline_x"
    assert slugify("HBM 行级故障预测", "pipeline_x") == "HBM"
    assert slugify("HBM row-level v2", "pipeline_x") == "HBM_row-level_v2"


def test_exported_source_stays_valid_with_windows_paths_and_backslash_names(tmp_path: Path) -> None:
    """回归：docstring 里出现 ``C:\\Users\\...`` 时 ``\\U`` 会被当转义 → 导出文件语法错误。

    Windows 上 ``storage_root`` 必然含反斜杠，所以这不是理论边界：修之前**每个 Windows 用户**
    都会拿到一个打不开的 .py。修复方式是路径只进注释、自由文本里的反斜杠加倍。
    """
    graph = build_small_graph(default_registry())
    graph.name = "路径\\A 测试"
    source = render_python(graph, data_root="C:\\data\\root", storage_hint="C:\\Users\\me\\x.py")
    compile(source, "exported.py", "exec")
    assert "# 导出位置: C:\\Users\\me\\x.py" in source


def test_exported_script_rebuilds_the_same_graph(tmp_path: Path) -> None:
    """保真：脚本用 --no-execute --xml 导出的 XML，必须与源图逐项一致。"""
    graph = build_small_graph(default_registry())
    script = tmp_path / "exported.py"
    script.write_text(render_python(graph, data_root=tmp_path), encoding="utf-8")
    out_xml = tmp_path / "rebuilt.xml"
    result = run_script([str(script), "--no-execute", "--xml", str(out_xml)])
    assert result.returncode == 0, result.stderr
    assert "graph ok: 3 nodes, 3 edges" in result.stdout
    rebuilt = XMLParser(default_registry()).loads(out_xml.read_text(encoding="utf-8"))
    assert graph_fingerprint(rebuilt) == graph_fingerprint(graph)


def test_exported_script_executes_end_to_end(tmp_path: Path) -> None:
    """可执行：子进程里独立跑完，11 行数据也要真的出指标。"""
    data_root = tmp_path / "data"
    data_root.mkdir()
    create_dataset(data_root / "sample.csv")
    graph = build_small_graph(default_registry())
    script = tmp_path / "exported.py"
    script.write_text(render_python(graph, data_root=data_root), encoding="utf-8")
    result = run_script([str(script)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "status: SUCCESS" in result.stdout
    assert "[forest] " in result.stdout and "accuracy=" in result.stdout
    assert "source: SUCCESS" in result.stdout

    missing = run_script([str(script), "--data-root", str(tmp_path / "nope")])
    assert missing.returncode == 2
    assert "data root does not exist" in missing.stdout


def test_export_python_writes_inside_storage_and_reports_problems(tmp_path: Path) -> None:
    service = PipelineService(tmp_path / "data", tmp_path / "storage")
    graph = build_small_graph(default_registry())
    service.graphs[graph.pipeline_id] = graph
    result = service.export_python(graph.pipeline_id)
    written = Path(result["path"])
    assert written.parent == (tmp_path / "storage").resolve()
    assert written.suffix == ".py" and written.exists()
    assert compile(written.read_text(encoding="utf-8"), str(written), "exec")
    assert result["node_count"] == 3 and result["edge_count"] == 3
    assert result["validation_problems"] == []

    # 未完成的图：能导出，但要把缺什么写在返回值里。
    incomplete = ComponentGraph(default_registry(), "未完成", "pipeline_incomplete")
    incomplete.add_node("feature.statistical", "stat", {"columns": ["vibration"]})
    service.graphs[incomplete.pipeline_id] = incomplete
    partial = service.export_python(incomplete.pipeline_id)
    assert partial["validation_problems"]

    empty = ComponentGraph(default_registry(), "空", "pipeline_empty")
    service.graphs[empty.pipeline_id] = empty
    with pytest.raises(ValueError, match="nothing to export"):
        service.export_python(empty.pipeline_id)
    with pytest.raises(ValueError, match="within the pipeline storage directory"):
        service.export_python(graph.pipeline_id, filename="../escape.py")
    with pytest.raises(ValueError, match="must be a Python file"):
        service.export_python(graph.pipeline_id, filename="pipeline.xml")
    service.close()


def test_export_python_is_available_over_the_control_api(tmp_path: Path) -> None:
    """MCP 工具就是控制面操作，因此这里按 MCP 桥的路径验证一次（含 include_code）。"""
    service = PipelineService(tmp_path / "data", tmp_path / "storage")
    graph = build_small_graph(default_registry())
    service.graphs[graph.pipeline_id] = graph
    with TestClient(create_app(service=service)) as client:
        response = client.post(
            "/api/control/export_python",
            json={"pipeline_id": graph.pipeline_id, "include_code": True},
        )
        payload = response.json()
        assert payload["success"] is True, payload
        assert payload["filename"].endswith(".py")
        compile(payload["code"], payload["filename"], "exec")
        assert payload["node_count"] == 3
    service.close()
