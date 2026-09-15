"""Recompute a saved pipeline headlessly and print a compact, honest digest.

Beyond accuracy it reports the confusion matrix, per-class recall, and how many test
instances come from a well that also appears in training.  That last number matters for
3W: the platform splits by ``instance``, and a well can carry several instances, so a
window-level group split is weaker than a well-level split.

Usage: python test_3w/report_metrics.py <pipeline.xml> [data-root]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fault_platform.registry import default_registry
from fault_platform.runtime import ExecutionContext, ExecutionEngine
from fault_platform.workspace import FaultWorkspace
from fault_platform.xml_io import XMLParser


def main() -> None:
    xml = Path(sys.argv[1]).resolve()
    data_root = Path(sys.argv[2] if len(sys.argv) > 2 else xml.parent.parent / "data").resolve()
    graph = XMLParser(default_registry()).load(str(xml), require_complete=True)
    workspace = ExecutionEngine().execute(graph, ExecutionContext(FaultWorkspace(graph.pipeline_id), data_root))

    print(f"status={workspace.status.value}  nodes={len(workspace.node_results)}")
    for node, ports in workspace.node_results.items():
        for name in ports:
            value = workspace.get_output(node, name)
            if name == "features" and hasattr(value, "shape"):
                print(f"  {node}.features shape={value.shape}")
            if name == "metrics":
                metrics = value
                matrix = metrics.get("confusion_matrix")
                classes = metrics.get("classes")
                print(
                    f"  {node}: acc={metrics['accuracy']:.4f} prec={metrics['precision']:.4f} "
                    f"rec={metrics['recall']:.4f} f1={metrics['f1']:.4f} auc={metrics['roc_auc']:.4f} "
                    f"train={metrics['train_count']} test={metrics['test_count']} split={metrics['split_method']}"
                )
                if matrix:
                    total = [sum(row) for row in matrix]
                    correct = [row[i] for i, row in enumerate(matrix)]
                    print(f"    confusion={matrix}")
                    print(f"    per-class support={total} recall={[round(c / t, 4) if t else None for c, t in zip(correct, total)]} classes={classes}")
                train_wells = sorted({idx.split("_")[1] for idx in metrics.get("train_indices", [])})
                test_ids = metrics.get("test_indices", [])
                test_wells = sorted({idx.split("_")[1] for idx in test_ids})
                seen = [well for well in test_wells if well in train_wells]
                print(f"    train wells={len(train_wells)} test wells={len(test_wells)} test wells also in train={len(seen)}/{len(test_wells)}")
                print(f"    test instances={sorted({'_'.join(i.split('_')[:3]) for i in test_ids})}")

    comparison = None
    for outputs in workspace.node_results.values():
        if "comparison" in outputs:
            comparison = outputs["comparison"]
            comparison = getattr(comparison, "value", comparison)
    if comparison:
        print("comparison:", json.dumps(comparison, ensure_ascii=False))
    for warning in workspace.warnings:
        print("warning:", warning)


if __name__ == "__main__":
    main()
