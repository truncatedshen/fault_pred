"""Typed, serializable component contracts, independent of UI and MCP."""

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
    name: str
    data_type: DataType
    required: bool = True
    description: str = ""


@dataclass(frozen=True)
class OutputPort:
    name: str
    data_type: DataType
    required: bool = True
    description: str = ""


@dataclass(frozen=True)
class ParameterDefinition:
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
        """Reject coercions, unknown enum values and invalid ranges."""
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
    component_type: str
    display_name: str
    category: str
    description: str
    version: str = "1.0"
    subcategory: str = ""
    tags: tuple[str, ...] = ()


@dataclass
class ComponentResult:
    outputs: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


class BaseComponent(ABC):
    """A reusable computation definition; instances store configuration only."""

    metadata: ClassVar[ComponentMetadata]
    input_ports: ClassVar[tuple[InputPort, ...]] = ()
    output_ports: ClassVar[tuple[OutputPort, ...]] = ()
    parameter_schema: ClassVar[tuple[ParameterDefinition, ...]] = ()
    #: Set on components that can consume a StreamedDataset chunk by chunk.
    accepts_streaming: ClassVar[bool] = False

    def __init__(self, component_id: str | None = None, parameters: dict[str, Any] | None = None):
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
        definitions = {p.name: p for p in self.parameter_schema}
        unknown = self.parameters.keys() - definitions.keys()
        if unknown:
            raise ValueError(f"Unknown parameters: {sorted(unknown)}")
        for name, spec in definitions.items():
            value = self.parameters.get(name)
            if value is None and not require_complete:
                continue
            spec.validate(value)

    def configure(self, parameters: dict[str, Any]) -> None:
        old = deepcopy(self.parameters)
        self.parameters.update(deepcopy(parameters))
        try:
            self.validate(require_complete=False)
        except Exception:
            self.parameters = old
            raise

    def reset(self) -> None:
        """Components have no runtime state; reset restores parameter defaults."""
        self.parameters = {p.name: deepcopy(p.default) for p in self.parameter_schema}

    def preflight(self, context: ExecutionContext) -> None:
        """Validate external resources before execution; override for data sources."""

    def external_fingerprint(self, context: ExecutionContext) -> str:
        """Override for components consuming mutable external resources."""
        return ""

    def serialize(self) -> dict[str, Any]:
        return {"id": self.component_id, "type": self.component_type, "parameters": deepcopy(self.parameters)}

    @classmethod
    def deserialize(cls, value: dict[str, Any]) -> BaseComponent:
        if value["type"] != cls.metadata.component_type:
            raise ValueError("Component type mismatch")
        return cls(value["id"], value.get("parameters", {}))

    @classmethod
    def schema(cls) -> dict[str, Any]:
        return {
            **asdict(cls.metadata),
            "input_ports": [asdict(p) for p in cls.input_ports],
            "output_ports": [asdict(p) for p in cls.output_ports],
            "parameter_schema": [asdict(p) for p in cls.parameter_schema],
            "implementation_class": f"{cls.__module__}.{cls.__name__}",
        }

    @abstractmethod
    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> ComponentResult:
        """Compute outputs from named inputs and the instance parameters."""
