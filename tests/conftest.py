from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from fault_platform.graph import ComponentGraph
from fault_platform.registry import default_registry
from fault_platform.runtime import ExecutionContext
from fault_platform.workspace import FaultWorkspace


@pytest.fixture
def registry():
    return default_registry()


@pytest.fixture
def dataset():
    rng = np.random.default_rng(42)
    groups = np.repeat(np.arange(40), 16)
    labels = groups % 2
    return pd.DataFrame(
        {
            "equipment": groups,
            "time": np.tile(np.arange(16), 40),
            "label": labels,
            "vibration": rng.normal(2 + labels * 3, 0.4, len(groups)),
            "temperature": rng.normal(30 + labels * 8, 2, len(groups)),
            "category": np.where(labels, "B", "A"),
        }
    )


@pytest.fixture
def context(tmp_path: Path, dataset):
    dataset.to_csv(tmp_path / "sample.csv", index=False)
    return ExecutionContext(FaultWorkspace("test_pipeline"), tmp_path)


@pytest.fixture
def pipeline(registry, context):
    graph = ComponentGraph(registry, "Test", "test_pipeline")
    graph.add_node("data.input", "source", {"path": "sample.csv"})
    graph.add_node("data.filter", "filter", {"column": "vibration", "operator": "gt", "value": 0})
    graph.add_node(
        "feature.statistical",
        "features",
        {
            "columns": ["vibration", "temperature"],
            "group_column": "equipment",
            "label_column": "label",
        },
    )
    graph.add_node("validation.random_forest", "model", {"n_estimators": 10, "split_method": "group"})
    graph.add_node("visual.overview", "overview")
    graph.connect("source", "dataset", "filter", "dataset")
    graph.connect("filter", "dataset", "features", "dataset")
    graph.connect("features", "features", "model", "features")
    graph.connect("features", "labels", "model", "labels")
    graph.connect("source", "dataset", "overview", "dataset")
    return graph
