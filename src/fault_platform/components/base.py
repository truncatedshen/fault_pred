"""Typed, serializable component contracts, independent of UI and MCP.

所有组件只依赖这份契约：声明元数据（分类/标签/版本/兼容区间）、输入输出端口、参数 schema，
并实现 :meth:`BaseComponent.execute`。它不知道 HTTP、MCP、前端与运行时的存在，
因此新增组件不需要改动 Graph、Runtime、XML、UI 与 MCP 中的任何代码。

四个关键概念：

* :class:`DataType` —— 端口类型枚举，连接时的类型检查基于它；
* :class:`ParameterDefinition` —— 参数类型/默认值/枚举/范围，既用于校验也用于生成表单；
* :class:`ComponentMetadata` —— 面向检索与展示的元数据（分类、子分类、标签、关键词、兼容性）；
* :class:`ComponentResult` —— 执行返回值：按端口名组织的产物 + 警告列表。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import uuid4

if TYPE_CHECKING:
    from fault_platform.runtime import ExecutionContext


class DataType(StrEnum):
    """端口数据类型。

    默认两端类型必须**完全相同**；只有输入端口可以显式声明 ``accepts`` 兼容类型
    （例如概览类组件既接受原始 ``Dataset``，也接受 ``FeatureDataset``）。
    """

    DATASET = "Dataset"
    TIME_SERIES = "TimeSeries"
    LABEL_VECTOR = "LabelVector"
    FEATURE_DATASET = "FeatureDataset"
    STATISTICS = "StatisticsResult"
    CORRELATION = "CorrelationMatrix"
    MODEL = "Model"
    PREDICTION = "Prediction"
    METRICS = "Metrics"
    IMPORTANCE = "FeatureImportance"
    FEATURE_TRANSFORMER = "FeatureTransformer"
    VISUALIZATION = "Visualization"
    PLOT = "PlotArtifact"
    GENERIC = "GenericArtifact"


@dataclass(frozen=True)
class InputPort:
    """输入端口：名字、类型、是否必填（必填未连接会在校验阶段报错）、说明。

    ``accepts`` 声明额外的兼容类型。"宽容"只放在接收端是有意的：生产者对自己的输出
    永远只有一个明确类型，而检查类组件（概览、绘图、探索）在原始表与特征表上语义相同。
    """

    name: str
    data_type: DataType
    required: bool = True
    description: str = ""
    accepts: tuple[DataType, ...] = ()

    @property
    def accepted_types(self) -> tuple[DataType, ...]:
        """本端口可接入的全部类型，``data_type`` 排在最前面。"""
        return (self.data_type, *(item for item in self.accepts if item != self.data_type))


@dataclass(frozen=True)
class OutputPort:
    """输出端口：名字、类型、说明。一个输出可被多个下游消费（扇形分发）。"""

    name: str
    data_type: DataType
    required: bool = True
    description: str = ""


def _input_port_schema(port: InputPort) -> dict[str, Any]:
    """输入端口的 schema：在基础字段上补 ``accepted_types``。

    输出端口只有一个类型，输入端口可以声明兼容类型（例如概览组件同时接受 ``Dataset``
    与 ``FeatureDataset``）；Agent 与前端据此判断"这个口能不能接"。
    """
    payload = asdict(port)
    payload.pop("accepts", None)
    payload["accepted_types"] = [item.value for item in port.accepted_types]
    return payload


@dataclass(frozen=True)
class ParameterDefinition:
    """参数定义：类型、默认值、必填、展示名、说明、取值范围、枚举选项、是否允许多值。

    一份声明同时服务三件事：运行时校验（:meth:`validate`）、前端表单生成、
    以及 Agent 填写参数时的参考（``get_component_schema`` 返回的就是它）。
    """

    name: str
    type: str = "string"
    default: Any = None
    required: bool = False
    display_name: str = ""
    description: str = ""
    min: float | None = None
    max: float | None = None
    options: tuple[Any, ...] = ()
    allow_multiple: bool = False

    def validate(self, value: Any) -> None:
        """校验一个参数值：拒绝隐式类型转换、未知枚举值、非法范围与非有限数。

        例如 ``"16"`` 不会被当成整数 16（``integer`` 要求真正的 int 且排除 bool）：
        静默的字符串转换会掩盖调用方的错误，把问题推到更晚、更难查的位置。
        """
        if value is None:
            if self.required:
                raise ValueError(f"Parameter '{self.name}' is required")
            return
        if self.type in {"column_list", "feature_list", "list"} or self.allow_multiple:
            if not isinstance(value, list):
                raise ValueError(f"{self.name} must be a list")
            if self.type in {"column_list", "feature_list"} and not all(isinstance(x, str) for x in value):
                raise ValueError(f"{self.name} must contain strings")
            if self.required and not value:
                raise ValueError(f"{self.name} cannot be empty")
            if self.options and any(x not in self.options for x in value):
                raise ValueError(f"{self.name} must be selected from {self.options}")
            return
        if self.type == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"{self.name} must be an integer")
        if self.type == "float" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError(f"{self.name} must be numeric")
        if self.type == "boolean" and not isinstance(value, bool):
            raise ValueError(f"{self.name} must be boolean")
        if self.type in {"string", "column", "expression", "enum"} and not isinstance(value, str):
            raise ValueError(f"{self.name} must be a string")
        if self.type == "object" and not isinstance(value, dict):
            raise ValueError(f"{self.name} must be an object")
        if self.required and isinstance(value, str) and not value.strip():
            raise ValueError(f"{self.name} cannot be blank")
        if self.options and value not in self.options:
            raise ValueError(f"{self.name} must be one of {self.options}")
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise ValueError(f"{self.name} must be finite")
            if self.min is not None and value < self.min:
                raise ValueError(f"{self.name} must be >= {self.min}")
            if self.max is not None and value > self.max:
                raise ValueError(f"{self.name} must be <= {self.max}")


@dataclass(frozen=True)
class ComponentMetadata:
    """组件元数据：类型名、展示名、分类/子分类、标签与搜索关键词、兼容区间。"""

    component_type: str
    display_name: str
    category: str
    description: str
    version: str = "1.0"
    subcategory: str = ""
    tags: tuple[str, ...] = ()
    search_keywords: tuple[str, ...] = ()
    compatibility: tuple[str, ...] = ("fault-platform>=0.1,<1",)


@dataclass
class ComponentResult:
    """一次执行的返回值：``outputs`` 按输出端口名组织，``warnings`` 会回流到运行状态。"""

    outputs: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


class BaseComponent(ABC):
    """可复用计算的定义；实例**只保存配置**，不保存任何运行产物。

    元数据与端口都是类属性（``ClassVar``），同类型的所有实例共享；
    运行时的状态（产物、节点状态、指纹）都放在 workspace 里，
    这也是同一组件类型能被多个节点同时使用的前提。
    """

    metadata: ClassVar[ComponentMetadata]
    input_ports: ClassVar[tuple[InputPort, ...]] = ()
    output_ports: ClassVar[tuple[OutputPort, ...]] = ()
    parameter_schema: ClassVar[tuple[ParameterDefinition, ...]] = ()
    #: Set on components that can consume a StreamedDataset chunk by chunk.
    accepts_streaming: ClassVar[bool] = False

    def __init__(self, component_id: str | None = None, parameters: dict[str, Any] | None = None):
        """先用默认值填满参数，再用传入值覆盖，最后做一次非完整校验。

        构造阶段用 ``require_complete=False``：允许"先建节点、后填参数"，
        对应前端拖拽组件时还没有配置的中间状态。
        """
        self.component_id = component_id or f"node_{uuid4().hex[:10]}"
        self.parameters = {p.name: deepcopy(p.default) for p in self.parameter_schema}
        if parameters:
            self.parameters.update(deepcopy(parameters))
        self.validate(require_complete=False)

    @property
    def component_type(self) -> str:
        return self.metadata.component_type

    @property
    def name(self) -> str:
        return self.metadata.display_name

    @property
    def display_name(self) -> str:
        return self.metadata.display_name

    @property
    def category(self) -> str:
        return self.metadata.category

    @property
    def description(self) -> str:
        return self.metadata.description

    def validate(self, require_complete: bool = True) -> None:
        """校验参数：未知参数名、缺失的必填参数、类型/枚举/范围问题。"""
        definitions = {p.name: p for p in self.parameter_schema}
        # 参数名打错必须报错：否则会静默使用默认值，用户以为设置已生效。
        unknown = self.parameters.keys() - definitions.keys()
        if unknown:
            raise ValueError(f"Unknown parameters: {sorted(unknown)}")
        for name, spec in definitions.items():
            value = self.parameters.get(name)
            if value is None and not require_complete:
                continue
            spec.validate(value)

    def configure(self, parameters: dict[str, Any]) -> None:
        """更新参数；任何校验失败都整体回滚到修改前状态，不留下半套配置。"""
        old = deepcopy(self.parameters)
        self.parameters.update(deepcopy(parameters))
        try:
            self.validate(require_complete=False)
        except Exception:
            self.parameters = old
            raise

    def reset(self) -> None:
        """重置为参数默认值：组件本身没有运行时状态，所以重置只做这一件事。"""
        self.parameters = {p.name: deepcopy(p.default) for p in self.parameter_schema}

    def preflight(self, context: ExecutionContext) -> None:
        """执行前校验外部资源（例如数据文件是否存在、能否读取）。

        默认不做事；数据源类组件应覆写它，把"文件不存在"这类错误挡在执行之前。
        """

    def external_fingerprint(self, context: ExecutionContext) -> str:
        """针对"会变的外部资源"返回指纹（通常是文件大小 + 修改时间）。

        运行时把指纹计入节点指纹：源文件变了，下游会自动重算，
        而不是继续复用基于旧数据算出的结果。
        """
        return ""

    def serialize(self) -> dict[str, Any]:
        """只序列化 id、类型与参数（位置与 ui 由图的序列化负责）。"""
        return {"id": self.component_id, "type": self.component_type, "parameters": deepcopy(self.parameters)}

    @classmethod
    def deserialize(cls, value: dict[str, Any]) -> BaseComponent:
        """从字典恢复实例；类型不匹配立刻报错，避免"用错类解析了数据"。"""
        if value["type"] != cls.metadata.component_type:
            raise ValueError("Component type mismatch")
        return cls(value["id"], value.get("parameters", {}))

    @classmethod
    def schema(cls) -> dict[str, Any]:
        """完整的组件 schema：元数据 + 输入输出端口 + 参数定义 + 实现类全名。

        这是 MCP 的 ``get_component_schema``、前端参数面板与 ``docs/components.md``
        的共同数据源；``implementation_class`` 便于排查"这个类型到底来自哪个类"。
        """
        return {
            **asdict(cls.metadata),
            "input_ports": [_input_port_schema(port) for port in cls.input_ports],
            "output_ports": [asdict(p) for p in cls.output_ports],
            "parameter_schema": [asdict(p) for p in cls.parameter_schema],
            "implementation_class": f"{cls.__module__}.{cls.__name__}",
        }

    @abstractmethod
    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> ComponentResult:
        """按输入端口名取出数据，结合实例参数计算，返回带端口名的 :class:`ComponentResult`。"""
