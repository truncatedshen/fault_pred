"""Reproducible synthetic equipment data and a complete example DAG."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from fault_platform.graph import ComponentGraph
from fault_platform.registry import ComponentRegistry


def create_dataset(path: Path) -> Path:
    """Generate labelled synthetic runs; never overwrite an existing user file."""
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(42)
    groups = np.repeat(np.arange(90), 64)
    label = groups % 3
    time = np.tile(np.arange(64), 90)
    pd.DataFrame(
        {
            "equipment": groups,
            "time": time,
            "label": label,
            "vibration": 2
            + 0.8 * label
            + rng.normal(0, 0.25 + label * 0.35, len(groups))
            + 0.02 * label * time,
            "temperature": 35 + 5 * label + rng.normal(0, 1.5, len(groups)) + 0.05 * time,
            "pressure": 10 - label + rng.normal(0, 0.3, len(groups)),
        }
    ).to_csv(path, index=False)
    return path


def example_graph(
    registry: ComponentRegistry, path: str = "synthetic_equipment.csv", include_xgboost: bool = False
) -> ComponentGraph:
    graph = ComponentGraph(registry, "设备故障 · 特征与模型验证")
    graph.metadata = {"description": "合成设备数据演示；模型指标不代表真实工业效果", "seed": 42}
    specs = [
        ("source", "data.input", {"path": path}, 60, 190),
        ("filter", "data.filter", {"column": "temperature", "operator": "gt", "value": 0}, 330, 190),
        ("overview", "visual.overview", {"time_column": "time"}, 600, 30),
        (
            "stat",
            "feature.statistical",
            {
                "columns": ["vibration", "temperature", "pressure"],
                "group_column": "equipment",
                "label_column": "label",
                "features": ["mean", "std", "rms", "kurtosis", "crest_factor"],
            },
            600,
            225,
        ),
        (
            "fitting",
            "feature.fitting",
            {
                "columns": ["vibration"],
                "group_column": "equipment",
                "label_column": "label",
                "time_column": "time",
            },
            600,
            440,
        ),
        ("merge", "feature.merge", {}, 885, 245),
        ("forest", "validation.random_forest", {"n_estimators": 100, "split_method": "group"}, 1170, 145),
        ("svm", "validation.svm", {"split_method": "group"}, 1170, 405),
        ("compare", "validation.compare", {}, 1460, 245),
        ("standardize", "data.standardization", {"columns": ["vibration"]}, 330, 630),
        (
            "line",
            "visual.line",
            {
                "time_column": "time",
                "value_columns": ["vibration"],
                "group": "equipment",
                "title": "标准化振动信号 · 探索分支",
            },
            620,
            665,
        ),
    ]
    for node_id, kind, parameters, x, y in specs:
        graph.add_node(kind, node_id, parameters, {"x": x, "y": y})
    edges = [
        ("source", "dataset", "filter", "dataset"),
        ("filter", "dataset", "overview", "dataset"),
        ("filter", "dataset", "stat", "dataset"),
        ("filter", "dataset", "fitting", "dataset"),
        ("stat", "features", "merge", "left"),
        ("fitting", "features", "merge", "right"),
        ("merge", "features", "forest", "features"),
        ("stat", "labels", "forest", "labels"),
        ("merge", "features", "svm", "features"),
        ("stat", "labels", "svm", "labels"),
        ("forest", "metrics", "compare", "first"),
        ("svm", "metrics", "compare", "second"),
        ("filter", "dataset", "standardize", "dataset"),
        ("standardize", "dataset", "line", "dataset"),
    ]
    if include_xgboost:
        graph.add_node("validation.xgboost", "xgboost", {"split_method": "group"}, {"x": 1170, "y": 650})
        edges.extend(
            [
                ("merge", "features", "xgboost", "features"),
                ("stat", "labels", "xgboost", "labels"),
                ("xgboost", "metrics", "compare", "third"),
            ]
        )
    for edge in edges:
        graph.connect(*edge)
    return graph
