"""Render a pipeline as a standalone, runnable Python script.

导出的目标是**能跑、能改、能进版本管理**的代码，而不是把图打印成一段文本：

* 只依赖 ``fault_platform`` 的 Python API（``ComponentGraph`` + ``ExecutionEngine``），
  不经过 HTTP、MCP 或网页；
* 节点、参数、画布位置与连线**逐字保留**，因此"导出的脚本建出来的图"与"服务里的图"
  是同一张图（``tests/test_python_export.py`` 会用 XML 往返比对这一点）；
* 数据路径用 ``--data-root`` 参数化：默认写成导出时服务的 ``data_root``（本机开箱能跑），
  换机器时改一个参数即可；
* 默认执行并打印逐节点状态与关键指标，``--no-execute`` 只建图+校验（不需要数据）。

刻意不做的事：不生成"读服务 API 的客户端脚本"。那种脚本把图留在服务里，
换一台机器就没了；这里要的是**图本身**变成代码。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from pprint import pformat
from typing import Any

from fault_platform.graph import ComponentGraph
from fault_platform.version import PLATFORM_VERSION

#: 指标里优先展示的键（存在才打印），顺序即阅读顺序。
SUMMARY_KEYS = (
    "algorithm",
    "accuracy",
    "balanced_accuracy",
    "averaging",
    "precision",
    "recall",
    "f1",
    "roc_auc",
    "average_precision",
    "miss_rate",
    "r2",
    "mae",
    "rmse",
    "anomaly_count",
    "anomaly_rate",
    "test_count",
    "train_count",
    "train_class_counts",
    "test_class_counts",
    "train_class_rates",
    "test_class_rates",
    "split_method",
    # 切分被调整过（时间切点移动）时的说明：不打印它，"留出集不是最后 25%" 就没人知道。
    "split_note",
    # 逐类指标：精确率/召回率/F1 是宏平均，逐类数值用来跟混淆矩阵对齐复核。
    "per_class_support",
    "per_class_precision",
    "per_class_recall",
    "per_class_f1",
)


def slugify(name: str, fallback: str) -> str:
    """把方案名变成安全的文件名主干。

    只保留 ASCII 字母、数字、下划线与短横线——中文名会得到空串，此时回退到 ``fallback``
    （调用方传 pipeline_id）。不保留中文是为了让文件名在各平台、各工具链里都不会出问题。
    """
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    return slug[:60] or fallback


def _clean(value: Any, *, where: str) -> Any:
    """把参数规整成纯 JSON 基本类型，保证 ``pformat`` 的输出是合法的 Python 字面量。"""
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Cannot export non-literal parameter value at {where}: {value!r}") from exc


def _comment(text: str) -> str:
    """把任意文本压成单行注释，避免换行把代码结构撑坏。"""
    return re.sub(r"\s+", " ", str(text)).strip()


def _docstring_safe(text: str) -> str:
    """让文本能安全地出现在**模块 docstring** 里。

    反斜杠在普通字符串字面量里是转义引导符：``C:\\Users`` 以 ``\\U`` 开头，Python 会抛
    ``unicodeescape ... truncated \\UXXXXXXXX escape``——导出文件在 Windows 上会**直接语法错误**。
    所以进 docstring 的自由文本必须把 ``\\`` 加倍；路径这类内容更适合放注释（注释不做转义）。
    """
    return _comment(text).replace("\\", "\\\\")


def render_python(
    graph: ComponentGraph,
    *,
    data_root: str | Path | None = None,
    storage_hint: str = "",
) -> str:
    """把一张图渲染成完整的、可直接运行的 Python 源码。"""
    if not graph.nodes:
        raise ValueError("Pipeline is empty; nothing to export")
    nodes: list[tuple[str, str, Any, Any]] = []
    for node_id, node in graph.nodes.items():
        parameters = _clean(node.component.parameters, where=f"node {node_id}")
        position = _clean(node.position, where=f"node {node_id} position")
        nodes.append((node_id, node.component.component_type, parameters, position))
    edges = list(graph.edges)
    data_root_literal = repr(str(Path(data_root).resolve())) if data_root else "''"
    name = _docstring_safe(graph.name)
    exported_filename = f"{slugify(graph.name, graph.pipeline_id)}.py"
    lines: list[str] = [
        '"""Pipeline exported from fault-platform.',
        "",
        f"Name         : {name}",
        f"Pipeline id  : {graph.pipeline_id}",
        f"Graph version: {graph.version}",
        f"Contents     : {len(nodes)} nodes, {len(edges)} edges",
        f"Exported by  : fault-platform {PLATFORM_VERSION}",
        "",
        "同一个图既可以在服务里跑（网页/MCP），也可以直接跑这个文件。这个文件**不连服务**：",
        "它用 fault_platform 的 Python API 在本地重建同一张图并执行。",
        "",
        f"    python {exported_filename} --data-root <数据目录>",
        f"    python {exported_filename} --no-execute   # 只建图+校验，不需要数据",
        f"    python {exported_filename} --dataset source=<同构数据文件>   # 换一份数据跑同一张图（可重复）",
        f"    python {exported_filename} --dataset source=a.csv,b.csv   # 或用同一节点拼多份（整组替换）",
        "",
        "改了图之后请重新导出：这个文件是**快照**，不会自动跟随服务里的方案。",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import argparse",
        "from pathlib import Path",
        "",
        "from fault_platform.graph import ComponentGraph",
        "from fault_platform.registry import default_registry",
        "from fault_platform.runtime import ExecutionContext, ExecutionEngine",
        "from fault_platform.workspace import FaultWorkspace",
        "",
        f"PIPELINE_ID = {graph.pipeline_id!r}",
        f"PIPELINE_NAME = {graph.name!r}",
        "#: 导出时服务的 data_root；换机器时用 --data-root 覆盖。",
        f"DEFAULT_DATA_ROOT = {data_root_literal}",
    ]
    if storage_hint:
        # 路径写注释里：注释不参与转义解析，反斜杠不会出事。
        lines.append(f"# 导出位置: {_comment(storage_hint)}")
    lines += [
        "",
        "#: (node_id, component_type, parameters, canvas position)",
        "NODES = (",
    ]
    for node_id, component_type, parameters, position in nodes:
        rendered = pformat(parameters, width=104, sort_dicts=False)
        lines.append(f"    ({node_id!r}, {component_type!r},")
        lines.append(f"     {rendered},")
        lines.append(f"     {position!r}),")
    lines += [
        ")",
        "",
        "#: (source_node, source_port, target_node, target_port)",
        "EDGES = (",
    ]
    for edge in edges:
        lines.append(
            f"    ({edge.source_node!r}, {edge.source_port!r}, {edge.target_node!r}, {edge.target_port!r}),"
        )
    lines += [
        ")",
        "",
        "#: 打印指标时优先看的键（存在才打印）。",
        f"SUMMARY_KEYS = {SUMMARY_KEYS!r}",
        "",
        "",
        "def build_graph():",
        '    """按导出的节点与连线重建图；参数与位置逐字照抄。"""',
        "    registry = default_registry()",
        "    graph = ComponentGraph(registry, PIPELINE_NAME, PIPELINE_ID)",
        "    for node_id, component_type, parameters, position in NODES:",
        "        graph.add_node(component_type, node_id, dict(parameters), dict(position))",
        "    for source_node, source_port, target_node, target_port in EDGES:",
        "        graph.connect(source_node, source_port, target_node, target_port)",
        "    return graph",
        "",
        "",
        "def report(workspace, graph) -> None:",
        '    """打印逐节点状态与可读的指标摘要。"""',
        '    print("status:", workspace.status)',
        "    for node_id in graph.nodes:",
        '        print(f"  {node_id}: {workspace.node_status.get(node_id)}")',
        "    for node_id in graph.nodes:",
        "        try:",
        '            metrics = workspace.get_output(node_id, "metrics")',
        "        except Exception:",
        "            continue",
        "        if not isinstance(metrics, dict):",
        "            continue",
        "        shown = {key: metrics[key] for key in SUMMARY_KEYS if key in metrics}",
        "        if shown:",
        '            print(f"  [{node_id}] " + ", ".join(f"{k}={v}" for k, v in shown.items()))',
        '        for warning in metrics.get("warnings", []):',
        '            print(f"    warning: {warning}")',
        "",
        "",
        "def main() -> int:",
        "    parser = argparse.ArgumentParser(description=PIPELINE_NAME)",
        '    parser.add_argument("--data-root", default=DEFAULT_DATA_ROOT,',
        '                        help="数据目录；data.input 的 path 相对它解析")',
        '    parser.add_argument("--no-execute", action="store_true",',
        '                        help="只建图与校验，不读数据、不执行")',
        '    parser.add_argument("--xml", default="", help="可选：把重建的图再导出成 XML")',
        '    parser.add_argument("--dataset", action="append", default=[], metavar="NODE=PATH[,PATH...]",',
        '                        help="执行期数据源覆盖（整组替换），可重复；NODE 是 data.input 节点 id")',
        "    args = parser.parse_args()",
        "",
        "    graph = build_graph()",
        "    problems = graph.validate_graph()",
        "    if problems:",
        '        print("graph is not runnable:")',
        "        for problem in problems:",
        '            print("  -", problem)',
        "        return 1",
        '    print(f"graph ok: {len(graph.nodes)} nodes, {len(graph.edges)} edges")',
        "    if args.xml:",
        "        # --xml 也支持 --no-execute：重建出来的图本身就是要交付的东西。",
        "        from fault_platform.xml_io import XMLSerializer",
        "        XMLSerializer().save(graph, args.xml)",
        '        print("xml:", args.xml)',
        "    if args.no_execute:",
        "        return 0",
        "",
        "    data_root = Path(args.data_root)",
        "    if not data_root.is_dir():",
        '        print(f"data root does not exist: {data_root}（用 --data-root 指向数据目录）")',
        "        return 2",
        "    overrides: dict[str, list[str]] = {}",
        "    for item in args.dataset:",
        '        node_id, _, value = item.partition("=")',
        '        paths = [part.strip() for part in value.split(",") if part.strip()]',
        "        if not node_id or not paths:",
        '            print(f"--dataset expects NODE=PATH[,PATH...], got: {item}")',
        "            return 2",
        "        if node_id not in graph.nodes:",
        '            print(f"--dataset names an unknown node: {node_id}")',
        "            return 2",
        "        overrides.setdefault(node_id, []).extend(paths)",
        "    if overrides:",
        "        for node_id, paths in overrides.items():",
        '            print(f"dataset override {node_id}: " + ", ".join(paths))',
        "    workspace = FaultWorkspace(PIPELINE_ID)",
        "    context = ExecutionContext(workspace, data_root, dataset_overrides=overrides)",
        "    ExecutionEngine().execute(graph, context)",
        "    report(workspace, graph)",
        '    return 0 if str(workspace.status) == "SUCCESS" else 1',
        "",
        "",
        'if __name__ == "__main__":',
        "    raise SystemExit(main())",
        "",
    ]
    return "\n".join(lines)
