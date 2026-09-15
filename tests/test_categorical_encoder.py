from __future__ import annotations

import pickle

import numpy as np
import pandas as pd
import pytest

from fault_core import features
from fault_core.models import validate_model
from fault_platform.components.base import DataType
from fault_platform.graph import ComponentGraph
from fault_platform.runtime import ExecutionEngine, validate_value
from fault_platform.workspace import PipelineStatus


def test_fitted_onehot_has_fixed_schema_and_unknown_policy():
    training = pd.DataFrame({"mode": ["idle", "run", None, "run"]})
    fitted = features.fit_categorical(training, ["mode"])
    encoder = fitted["encoder"]
    inference = pd.DataFrame({"mode": ["run", "new-mode", None]})

    transformed = encoder.transform(inference)

    assert list(transformed.columns) == list(fitted["features"].columns)
    assert transformed.loc[0, "mode_run"] == 1.0
    assert transformed.loc[1].sum() == 0.0
    assert transformed.loc[2, "mode_<missing>"] == 1.0
    validate_value(encoder, DataType.FEATURE_TRANSFORMER)

    strict = features.fit_categorical_encoder(training, ["mode"], handle_unknown="error")
    with pytest.raises(ValueError, match="Unknown categories"):
        strict.transform(inference)


@pytest.mark.parametrize("method", ["ordinal", "frequency", "category_statistics"])
def test_fitted_non_onehot_methods_transform_unseen_values(method):
    training = pd.DataFrame({"mode": ["idle", "run", "run", "alarm"]})
    encoder = features.fit_categorical_encoder(training, ["mode"], method=method)
    transformed = encoder.transform(pd.DataFrame({"mode": ["run", "new-mode"]}))

    assert list(transformed.columns) == list(encoder.output_columns)
    assert np.isfinite(transformed.to_numpy(dtype=float)).all()
    if method == "ordinal":
        assert transformed.iloc[1, 0] == -1
    else:
        assert transformed.iloc[1].eq(0.0).all()


def test_target_encoder_uses_persisted_training_mapping_for_inference():
    training = pd.DataFrame(
        {"mode": ["idle", "idle", "run", "run", "alarm", "alarm"], "label": [0, 0, 1, 1, 1, 0]}
    )
    fitted = features.fit_categorical(training, ["mode"], method="target", target_column="label")
    encoder = fitted["encoder"]
    transformed = encoder.transform(pd.DataFrame({"mode": ["idle", "unseen"]}))

    assert transformed.loc[0, "mode__target"] == 0.0
    assert transformed.loc[1, "mode__target"] == pytest.approx(training["label"].mean())
    assert np.isfinite(fitted["features"].to_numpy(dtype=float)).all()


def test_ordinal_encoder_distinguishes_raw_numeric_category_from_encoded_feature():
    encoder = features.fit_categorical_encoder(
        pd.DataFrame({"mode_code": [10, 20, 10]}), ["mode_code"], method="ordinal"
    )

    transformed = encoder.transform(pd.DataFrame({"mode_code": [20, 30]}))

    assert list(transformed.columns) == ["mode_code__ordinal"]
    assert transformed["mode_code__ordinal"].tolist() == [1, -1]


def test_component_exposes_encoder_and_transform_component_reuses_it(registry, dataset, context):
    fitted = registry.create(
        "feature.categorical", parameters={"columns": ["category"], "method": "onehot"}
    ).execute({"dataset": dataset}, context)
    encoder = fitted.outputs["encoder"]
    future = dataset.iloc[:3].copy()
    future.loc[future.index[0], "category"] = "future-category"

    transformed = (
        registry.create("feature.categorical_transform")
        .execute({"dataset": future, "encoder": encoder}, context)
        .outputs["features"]
    )

    assert list(transformed.columns) == list(fitted.outputs["features"].columns)
    assert transformed.iloc[0].sum() == 0.0


def test_model_embeds_encoder_and_predicts_raw_categories_after_pickle(dataset):
    categorical = features.fit_categorical(dataset, ["category"])
    numeric = dataset[["vibration", "temperature"]].copy()
    combined = features.merge_features(numeric, categorical["features"])
    trained = validate_model(
        combined,
        dataset["label"].copy(),
        split_method="stratified",
        n_estimators=10,
        random_state=7,
    )["model"]
    restored = pickle.loads(pickle.dumps(trained))
    future = dataset.iloc[:4][["vibration", "temperature", "category"]].copy()
    future.loc[future.index[0], "category"] = "future-category"

    prediction = restored.predict(future)

    assert len(restored.categorical_encoders) == 1
    assert len(prediction) == len(future)
    assert set(prediction).issubset(set(dataset["label"]))


def test_runtime_graph_carries_encoder_into_model(registry, dataset, context):
    graph = ComponentGraph(registry, "categorical inference", context.workspace.pipeline_id)
    graph.add_node("data.input", "source", {"path": "sample.csv"})
    graph.add_node("feature.select", "numeric", {"columns": ["vibration", "temperature"]})
    graph.add_node("feature.categorical", "categorical", {"columns": ["category"]})
    graph.add_node("feature.merge", "merged")
    graph.add_node("data.labels", "labels", {"column": "label"})
    graph.add_node("validation.random_forest", "model", {"n_estimators": 10})
    for target in ("numeric", "categorical", "labels"):
        graph.connect("source", "dataset", target, "dataset")
    graph.connect("numeric", "features", "merged", "left")
    graph.connect("categorical", "features", "merged", "right")
    graph.connect("merged", "features", "model", "features")
    graph.connect("labels", "labels", "model", "labels")

    workspace = ExecutionEngine().execute(graph, context)

    assert workspace.status == PipelineStatus.SUCCESS
    encoder = workspace.get_output("categorical", "encoder")
    trained = workspace.get_output("model", "model")
    future = dataset.iloc[:2][["vibration", "temperature", "category"]].copy()
    future.loc[future.index[0], "category"] = "new-at-runtime"
    assert encoder.describe()["output_columns"] == list(encoder.output_columns)
    assert len(trained.predict(future)) == 2
