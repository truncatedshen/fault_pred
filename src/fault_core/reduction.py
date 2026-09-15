"""Dimensionality reduction that keeps feature row identity intact."""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.decomposition import PCA

from fault_core.data import numeric_columns
from fault_core.preprocessing import mark_fitted


def pca(
    data: pd.DataFrame, columns: list[str] | None = None, n_components: int = 2, whiten: bool = False
) -> pd.DataFrame:
    """Project features onto principal components, preserving index and provenance."""
    cols = numeric_columns(data, columns)
    if not isinstance(n_components, int) or isinstance(n_components, bool) or n_components < 1:
        raise ValueError("n_components must be a positive integer")
    if n_components > len(cols):
        raise ValueError(f"n_components cannot exceed the {len(cols)} selected features")
    values = data[cols].to_numpy(dtype=float, copy=False)
    if not np.isfinite(values).all():
        raise ValueError("PCA requires finite numeric values")
    model = PCA(n_components=n_components, whiten=whiten, random_state=0).fit(values)
    result = pd.DataFrame(
        model.transform(values), index=data.index, columns=[f"pc_{i + 1}" for i in range(n_components)]
    )
    result.attrs = {
        **data.attrs,
        "pca_source_columns": cols,
        "pca_explained_variance_ratio": [float(v) for v in model.explained_variance_ratio_],
    }
    return mark_fitted(result, "pca")
