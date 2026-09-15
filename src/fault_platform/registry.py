"""Single component catalog for the runtime, designer and Agent tools.

注册表是全平台**唯一**的组件目录：运行时按它创建组件，前端按它渲染组件库，
Agent 按它发现组件（``list``/``search``/``retrieve``/``facets``）并取 schema。

三种查询方式的分工：

* :meth:`ComponentRegistry._matching` —— 结构化过滤（分类、子分类、标签、端口类型、版本、兼容区间），
  是 ``list``/``count``/``retrieve`` 的公共基础；
* :meth:`ComponentRegistry.retrieve` —— 面向自然语言的排序检索，支持中英混排；
* :meth:`ComponentRegistry.facets` —— 只回答"有哪些分类/标签/版本"，不返回任何 schema，
  让 Agent 在组件数量增长时也能先看目录再决定拉哪几个 schema。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from fault_platform.components.base import BaseComponent


class ComponentRegistry:
    def __init__(self) -> None:
        self._types: dict[str, type[BaseComponent]] = {}

    def register(self, implementation: type[BaseComponent]) -> None:
        """注册一个组件类，并校验元数据与端口/参数定义的自洽性。

        校验项：类型名唯一且非空、版本号形如 ``1.0``/``1.0.2``、兼容区间非空、
        端口与参数名在本组件内不重复。这些错误都属于"组件写错了"，
        因此在**注册时**（进程启动阶段）就暴露，而不是等某个用户用到它。
        """
        metadata = implementation.metadata
        name = metadata.component_type
        if name in self._types:
            raise ValueError(f"Component already registered: {name}")
        if not all((name.strip(), metadata.display_name.strip(), metadata.category.strip())):
            raise ValueError("Component type, display name and category cannot be blank")
        if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.-]+)?", metadata.version):
            raise ValueError(f"Invalid component version: {metadata.version}")
        if not metadata.compatibility or any(not value.strip() for value in metadata.compatibility):
            raise ValueError(f"Component compatibility cannot be empty: {name}")
        for ports in (
            implementation.input_ports,
            implementation.output_ports,
            implementation.parameter_schema,
        ):
            if len({p.name for p in ports}) != len(ports):
                raise ValueError(f"Duplicate port/parameter names in {name}")
        self._types[name] = implementation

    def unregister(self, component_type: str) -> None:
        """摘除一个组件类型（测试与动态扩展使用）。"""
        del self._types[component_type]

    def get(self, component_type: str) -> type[BaseComponent]:
        """按类型名取实现类；未注册时抛 ValueError，错误信息里带具体类型名。"""
        if component_type not in self._types:
            raise ValueError(f"Unknown component: {component_type}")
        return self._types[component_type]

    def create(
        self, component_type: str, component_id: str | None = None, parameters: dict[str, Any] | None = None
    ) -> BaseComponent:
        """创建一个组件实例（内部会按 schema 校验参数）。"""
        return self.get(component_type)(component_id, parameters)

    def __len__(self) -> int:
        """已注册的组件数量（服务信息与验收脚本会展示它）。"""
        return len(self._types)

    @staticmethod
    def _summary(implementation: type[BaseComponent], include_schema: bool) -> dict[str, Any]:
        """生成组件的对外摘要；``include_schema=False`` 时去掉参数表与实现类，节省 token。"""
        schema = implementation.schema()
        if not include_schema:
            schema.pop("parameter_schema")
            schema.pop("implementation_class")
        return schema

    @staticmethod
    def _search_text(implementation: type[BaseComponent]) -> str:
        """把所有可搜索字段拼成一段小写文本，供关键词过滤使用。

        包含类型名、展示名、分类、子分类、描述、标签、搜索关键词，以及端口名与**端口类型**
        （所以 ``list_components(input_type="Dataset")`` 与文本搜索 ``"Dataset"`` 都能命中）。
        """
        metadata = implementation.metadata
        values = (
            metadata.component_type,
            metadata.display_name,
            metadata.category,
            metadata.subcategory,
            metadata.description,
            *metadata.tags,
            *metadata.search_keywords,
            *(port.name for port in implementation.input_ports),
            *(port.name for port in implementation.output_ports),
            *(port.data_type.value for port in implementation.input_ports),
            *(item.value for port in implementation.input_ports for item in port.accepts),
            *(port.data_type.value for port in implementation.output_ports),
        )
        return " ".join(values).casefold()

    def _matching(
        self,
        category: str | None = None,
        query: str = "",
        tags: list[str] | None = None,
        input_type: str | None = None,
        output_type: str | None = None,
        subcategory: str | None = None,
        version: str | None = None,
        compatibility: str | None = None,
    ) -> list[type[BaseComponent]]:
        """结构化过滤：所有条件之间是"与"关系，返回满足条件的实现类列表。

        * ``query`` 按空白分词，要求每个词都出现在搜索文本里（AND 语义）；
        * ``tags`` 要求全部命中（子集语义）；
        * ``input_type``/``output_type`` 按端口类型名精确匹配；
        * ``version`` 精确匹配，``compatibility`` 按子串匹配（例如 ``">=0.1"``）。
        """
        terms = [part for part in re.split(r"\s+", query.casefold().strip()) if part]
        required_tags = {tag.casefold() for tag in tags or []}
        found = []
        for implementation in self._types.values():
            metadata = implementation.metadata
            if category and metadata.category.casefold() != category.casefold():
                continue
            if subcategory and metadata.subcategory.casefold() != subcategory.casefold():
                continue
            if version and metadata.version != version:
                continue
            if compatibility and not any(
                compatibility.casefold() in value.casefold() for value in metadata.compatibility
            ):
                continue
            if terms and not all(term in self._search_text(implementation) for term in terms):
                continue
            if required_tags and not required_tags.issubset({tag.casefold() for tag in metadata.tags}):
                continue
            if input_type and not any(
                item.value == input_type
                for port in implementation.input_ports
                for item in port.accepted_types
            ):
                continue
            if output_type and not any(
                port.data_type.value == output_type for port in implementation.output_ports
            ):
                continue
            found.append(implementation)
        return found

    def list(
        self,
        category: str | None = None,
        query: str = "",
        tags: list[str] | None = None,
        input_type: str | None = None,
        output_type: str | None = None,
        limit: int = 100,
        include_schema: bool = True,
        offset: int = 0,
        subcategory: str | None = None,
        version: str | None = None,
        compatibility: str | None = None,
    ) -> list[dict[str, Any]]:
        """分页列出组件摘要。``limit`` 上限 500，``offset`` 用于翻页。"""
        found = self._matching(
            category,
            query,
            tags,
            input_type,
            output_type,
            subcategory,
            version,
            compatibility,
        )
        start = max(0, offset)
        stop = start + max(0, min(limit, 500))
        return [self._summary(implementation, include_schema) for implementation in found[start:stop]]

    def count(self, **filters: Any) -> int:
        """统计满足条件的组件数量（与 list 共用同一套过滤条件）。"""
        return len(self._matching(**filters))

    def facets(self) -> dict[str, Any]:
        """目录导航信息：分类、每个分类下的子分类、标签、版本、兼容区间的计数分布。

        只返回计数，不返回组件本身，因此调用成本与组件数量几乎无关。
        """
        metadata = [implementation.metadata for implementation in self._types.values()]
        return {
            "categories": dict(sorted(Counter(item.category for item in metadata).items())),
            "subcategories": {
                category: dict(
                    sorted(
                        Counter(
                            item.subcategory or "general" for item in metadata if item.category == category
                        ).items()
                    )
                )
                for category in sorted({item.category for item in metadata})
            },
            "tags": dict(sorted(Counter(tag for item in metadata for tag in item.tags).items())),
            "versions": dict(sorted(Counter(item.version for item in metadata).items())),
            "compatibility": dict(
                sorted(Counter(value for item in metadata for value in item.compatibility).items())
            ),
        }

    @staticmethod
    def _cjk_bigrams(value: str) -> set[str]:
        """把中文串切成相邻两字的二元组，用于中文意图匹配。

        中文没有空格分词，二元组是最省事且效果稳定的近似：
        "留一井评估" 与 "留一井" 会有 2 个二元组重叠，足以命中。
        """
        characters = [character for character in value if "\u3400" <= character <= "\u9fff"]
        return {"".join(characters[index : index + 2]) for index in range(len(characters) - 1)}

    def retrieve(
        self,
        intent: str,
        category: str | None = None,
        tags: list[str] | None = None,
        input_type: str | None = None,
        output_type: str | None = None,
        source_component_type: str | None = None,
        target_component_type: str | None = None,
        limit: int = 10,
        include_schema: bool = False,
    ) -> list[dict[str, Any]]:
        """按"意图"给组件排序，可用端口类型约束候选集。

        打分由三部分组成：

        * **端口兼容**（+12/侧）：给出 ``source_component_type``/``target_component_type`` 时，
          只保留能把上游输出接进来、并把输出接到下游输入的类型——这是"在已有图中间插一个组件"
          最实用的能力，直接屏蔽掉接不上的候选；
        * **字段加权匹配**：展示名 18 分、类型 15 分、关键词 14 分、标签 12 分、
          子分类 7 分、分类 5 分、描述 4 分；整串包含得满分，分词命中按比例给分；
        * **中文二元组重叠**：与字段的二元组重叠率 ≥ 0.34 时按重叠率给分。

        返回按分数降序（同分按类型名字典序）的摘要列表，并带 ``score`` 与 ``match_reasons``，
        让调用方能判断"为什么它排第一"。``intent`` 为空时只按端口兼容排序，不按文本过滤。
        """
        candidates = self._matching(
            category=category,
            tags=tags,
            input_type=input_type,
            output_type=output_type,
        )
        source_types = None
        if source_component_type:
            source_types = {port.data_type for port in self.get(source_component_type).output_ports}
        target_types = None
        if target_component_type:
            target_types = {
                item for port in self.get(target_component_type).input_ports for item in port.accepted_types
            }

        normalized_intent = intent.casefold().strip()
        intent_tokens = {token for token in re.findall(r"[a-z0-9_.+-]{2,}", normalized_intent) if token}
        intent_bigrams = self._cjk_bigrams(normalized_intent)
        ranked = []
        for implementation in candidates:
            input_types = {item for port in implementation.input_ports for item in port.accepted_types}
            output_types = {port.data_type for port in implementation.output_ports}
            reasons = []
            compatibility_score = 0.0
            if source_types is not None:
                shared = source_types & input_types
                if not shared:
                    # 接不上上游：直接淘汰，而不是给个低分排在后面。
                    continue
                compatibility_score += 12
                reasons.append("source port: " + ", ".join(sorted(value.value for value in shared)))
            if target_types is not None:
                shared = output_types & target_types
                if not shared:
                    continue
                compatibility_score += 12
                reasons.append("target port: " + ", ".join(sorted(value.value for value in shared)))

            metadata = implementation.metadata
            weighted_fields = (
                ("type", metadata.component_type, 15.0),
                ("name", metadata.display_name, 18.0),
                ("subcategory", metadata.subcategory, 7.0),
                ("category", metadata.category, 5.0),
                *(("tag", value, 12.0) for value in metadata.tags),
                *(("keyword", value, 14.0) for value in metadata.search_keywords),
                ("description", metadata.description, 4.0),
            )
            text_score = 0.0
            for label, raw_value, weight in weighted_fields:
                value = raw_value.casefold().strip()
                if not value:
                    continue
                matched = False
                if normalized_intent and (value in normalized_intent or normalized_intent in value):
                    text_score += weight
                    matched = True
                token_matches = sum(token in value for token in intent_tokens)
                if token_matches:
                    text_score += min(weight, token_matches * weight / 3)
                    matched = True
                field_bigrams = self._cjk_bigrams(value)
                if intent_bigrams and field_bigrams:
                    overlap = len(intent_bigrams & field_bigrams) / len(field_bigrams)
                    if overlap >= 0.34:
                        text_score += weight * overlap
                        matched = True
                if matched and len(reasons) < 5:
                    reasons.append(f"{label}: {raw_value}")
            if normalized_intent and text_score <= 0:
                # 有意图但完全没命中任何字段：不返回，避免用无关组件淹没结果。
                continue
            item = self._summary(implementation, include_schema)
            item["score"] = round(text_score + compatibility_score, 3)
            item["match_reasons"] = reasons
            ranked.append(item)
        ranked.sort(key=lambda item: (-item["score"], item["component_type"]))
        return ranked[: max(0, min(limit, 50))]

    def search(self, query: str, **filters: Any) -> list[dict[str, Any]]:
        """关键词检索的便捷入口（等价于 ``list(query=...)``）。"""
        return self.list(query=query, **filters)

    def filter(self, **filters: Any) -> list[dict[str, Any]]:
        """纯结构化过滤的便捷入口（等价于 ``list(**filters)``）。"""
        return self.list(**filters)


def default_registry() -> ComponentRegistry:
    """构造带全部内置组件的新注册表（每次调用都是独立实例，测试之间互不影响）。"""
    from fault_platform.components.builtin import BUILTIN_COMPONENTS

    registry = ComponentRegistry()
    for component in BUILTIN_COMPONENTS:
        registry.register(component)
    return registry
