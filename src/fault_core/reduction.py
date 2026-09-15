"""Dimensionality reduction that keeps feature row identity intact.

目前只有主成分分析。它**在全部行上拟合**，因此属于探索性操作：调用
:func:`fault_core.preprocessing.mark_fitted` 往 ``attrs["evaluation_warnings"]`` 里写一条
泄漏提示，这条提示会一路传到模型指标里，让最终报告不会把探索结果当成验证结果。
"""

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
    # random_state 固定：PCA 在完全分解时是确定性的，但固定种子能保证随机化 SVD 路径可复现。
    model = PCA(n_components=n_components, whiten=whiten, random_state=0).fit(values)
    result = pd.DataFrame(
        model.transform(values), index=data.index, columns=[f"pc_{i + 1}" for i in range(n_components)]
    )
    # 保留原索引与 attrs，并额外记录源列与解释方差比：
    # 下游 ``feature.merge`` 用 attrs 做 provenance 校验，可视化则可以直接画碎石图。
    result.attrs = {
        **data.attrs,
        "pca_source_columns": cols,
        "pca_explained_variance_ratio": [float(v) for v in model.explained_variance_ratio_],
    }
    return mark_fitted(result, "pca")
