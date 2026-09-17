"""Bounded, renderer-independent visualization specifications.

这一层把 DataFrame 变成**与渲染器无关的 JSON 结构**（``kind`` + ``series``），
前端只负责画，不需要知道 pandas/sklearn。所有函数都遵守同一条纪律：
输出必须**有界**——采样策略 ``stride = ceil(len / max_points)`` 先抽稀再取前 N 点，
系列数上限 12、关系图边数上限 100，并在返回值里如实标注 ``sampled`` / ``truncated_*``。
这样即使输入是百万行、上百列，工具的响应体也不会撑爆 Agent 或浏览器。
"""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np
import pandas as pd

from fault_core.data import numeric_columns


def _distribution_from_counts(
    counts: dict[Any, int], *, source: str, rows: int, missing: int
) -> dict[str, Any]:
    """由"类别 → 计数"拼出标签构成（计数、占比、正类、结论句）。

    批处理与流式两条路都收敛到这里：流式只能攒计数（攒不下整列标签），
    但两条路必须给出**同一个结构**，否则同一份数据换个读法就得到两种报告。
    """
    order = sorted(counts, key=lambda value: str(value))
    counted = {str(label): int(counts[label]) for label in order}
    total = sum(counted.values())
    rates = {name: (count / total if total else 0.0) for name, count in counted.items()}
    binary = len(order) == 2
    positive_class = str(order[-1]) if binary else None
    positive_count = counted.get(positive_class, 0) if binary else None
    positive_rate = rates.get(positive_class, 0.0) if binary else None

    findings: list[str] = []
    if not total:
        findings.append(f"Label source {source!r} has no non-null values; class balance is unknown.")
    elif missing:
        findings.append(
            f"{missing} of {rows} rows have no label in {source!r}; the ratios below cover the "
            f"{total} labelled rows only."
        )
    if binary and positive_rate is not None:
        if positive_rate == 0.0:
            findings.append(
                f"Positive class {positive_class} never occurs in {source!r}: every model on this label "
                "is untrainable and accuracy is meaningless."
            )
        elif positive_rate < 0.1:
            findings.append(
                f"Label is imbalanced: positive class {positive_class} is {positive_count}/{total} "
                f"({positive_rate:.2%}). A model that always predicts normal still scores "
                f"{1 - positive_rate:.2%} accuracy — report balanced_accuracy, per_class_recall and "
                "miss_rate next to it."
            )
        elif positive_rate > 0.9:
            findings.append(
                f"Label is imbalanced: positive class {positive_class} is {positive_count}/{total} "
                f"({positive_rate:.2%}); a model that always predicts a fault scores "
                f"{positive_rate:.2%} accuracy."
            )

    return {
        "source": source,
        "total": total,
        "rows": rows,
        "missing": missing,
        "classes": [str(label) for label in order],
        "counts": counted,
        "rates": rates,
        "balanced": len(order) > 1 and max(rates.values(), default=0.0) != min(rates.values(), default=0.0),
        "binary": binary,
        "positive_class": positive_class,
        "positive_count": positive_count,
        "positive_rate": positive_rate,
        "findings": findings,
    }


def label_distribution(labels: pd.Series, *, source: str) -> dict[str, Any]:
    """标签构成：每个类别的计数与**占比**，并点名哪个是正类。

    为什么单独做这一块：故障预测里"正类占几成"是决定其它指标怎么读的前提。
    正类只占 4% 时，一个永远判正常的模型 accuracy 就有 96%，而这恰恰是最坏的结果。
    所以这里既给比例，也在比例极端时给一条可直接写进报告的结论。

    正类沿用平台其它地方的约定（与 ``models._positive_index`` 一致）：二分类取排序后的
    **最后一个**类别；类别数不是 2 时不给正类（多分类里"正类"没有唯一含义）。
    """
    values = pd.Series(labels, copy=False)
    present = values.dropna()
    counts = {label: int(count) for label, count in present.value_counts().items()}
    return _distribution_from_counts(
        counts, source=source, rows=int(values.size), missing=int(values.size) - int(present.size)
    )


def _resolve_labels(
    data: pd.DataFrame, label_column: str | None, labels: Any
) -> tuple[pd.Series | None, str | None]:
    """决定用哪份标签算类别构成：显式接进来的标签向量优先，其次才是表里的标签列。

    写错列的代价远大于报错——所以 ``label_column`` 指定的列不存在时直接抛错，
    而不是悄悄跳过这段统计（那会让人以为"标签是均衡的"）。
    """
    if labels is not None:
        return pd.Series(labels, copy=False), label_column or "<labels input>"
    if not label_column:
        return None, None
    if label_column not in data.columns:
        raise ValueError(f"label_column {label_column!r} is not in the data: {list(data.columns)[:12]}")
    return data[label_column], label_column


