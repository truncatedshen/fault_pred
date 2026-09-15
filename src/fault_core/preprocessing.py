"""Pure preprocessing APIs; fitted-on-all-data transformations are marked."""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.preprocessing import (
    MaxAbsScaler,
    MinMaxScaler,
    PowerTransformer,
    RobustScaler,
    StandardScaler,
    normalize,
)

from fault_core.data import expression, numeric_columns


def mark_fitted(data: pd.DataFrame, operation: str) -> pd.DataFrame:
    data.attrs["evaluation_warnings"] = [
        *data.attrs.get("evaluation_warnings", []),
        f"{operation} fitted on the full dataset; metrics are exploratory and may contain leakage.",
    ]
    return data


def scale(
    data: pd.DataFrame,
    columns: list[str],
    method: str,
    feature_range: list[float] | None = None,
    with_mean: bool = True,
    with_std: bool = True,
) -> pd.DataFrame:
    cols = numeric_columns(data, columns)
    result = data.copy()
    values = data[cols]
    if not np.isfinite(values.to_numpy(dtype=float, copy=False)).all():
        raise ValueError("Scaling requires finite values; clean missing/infinite values first")
    if method in {"l1", "l2"}:
        result[cols] = normalize(values, norm=method)
        return result
    bounds = feature_range or [0, 1]
    if method == "minmax" and (len(bounds) != 2 or bounds[0] >= bounds[1]):
        raise ValueError("feature_range must have two increasing values")
    scalers = {
        "minmax": lambda: MinMaxScaler(feature_range=tuple(bounds)),
        "maxabs": MaxAbsScaler,
        "zscore": lambda: StandardScaler(with_mean=with_mean, with_std=with_std),
        "robust": lambda: RobustScaler(with_centering=with_mean, with_scaling=with_std),
    }
    if method not in scalers:
        raise ValueError(f"Unknown scaling method: {method}")
    result[cols] = scalers[method]().fit_transform(values)
    return mark_fitted(result, method)


def transform(
    data: pd.DataFrame, columns: list[str], method: str, power: float = 2, expression_text: str = ""
) -> pd.DataFrame:
    cols = numeric_columns(data, columns)
    result = data.copy()
    values = data[cols].astype(float)
    if not np.isfinite(values.to_numpy()).all():
        raise ValueError("Transformation requires finite values")
    if method in {"box-cox", "yeo-johnson"}:
        result[cols] = PowerTransformer(method=method).fit_transform(values)
        return mark_fitted(result, method)
    functions = {"log": np.log, "log1p": np.log1p, "sqrt": np.sqrt, "power": lambda x: np.power(x, power)}
    with np.errstate(all="ignore"):
        if method == "expression":
            for col in cols:
                result[col] = expression(pd.DataFrame({"x": data[col]}), expression_text)
        elif method in functions:
            result[cols] = functions[method](values)
        else:
            raise ValueError(f"Unknown transformation: {method}")
    if not np.isfinite(result[cols].to_numpy(dtype=float)).all():
        raise ValueError("Transformation domain error (e.g. log of a non-positive value)")
    return result
