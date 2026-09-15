"""Lossless graph XML with structural and registry validation."""

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
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _parse(text: str) -> etree._Element:
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
        errors = graph.validate_graph(require_complete=False)
        if errors:
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
        Path(path).write_text(self.dumps(graph), encoding="utf-8")


class XMLParser:
    def __init__(self, registry: ComponentRegistry) -> None:
        self.registry = registry

    def loads(self, text: str, require_complete: bool = False) -> ComponentGraph:
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
            if declared != expected:
                raise ValueError(f"Port declarations differ from registry: {node.id}")
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
        return self.loads(Path(path).read_text(encoding="utf-8-sig"), require_complete)


def validate_xml(text: str, registry: ComponentRegistry, require_complete: bool = False) -> list[str]:
    try:
        XMLParser(registry).loads(text, require_complete)
        return []
    except (ValueError, TypeError) as exc:
        return [str(exc)]