def overview(
    data: pd.DataFrame,
    time_column: str | None = None,
    label_column: str | None = None,
    labels: Any = None,
) -> dict[str, Any]:
    """整表概览：行数/列数/类型/缺失率/唯一值数/描述统计/时间范围/内存占用/标签构成。

    ``label_column``（表里的标签列）或 ``labels``（接进来的标签向量，特征分支用）给了任意一个，
    就会附上正负样本的计数与占比——故障预测里这是读其它指标的前提。

    注意它**看不出**"非空但恒定"的通道（缺失率为 0、唯一值也没少），
    这类问题要用 ``data.quality`` 才能发现。
    """
    series, source = _resolve_labels(data, label_column, labels)
    distribution = label_distribution(series, source=source or "") if series is not None else None
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
        "label_column": source,
        "label_distribution": distribution,
        "findings": list(distribution["findings"]) if distribution else [],
    }


def overview_stream(
    chunks: Iterable[pd.DataFrame],
    time_column: str | None = None,
    unique_cap: int = 20_000,
    label_column: str | None = None,
    labels: Any = None,
) -> dict[str, Any]:
    """流式概览：单遍扫描分块数据，不把整表读进内存。

    逐块累加行数、缺失计数、内存占用与时间范围；唯一值用集合累计，
    单列超过 ``unique_cap`` 就停止收集并把该列标成 ``>= 上限``（继续收集会让内存重新失控）。
    描述性统计量（均值/分位）需要全表，流式模式下不计算，返回 ``note`` 说明原因。

    标签构成同样在单遍里累加：只留每个类别的计数，不需要把标签列攒下来。
    """
    rows = 0
    columns: list[str] = []
    data_types: dict[str, str] = {}
    missing = pd.Series(dtype="int64")
    uniques: dict[str, set[Any]] = {}
    overflow: set[str] = set()
    memory = 0
    time_min: Any = None
    time_max: Any = None
    label_series = pd.Series(labels) if labels is not None else None
    label_counts: dict[Any, int] = {}
    label_rows = 0
    know_labels = label_series is not None
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
        # 标签列存在时按块累计计数；块之间的顺序不影响结果。
        if label_column and label_column in chunk.columns:
            know_labels = True
            values = chunk[label_column].dropna()
            label_rows += int(values.size)
            for label, count in values.value_counts().items():
                label_counts[label] = label_counts.get(label, 0) + int(count)
    if not rows:
        raise ValueError("Data source produced no rows")
    if label_series is not None:
        label_column = label_column or "<labels input>"
    distribution = None
    if know_labels:
        # 只拿计数拼报告：流式路径不能把标签列攒成整列，否则"流式"就白叫了。
        counted = (
            {label: int(count) for label, count in label_series.dropna().value_counts().items()}
            if label_series is not None
            else label_counts
        )
        distribution = _distribution_from_counts(
            counted,
            source=label_column or "<labels input>",
            rows=rows,
            missing=rows - (int(label_series.dropna().size) if label_series is not None else label_rows),
        )
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
        "label_column": label_column if know_labels else None,
        "label_distribution": distribution,
        "findings": list(distribution["findings"]) if distribution else [],
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
    """通用折线/散点系列构造：按 ``group`` 分组、逐组抽稀到 ``max_points``。

    关键词是**先抽稀再取头部**：``iloc[::stride]`` 以固定步长跨越整段数据，
    因此曲线保留整体形状，而不是只看到开头一小段。分组与 y 列各截断到 12 个。
    """
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


