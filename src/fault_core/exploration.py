"""Descriptive statistics without graph dependencies."""

from __future__ import annotations

import numpy as np
import pandas as pd

from fault_core.data import numeric_columns


def central_tendency(
    data: pd.DataFrame, columns: list[str], method: str = "mean", weight_column: str | None = None
) -> dict:
    values = data[numeric_columns(data, columns)]
    if method == "mode":
        return {"method": method, "values": values.mode().to_dict(orient="list")}
    if method == "weighted_mean":
        if not weight_column:
            raise ValueError("weighted_mean requires weight_column")
        weights = data[weight_column].to_numpy(dtype=float)
        if not np.isfinite(weights).all() or (weights < 0).any() or weights.sum() <= 0:
            raise ValueError("Weights must be finite, nonnegative and have a positive sum")
        result = pd.Series(np.average(values, weights=weights, axis=0), index=values.columns)
    else:
        result = getattr(values, method)()
    return {"method": method, "values": result.to_dict()}


def dispersion(data: pd.DataFrame, columns: list[str], method: str = "std") -> dict:
    x = data[numeric_columns(data, columns)]
    functions = {
        "variance": lambda: x.var(ddof=1),
        "std": lambda: x.std(ddof=1),
        "range": lambda: x.max() - x.min(),
        "iqr": lambda: x.quantile(0.75) - x.quantile(0.25),
        "mad": lambda: (x - x.median()).abs().median(),
        "cv": lambda: x.std(ddof=1) / x.mean().replace(0, np.nan),
    }
    return {"method": method, "values": functions[method]().to_dict()}


def correlation(data: pd.DataFrame, columns: list[str], method: str = "pearson") -> pd.DataFrame:
    return data[numeric_columns(data, columns)].corr(method=method)
