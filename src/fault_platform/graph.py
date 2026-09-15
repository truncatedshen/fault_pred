"""DAG model containing configuration and edges, never runtime data.

``ComponentGraph`` 是"图纸"：节点（组件实例 + 参数 + 画布位置）与边（端口连接）。
它**只存配置，不存任何运行产物**——数据、模型、指标都在
:mod:`fault_platform.workspace` 里，按 ``pipeline_id`` 关联。因此同一张图可以反复执行、
序列化成 XML、或者整体复制（:meth:`ComponentGraph.clone`）而不牵扯任何大数据。

三条硬约束在这里就检查，而不是等运行时才发现：

1. 端口类型必须一致（``Incompatible port types``）；
2. 一个输入端口只能有一个生产者（``Target input already connected``）；
3. 图必须是无环 DAG（``Connection creates a cycle``，通过拓扑排序验证）。

每次成功修改都会让 ``version`` 自增，供乐观并发控制与运行时缓存指纹使用。
"""

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
    """校验标识符：字母或下划线开头，最长 100 字符。

    限制字符集是为了让 id 能安全地出现在 XML 属性、URL 路径与文件名里。
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,99}", value):
        raise ValueError(f"Invalid identifier: {value!r}")


@dataclass
class ComponentNode:
    """图上的一个节点：组件实例 + 画布位置 + 前端附加的 ui 状态。"""

    component: BaseComponent
    position: dict[str, float] = field(default_factory=lambda: {"x": 100, "y": 100})
    ui: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """节点 id 就是组件实例 id（不另设一套编号，避免两个标识对不上）。"""
        return self.component.component_id

    def serialize(self) -> dict[str, Any]:
        """序列化成节点字典（组件参数 + 位置 + ui），供 XML/JSON 与前端使用。"""
        return {**self.component.serialize(), "position": deepcopy(self.position), "ui": deepcopy(self.ui)}


@dataclass(frozen=True)
class Connection:
    """一条边：源节点/端口 → 目标节点/端口。不可变，可安全用于集合与比较。"""

    source_node: str
    source_port: str
    target_node: str
    target_port: str


class ComponentGraph:
    def __init__(
        self, registry: ComponentRegistry, name: str = "Untitled pipeline", pipeline_id: str | None = None
    ) -> None:
        """只需要一个注册表（用来按类型创建组件）+ 名称 + 可选 id。"""
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
        """新增一个已配置的组件节点。

        位置必须是有限的 x/y 数值（画布坐标）；重复 id 直接拒绝；
        参数在 ``registry.create`` 里按 schema 校验一次。
        """
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
        """按 id 取节点；不存在时报错，调用方不必自己判空。"""
        if node_id not in self.nodes:
            raise ValueError(f"Unknown node: {node_id}")
        return self.nodes[node_id]

    def remove_node(self, node_id: str) -> None:
        """删除节点及其全部相关边，不留悬空边。"""
        self.get_node(node_id)
        del self.nodes[node_id]
        self.edges = [e for e in self.edges if node_id not in (e.source_node, e.target_node)]
        self.version += 1

    def configure(self, node_id: str, parameters: dict[str, Any]) -> None:
        """更新节点参数；校验失败时组件自身会回滚到旧参数。"""
        self.get_node(node_id).component.configure(parameters)
        self.version += 1

    def _validate_edge(self, edge: Connection) -> None:
        """校验端口存在且类型一致——这是"图是有类型的"这条核心约束的落点。"""
        source = self.get_node(edge.source_node).component
        target = self.get_node(edge.target_node).component
        outputs = {p.name: p for p in source.output_ports}
        inputs = {p.name: p for p in target.input_ports}
        if edge.source_port not in outputs or edge.target_port not in inputs:
            raise ValueError("Unknown source output or target input port")
        target_port = inputs[edge.target_port]
        # 输入端口可以声明兼容类型（例如概览组件同时接受 Dataset 与 FeatureDataset）；
        # 输出端口永远只有一个类型，"宽容"属于接收方。
        if outputs[edge.source_port].data_type not in target_port.accepted_types:
            allowed = " | ".join(item.value for item in target_port.accepted_types)
            raise ValueError(f"Incompatible port types: {outputs[edge.source_port].data_type} -> {allowed}")

    def connect(self, source_node: str, source_port: str, target_node: str, target_port: str) -> Connection:
        """连接两个端口：先查类型与占用，再查是否成环（成环则回滚这条边）。"""
        edge = Connection(source_node, source_port, target_node, target_port)
        self._validate_edge(edge)
        if any(e.target_node == target_node and e.target_port == target_port for e in self.edges):
            raise ValueError("Target input already connected")
        self.edges.append(edge)
        if self.detect_cycle():
            # 先加后验再回滚：环检测需要看到包含新边的完整图。
            self.edges.pop()
            raise ValueError("Connection creates a cycle")
        self.version += 1
        return edge

    def disconnect(self, source_node: str, source_port: str, target_node: str, target_port: str) -> None:
        """删除指定的边；边不存在时抛 ValueError（沿用 list.remove 的语义）。"""
        self.edges.remove(Connection(source_node, source_port, target_node, target_port))
        self.version += 1

    def topological_sort(self) -> list[str]:
        """Kahn 算法求拓扑序，也是"图无环"的证明；有环时抛错。"""
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
        """能否拓扑排序（不能即有环），用于连接时的即时校验。"""
        try:
            self.topological_sort()
            return False
        except ValueError:
            return True

    def descendants(self, node_id: str) -> set[str]:
        """求某节点的全部下游节点（含间接下游），供"从该节点重算"使用。"""
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
        """收集**全部**结构问题后一次性返回，而不是遇到第一个就抛。

        检查项：组件类型仍在注册表且满足兼容性要求、参数合法、必填输入端口已连接、
        端口类型匹配、同一输入没有重复连接、图无环、图非空。
        ``require_complete=False`` 用于"编辑中的图"：允许暂时缺参数或未连线。
        """
        errors = []
        for node in self.nodes.values():
            try:
                self.registry.get(node.component.component_type)
                # 组件自身的 validate 同时检查 compatibility：平台版本不满足也会报错。
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
        """导出为纯 JSON 结构（节点列表 + 边列表 + 版本 + ui/metadata）。"""
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
        """从 JSON 结构重建图。

        先用 ``json.dumps(allow_nan=False)`` 拒绝 NaN/Infinity，
        再逐层检查字段类型，最后按"节点 → 边"的顺序重建——边必须在节点之后，
        因为连接时要查端口类型。``version`` 由反序列化恢复，而不是重新递增，
        这样"导入的图版本号"与原图一致，乐观并发控制才有意义。
        """
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
        """通过"序列化 → 反序列化"做深拷贝：新图与旧图完全不共享可变对象。"""
        return self.deserialize(self.serialize(), self.registry)
