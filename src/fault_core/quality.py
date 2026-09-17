"""Data quality pre-checks that a missing-rate column cannot reveal.

Real telemetry is full of channels that are *not missing* but carry no information in
part of the data: held tag values, all-zero registers, quantised measurement points.
Per-group constant columns and flat windows are the two that silently break window
features and frequency analysis, so they are reported first.

生产数据里最伤人的不是 NaN，而是"非空但没信息"的通道：阀门卡住导致的保持值、
未投用的寄存器（恒 0）、被量化死的测点。它们 ``missing_rate = 0``，
``visual.overview`` 看不出任何异常，但会让窗口方差为 0、频谱整段 NaN。
本模块把这类问题提前量化成报告，供 Agent 与人在建模前使用。

两个参数约定：``group_column``/``label_column``/``time_column`` 属于标识列，
不参与恒定列统计；``window_size``/``step``/``time_column`` 必须与后续窗口特征保持一致，
否则报告的"平窗口比例"和真正跑出来的结果对不上。
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
    """生成质量预检报告（返回 dict，由组件包装成 Visualization 产物）。

    检查项：全 NaN 列、整表恒定列、全零列、逐组恒定列、重复行、逐列平窗口比例、
    窗口内标签混合数，以及标签变化次数。

    复杂度是 O(窗口数 × 列数)，所以按窗口统计只在配置了窗口参数时有意义；
    ``max_groups`` 限制逐组明细的条数（上限 200），明细只是"举例"，
    计数 ``groups_scanned`` 会如实说明扫了多少组。
    """
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
    # 重复行按**参与检查的列**判定，不按整张表。
    #
    # 这条曾经是 `data.duplicated()`（整表）：于是加了一列簿记信息（`data.concat` 的
    # `source_column`、上游 `data.input` 的同名列）就会让重复数变小——同一个检查的含义
    # 被一个与数据质量无关的列改变了。现在它只回答"被建模的这些列里，有没有完全相同的行"，
    # 并在 detail 里写明范围。
    duplicates = int(data.duplicated(subset=numeric).sum())

    per_group: list[dict[str, Any]] = []
    group_constants = 0
    if group_column:
        # 明细有上限（默认 20、最多 200）：异常组本身会被计数，明细够用就行。
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
            # 峰峰值（ptp）小于等于阈值即视为平窗口；默认阈值 0 表示"完全不变"。
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
        # 按组分内比较相邻标签：diff 不为 0 即发生一次标签变化（跨组的边界不计）。
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
        {
            "check": "duplicate rows",
            "count": duplicates,
            "detail": f"identical in the {len(numeric)} checked column(s)",
        },
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
    """把统计量翻译成可直接写进报告的结论句。

    每条结论都给出**下一步动作**（删列、改 label_policy、调 flat_policy），
    这样 Agent 不必自己从数字反推该做什么；没有标签变化时不写"标签"那两条，
    避免报告里出现无意义的"0 个窗口混合标签"。
    """
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
