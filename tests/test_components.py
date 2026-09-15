import numpy as np
import pytest

from fault_core import features
from fault_core.data import expression
from fault_core.models import validate_model
from fault_platform.components.base import DataType, ParameterDefinition
from fault_platform.runtime import validate_value


@pytest.mark.parametrize(
    "component_type,parameters,output",
    [
        ("data.input", {"path": "sample.csv"}, "dataset"),
        ("data.filter", {"column": "label", "operator": "eq", "value": 1}, "dataset"),
        ("data.row_operation", {"operation": "head", "count": 3}, "dataset"),
        ("data.column_operation", {"operation": "select", "columns": ["vibration"]}, "dataset"),
        ("data.normalization", {"columns": ["vibration"]}, "dataset"),
        ("data.standardization", {"columns": ["vibration"]}, "dataset"),
        ("data.transformation", {"columns": ["vibration"], "method": "sqrt"}, "dataset"),
        ("explore.central_tendency", {"columns": ["vibration"]}, "statistics"),
        ("explore.dispersion", {"columns": ["vibration"]}, "statistics"),
        ("explore.correlation", {"columns": ["vibration", "temperature"]}, "matrix"),
        ("visual.scatter", {"x": "vibration", "y": "temperature"}, "plot"),
        ("visual.line", {"time_column": "time", "value_columns": ["vibration"]}, "plot"),
        ("visual.overview", {}, "overview"),
        ("feature.statistical", {"columns": ["vibration"], "group_column": "equipment"}, "features"),
        ("feature.fitting", {"columns": ["vibration"], "group_column": "equipment"}, "features"),
        ("feature.categorical", {"columns": ["category"]}, "features"),
        ("data.labels", {"column": "label"}, "labels"),
        ("feature.select", {"columns": ["vibration"]}, "features"),
    ],
)
def test_each_non_model_component(registry, dataset, context, component_type, parameters, output):
    component = registry.create(component_type, parameters=parameters)
    component.validate()
    result = component.execute({"dataset": dataset}, context)
    assert output in result.outputs
    spec = next(p for p in component.output_ports if p.name == output)
    validate_value(result.outputs[output], spec.data_type)
    assert not dataset.isna().any().any()
    if component_type == "data.filter":
        assert result.outputs[output]["label"].eq(1).all()
    if component_type == "data.standardization":
        assert abs(result.outputs[output]["vibration"].mean()) < 1e-10
    if component_type == "data.row_operation":
        assert len(result.outputs[output]) == 3
    if component_type == "feature.statistical":
        assert len(result.outputs[output]) == 40


@pytest.mark.parametrize("algorithm", ["random_forest", "svm", "xgboost"])
def test_models(registry, dataset, context, algorithm):
    if algorithm == "xgboost":
        pytest.importorskip("xgboost")
    extracted = features.extract_features(
        dataset, ["vibration", "temperature"], group_column="equipment", label_column="label"
    )
    component = registry.create(f"validation.{algorithm}", parameters={"split_method": "group"})
    result = component.execute(extracted, context).outputs
    assert result["metrics"]["accuracy"] > 0.8
    assert set(result["metrics"]["train_indices"]).isdisjoint(result["metrics"]["test_indices"])
    assert len(result["model"].predict(extracted["features"].iloc[:3])) == 3


def test_schema_strictness(registry):
    for value in [True, 1.5, "20", -1]:
        with pytest.raises(ValueError):
            registry.create("validation.random_forest", parameters={"n_estimators": value})
    with pytest.raises(ValueError):
        registry.create("data.filter", parameters={"unknown": 1})
    with pytest.raises(ValueError):
        registry.create("data.filter").validate()
    with pytest.raises(ValueError):
        ParameterDefinition("x", "float").validate(float("nan"))
    with pytest.raises(ValueError):
        validate_value([], DataType.DATASET)


def test_expression_is_arithmetic_only(dataset):
    np.testing.assert_allclose(expression(dataset, "sqrt(vibration) + 2"), np.sqrt(dataset.vibration) + 2)
    for payload in ["__import__('os').getcwd()", "vibration.__class__", "[x for x in vibration]", "2**10000"]:
        with pytest.raises(ValueError):
            expression(dataset, payload)


def test_feature_merge_and_window_label_alignment(registry, dataset, context):
    kwargs = {
        "columns": ["vibration"],
        "group_column": "equipment",
        "label_column": "label",
        "window_size": 8,
    }
    a = features.extract_features(dataset, **kwargs)
    b = features.extract_features(dataset, kind="fitting", **kwargs)
    assert a["features"].index.equals(a["labels"].index)
    assert a["features"].attrs["source_rows"] == b["features"].attrs["source_rows"]
    merged = registry.create("feature.merge").execute(
        {"left": a["features"], "right": b["features"]}, context
    )
    assert len(merged.outputs["features"]) == 80
    with pytest.raises(ValueError):
        features.merge_features(a["features"], b["features"].iloc[::-1])
    with pytest.raises(ValueError):
        features.extract_features(dataset, ["vibration"], label_column="label")


def test_reject_label_misalignment_and_overlapping_random_split(dataset):
    extracted = features.extract_features(
        dataset, ["vibration"], group_column="equipment", label_column="label", window_size=8, step=4
    )
    with pytest.raises(ValueError, match="Overlapping"):
        validate_model(**extracted)
    with pytest.raises(ValueError, match="aligned"):
        validate_model(extracted["features"], extracted["labels"].iloc[::-1])


def test_compare_component(registry, context):
    metrics = {
        "algorithm": "rf",
        "accuracy": 0.8,
        "precision": 0.8,
        "recall": 0.8,
        "f1": 0.8,
        "roc_auc": 0.9,
        "train_count": 8,
        "test_count": 2,
        "test_indices": ["a", "b"],
    }
    c = registry.create("validation.compare")
    assert len(c.execute({"first": metrics, "second": metrics}, context).outputs["comparison"]["rows"]) == 2
    with pytest.raises(ValueError):
        c.execute({"first": metrics, "second": {**metrics, "test_indices": ["c"]}}, context)
