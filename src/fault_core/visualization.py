"""Bounded, renderer-independent visualization specifications."""

from __future__ import annotations

from typing import Any, Iterable

import pandas as pd


def overview(data: pd.DataFrame, time_column: str | None = None) -> dict[str, Any]:
    return {
        "kind": "overview",
        "row_count": len(data),
        "column_count": len(data.columns),
        "column_names": list(data.columns),
        "data_types": data.dtypes.astype(str).to_dict(),
        "missing_rate": data.isna().mean().to_dict(),
        "unique_count": data.nunique().to_dict(),
        "basic_statistics": data.describe(include="all").to_dict() if len(data.columns) else {},
        "time_range": [str(data[time_column].min()), str(data[time_column].max())] if time_column else None,
        "memory_usage": int(data.memory_usage(deep=True).sum()),
    }


def overview_stream(
    chunks: Iterable[pd.DataFrame], time_column: str | None = None, unique_cap: int = 20_000
) -> dict[str, Any]:
    """Single-pass overview: counts, missing rates and ranges without holding the data."""
    rows = 0
    columns: list[str] = []
    data_types: dict[str, str] = {}
    missing = pd.Series(dtype="int64")
    uniques: dict[str, set[Any]] = {}
    overflow: set[str] = set()
    memory = 0
    time_min: Any = None
    time_max: Any = None
    for chunk in chunks:
        if not columns:
            columns = list(chunk.columns)
            data_types = chunk.dtypes.astype(str).to_dict()
            missing = pd.Series(0, index=columns, dtype="int64")
            uniques = {name: set() for name in columns}
        rows += len(chunk)
        memory += int(chunk.memory_usage(deep=True).sum())
        missing = missing.add(chunk.isna().sum(), fill_value=0)
        for name in columns:
            if name in overflow:
                continue
            uniques[name].update(chunk[name].dropna().unique().tolist())
            if len(uniques[name]) > unique_cap:
                overflow.add(name)
                uniques[name] = set()
        if time_column and time_column in chunk.columns and len(chunk):
            low, high = chunk[time_column].min(), chunk[time_column].max()
            time_min = low if time_min is None else min(time_min, low)
            time_max = high if time_max is None else max(time_max, high)
    if not rows:
        raise ValueError("Data source produced no rows")
    return {
        "kind": "overview",
        "row_count": rows,
        "column_count": len(columns),
        "column_names": columns,
        "data_types": data_types,
        "missing_rate": (missing / rows).to_dict(),
        "unique_count": {
            name: (f">={unique_cap}" if name in overflow else len(values)) for name, values in uniques.items()
        },
        "basic_statistics": {},
        "time_range": [str(time_min), str(time_max)] if time_column and time_min is not None else None,
        "memory_usage": memory,
        "streamed": True,
        "note": "基本统计量未在流式模式下计算（需要全表扫描的均值/分位等请先物化数据）",
    }


def plot(
    data: pd.DataFrame,
    kind: str,
    x: str,
    y: list[str],
    title: str = "",
    group: str | None = None,
    x_label: str = "",
    y_label: str = "",
    max_points: int = 500,
) -> dict:
    if not y:
        raise ValueError("Select a y/value column")
    groups = list(data.groupby(group, sort=False, dropna=False)) if group else [("", data)]
    series = []
    for key, frame in groups[:12]:
        stride = max(1, (len(frame) + max_points - 1) // max_points)
        sample = frame.iloc[::stride].head(max_points)
        for col in y[:12]:
            series.append(
                {"name": f"{key} {col}".strip(), "x": sample[x].tolist(), "y": sample[col].tolist()}
            )
    return {
        "kind": kind,
        "title": title,
        "x_label": x_label or x,
        "y_label": y_label,
        "series": series,
        "sampled": any(len(g) > max_points for _, g in groups),
        "truncated_groups": len(groups) > 12,
    }