def subplot(
    data: pd.DataFrame,
    x: str,
    value_columns: list[str],
    kind: str = "line",
    title: str = "Subplots",
    max_points: int = 500,
) -> dict[str, Any]:
    """把若干列拆成面板（每个面板一条序列），同时保留一张合并的 series 列表。

    前端可以用 ``panels`` 画多子图，也可以用 ``series`` 画叠加图，
    两种视图共用同一份抽样后的数据，避免重复传输。
    """
    if x not in data.columns:
        raise ValueError(f"Missing x column: {x}")
    missing = set(value_columns) - set(data.columns)
    if not value_columns or missing:
        raise ValueError(f"Missing subplot columns: {sorted(missing)}")
    numeric_columns(data, value_columns)
    panels = []
    series = []
    stride = max(1, (len(data) + max_points - 1) // max_points)
    sample = data.iloc[::stride].head(max_points)
    for column in value_columns[:12]:
        item = {"name": column, "x": sample[x].tolist(), "y": sample[column].tolist()}
        panels.append({"title": column, "kind": kind, "series": [item]})
        series.append(item)
    return {
        "kind": "subplot",
        "plot_kind": kind,
        "title": title,
        "panels": panels,
        "series": series,
        "sampled": len(data) > max_points,
        "truncated_panels": len(value_columns) > 12,
    }


def histogram(
    data: pd.DataFrame,
    columns: list[str],
    bins: int = 20,
    density: bool = False,
    title: str = "Histogram",
) -> dict[str, Any]:
    """直方图：返回每个通道的箱中心、计数（或密度）与箱边界。

    箱边界一并返回是为了让前端标注坐标轴；密度模式下 y 轴语义不同，标签会跟着变。
    """
    cols = numeric_columns(data, columns)
    series = []
    for column in cols[:12]:
        values = data[column].dropna().to_numpy(dtype=float)
        counts, edges = np.histogram(values, bins=bins, density=density)
        centers = ((edges[:-1] + edges[1:]) / 2).tolist()
        series.append(
            {
                "name": column,
                "x": centers,
                "y": counts.tolist(),
                "bin_edges": edges.tolist(),
            }
        )
    return {
        "kind": "histogram",
        "title": title,
        "x_label": "value",
        "y_label": "density" if density else "count",
        "series": series,
        "truncated_series": len(cols) > 12,
    }


def compare(
    first: pd.DataFrame,
    second: pd.DataFrame,
    columns: list[str],
    x_column: str | None = None,
    first_name: str = "first",
    second_name: str = "second",
    title: str = "Dataset comparison",
    max_points: int = 500,
) -> dict[str, Any]:
    """对比两份数据（例如"过滤前 / 过滤后"或"修复前 / 修复后"）。

    两侧都必须包含被对比的列，x 轴可取两侧共有的时间/序号列，否则退回行号；
    每侧抽稀到 ``max_points``，列数截断到 6。
    """
    missing = set(columns) - set(first.columns) | (set(columns) - set(second.columns))
    if not columns or missing:
        raise ValueError(f"Comparison columns are missing: {sorted(missing)}")
    if x_column and (x_column not in first.columns or x_column not in second.columns):
        raise ValueError(f"Comparison x column is missing: {x_column}")
    numeric_columns(first, columns)
    numeric_columns(second, columns)
    series = []
    for frame, label in ((first, first_name), (second, second_name)):
        stride = max(1, (len(frame) + max_points - 1) // max_points)
        sample = frame.iloc[::stride].head(max_points)
        x = sample[x_column].tolist() if x_column else list(range(len(sample)))
        for column in columns[:6]:
            series.append({"name": f"{label} · {column}", "x": x, "y": sample[column].tolist()})
    return {
        "kind": "compare",
        "title": title,
        "x_label": x_column or "row",
        "y_label": "value",
        "series": series,
        "sampled": len(first) > max_points or len(second) > max_points,
        "truncated_series": len(columns) > 6,
    }


def anomaly_plot(
    data: pd.DataFrame,
    x: str,
    y: str,
    anomaly_column: str,
    title: str = "Anomalies",
    max_points: int = 500,
) -> dict[str, Any]:
    """把异常点叠加到信号上：正常与异常各抽稀一次后分成两条 series。

    分开抽稀很关键——异常点通常很少，混在一起抽稀容易被"均匀抽掉"，
    导致图上看起来完全没有异常。返回 ``anomaly_count`` 是**抽稀前**的真实计数。
    """
    missing = {x, y, anomaly_column} - set(data.columns)
    if missing:
        raise ValueError(f"Anomaly plot columns are missing: {sorted(missing)}")
    numeric_columns(data, [y])
    mask = data[anomaly_column].astype(bool)
    normal, abnormal = data.loc[~mask], data.loc[mask]

    def bounded(frame: pd.DataFrame) -> pd.DataFrame:
        stride = max(1, (len(frame) + max_points - 1) // max_points)
        return frame.iloc[::stride].head(max_points)

    normal, abnormal = bounded(normal), bounded(abnormal)
    return {
        "kind": "anomaly",
        "title": title,
        "x_label": x,
        "y_label": y,
        "series": [
            {"name": "normal", "x": normal[x].tolist(), "y": normal[y].tolist()},
            {"name": "anomaly", "x": abnormal[x].tolist(), "y": abnormal[y].tolist()},
        ],
        "anomaly_count": int(mask.sum()),
        "sampled": len(normal) + len(abnormal) < len(data),
    }


def relationship(
    data: pd.DataFrame,
    columns: list[str] | None = None,
    method: str = "pearson",
    threshold: float = 0.3,
) -> dict[str, Any]:
    """相关关系图：输出节点、按|相关系数|排序的边，以及与之等长的便捷 series。

    只保留绝对值 ≥ ``threshold`` 的边并截断到 100 条（同时用 ``truncated_edges`` 标记），
    让"哪些通道高度相关"一眼可见，而不是画一张上百列的热力图。
    """
    cols = numeric_columns(data, columns)
    matrix = data[cols].corr(method=method)
    edges = []
    for left_index, left in enumerate(cols):
        for right in cols[left_index + 1 :]:
            value = float(matrix.loc[left, right])
            if np.isfinite(value) and abs(value) >= threshold:
                edges.append({"source": left, "target": right, "value": value})
    edges.sort(key=lambda item: abs(item["value"]), reverse=True)
    return {
        "kind": "relationship",
        "title": "Feature relationships",
        "nodes": [{"id": column} for column in cols],
        "edges": edges[:100],
        "series": [
            {
                "name": "correlation",
                "x": list(range(len(edges[:100]))),
                "y": [edge["value"] for edge in edges[:100]],
                "labels": [f"{edge['source']} ↔ {edge['target']}" for edge in edges[:100]],
            }
        ],
        "method": method,
        "threshold": threshold,
        "truncated_edges": len(edges) > 100,
    }
