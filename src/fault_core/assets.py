"""Derive an asset (well / machine) column from an instance identifier.

资产列是"这个模型能不能用在没见过的设备上"这个问题的前提：窗口分组列（实例）通常
比资产更细，按实例划分仍会把同一台设备的数据留在训练集里。这里把 ``WELL-00001_20170201``
这类实例键还原成资产键，供窗口组件的 ``asset_column`` 与验证器的 ``split_method=asset`` 使用。
"""

from __future__ import annotations

import re

import pandas as pd


def asset_key(
    data: pd.DataFrame,
    column: str,
    target: str = "asset",
    mode: str = "split",
    separator: str = "_",
    index: int = 0,
    pattern: str = "",
) -> pd.DataFrame:
    """Add one column naming the asset each row belongs to.

    ``mode=split`` takes the ``index``-th part of the source column split by ``separator``
    (``WELL-00001_20170201010207`` -> ``WELL-00001``). ``mode=regex`` takes the first
    capture group of ``pattern``. The result feeds ``asset_column`` on window components
    so validation can hold out whole assets.
    """
    if column not in data.columns:
        raise ValueError(f"Missing source column: {column}")
    if mode not in {"split", "regex"}:
        raise ValueError("mode must be split or regex")
    # 目标列已存在时拒绝覆盖：静默改写用户数据会让"没见过的设备"这一划分悄悄失真。
    if target in data.columns:
        raise ValueError(f"Column already exists: {target}; rename it or pick another target")
    values = data[column].astype(str)
    if mode == "split":
        if index < 0:
            raise ValueError("index must be zero or positive")
        parts = values.str.split(separator)
        derived = parts.str[index]
        width = int(parts.str.len().max())
        if index >= width:
            raise ValueError(f"index {index} is beyond the longest key of width {width}")
    else:
        if not pattern:
            raise ValueError("regex mode needs a pattern with one capture group")
        expression = re.compile(pattern)
        # 只取第一个捕获组：多组会让"资产到底指哪一段"变得含糊。
        if expression.groups < 1:
            raise ValueError("pattern needs at least one capture group")
        derived = values.str.extract(expression, expand=True).iloc[:, 0]
    # 任何一行推不出资产（分隔符不匹配、正则没命中）都视为参数错误：
    # 留空会让这些行在 asset 划分里被静默丢弃或归成同一类。
    if derived.isna().any() or (derived.astype(str).str.len() == 0).any():
        raise ValueError(f"Could not derive an asset from every row of {column}")
    result = data.copy()
    result[target] = derived.astype(str)
    # attrs 里存着窗口来源、类别编码器等元数据，必须原样带过去。
    result.attrs = dict(data.attrs)
    return result
