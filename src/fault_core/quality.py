"""Data quality pre-checks that a missing-rate column cannot reveal.

Real telemetry is full of channels that are *not missing* but carry no information in
part of the data: held tag values, all-zero registers, quantised measurement points.
Per-group constant columns and flat windows are the two that silently break window
features and frequency analysis, so they are reported first.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from fault_core.data import numeric_columns
from fault_core.features import windows


def quality_report(
    data: pd.DataFrame,
    columns: list[str] | None = None,
    group_column: str | None = None,
    label_column: str | None = None,
    time_column: str | None = None,
    window_size: int = 0,
    step: int = 0,
    max_groups: int = 20,
    flat_threshold: float = 0.0,
) -> dict[str, Any]:
    if not data.index.is_unique:
        raise ValueError("Quality report needs a unique row index")
    # Identifier columns are constant by nature; measuring them only adds noise.
    excluded = {name for name in (group_column, label_column, time_column) if name}
    numeric = [name for name in numeric_columns(data, columns) if name not in excluded]
    if not numeric:
        raise ValueError("Quality report needs at least one measurement column")
    frame = data[numeric]
    missing_rate = frame.isna().mean().to_dict()
    all_nan = sorted(name for name, rate in missing_rate.items() if rate == 1.0)
    constant = sorted(name for name in numeric if frame[name].nunique(dropna=True) <= 1)
    zeros = sorted(
        name for name in numeric if frame[name].notna().any() and bool((frame[name].dropna() == 0).all())
    )
    duplicates = int(data.duplicated().sum())

    per_group: list[dict[str, Any]] = []
    group_constants = 0
    if group_column:
        limit = max(1, min(max_groups, 200))
        for index, (value, group_frame) in enumerate(data.groupby(group_column, sort=False, dropna=False)):
            if index >= limit:
                break
            constant_here = sorted(name for name in numeric if group_frame[name].nunique(dropna=True) <= 1)
            if constant_here:
                group_constants += 1
            per_group.append(
                {"group": str(value), "rows": len(group_frame), "constant_columns": constant_here}
            )

    flat_counts = {name: 0 for name in numeric}
    window_count = 0
    mixed_labels = 0
    for _, chunk, _, _ in windows(data, group_column, window_size, step, time_column):
        window_count += 1
        for name in numeric:
            if float(np.ptp(chunk[name].to_numpy(dtype=float))) <= flat_threshold:
                flat_counts[name] += 1
        if label_column and chunk[label_column].nunique(dropna=True) > 1:
            mixed_labels += 1
    flat_ratio = {
        name: (count / window_count if window_count else 0.0) for name, count in flat_counts.items()
    }

    transitions = 0
    if label_column:
        ordered = data.sort_values(time_column, kind="stable") if time_column else data
        groups = (
            ordered.groupby(group_column, sort=False, dropna=False) if group_column else [("all", ordered)]
        )
        for _, group_frame in groups:
            transitions += int((group_frame[label_column].diff().fillna(0) != 0).sum())

    rows = [
        {"check": "all-NaN columns", "count": len(all_nan), "detail": ", ".join(all_nan[:8])},
        {"check": "constant columns (overall)", "count": len(constant), "detail": ", ".join(constant[:8])},
        {"check": "all-zero columns", "count": len(zeros), "detail": ", ".join(zeros[:8])},
        {
            "check": "groups with constant columns",
            "count": group_constants,
            "detail": f"scanned {len(per_group)} groups",
        },
        {"check": "duplicate rows", "count": duplicates, "detail": ""},
        {
            "check": "columns with flat windows",
            "count": sum(1 for value in flat_ratio.values() if value > 0),
            "detail": f"of {window_count} windows; worst "
            + ", ".join(
                f"{name} {value:.0%}" for name, value in sorted(flat_ratio.items(), key=lambda kv: -kv[1])[:3]
            ),
        },
        {
            "check": "windows with mixed labels",
            "count": mixed_labels,
            "detail": f"of {window_count} windows; label changes {transitions}",
        },
    ]
    findings = _findings(
        all_nan, constant, zeros, group_constants, flat_ratio, mixed_labels, transitions, window_count
    )
    return {
        "kind": "quality",
        "row_count": len(data),
        "column_count": len(numeric),
        "columns": numeric,
        "excluded_columns": sorted(excluded),
        "group_column": group_column,
        "window_count": window_count,
        "all_nan_columns": all_nan,
        "constant_columns": constant,
        "zero_columns": zeros,
        "duplicate_rows": duplicates,
        "missing_rate": missing_rate,
        "flat_window_ratio": flat_ratio,
        "mixed_label_windows": mixed_labels,
        "label_changes": transitions,
        "groups_scanned": len(per_group),
        "per_group": per_group,
        "rows": rows,
        "findings": findings,
    }


def _findings(
    all_nan: list[str],
    constant: list[str],
    zeros: list[str],
    group_constants: int,
    flat_ratio: dict[str, float],
    mixed_labels: int,
    transitions: int,
    window_count: int,
) -> list[str]:
    notes: list[str] = []
    if all_nan:
        notes.append(f"All-NaN columns: {', '.join(all_nan[:6])}; drop or impute them before features.")
    if zeros:
        notes.append(f"All-zero columns: {', '.join(zeros[:6])}; they carry no signal.")
    if constant:
        notes.append(f"Constant columns overall: {', '.join(constant[:6])}.")
    if group_constants:
        notes.append(
            f"{group_constants} groups contain at least one constant column; per-group constants "
            "produce zero-variance windows and NaN spectral features."
        )
    worst = [(name, value) for name, value in sorted(flat_ratio.items(), key=lambda kv: -kv[1]) if value > 0]
    if worst:
        detail = ", ".join(f"{name} {value:.0%}" for name, value in worst[:5])
        notes.append(
            f"Flat (constant) windows detected: {detail}. Held or quantised channels cannot carry "
            "spectral features; keep feature.spectral.flat_policy=nan or drop those channels."
        )
    if mixed_labels and window_count:
        notes.append(
            f"{mixed_labels} of {window_count} windows contain a label change; label_policy=strict "
            "fails on them — use mode/last for onset tasks."
        )
    elif transitions:
        notes.append(
            f"{transitions} label changes occur across the data. No window currently straddles one, "
            "but a different window_size/step can: onset data is safer with label_policy=mode or last."
        )
    return notes
