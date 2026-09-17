"""Lossless graph XML with structural and registry validation.

XML 只承载**配置**（节点、参数、端口、连接、画布信息），不承载任何运行产物，
因此文件很小（超过 2 MB 直接拒绝）。写与读都做严格校验：

* 写：先校验图，再按 XSD 校验生成的文档，保证导出物一定可被本平台读回；
* 读：先做 XXE 防护（禁用实体解析、禁止 DTD、不联网），再按 XSD 校验，
  然后逐节点比对"XML 里声明的端口"与"注册表里的端口"，最后校验连接与整体结构。

这样"导出的 XML 换个版本读不回来"这类问题会在导入时立刻暴露成明确错误，
而不是在运行时变成难以定位的行为差异。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lxml import etree

from fault_platform.graph import ComponentGraph
from fault_platform.registry import ComponentRegistry

SCHEMA_PATH = Path(__file__).with_name("schema.xsd")
MAX_XML_BYTES = 2_000_000


def _json(value: Any) -> str:
    """按 JSON 存参数值：保留类型（数字/布尔/列表/字典）且拒绝 NaN。"""
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _parse(text: str) -> etree._Element:
    """把 XML 文本解析成元素树，并做完整体积、安全与 XSD 校验。

    安全设置：``resolve_entities=False``、``no_network=True``、``load_dtd=False``、
    ``huge_tree=False``，并显式拒绝带 DOCTYPE 的文档——这些都是为了阻断 XXE
    与实体膨胀（billion laughs）这类解析器层面的攻击。
    """
    raw = text.encode("utf-8")
    if len(raw) > MAX_XML_BYTES:
        raise ValueError("XML exceeds 2 MB; graphs must not embed runtime datasets")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    try:
        root = etree.fromstring(raw, parser)
        if root.getroottree().docinfo.doctype:
            raise ValueError("DOCTYPE/entity declarations are not supported")
        schema = etree.XMLSchema(etree.parse(str(SCHEMA_PATH)))
        schema.assertValid(root)
        return root
    except (etree.XMLSyntaxError, etree.DocumentInvalid) as exc:
        raise ValueError(f"Invalid pipeline XML: {exc}") from exc


class XMLSerializer:
    def dumps(self, graph: ComponentGraph) -> str:
        """把图序列化成 XML 文本。

        参数按 ``encoding="json"`` 存放，端口声明的顺序与注册表一致，
        因此同一个图重复导出得到稳定一致的文本（便于 diff 与版本管理）。
        生成后立刻用 :func:`_parse` 自校验：不会写出"自己都读不回来"的文件。
        """
        errors = graph.validate_graph(require_complete=False)
        if errors:
            # 允许导出"编辑中"的图（未填参数/未连线），但结构错误必须挡住。
            raise ValueError("; ".join(errors))
        root = etree.Element(
            "faultPredictionPipeline",
            id=graph.pipeline_id,
            name=graph.name,
            version="1.0",
            graphVersion=str(graph.version),
        )
        etree.SubElement(root, "metadata").text = _json(graph.metadata)
        nodes = etree.SubElement(root, "nodes")
        for node in graph.nodes.values():
            element = etree.SubElement(
                nodes,
                "node",
                id=node.id,
                type=node.component.component_type,
                componentVersion=node.component.metadata.version,
            )
            etree.SubElement(element, "position", x=str(node.position["x"]), y=str(node.position["y"]))
            parameters = etree.SubElement(element, "parameters")
            for name, value in node.component.parameters.items():
                etree.SubElement(parameters, "parameter", name=name, encoding="json").text = _json(value)
            ports = etree.SubElement(element, "ports")
            for tag, specs in (
                ("inputPort", node.component.input_ports),
                ("outputPort", node.component.output_ports),
            ):
                for port in specs:
                    etree.SubElement(
                        ports,
                        tag,
                        name=port.name,
                        dataType=port.data_type,
                        required=str(port.required).lower(),
                    )
            etree.SubElement(element, "ui").text = _json(node.ui)
        connections = etree.SubElement(root, "connections")
        for edge in graph.edges:
            etree.SubElement(
                connections,
                "connection",
                sourceNode=edge.source_node,
                sourcePort=edge.source_port,
                targetNode=edge.target_node,
                targetPort=edge.target_port,
            )
        etree.SubElement(root, "ui").text = _json(graph.ui)
        result = etree.tostring(root, encoding="utf-8", xml_declaration=True, pretty_print=True).decode(
            "utf-8"
        )
        _parse(result)
        return result

    def save(self, graph: ComponentGraph, path: str | Path) -> None:
        """把图写到磁盘（调用方负责先写临时文件再原子替换）。"""
        Path(path).write_text(self.dumps(graph), encoding="utf-8")


def _port_declaration_problem(
    declared: list[tuple[str, str | None, str | None, str | None]],
    expected: list[tuple[str, str, str, str]],
) -> str:
    """比较 XML 里声明的端口与注册表里的端口，返回第一条不一致（一致则返回空串）。

    为什么不直接 `declared == expected`：给组件**新增一个可选端口**是完全向后兼容的改动，
    但严格相等会让所有旧 XML 立刻无法导入（实测：给 `visual.overview` 加一个可选 `labels`
    输入端口后，之前保存的 HBM 方案报 `Port declarations differ from registry: overview`）。
    所以这里只放行这一种情况：

    * 旧 XML 里的每个端口仍必须存在、类型与必填一致、相对顺序不变；
    * 注册表里**必填**的端口一个都不能少（少了就是真的不兼容）；
    * 旧 XML 不认识的新端口，只允许是**可选**的。

    改名、改类型、删端口、漏掉必填端口依然被拒绝——这些才是不兼容。
    """
    lookup = {(tag, name): (data_type, required) for tag, name, data_type, required in expected}
    order = {entry: index for index, entry in enumerate(expected)}
    previous = -1
    for entry in declared:
        tag, name, data_type, required = entry
        spec = lookup.get((tag, name))
        if spec is None:
            return f"{tag} {name!r} is not declared by this component"
        if spec != (data_type, required):
            return f"{tag} {name!r} declares {data_type!r}/{required}, registry has {spec[0]!r}/{spec[1]}"
        index = order[(tag, name, data_type, required)]
        if index < previous:
            return f"{tag} {name!r} is out of order"
        previous = index
    declared_set = set(declared)
    for entry in expected:
        if entry[3] == "true" and entry not in declared_set:
            return f"required {entry[0]} {entry[1]!r} is missing from the document"
    return ""


class XMLParser:
    def __init__(self, registry: ComponentRegistry) -> None:
        """需要一个注册表：导入时必须能按类型名找到组件实现，才能做端口一致性校验。"""
        self.registry = registry

    def loads(self, text: str, require_complete: bool = False) -> ComponentGraph:
        """从 XML 文本还原图，并在每一层做交叉校验。

        * 组件版本必须与注册表一致，否则拒绝（避免"配置来自别的版本"的静默错配）；
        * 参数不能重复，值按 JSON 解析；
        * 文件里声明的端口列表必须与注册表**对得上**（名字、类型、必填、顺序），
          防止手改 XML 绕过类型系统；
        * 最后跑一遍图校验；``require_complete=True`` 时缺参数或未连线也会失败。
        """
        root = _parse(text)
        graph = ComponentGraph(self.registry, root.get("name"), root.get("id"))
        graph.metadata = json.loads(root.findtext("metadata", "{}"))
        graph.ui = json.loads(root.findtext("ui", "{}"))
        if not isinstance(graph.metadata, dict) or not isinstance(graph.ui, dict):
            raise ValueError("Metadata and UI must be JSON objects")
        for element in root.findall("./nodes/node"):
            cls = self.registry.get(element.get("type"))
            if element.get("componentVersion") != cls.metadata.version:
                raise ValueError(f"Unsupported component version: {element.get('id')}")
            values = {}
            for parameter in element.findall("./parameters/parameter"):
                name = parameter.get("name")
                if name in values:
                    raise ValueError(f"Duplicate parameter: {name}")
                values[name] = json.loads(parameter.text or "null")
            position = element.find("position")
            node = graph.add_node(
                element.get("type"),
                element.get("id"),
                values,
                {"x": float(position.get("x")), "y": float(position.get("y"))},
            )
            node.ui = json.loads(element.findtext("ui", "{}"))
            if not isinstance(node.ui, dict):
                raise ValueError("Node UI must be a JSON object")
            declared = [
                (p.tag, p.get("name"), p.get("dataType"), p.get("required"))
                for p in element.findall("./ports/*")
            ]
            expected = [
                (tag, p.name, p.data_type, str(p.required).lower())
                for tag, specs in (("inputPort", cls.input_ports), ("outputPort", cls.output_ports))
                for p in specs
            ]
            problem = _port_declaration_problem(declared, expected)
            if problem:
                # 端口对不上说明 XML 被改过或来自不兼容的版本，必须拒绝而不是"尽力解析"。
                raise ValueError(f"Port declarations differ from registry: {node.id} ({problem})")
        for edge in root.findall("./connections/connection"):
            graph.connect(
                edge.get("sourceNode"), edge.get("sourcePort"), edge.get("targetNode"), edge.get("targetPort")
            )
        graph.version = int(root.get("graphVersion"))
        errors = graph.validate_graph(require_complete)
        if errors:
            raise ValueError("; ".join(errors))
        return graph

    def load(self, path: str | Path, require_complete: bool = False) -> ComponentGraph:
        """从文件读取（容忍 UTF-8 BOM：Windows 编辑器的常见产物）。"""
        return self.loads(Path(path).read_text(encoding="utf-8-sig"), require_complete)


def validate_xml(text: str, registry: ComponentRegistry, require_complete: bool = False) -> list[str]:
    """校验一段 XML 是否是合法方案：返回错误列表（空列表表示通过），不抛异常。"""
    try:
        XMLParser(registry).loads(text, require_complete)
        return []
    except (ValueError, TypeError) as exc:
        return [str(exc)]
