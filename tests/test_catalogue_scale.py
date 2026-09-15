"""Catalogue navigation at scale: facets, ranked retrieval, port compatibility, versioning."""

from __future__ import annotations

from fault_platform.components.base import (
    BaseComponent,
    ComponentMetadata,
    ComponentResult,
    DataType,
)
from fault_platform.graph import ComponentGraph
from fault_platform.registry import ComponentRegistry, default_registry
from fault_platform.runtime import ExecutionContext, ExecutionEngine
from fault_platform.version import PLATFORM_VERSION, compatibility_error, satisfies
from fault_platform.workspace import FaultWorkspace


def test_facets_describe_the_whole_catalogue():
    registry = default_registry()
    facets = registry.facets()
    total = registry.count()
    assert total >= 56
    assert sum(facets["categories"].values()) == total
    assert "feature" in facets["subcategories"] and "validation" in facets["subcategories"]
    # Subcategory counts must add up inside each category.
    for category, buckets in facets["subcategories"].items():
        assert sum(buckets.values()) == facets["categories"][category], category
    assert facets["versions"] and facets["compatibility"]
    assert any("统计" in name for name in facets["subcategories"]["feature"])


def test_list_filters_compose_with_paging():
    registry = default_registry()
    page = registry.list(category="feature", input_type="Dataset", limit=3)
    assert page and all(
        any(port.data_type == DataType.DATASET for port in registry.get(item["component_type"]).input_ports)
        for item in page
    )
    every = registry.list(category="feature", include_schema=False, limit=500)
    assert len(every) == registry.count(category="feature")
    assert registry.count(input_type="FeatureDataset") >= 1
    subcategory = registry.list(category="feature", subcategory="频域 Frequency", include_schema=False)
    assert [item["component_type"] for item in subcategory] == ["feature.spectral"]


def test_retrieval_ranks_by_intent_and_respects_ports():
    registry = default_registry()
    top = registry.retrieve("统计特征")[0]
    assert top["component_type"] == "feature.statistical"
    assert top["score"] > 0 and top["match_reasons"]

    english = registry.retrieve("extract frequency content from a waveform")
    assert english[0]["component_type"] == "feature.spectral"

    # Only components that accept a Dataset and emit a FeatureDataset can sit between them.
    chained = registry.retrieve(
        "",
        category="feature",
        source_component_type="data.input",
        target_component_type="feature.merge",
    )
    assert chained and all("source port" in " ".join(item["match_reasons"]) for item in chained)
    assert all(item["component_type"] != "feature.merge" for item in chained)

    assert registry.retrieve("no-such-capability-xyz", limit=5) == []
    assert registry.retrieve("", limit=1000)


def test_compatibility_requirements_are_checked():
    assert satisfies(["fault-platform>=0.1,<1"], "0.1.0")
    assert satisfies([">=0.1.0"], "0.2.0")
    assert not satisfies(["fault-platform>=0.1,<1"], "1.0.0")
    assert not satisfies(["==0.2.0"], "0.1.0")
    assert satisfies(["fault-platform>=0.1.0, <0.2"], "0.1.0")
    assert compatibility_error(["fault-platform>=9.0"], "0.1.0")
    assert compatibility_error([], "0.1.0") is None

    class FutureComponent(BaseComponent):
        metadata = ComponentMetadata(
            "test.future",
            "未来组件",
            "feature",
            "Requires a newer platform",
            compatibility=("fault-platform>=9.0",),
        )

        def execute(self, inputs, context):  # pragma: no cover - never runs
            return ComponentResult({})

    registry = ComponentRegistry()
    registry.register(FutureComponent)
    graph = ComponentGraph(registry, "compat", "compat_pipeline")
    graph.add_node("test.future", "future")
    workspace = FaultWorkspace("compat_pipeline")
    errors = ExecutionEngine().execute(graph, ExecutionContext(workspace, workspace_path())).errors
    message = errors["_validation"]["error_message"]
    assert "future" in message and "fault-platform>=9.0" in message and PLATFORM_VERSION in message


def test_prediction_capability_is_discoverable() -> None:
    """新能力必须能被 Agent 的检索入口找到，否则等于不存在。

    这条守的是"接口对 Agent 可用"：当用户说"预测未来 7 天会不会故障""故障预警"时，
    `retrieve_components` / `list_components` 要把窗口生产者排到前面，而不是只找到
    `validation.arma` 这类时序基线；参数名与 `label_policy` 的取值也要能从 schema 读出来。
    """
    registry = default_registry()
    producers = {"feature.statistical", "feature.fitting", "feature.spectral", "feature.entropy"}
    for intent in ("预测未来是否故障", "未来 7 天 故障", "prediction horizon", "故障预警", "滑动时间窗口"):
        found = {item["component_type"] for item in registry.retrieve(intent=intent, limit=3)}
        assert found & producers, f"intent {intent!r} found {sorted(found)}"
    for query in ("时间窗口", "预测", "horizon", "window_span", "故障预警"):
        found = {item["component_type"] for item in registry.list(query=query, limit=5)}
        assert found & producers, f"query {query!r} found {sorted(found)}"
    tagged = {item["component_type"] for item in registry.list(tags=["prediction"])}
    assert producers <= tagged
    schema = registry.get("feature.statistical").schema()
    names = {p["name"] for p in schema["parameter_schema"]}
    assert {"window_span", "step_span", "prediction_horizon", "prediction_gap"} <= names
    policy = next(p for p in schema["parameter_schema"] if p["name"] == "label_policy")
    assert "horizon" in policy["options"]


def workspace_path():
    from pathlib import Path

    return Path.cwd()
