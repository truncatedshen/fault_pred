"""Single component catalog for the runtime, designer and Agent tools."""

from __future__ import annotations

from typing import Any

from fault_platform.components.base import BaseComponent


class ComponentRegistry:
    def __init__(self) -> None:
        self._types: dict[str, type[BaseComponent]] = {}

    def register(self, implementation: type[BaseComponent]) -> None:
        name = implementation.metadata.component_type
        if name in self._types:
            raise ValueError(f"Component already registered: {name}")
        for ports in (
            implementation.input_ports,
            implementation.output_ports,
            implementation.parameter_schema,
        ):
            if len({p.name for p in ports}) != len(ports):
                raise ValueError(f"Duplicate port/parameter names in {name}")
        self._types[name] = implementation

    def unregister(self, component_type: str) -> None:
        del self._types[component_type]

    def get(self, component_type: str) -> type[BaseComponent]:
        if component_type not in self._types:
            raise ValueError(f"Unknown component: {component_type}")
        return self._types[component_type]

    def create(
        self, component_type: str, component_id: str | None = None, parameters: dict[str, Any] | None = None
    ) -> BaseComponent:
        return self.get(component_type)(component_id, parameters)

    def list(
        self,
        category: str | None = None,
        query: str = "",
        tags: list[str] | None = None,
        input_type: str | None = None,
        output_type: str | None = None,
        limit: int = 100,
        include_schema: bool = True,
    ) -> list[dict[str, Any]]:
        found = []
        for cls in self._types.values():
            m = cls.metadata
            if category and m.category != category:
                continue
            if (
                query
                and query.casefold()
                not in " ".join([m.component_type, m.display_name, m.description, *m.tags]).casefold()
            ):
                continue
            if tags and not set(tags).issubset(m.tags):
                continue
            if input_type and not any(p.data_type == input_type for p in cls.input_ports):
                continue
            if output_type and not any(p.data_type == output_type for p in cls.output_ports):
                continue
            schema = cls.schema()
            if not include_schema:
                schema.pop("parameter_schema")
            found.append(schema)
        return found[: max(0, min(limit, 500))]

    def search(self, query: str, **filters: Any) -> list[dict[str, Any]]:
        return self.list(query=query, **filters)

    def filter(self, **filters: Any) -> list[dict[str, Any]]:
        return self.list(**filters)


def default_registry() -> ComponentRegistry:
    from fault_platform.components.builtin import BUILTIN_COMPONENTS

    registry = ComponentRegistry()
    for component in BUILTIN_COMPONENTS:
        registry.register(component)
    return registry
