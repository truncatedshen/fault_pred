"""DAG model containing configuration and edges, never runtime data."""

from __future__ import annotations

import json
import math
import re
from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4

from fault_platform.components.base import BaseComponent
from fault_platform.registry import ComponentRegistry


def valid_id(value: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,99}", value):
        raise ValueError(f"Invalid identifier: {value!r}")


@dataclass
class ComponentNode:
    component: BaseComponent
    position: dict[str, float] = field(default_factory=lambda: {"x": 100, "y": 100})
    ui: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.component.component_id

    def serialize(self) -> dict[str, Any]:
        return {**self.component.serialize(), "position": deepcopy(self.position), "ui": deepcopy(self.ui)}


@dataclass(frozen=True)
class Connection:
    source_node: str
    source_port: str
    target_node: str
    target_port: str


class ComponentGraph:
    def __init__(
        self, registry: ComponentRegistry, name: str = "Untitled pipeline", pipeline_id: str | None = None
    ) -> None:
        self.registry = registry
        self.pipeline_id = pipeline_id or f"pipeline_{uuid4().hex[:12]}"
        valid_id(self.pipeline_id)
        self.name = name
        self.version = 1
        self.metadata: dict[str, Any] = {}
        self.ui: dict[str, Any] = {}
        self.nodes: dict[str, ComponentNode] = {}
        self.edges: list[Connection] = []

    def add_node(
        self,
        component_type: str,
        node_id: str | None = None,
        parameters: dict[str, Any] | None = None,
        position: dict[str, float] | None = None,
    ) -> ComponentNode:
        component = self.registry.create(component_type, node_id, parameters)
        valid_id(component.component_id)
        if component.component_id in self.nodes:
            raise ValueError(f"Duplicate node: {component.component_id}")
        pos = position or {"x": 100.0, "y": 100.0}
        if set(pos) != {"x", "y"} or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in pos.values()
        ):
            raise ValueError("Position must contain finite numeric x and y")
        node = ComponentNode(component, deepcopy(pos))
        self.nodes[node.id] = node
        self.version += 1
        return node

    def get_node(self, node_id: str) -> ComponentNode:
        if node_id not in self.nodes:
            raise ValueError(f"Unknown node: {node_id}")
        return self.nodes[node_id]

    def remove_node(self, node_id: str) -> None:
        self.get_node(node_id)
        del self.nodes[node_id]
        self.edges = [e for e in self.edges if node_id not in (e.source_node, e.target_node)]
        self.version += 1

    def configure(self, node_id: str, parameters: dict[str, Any]) -> None:
        self.get_node(node_id).component.configure(parameters)
        self.version += 1

    def _validate_edge(self, edge: Connection) -> None:
        source = self.get_node(edge.source_node).component
        target = self.get_node(edge.target_node).component
        outputs = {p.name: p for p in source.output_ports}
        inputs = {p.name: p for p in target.input_ports}
        if edge.source_port not in outputs or edge.target_port not in inputs:
            raise ValueError("Unknown source output or target input port")
        if outputs[edge.source_port].data_type != inputs[edge.target_port].data_type:
            raise ValueError(
                f"Incompatible port types: {outputs[edge.source_port].data_type} -> "
                f"{inputs[edge.target_port].data_type}"
            )

    def connect(self, source_node: str, source_port: str, target_node: str, target_port: str) -> Connection:
        edge = Connection(source_node, source_port, target_node, target_port)
        self._validate_edge(edge)
        if any(e.target_node == target_node and e.target_port == target_port for e in self.edges):
            raise ValueError("Target input already connected")
        self.edges.append(edge)
        if self.detect_cycle():
            self.edges.pop()
            raise ValueError("Connection creates a cycle")
        self.version += 1
        return edge

    def disconnect(self, source_node: str, source_port: str, target_node: str, target_port: str) -> None:
        self.edges.remove(Connection(source_node, source_port, target_node, target_port))
        self.version += 1

    def topological_sort(self) -> list[str]:
        degrees = dict.fromkeys(self.nodes, 0)
        children: dict[str, list[str]] = {n: [] for n in self.nodes}
        for e in self.edges:
            if e.source_node not in self.nodes or e.target_node not in self.nodes:
                raise ValueError("Edge references a missing node")
            degrees[e.target_node] += 1
            children[e.source_node].append(e.target_node)
        ready = deque(n for n, count in degrees.items() if not count)
        ordered = []
        while ready:
            node = ready.popleft()
            ordered.append(node)
            for target in children[node]:
                degrees[target] -= 1
                if degrees[target] == 0:
                    ready.append(target)
        if len(ordered) != len(self.nodes):
            raise ValueError("Graph contains a cycle")
        return ordered

    def detect_cycle(self) -> bool:
        try:
            self.topological_sort()
            return False
        except ValueError:
            return True

    def descendants(self, node_id: str) -> set[str]:
        self.get_node(node_id)
        found: set[str] = set()
        pending = [node_id]
        while pending:
            for e in self.edges:
                if e.source_node == pending[0] and e.target_node not in found:
                    found.add(e.target_node)
                    pending.append(e.target_node)
            pending.pop(0)
        return found

    def validate_graph(self, require_complete: bool = True) -> list[str]:
        errors = []
        for node in self.nodes.values():
            try:
                self.registry.get(node.component.component_type)
                node.component.validate(require_complete)
            except ValueError as exc:
                errors.append(f"{node.id}: {exc}")
            if require_complete:
                for port in node.component.input_ports:
                    if port.required and not any(
                        e.target_node == node.id and e.target_port == port.name for e in self.edges
                    ):
                        errors.append(f"{node.id}.{port.name}: required input not connected")
        targets = set()
        for edge in self.edges:
            try:
                self._validate_edge(edge)
                key = (edge.target_node, edge.target_port)
                if key in targets:
                    raise ValueError("Multiple connections to one input")
                targets.add(key)
            except ValueError as exc:
                errors.append(str(exc))
        try:
            self.topological_sort()
        except ValueError as exc:
            errors.append(str(exc))
        if require_complete and not self.nodes:
            errors.append("Pipeline is empty")
        return errors

    validate = validate_graph

    def serialize(self) -> dict[str, Any]:
        return {
            "id": self.pipeline_id,
            "name": self.name,
            "version": self.version,
            "metadata": deepcopy(self.metadata),
            "ui": deepcopy(self.ui),
            "nodes": [n.serialize() for n in self.nodes.values()],
            "edges": [asdict(e) for e in self.edges],
        }

    @classmethod
    def deserialize(cls, value: dict[str, Any], registry: ComponentRegistry) -> ComponentGraph:
        json.dumps(value, allow_nan=False)
        if not isinstance(value.get("name", ""), str):
            raise ValueError("Graph name must be a string")
        for key in ("metadata", "ui"):
            if not isinstance(value.get(key, {}), dict):
                raise ValueError(f"Graph {key} must be an object")
        for key in ("nodes", "edges"):
            if not isinstance(value.get(key, []), list):
                raise ValueError(f"Graph {key} must be a list")
        graph = cls(registry, value.get("name", "Untitled"), value.get("id"))
        graph.metadata = deepcopy(value.get("metadata", {}))
        graph.ui = deepcopy(value.get("ui", {}))
        for spec in value.get("nodes", []):
            node = graph.add_node(spec["type"], spec["id"], spec.get("parameters"), spec.get("position"))
            node.ui = deepcopy(spec.get("ui", {}))
            if not isinstance(node.ui, dict):
                raise ValueError("Node UI must be an object")
        for edge in value.get("edges", []):
            graph.connect(**edge)
        graph.version = int(value.get("version", 1))
        if graph.version < 1:
            raise ValueError("Graph version must be positive")
        return graph

    def clone(self) -> ComponentGraph:
        return self.deserialize(self.serialize(), self.registry)
