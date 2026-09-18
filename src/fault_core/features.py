"""Window features with aligned labels and source coverage for validation.

这是整个平台最核心的一层：把**逐行采样**的原始数据变成**逐窗口**的特征表——
一个窗口产出一行特征（不是每个采样点一行）。

**特征行数由窗口大小与步长决定，与输入行数无关**：每组大约"组内时长 / 步长"个窗口，
尾部不足一个窗口的部分丢弃；按行切窗时则是"组内行数 / 步长"。例如 48.9 万行的原始数据，
窗口 `180s` / 步长 `60s` 得到 8081 行特征；把步长换成 `180s` 就只剩 2700 行。
每个窗口同时产出三样东西，供下游验证器使用：

1. **窗口键**（按行切窗为 ``g3_w128``，按时间切窗为 ``g3_t1577836800``）：分组序号 + 组内起始行/起始秒，用于人工定位；
2. **窗口标签**：按 ``label_policy`` 从窗口内的行标签聚合，与特征逐行对齐；
3. **来源行覆盖**（``source_rows``，流式下为 ``source_rows_ranges``）：
   每个窗口用了哪些原始行，用于"训练/测试是否共享原始行"的泄漏检查。

因此下面的函数都围绕三条不变量：

* **行对齐**：特征、标签、来源覆盖三者的行数与顺序严格一致；
* **窗口完整**：只保留完整的窗口（按行切窗时长度为 ``window_size``；按时间切窗时"起点 + 跨度"不超过组内最后一个采样时刻），尾部不足的丢弃；
* **可追溯**：``attrs`` 里带着分组、窗口参数、资产、类别编码器与探索性警告，
  经过特征合并、缓存、检查点与磁盘溢写都不能丢。

批处理（整表）与流式（分块）两条路径的输出 schema 完全一致，
``tests/test_streaming.py`` 用 ``assert_allclose(atol=1e-12)`` 逐位校验这一点。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.metrics import r2_score
from sklearn.model_selection import KFold

from fault_core.data import numeric_columns
from fault_core.preprocessing import mark_fitted

STATISTICS = (
    "mean",
    "std",
    "variance",
    "min",
    "max",
    "median",
    "rms",
    "skewness",
    "kurtosis",
    "quantile",
    "range",
    "iqr",
    "mad",
    "peak",
    "crest_factor",
    # ── 结构类窗口特征（对应《完整组件》清单里的"位置/计数/重复/变化"那一批）──
    "count",
    "distinct_count",
    "argmax_first",
    "argmax_last",
    "argmin_first",
    "argmin_last",
    "count_above_mean",
    "count_below_mean",
    "longest_above_mean",
    "longest_below_mean",
    "mean_delta",
    "mean_abs_delta",
    "mean_second_derivative",
    "duplicate_point_ratio",
    "repeated_value_ratio",
    "duplicate_sum",
    "time_reversal_asymmetry",
    "std_gt_range",
    "variance_gt_std",
    "max_repeated",
    "min_repeated",
)

SPECTRAL = (
    "dominant_frequency",
    "dominant_amplitude",
    "spectral_centroid",
    "spectral_spread",
    "spectral_entropy",
    "spectral_rms",
    "high_frequency_ratio",
    "harmonic_ratio",
    "band_energy_ratio",
)

#: 类别编码方法：独热、序数、频率、目标均值、频率+计数。
CATEGORICAL_METHODS = ("onehot", "ordinal", "frequency", "target", "category_statistics")
#: 推理期遇到训练没见过的类别时的策略：按默认值忽略，或直接报错。
UNKNOWN_CATEGORY_POLICIES = ("ignore", "error")
#: 缺失类别统一编码成这个名字，而不是被 dropna 丢掉。
MISSING_CATEGORY = "<missing>"

#: Shorter windows cannot resolve a usable spectrum, so they are rejected outright.
MIN_SPECTRAL_SAMPLES = 8

#: 时长文本的单位换算（秒）。只接受"数字 + 单位"，纯数字按秒解释。
DURATION_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}
_DURATION = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([smhdw])?$")
#: 判断"窗口起点 + 跨度是否落在数据末尾之内"时允许的浮点误差（秒）。
TIME_EPSILON = 1e-6


def parse_duration(value: Any, *, name: str = "duration") -> float:
    """把 ``"7d"``/``"12h"``/``"30m"``/``"180s"`` 解析成秒；空值返回 0。

    只认"数字 + 单位"（``s`` 秒、``m`` 分、``h`` 时、``d`` 天、``w`` 周），纯数字按秒。
    单位写错会**明确报错**，而不是悄悄退化成 0——那样"7 天窗口"会变成"整组一个窗口"，
    是最安静也最贵的一类错。
    """
    if value is None or value == "":
        return 0.0
    text = str(value).strip().casefold()
    match = _DURATION.match(text)
    if not match:
        raise ValueError(f"{name} must look like 7d / 12h / 30m / 180s, got {value!r}")
    amount, unit = match.groups()
    return float(amount) * DURATION_UNITS[unit or "s"]


def order_by_time(frame: pd.DataFrame, time_column: str) -> tuple[pd.DataFrame, np.ndarray]:
    """按时间稳定排序，并返回 ``(排好序的表, 秒数组)``。

    预测任务里"未来"必须有定义，所以顺序在这里统一保证，而不是指望调用方先排好。
    时间列支持两种：时间戳（datetime64 或可解析字符串）与数值（按**秒**解释）。
    """
    column = frame[time_column]
    if pd.api.types.is_numeric_dtype(column) and not pd.api.types.is_bool_dtype(column):
        seconds = column.to_numpy(dtype=float, copy=True)
    else:
        parsed = pd.to_datetime(column, errors="raise")
        seconds = parsed.to_numpy(dtype="datetime64[ns]").astype("int64") / 1e9
    if seconds.size > 1 and not np.all(np.diff(seconds) >= 0):
        order = np.argsort(seconds, kind="stable")
        return frame.iloc[order], seconds[order]
    return frame, seconds


def group_frames(data: pd.DataFrame, group_column: str | None) -> Iterator[tuple[int, str, pd.DataFrame]]:
    """按分组列逐组产出 ``(组序号, 组标签, 数据)``；不给分组列时整表算一组。"""
    groups = data.groupby(group_column, sort=False, dropna=False) if group_column else [("all", data)]
    for group_number, (group, frame) in enumerate(groups):
        yield group_number, str(group), frame


def windows_in_frame_by_time(
    frame: pd.DataFrame, seconds: np.ndarray, window_span: float, step_span: float
) -> Iterator[tuple[int, pd.DataFrame, list[Any], float, float]]:
    """在**已按时间排序**的组内按时间跨度切窗。

    产出 ``(起始行号, 窗口数据, 来源行, 起点秒, 终点秒)``。与按行切窗的区别只在"完整"
    的判定：这里要求窗口起点加跨度不超过组内最后一个采样时间——也就是这段时间的数据确实
    存在；窗口内的行则是区间 ``[起点, 起点+跨度)`` 里的全部采样。采样不规则时每个窗口的
    行数可以不同，这正是按时间切的意义。
    """
    if window_span <= 0 or seconds.size == 0:
        return
    last = float(seconds[-1])
    stride = step_span or window_span
    start = 0
    while start < seconds.size:
        begin = float(seconds[start])
        end = begin + window_span
        if end > last + TIME_EPSILON:
            break
        stop = int(np.searchsorted(seconds, end, side="left"))
        chunk = frame.iloc[start:stop]
        yield start, chunk, chunk.index.tolist(), begin, end
        nxt = int(np.searchsorted(seconds, begin + stride, side="left"))
        start = nxt if nxt > start else start + 1


def time_windows(
    data: pd.DataFrame,
    group_column: str | None,
    time_column: str,
    window_span: float,
    step_span: float,
) -> Iterator[tuple[str, pd.DataFrame, str, list[Any], float, float]]:
    """按时间跨度切窗（分组版），产出 ``(键, 窗口数据, 分组标签, 来源行, 起点秒, 终点秒)``。

    与 :func:`windows` 的关系：同样的分组与键命名规则，只是按时间而不按行数切窗，
    所以窗口内的行数可以不同——"用最近 7 天的数据"这种需求就该用它。
    """
    if not data.index.is_unique:
        raise ValueError("Source row index must be unique")
    for group_number, group_label, frame in group_frames(data, group_column):
        ordered, seconds = order_by_time(frame, time_column)
        for start, chunk, source_rows, begin, end in windows_in_frame_by_time(
            ordered, seconds, window_span, step_span
        ):
            yield f"g{group_number}_t{int(begin)}", chunk, group_label, source_rows, begin, end


def prediction_label(
    seconds: np.ndarray,
    labels: pd.Series,
    window_end: float,
    horizon: float,
    gap: float,
    normal_label: Any,
) -> int | None:
    """预测标签：窗口结束 + 间隔之后的视野内，有没有出现故障。

    * ``1``：``(end+gap, end+gap+horizon]`` 这段里出现过不等于 ``normal_label`` 的标签；
    * ``0``：这段区间有采样，而且全是正常；
    * ``None``：视野超出可用数据，或这段区间压根没有采样——**不能诚实地标 0**，
      由调用方丢弃并计数。宁可少几个窗口，也不要造标签。
    """
    start = window_end + gap
    stop = start + horizon
    if seconds.size == 0 or stop > float(seconds[-1]) + TIME_EPSILON:
        return None
    left = int(np.searchsorted(seconds, start, side="right"))
    right = int(np.searchsorted(seconds, stop, side="right"))
    if right <= left:
        return None
    return int(bool(np.any(labels.iloc[left:right].to_numpy() != normal_label)))


def coerce_normal_label(labels: pd.Series, raw: Any) -> Any:
    """``normal_label`` 在参数 schema 里是字符串；这里按标签列的真实类型对齐。"""
    if raw is None or raw == "":
        return 0
    if pd.api.types.is_numeric_dtype(labels):
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"normal_label must be numeric for a numeric label column: {raw!r}") from exc
        return int(value) if value.is_integer() else value
    return str(raw)


def _horizon_windows(
    data: pd.DataFrame,
    group_column: str | None,
    time_column: str,
    label_column: str,
    span: float,
    stride: float,
    horizon: float,
    gap: float,
    normal_label: Any,
    current_fault_policy: str,
    counters: dict[str, int],
) -> Iterator[tuple[str, pd.DataFrame, str, list[Any], int]]:
    """预测任务的窗口流：按时间切窗，用未来视野打标签。

    三种窗口会被**丢弃并计数**（``counters``）：窗口自身已经故障且策略为 ``drop``；
    视野超出数据末尾；视野内没有任何采样。前两种必须计数后写进报告，否则"少了多少样本"
    就没人知道了。
    """
    for group_number, group_label, frame in group_frames(data, group_column):
        ordered, seconds = order_by_time(frame, time_column)
        if seconds.size == 0:
            continue
        group_labels = ordered[label_column]
        normal = coerce_normal_label(group_labels, normal_label)
        for start, chunk, source_rows, begin, end in windows_in_frame_by_time(ordered, seconds, span, stride):
            if bool(np.any(chunk[label_column].to_numpy() != normal)):
                if current_fault_policy == "drop":
                    counters["current_fault"] += 1
                    continue
                label = 1 if current_fault_policy == "positive" else 0
            else:
                label = prediction_label(seconds, group_labels, end, horizon, gap, normal)
                if label is None:
                    counters["unknown_future"] += 1
                    continue
            key = f"g{group_number}_t{int(begin)}"
            yield key, chunk, group_label, source_rows, label


def windows(
    data: pd.DataFrame, group_column: str | None, window_size: int, step: int, time_column: str | None = None
) -> Iterator[tuple[str, pd.DataFrame, str, list[Any]]]:
    """按分组切窗口，逐个产出 ``(窗口键, 窗口数据, 分组标签, 来源行列表)``。

    * 分组：给了 ``group_column`` 就按它分组（**绝不跨组**），否则整表算一组；
    * 窗口键命名 ``g<组序号>_w<组内起始行号>``，便于在报告里指认具体窗口；
    * 组序号按数据中出现的顺序编号，没有任何随机性，因此键可复现。

    这是批处理入口；流式路径复用 :func:`windows_in_frame`，保证两条路径切窗规则一致。
    """
    if not data.index.is_unique:
        # 行索引是窗口覆盖的"身份证"：重复索引会让来源行与标签对齐全部失去意义。
        raise ValueError("Source row index must be unique")
    groups = data.groupby(group_column, sort=False, dropna=False) if group_column else [("all", data)]
    for group_number, (group, frame) in enumerate(groups):
        for start, chunk, source_rows in windows_in_frame(frame, window_size, step, time_column):
            yield f"g{group_number}_w{start}", chunk, str(group), source_rows


def group_assets(data: pd.DataFrame, group_column: str, asset_column: str) -> dict[str, str]:
    """建立"窗口分组 → 资产"的映射（一台设备通常包含多个实例）。

    一个分组只能对应一个资产；出现一对多说明同一实例键跨了多台设备，
    这会让资产留出结果失真，因此直接报错并列出冲突的分组名。
    """
    if asset_column not in data.columns:
        raise ValueError(f"Missing asset column: {asset_column}")
    pairs = data[[group_column, asset_column]].drop_duplicates()
    conflicting = pairs.groupby(group_column)[asset_column].nunique()
    if (conflicting > 1).any():
        raise ValueError(
            f"Groups map to several assets via {asset_column}: {conflicting[conflicting > 1].index[:5].tolist()}"
        )
    return {str(row[group_column]): str(row[asset_column]) for _, row in pairs.iterrows()}


def attach_assets(outputs: dict[str, Any], asset_map: dict[str, str] | None, group_ids: list[str]) -> None:
    """把每个窗口所属的资产写进 ``attrs["assets"]``，顺序与窗口行严格一致。

    验证器的 ``split_method=asset`` 直接读这个列表；缺映射的分组会报错，
    而不是留下空值让下游把两台设备混在一起。特征与标签两侧都写入，保证 provenance 比较通过。
    """
    if not asset_map:
        # 未配置 asset_column 属于正常情况：资产级留出是可选能力。
        return
    missing = sorted({group for group in group_ids if group not in asset_map})
    if missing:
        raise ValueError(f"Window groups without an asset mapping: {missing[:5]}")
    assets = [asset_map[group] for group in group_ids]
    outputs["features"].attrs["assets"] = assets
    if "labels" in outputs:
        outputs["labels"].attrs["assets"] = list(assets)


def windows_in_frame(
    frame: pd.DataFrame, window_size: int, step: int, time_column: str | None = None
) -> Iterator[tuple[int, pd.DataFrame, list[Any]]]:
    """在单个分组内切窗口，产出 ``(起始行号, 窗口数据, 来源行列表)``。

    批处理与流式共用这一份逻辑，避免两条路径出现细微差异：

    * ``window_size=0`` 表示"整组作为一个窗口"，``step=0`` 表示无重叠（步长=窗口长度）；
    * 只产出**完整**窗口，``range(0, len - size + 1, stride)`` 让尾部不足部分被丢弃；
    * 指定 ``time_column`` 时先按时间排序，已单调则跳过排序（避免整组拷贝）。
    """
    # Sorting copies the whole group; skip it when the rows are already ordered.
    if time_column and not frame[time_column].is_monotonic_increasing:
        frame = frame.sort_values(time_column, kind="stable")
    size = window_size or len(frame)
    if not size:
        return
    stride = step or size
    for start in range(0, len(frame) - size + 1, stride):
        chunk = frame.iloc[start : start + size]
        yield start, chunk, chunk.index.tolist()


def _longest_run(mask: np.ndarray) -> int:
    """最长连续 True 的长度（"大于均值的最长时段"这类特征用）。"""
    best = current = 0
    for flag in mask:
        current = current + 1 if flag else 0
        best = max(best, current)
    return best


def _half_delta(x: np.ndarray) -> float:
    """窗口后半段均值减前半段均值；样本少于两个时返回 0（不产生 NaN 特征）。"""
    if len(x) < 2:
        return 0.0
    middle = len(x) // 2
    return float(np.mean(x[middle:]) - np.mean(x[:middle]))


def _stat(x: np.ndarray, name: str, quantile: float) -> float:
    """计算单个统计量；``quantile`` 只在 ``name="quantile"`` 时使用。

    两处防御：常数列的偏度/峰度返回 0（数学上分母为 0，无定义）；
    RMS 为 0 时波峰因数返回 0，而不是除零得到 inf。
    """
    rms = float(np.sqrt(np.mean(x**2)))
    funcs = {
        "mean": lambda: np.mean(x),
        "std": lambda: np.std(x),
        "variance": lambda: np.var(x),
        "min": lambda: np.min(x),
        "max": lambda: np.max(x),
        "median": lambda: np.median(x),
        "rms": lambda: rms,
        "skewness": lambda: stats.skew(x) if np.ptp(x) else 0.0,
        "kurtosis": lambda: stats.kurtosis(x) if np.ptp(x) else 0.0,
        "quantile": lambda: np.quantile(x, quantile),
        "range": lambda: np.ptp(x),
        "iqr": lambda: np.quantile(x, 0.75) - np.quantile(x, 0.25),
        "mad": lambda: np.median(np.abs(x - np.median(x))),
        "peak": lambda: np.max(np.abs(x)),
        "crest_factor": lambda: np.max(np.abs(x)) / rms if rms else 0.0,
        # ── 结构类特征：位置、计数、重复、变化 ──
        "count": lambda: len(x),
        # 窗口内**不同取值个数**。给"地址/编号/档位"这类标识列用：它们不该被求均值
        # （地址是坐标，不是物理量），但"这一小段时间里错误落到多少个不同的 bank / col 上"
        # 是真实的物理问题。等价于 `count × (1 - duplicate_point_ratio)`，单独给出来是因为
        # 调用方要从枚举里直接选到它，而不是自己拼算术。
        "distinct_count": lambda: float(len(np.unique(x))),
        # 位置按 0..1 归一化，跨窗口长度可比；首/末分别指该极值第一次与最后一次出现。
        "argmax_first": lambda: float(np.argmax(x)) / max(len(x) - 1, 1),
        "argmax_last": lambda: float(len(x) - 1 - np.argmax(x[::-1])) / max(len(x) - 1, 1),
        "argmin_first": lambda: float(np.argmin(x)) / max(len(x) - 1, 1),
        "argmin_last": lambda: float(len(x) - 1 - np.argmin(x[::-1])) / max(len(x) - 1, 1),
        "count_above_mean": lambda: float(np.sum(x > np.mean(x))),
        "count_below_mean": lambda: float(np.sum(x < np.mean(x))),
        "longest_above_mean": lambda: float(_longest_run(x > np.mean(x))),
        "longest_below_mean": lambda: float(_longest_run(x < np.mean(x))),
        "mean_delta": lambda: _half_delta(x),
        "mean_abs_delta": lambda: abs(_half_delta(x)),
        "mean_second_derivative": lambda: float(np.mean(np.diff(x, n=2))) if len(x) > 2 else 0.0,
        "duplicate_point_ratio": lambda: float(1.0 - len(np.unique(x)) / len(x)),
        "repeated_value_ratio": lambda: float(
            sum(int(count) for count in pd.Series(x).value_counts() if count > 1) / len(x)
        ),
        "duplicate_sum": lambda: float(
            sum(
                float(value) * (int(count) - 1)
                for value, count in pd.Series(x).value_counts().items()
                if count > 1
            )
        ),
        # 时间反转不对称：对 (x[t+2] - x[t])² 取均值，正/负向变化在这一统计量上不对称。
        "time_reversal_asymmetry": lambda: float(np.mean((x[2:] - x[:-2]) ** 2)) if len(x) > 2 else 0.0,
        # 以下四个是清单里的"比较型"特征，按字面实现；它们是否对模型有用需要单独评估。
        "std_gt_range": lambda: float(np.std(x) > np.ptp(x)),
        "variance_gt_std": lambda: float(np.var(x) > np.std(x)),
        "max_repeated": lambda: float(np.sum(x == np.max(x)) > 1),
        "min_repeated": lambda: float(np.sum(x == np.min(x)) > 1),
    }
    return float(funcs[name]())


def _window_label(chunk: pd.DataFrame, label_column: str, label_policy: str) -> Any:
    """把一个窗口内的多行标签聚合成窗口标签。

    * ``strict``：窗口内标签必须完全一致，否则报错——失败位置通常正是故障起点，
      这正是它存在的意义（提醒不要把 onset 抹平）；
    * ``mode``：取窗口内出现最多的标签（onset/退化数据最常用）；
    * ``last``：取窗口末行的标签（预测窗口结束时刻的状态）。

    标签含缺失值一律报错：任何聚合都会掩盖"这一段没有标注"。
    """
    y = chunk[label_column]
    if y.isna().any():
        raise ValueError("Labels contain missing values")
    if label_policy not in {"strict", "mode", "last"}:
        raise ValueError(f"Unknown label_policy: {label_policy}")
    if label_policy == "strict" and y.nunique() != 1:
        raise ValueError("Mixed labels in a window; reduce window or select an explicit label_policy")
    return y.mode().iloc[0] if label_policy == "mode" else y.iloc[-1]


def _feature_row(
    chunk: pd.DataFrame,
    cols: list[str],
    kind: str,
    chosen: list[str],
    quantile: float,
    degree: int,
    fitting_method: str,
    time_column: str | None,
) -> dict[str, float]:
    """为一个窗口生成一行特征；批处理与流式共用，保证两条路径数值一致。

    ``kind="statistical"`` 时按 ``chosen`` 逐个算统计量；否则按 ``fitting_method``
    做拟合，时间轴取自 ``time_column``（数值列直接用，时间戳换算成相对首点的秒数，
    没有则用行号 0,1,2…）。

    拟合输出：各阶系数 ``coef_*``、R²、趋势强度（``1 - 残差方差/原始方差``，负值截断为 0）、
    残差均值与标准差。指数拟合在对数域做多项式拟合，因此要求取值全为正。
    """
    row: dict[str, float] = {}
    for col in cols:
        values = chunk[col].to_numpy(dtype=float)
        if kind == "statistical":
            row.update({f"{col}__{name}": _stat(values, name, quantile) for name in chosen})
            continue
        if time_column:
            raw_t = chunk[time_column]
            if pd.api.types.is_numeric_dtype(raw_t):
                t = raw_t.to_numpy(dtype=float)
            else:
                # 时间戳转成秒数后减去首点，得到相对时间轴：
                # 否则大数值常量项会让多项式拟合出现明显的数值误差。
                t = pd.to_datetime(raw_t).astype("int64").to_numpy(dtype=float) / 1e9
            t = t - t[0]
        else:
            t = np.arange(len(values), dtype=float)
        deg = degree if fitting_method == "polynomial" else 1
        # 拟合需要"不同时间点"多于待估系数，否则是欠定问题。
        if len(values) <= deg or len(np.unique(t)) <= deg:
            raise ValueError("Fitting window needs more distinct time points than polynomial degree")
        if fitting_method == "exponential" and (values <= 0).any():
            raise ValueError("Exponential fitting requires positive values")
        coefficients = np.polyfit(t, np.log(values) if fitting_method == "exponential" else values, deg)
        fitted = np.polyval(coefficients, t)
        if fitting_method == "exponential":
            fitted = np.exp(fitted)
        residual = values - fitted
        for i, coefficient in enumerate(coefficients):
            # 系数按下标降幂命名：degree=1 时 coef_1 是斜率、coef_0 是截距。
            row[f"{col}__coef_{deg - i}"] = float(coefficient)
        row.update(
            {
                f"{col}__r2": float(r2_score(values, fitted)),
                f"{col}__trend_strength": float(max(0, 1 - np.var(residual) / np.var(values)))
                if np.var(values)
                else 0.0,
                f"{col}__residual_mean": float(np.mean(residual)),
                f"{col}__residual_std": float(np.std(residual)),
            }
        )
    return row


def _assemble(
    rows: list[dict[str, float]],
    keys: list[str],
    labels: list[Any],
    group_ids: list[str],
    coverage: list[list[Any]],
    attrs: dict[str, Any],
    group_column: str | None,
    label_column: str | None,
    window_size: int,
    step: int,
    coverage_as_ranges: bool = False,
    overlapping: bool | None = None,
) -> dict[str, Any]:
    """把逐窗口结果拼成特征表与标签向量，并写全 provenance。

    ``rows``/``keys``/``labels``/``group_ids``/``coverage`` 由调用方的循环保证等长有序。
    写入 attrs 的关键字段：

    * ``groups`` / ``grouped``：分组标签与"是否真的分组"，验证器据此判断能否分组留出；
    * ``window_size`` / ``step`` / ``overlapping``：重叠窗口不能随机划分，验证器会检查这个标记；
    * ``source_rows``（批处理，逐窗口行列表）或 ``source_rows_ranges``（流式，``[start, stop)``
      区间）：训练/测试共享行的泄漏检查依据。流式刻意保留区间而不展开成行号列表，
      否则 400 万行的输入仅这一项就要几百 MB。
    """
    if not rows:
        # 一个窗口都没切出来（例如窗口比任何分组都长），说明参数与数据不匹配。
        if attrs.get("window_span_seconds"):
            # 时间窗口最常见的错因：跨度比数据跨度还大，或时间列其实不是秒。
            raise ValueError(
                "No complete time windows; shorten window_span, or check that time_column is really in seconds"
            )
        raise ValueError("No complete feature windows; reduce window_size")
    result = pd.DataFrame(rows, index=pd.Index(keys, name="window_id"))
    result.attrs = {
        **attrs,
        "groups": group_ids,
        "grouped": bool(group_column),
        "window_size": window_size,
        "step": step,
        # 按时间切窗时行数不固定，"是否重叠"由调用方按跨度给出；按行切窗在这里推导。
        "overlapping": overlapping
        if overlapping is not None
        else bool(window_size and step and step < window_size),
    }
    # Streamed extraction records [start, stop) ranges; a full list of label objects
    # per window would cost ~36 bytes per source row and dominate memory.
    if coverage_as_ranges:
        result.attrs["source_rows_ranges"] = coverage
    else:
        result.attrs["source_rows"] = coverage
    outputs: dict[str, Any] = {"features": result}
    if label_column:
        outputs["labels"] = pd.Series(labels, index=result.index, name=label_column)
        outputs["labels"].attrs = dict(result.attrs)
    return outputs


def window_arguments(
    *,
    window_size: int,
    window_span: Any,
    step_span: Any,
    label_policy: str,
    prediction_horizon: Any,
    prediction_gap: Any,
    current_fault_policy: str,
    time_column: str | None,
    label_column: str | None,
) -> tuple[float, float, float, float]:
    """校验并解析窗口/预测参数，返回 ``(span, stride, horizon, gap)``（秒）。

    三条实现（statistical/fitting、spectral、entropy）共用这一份校验：参数组合写错时报一样
    的错，也不会再出现"某个组件悄悄少支持一个参数"的漂移。
    """
    span = parse_duration(window_span, name="window_span")
    stride = parse_duration(step_span, name="step_span")
    horizon = parse_duration(prediction_horizon, name="prediction_horizon")
    gap = parse_duration(prediction_gap, name="prediction_gap")
    horizon_mode = label_policy == "horizon"
    # 参数组合在这里一次说清楚：静默忽略一个写错的参数，比报错贵得多。
    if span and window_size:
        raise ValueError("window_span and window_size cannot both be set; pick one window definition")
    if stride and not span:
        raise ValueError("step_span needs window_span")
    if not horizon_mode and (horizon or gap):
        raise ValueError("prediction_horizon/prediction_gap need label_policy=horizon")
    if not horizon_mode and current_fault_policy != "drop":
        raise ValueError("current_fault_policy needs label_policy=horizon")
    if horizon_mode:
        if not label_column:
            raise ValueError("label_policy=horizon needs label_column")
        if not time_column:
            raise ValueError("label_policy=horizon needs time_column")
        if not span:
            raise ValueError("label_policy=horizon needs window_span, e.g. 7d")
        if not horizon:
            raise ValueError("label_policy=horizon needs prediction_horizon, e.g. 2d")
        if current_fault_policy not in {"drop", "positive", "negative"}:
            raise ValueError("current_fault_policy must be drop, positive or negative")
    return span, stride, horizon, gap


def prepared_windows(
    data: pd.DataFrame,
    group_column: str | None,
    label_column: str | None,
    time_column: str | None,
    window_size: int,
    step: int,
    label_policy: str,
    span: float,
    stride: float,
    horizon: float,
    gap: float,
    current_fault_policy: str,
    normal_label: str,
    counters: dict[str, int],
) -> Iterator[tuple[str, pd.DataFrame, str, list[Any], Any]]:
    """统一的窗口流：``(键, 窗口数据, 分组标签, 来源行, 现成标签或 None)``。

    ``None`` 表示标签由调用方按窗口内容聚合（``strict``/``mode``/``last``）；预测模式
    （``horizon``）直接给出 0/1。窗口定义（按行 / 按时间）与标签语义只在这里定义一次，
    三个组件实现共用——避免再出现"某个组件悄悄少支持一个参数"的漂移。
    """
    if label_policy == "horizon":
        yield from _horizon_windows(
            data,
            group_column,
            time_column,
            label_column,
            span,
            stride or span,
            horizon,
            gap,
            normal_label,
            current_fault_policy,
            counters,
        )
    elif span:
        for key, chunk, group, source_rows, _, _ in time_windows(
            data, group_column, time_column, span, stride or span
        ):
            yield key, chunk, group, source_rows, None
    else:
        for key, chunk, group, source_rows in windows(data, group_column, window_size, step, time_column):
            yield key, chunk, group, source_rows, None


def window_shape(span: float, stride: float, window_size: int, step: int) -> tuple[int, int, bool | None]:
    """``_assemble`` 需要的 ``(window_size, step, overlapping)``。

    按时间切窗时行数不固定，所以 ``window_size``/``step`` 记 0，是否重叠直接由跨度比较得出；
    按行切窗时原样交回，让 ``_assemble`` 自己推导。
    """
    if span:
        return 0, 0, bool(stride or span) < span
    return window_size, step, None


def window_attrs(
    base: Any,
    *,
    label_policy: str,
    span: float,
    stride: float,
    horizon: float,
    gap: float,
    current_fault_policy: str,
    counters: dict[str, int],
) -> dict[str, Any]:
    """把时间窗口/预测视野的元数据与丢弃计数写进 attrs（三条实现共用）。

    "丢了多少窗口、为什么丢"必须能汇报出去，否则样本为什么变少就没人知道。
    """
    attrs = dict(base)
    horizon_mode = label_policy == "horizon"
    if span or horizon_mode:
        attrs.update(
            {
                "window_span_seconds": span or None,
                "step_span_seconds": (stride or span) or None,
                "prediction_horizon_seconds": horizon or None,
                "prediction_gap_seconds": gap if horizon_mode else None,
                "current_fault_policy": current_fault_policy if horizon_mode else None,
            }
        )
    if horizon_mode:
        dropped_current = counters["current_fault"]
        dropped_unknown = counters["unknown_future"]
        attrs["horizon_dropped_current_fault"] = dropped_current
        attrs["horizon_dropped_unknown_future"] = dropped_unknown
        notices = list(attrs.get("warnings", []))
        if dropped_current:
            notices.append(
                f"{dropped_current} 个窗口自身已经包含故障，按 current_fault_policy={current_fault_policy} 处理："
                "这些样本属于检测而不是预测"
            )
        if dropped_unknown:
            notices.append(f"{dropped_unknown} 个窗口的预测视野超出了可用数据，已丢弃，而不是标成 0")
        if notices:
            attrs["warnings"] = notices
    return attrs


def extract_features(
    data: pd.DataFrame,
    columns: list[str],
    kind: str = "statistical",
    features: list[str] | None = None,
    group_column: str | None = None,
    label_column: str | None = None,
    time_column: str | None = None,
    window_size: int = 0,
    step: int = 0,
    label_policy: str = "strict",
    quantile: float = 0.75,
    degree: int = 2,
    fitting_method: str = "linear",
    asset_column: str | None = None,
    window_span: str | float = "",
    step_span: str | float = "",
    prediction_horizon: str | float = "",
    prediction_gap: str | float = "",
    current_fault_policy: str = "drop",
    normal_label: str = "0",
) -> dict[str, Any]:
    """批处理入口：一次读完整表，输出窗口特征与对齐标签。

    ``kind`` 决定走统计还是拟合分支（``_feature_row``）。窗口有两种定义：

    * **按行**（默认）：``window_size`` / ``step``，适合固定采样率的短窗；
    * **按时间**：``window_span``（如 ``"7d"``）/ ``step_span``，适合"用最近 7 天的数据"
      这种说法，也天然处理采样不规则或带缺口的数据；每个窗口的行数可以不同。

    标签有两种来源：窗口内聚合（``strict``/``mode``/``last``），或**未来视野**
    （``label_policy="horizon"``）：窗口结束加 ``prediction_gap`` 之后、``prediction_horizon``
    之内出现过故障就标 1。后者才是"预测未来会不会故障"，也是把检测任务变成预测任务的那一步。
    窗口自身已经故障、或视野超出数据末尾的样本会被丢弃并计数，写进 attrs 与 warnings。

    返回 ``{"features": DataFrame, "labels": Series}``（没有标签列时只有 features），
    两者的 ``attrs`` 携带分组、窗口参数、来源行与预测视野，供合并与验证使用。
    """
    cols = numeric_columns(data, columns)
    # 标签列与分组列不能同时当特征输入，否则等于把答案（或身份）喂给模型。
    if label_column in cols or group_column in cols:
        raise ValueError("Label/group columns cannot be feature inputs")
    if not np.isfinite(data[cols].to_numpy(dtype=float, copy=False)).all():
        raise ValueError("Feature extraction requires finite numeric values")
    chosen = features or ["mean", "std", "rms"]
    asset_of = group_assets(data, group_column, asset_column) if asset_column and group_column else None
    span, stride, horizon, gap = window_arguments(
        window_size=window_size,
        window_span=window_span,
        step_span=step_span,
        label_policy=label_policy,
        prediction_horizon=prediction_horizon,
        prediction_gap=prediction_gap,
        current_fault_policy=current_fault_policy,
        time_column=time_column,
        label_column=label_column,
    )
    counters = {"current_fault": 0, "unknown_future": 0}
    rows, labels, keys, group_ids, coverage = [], [], [], [], []
    for key, chunk, group, source_rows, ready_label in prepared_windows(
        data,
        group_column,
        label_column,
        time_column,
        window_size,
        step,
        label_policy,
        span,
        stride,
        horizon,
        gap,
        current_fault_policy,
        normal_label,
        counters,
    ):
        rows.append(_feature_row(chunk, cols, kind, chosen, quantile, degree, fitting_method, time_column))
        if label_column:
            label = (
                ready_label if ready_label is not None else _window_label(chunk, label_column, label_policy)
            )
            labels.append(label)
        keys.append(key)
        group_ids.append(group)
        coverage.append(source_rows)

    assemble_size, assemble_step, overlapping = window_shape(span, stride, window_size, step)
    attrs = window_attrs(
        data.attrs,
        label_policy=label_policy,
        span=span,
        stride=stride,
        horizon=horizon,
        gap=gap,
        current_fault_policy=current_fault_policy,
        counters=counters,
    )
    outputs = _assemble(
        rows,
        keys,
        labels,
        group_ids,
        coverage,
        attrs,
        group_column,
        label_column,
        assemble_size,
        assemble_step,
        overlapping=overlapping,
    )
    attach_assets(outputs, asset_of, group_ids)
    return outputs


def _spectral_names(chosen: list[str], edges: list[float]) -> list[str]:
    """列出频域一行会包含的全部列名，用于让平窗口也能输出同构的 NaN 行。

    列结构必须与正常窗口完全一致，否则平窗口一旦少列，
    下游按列名取数或参与合并时就会错位。
    """
    names: list[str] = []
    for name in chosen:
        if name == "band_energy_ratio":
            names.extend(f"band_energy_ratio_{index}" for index in range(len(edges) + 1))
        else:
            names.append(name)
    return names


def _flat_warnings(flat_counts: dict[str, int], window_count: int) -> list[str]:
    """把平窗口统计转成警告文案（每个通道一条），说明"这些位置没有可用的频谱"。"""
    return [
        f"{col}: {count} of {window_count} windows carry no usable spectrum "
        "(constant, or variation only where the taper is zero); spectral features are NaN there."
        for col, count in flat_counts.items()
        if count
    ]


def _stream_runs(chunk: pd.DataFrame, group_column: str) -> Iterator[tuple[Any, pd.DataFrame]]:
    """在一个数据块内找出"同一个分组值的极大连续段"。

    用向量化的边界检测（当前值 ≠ 前一行且不是双 NaN）先定位所有段的起点，再按段切片。
    之所以需要它：流式输入可能在一个块中间换组，窗口不能跨组，必须先把块切开。
    """
    values = chunk[group_column]
    shifted = values.shift()
    changed = values.ne(shifted) & ~(values.isna() & shifted.isna())
    if len(changed):
        changed.iloc[0] = True
    starts = np.flatnonzero(changed.to_numpy())
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(values)
        yield values.iloc[start], chunk.iloc[start:end]


def _stream_windows(
    chunks: Iterable[pd.DataFrame],
    columns: list[str],
    build_row: Callable[[pd.DataFrame, list[str]], dict[str, float]],
    group_column: str | None,
    label_column: str | None,
    time_column: str | None,
    window_size: int,
    step: int,
    label_policy: str,
    attrs: dict[str, Any],
    asset_column: str | None = None,
    min_window_rows: int = 1,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Window features over a chunk iterator without ever holding the whole input.

    Memory is bounded by one chunk plus the largest single group (grouped mode), or by
    the rolling window buffer (group-less mode). Rows must already be ordered: by group
    (contiguous) and, when ``time_column`` is given, by time inside each group.

    ``build_row`` 返回 ``None`` 表示"跳过这个窗口"（频域组件的 ``flat_policy=skip`` 用它）；
    ``min_window_rows`` 是每个窗口的最少样本数（频域要求至少 8 个点）。
    进度通过 ``on_progress(已处理行数)`` 回调上报，用于给用户显示执行进度。
    """
    rows: list[dict[str, float]] = []
    labels: list[Any] = []
    keys: list[str] = []
    group_ids: list[str] = []
    coverage: list[list[Any]] = []
    processed = 0
    cols: list[str] = []
    asset_of: dict[str, str] = {}

    def add_window(group_number: int, start: int, window: pd.DataFrame, group_label: str) -> None:
        """收下一个窗口：校验时间有序、调用 build_row、记录键/分组/来源区间/标签。"""
        if len(window) < min_window_rows:
            raise ValueError(f"Window needs at least {min_window_rows} samples")
        if time_column and not window[time_column].is_monotonic_increasing:
            # 流式只有一个方向：读到哪算到哪，事后无法重排，所以顺序错了必须立刻报错。
            raise ValueError(
                "Streamed feature extraction requires rows ordered by time within each group; "
                "sort the file first or disable streaming on the data input"
            )
        row = build_row(window, cols)
        if row is None:  # the builder asked to skip this window
            return
        rows.append(row)
        keys.append(f"g{group_number}_w{start}")
        group_ids.append(group_label)
        # 覆盖记录为 [起始行, 结束行) 区间：流式下不展开成行号列表，内存才能有界。
        coverage.append([int(window.index[0]), int(window.index[-1]) + 1])
        if label_column:
            labels.append(_window_label(window, label_column, label_policy))

    def check_chunk(chunk: pd.DataFrame) -> list[str]:
        """逐块校验列与数值有效性，返回本块实际参与计算的列名。"""
        found = numeric_columns(chunk, columns)
        if label_column in found or group_column in found:
            raise ValueError("Label/group columns cannot be feature inputs")
        if not np.isfinite(chunk[found].to_numpy(dtype=float, copy=False)).all():
            raise ValueError("Feature extraction requires finite numeric values")
        return found

    if group_column is None:
        # 无分组模式：维护一个"跨块残留缓冲"carry，保证跨越块边界的窗口不被切断。
        carry: pd.DataFrame | None = None
        offset = 0
        for chunk in chunks:
            cols = check_chunk(chunk)
            processed += len(chunk)
            frame = chunk if carry is None else pd.concat([carry, chunk])
            last_start = None
            for start, window, source_rows in windows_in_frame(frame, window_size, step, None):
                add_window(0, offset + start, window, "all")
                last_start = start
            stride = step or window_size or len(frame)
            if last_start is None:
                # 本块还没形成任何一个完整窗口：整块留到下一轮拼。
                carry = frame
            else:
                # 只保留最后一个窗口之后的数据，其余已消费的块直接释放。
                consumed = last_start + stride
                carry = frame.iloc[consumed:] if consumed < len(frame) else None
                offset += consumed
            if on_progress is not None:
                on_progress(processed)
    else:
        # 分组模式：pending 累积分组值相同的连续段；一旦换组就先把上一组的窗口全部切完。
        pending: list[pd.DataFrame] = []
        pending_group: Any = None
        group_number = -1
        seen: set[Any] = set()
        for chunk in chunks:
            cols = check_chunk(chunk)
            processed += len(chunk)
            for group_value, run in _stream_runs(chunk, group_column):
                key = group_value
                if pending_group is None or key != pending_group:
                    if pending:
                        frame = pending[0] if len(pending) == 1 else pd.concat(pending)
                        for start, window, _ in windows_in_frame(frame, window_size, step, time_column):
                            add_window(group_number, start, window, str(pending_group))
                    pending, pending_group = [run], key
                    if asset_column:
                        # Streaming keeps the group -> asset mapping as groups appear.
                        asset_of[str(key)] = str(run[asset_column].iloc[0])
                    if key in seen:
                        # 同一个分组值再次出现说明文件里该组的行不连续，
                        # 这种情况无法在流式下正确切窗，必须让用户先排序。
                        raise ValueError(
                            "Streamed input requires each group's rows to be contiguous; "
                            "sort the file by the group column first"
                        )
                    seen.add(key)
                    group_number += 1
                else:
                    pending.append(run)
            if on_progress is not None:
                on_progress(processed)
        if pending:
            # 收尾：最后一个分组的残留数据也要切窗。
            frame = pending[0] if len(pending) == 1 else pd.concat(pending)
            for start, window, _ in windows_in_frame(frame, window_size, step, time_column):
                add_window(group_number, start, window, str(pending_group))

    # 标记这是流式结果：下游据此知道覆盖信息是区间形式，并用区间算法做重叠判断。
    attrs = {**attrs, "streamed_rows": processed, "streaming": True}
    outputs = _assemble(
        rows,
        keys,
        labels,
        group_ids,
        coverage,
        attrs,
        group_column,
        label_column,
        window_size,
        step,
        coverage_as_ranges=True,
    )
    attach_assets(outputs, asset_of or None, group_ids)
    return outputs


def expand_coverage(attrs: dict[str, Any]) -> list[list[Any]]:
    """把覆盖信息统一展开成"每窗口一个行号列表"。

    只在确实需要逐行判断时才调用（例如没有区间数据的老路径）；
    流式的大数据请优先用 :func:`_intervals` 这类区间算法。
    """
    ranges = attrs.get("source_rows_ranges")
    if ranges is not None:
        return [list(range(start, stop)) for start, stop in ranges]
    return [list(rows) for rows in attrs.get("source_rows", [])]


def _ranges(attrs: dict[str, Any]) -> list[tuple[int, int]] | None:
    """取流式区间形式的覆盖信息；批处理结果没有该字段时返回 None。"""
    ranges = attrs.get("source_rows_ranges")
    if ranges is None:
        return None
    return [(int(start), int(stop)) for start, stop in ranges]


def _close_ranges(values: list[Any]) -> list[tuple[int, int]]:
    """把具体行号列表压缩成 ``[start, stop)`` 区间（相邻行号自动合并）。"""
    ordered = sorted(int(value) for value in values)
    merged: list[tuple[int, int]] = []
    for value in ordered:
        if merged and value == merged[-1][1]:
            merged[-1] = (merged[-1][0], value + 1)
        else:
            merged.append((value, value + 1))
    return merged


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠或相邻的区间，得到一个有序且互不相交的区间列表。"""
    merged: list[tuple[int, int]] = []
    for start, stop in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
        else:
            merged.append((start, stop))
    return merged


def _intervals(attrs: dict[str, Any]) -> list[tuple[int, int]]:
    """把一侧（特征或标签）的覆盖信息统一成合并后的区间列表。

    流式结果直接合并已有区间；批处理结果把逐窗口行号压缩成区间。
    两种形式在这里被抹平，因此重叠判断不需要区分数据来源。
    """
    ranges = _ranges(attrs)
    if ranges is not None:
        return _merge_intervals(ranges)
    return _close_ranges([row for window in attrs.get("source_rows", []) for row in window])


def windows_share_rows(left_attrs: dict[str, Any], right_attrs: dict[str, Any]) -> bool:
    """两侧是否有任意窗口共享原始行（有共享即说明存在泄漏风险）。

    用双指针扫描两个有序区间列表，复杂度 O(n+m)，不需要展开成行号集合。
    """
    left_intervals, right_intervals = _intervals(left_attrs), _intervals(right_attrs)
    index = 0
    for start, stop in left_intervals:
        while index < len(right_intervals) and right_intervals[index][1] <= start:
            index += 1
        if index < len(right_intervals) and right_intervals[index][0] < stop:
            return True
    return False


def rows_without_overlap(attrs: dict[str, Any], held_out: list[int], candidates: list[int]) -> list[int]:
    """剔除与"留出窗口"共享原始行的候选窗口（时间切分时的 purge）。

    区间形式用双指针；退化到逐个行号的形式用集合求交。返回的是候选窗口的位置下标。
    """
    ranges = _ranges(attrs)
    if ranges is None:
        coverage = expand_coverage(attrs)
        blocked = {row for position in held_out for row in coverage[position]}
        return [position for position in candidates if not blocked.intersection(coverage[position])]
    held_out_ranges = _merge_intervals([ranges[position] for position in held_out])
    kept: list[int] = []
    index = 0
    for position in candidates:
        start, stop = ranges[position]
        while index < len(held_out_ranges) and held_out_ranges[index][1] <= start:
            index += 1
        if not (index < len(held_out_ranges) and held_out_ranges[index][0] < stop):
            kept.append(position)
    return kept


def provenance_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """比较两侧来源是否完全一致，兼容"批处理行列表"与"流式区间"两种表示。

    一侧用列表、另一侧用区间时统一展开后比较，因此批处理与流式提取出的特征可以直接合并。
    """
    left_ranges, right_ranges = _ranges(left), _ranges(right)
    if left_ranges is not None and right_ranges is not None:
        return left_ranges == right_ranges
    if left_ranges is None and right_ranges is None:
        return left.get("source_rows") == right.get("source_rows")
    return expand_coverage(left) == expand_coverage(right)


def coverage_subset(attrs: dict[str, Any], positions: list[int]) -> dict[str, Any]:
    """取覆盖信息在指定窗口位置上的子集，形成可直接喂给比较函数的轻量 attrs。

    只搬需要的几个位置，不复制整份覆盖数据——这是切分后复查泄漏时的性能关键。
    """
    ranges = attrs.get("source_rows_ranges")
    if ranges is not None:
        return {"source_rows_ranges": [ranges[position] for position in positions]}
    source_rows = attrs.get("source_rows", [])
    return {"source_rows": [source_rows[position] for position in positions]}


def extract_features_stream(
    chunks: Iterable[pd.DataFrame],
    columns: list[str],
    kind: str = "statistical",
    features: list[str] | None = None,
    group_column: str | None = None,
    label_column: str | None = None,
    time_column: str | None = None,
    window_size: int = 0,
    step: int = 0,
    label_policy: str = "strict",
    quantile: float = 0.75,
    degree: int = 2,
    fitting_method: str = "linear",
    asset_column: str | None = None,
    window_span: str | float = "",
    step_span: str | float = "",
    prediction_horizon: str | float = "",
    prediction_gap: str | float = "",
    current_fault_policy: str = "drop",
    normal_label: str = "0",
    attrs: dict[str, Any] | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """流式版 :func:`extract_features`，输出 schema 完全一致（统计/拟合分支）。

    与批处理的唯一差别在覆盖信息：流式写入 ``source_rows_ranges`` 而不是 ``source_rows``，
    并用 ``streaming``/``streamed_rows`` 标注来源。数值结果由测试逐位校验为相同。
    """
    chosen = features or ["mean", "std", "rms"]

    # 时间窗口与未来视野标签都需要"整组 + 它的未来"，而流式是按块看的，看不到未来。
    # 与其给出一个看起来能跑、标签却是错的流式结果，不如在这里明确拒绝。
    if label_policy == "horizon" or parse_duration(window_span, name="window_span"):
        raise ValueError(
            "window_span / label_policy=horizon need the whole group and its future: "
            "turn streaming off on data.input, or insert data.materialize"
        )
    if parse_duration(prediction_horizon, name="prediction_horizon") or parse_duration(
        prediction_gap, name="prediction_gap"
    ):
        raise ValueError("prediction_horizon/prediction_gap need label_policy=horizon")

    def build_row(window: pd.DataFrame, cols: list[str]) -> dict[str, float]:
        return _feature_row(window, cols, kind, chosen, quantile, degree, fitting_method, time_column)

    return _stream_windows(
        chunks,
        columns,
        build_row,
        group_column,
        label_column,
        time_column,
        window_size,
        step,
        label_policy,
        attrs or {},
        asset_column=asset_column,
        on_progress=on_progress,
    )


def spectral_stream(
    chunks: Iterable[pd.DataFrame],
    columns: list[str],
    sampling_rate: float,
    features: list[str] | None = None,
    band_edges: list[float] | None = None,
    harmonic_tolerance: float = 0.02,
    flat_policy: str = "nan",
    flat_threshold: float = 0.0,
    group_column: str | None = None,
    label_column: str | None = None,
    time_column: str | None = None,
    window_size: int = 0,
    step: int = 0,
    label_policy: str = "strict",
    asset_column: str | None = None,
    attrs: dict[str, Any] | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """流式版 :func:`spectral`，输出 schema 完全一致。

    参数校验（采样率、频带边界、平窗口策略、阈值）与批处理版逐条对应；
    平窗口计数在块之间累积，最后统一转成警告，因此流式执行也能报告"哪些通道没有可用频谱"。
    """
    if not isinstance(sampling_rate, (int, float)) or isinstance(sampling_rate, bool):
        raise ValueError("Sampling rate must be numeric")
    if not np.isfinite(sampling_rate) or sampling_rate <= 0:
        raise ValueError("Sampling rate must be a positive, finite number of samples per second")
    if not 0 <= harmonic_tolerance <= 0.5:
        raise ValueError("Harmonic tolerance must be between 0 and 0.5")
    edges = [float(edge) for edge in ([0.25, 0.5] if band_edges is None else band_edges)]
    if any(not np.isfinite(edge) or not 0 < edge < 1 for edge in edges):
        raise ValueError("Band edges must be fractions of the Nyquist frequency between 0 and 1")
    if edges != sorted(set(edges)):
        raise ValueError("Band edges must increase without duplicates")
    if flat_policy not in {"nan", "skip", "error"}:
        raise ValueError("flat_policy must be one of nan, skip, error")
    if not np.isfinite(flat_threshold) or flat_threshold < 0:
        raise ValueError("flat_threshold must be a non-negative, finite number")
    chosen = features or ["dominant_frequency", "spectral_centroid", "spectral_entropy", "band_energy_ratio"]
    unknown = set(chosen) - set(SPECTRAL)
    if unknown:
        raise ValueError(f"Unknown spectral features: {sorted(unknown)}")

    flat_counts: dict[str, int] = {}
    # 用单元素列表当可变计数器：闭包内要改写，且需要在流结束后读取最终值。
    window_count = [0]

    def build_row(window: pd.DataFrame, cols: list[str]) -> dict[str, float] | None:
        """按 flat_policy 决定是报错、跳过（返回 None）还是输出 NaN 行。"""
        window_count[0] += 1
        series = {col: window[col].to_numpy(dtype=float) for col in cols}
        flat_here = _flat_columns(series, flat_threshold)
        if flat_here:
            for col in flat_here:
                flat_counts[col] = flat_counts.get(col, 0) + 1
            if flat_policy == "error":
                raise ValueError(
                    f"Flat (constant) window for {flat_here}; a spectrum needs variation. "
                    "Use flat_policy=nan (keeps row alignment) or skip, or drop quantised channels."
                )
            if flat_policy == "skip":
                # 返回 None 让 _stream_windows 丢弃该窗口（注意：只在不与其它分支合并时才安全）。
                return None
        row: dict[str, float] = {}
        for col in cols:
            values = series[col]
            if col in flat_here:
                for name in _spectral_names(chosen, edges):
                    row[f"{col}__{name}"] = float("nan")
                continue
            row.update(
                {
                    f"{col}__{name}": value
                    for name, value in _spectral_row(
                        values, float(sampling_rate), chosen, edges, harmonic_tolerance
                    ).items()
                }
            )
        return row

    outputs = _stream_windows(
        chunks,
        columns,
        build_row,
        group_column,
        label_column,
        time_column,
        window_size,
        step,
        label_policy,
        attrs or {},
        min_window_rows=MIN_SPECTRAL_SAMPLES,
        asset_column=asset_column,
        on_progress=on_progress,
    )
    outputs["warnings"] = _flat_warnings(flat_counts, window_count[0])
    return outputs


def _spectrum(
    values: np.ndarray, sampling_rate: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """对单个窗口做加窗 FFT，返回频率轴、幅值谱、原始频谱与所用窗。

    三个数值细节：

    * 先减去均值，避免直流分量压过幅值类特征；
    * 用 Hann 窗抑制泄漏，并按"相干增益"（窗函数之和）归一化，使单音幅值可还原；
    * 直流与 Nyquist 频点只对应单边分量，幅值除以 2，其余频点乘 2。
    """
    taper = np.hanning(values.size)
    coherent_gain = float(taper.sum())
    # Removing the mean keeps the DC bin from dominating amplitude-based features.
    spectrum = np.fft.rfft((values - values.mean()) * taper)
    frequencies = np.fft.rfftfreq(values.size, d=1.0 / sampling_rate)
    amplitude = 2.0 * np.abs(spectrum) / coherent_gain
    amplitude[0] /= 2  # DC is not a doubled two-sided component.
    if values.size % 2 == 0:
        amplitude[-1] /= 2  # Nyquist bin is likewise single-sided.
    return frequencies, amplitude, spectrum, taper


def _signal_power(spectrum: np.ndarray, taper: np.ndarray) -> float:
    """按 Parseval 关系还原原始（未加窗）信号的均方值，即谱 RMS 的平方。

    直流与 Nyquist 箱计一次，其余箱计两次（单边谱的补偿）；分母含窗能量，
    因此结果与窗函数无关，可跨窗口长度比较。
    """
    interior = spectrum[1:-1] if taper.size % 2 == 0 else spectrum[1:]
    total = float(np.abs(spectrum[0]) ** 2) + 2.0 * float(np.sum(np.abs(interior) ** 2))
    if taper.size % 2 == 0:
        total += float(np.abs(spectrum[-1]) ** 2)
    return total / (taper.size * float(np.sum(taper**2)))


def _spectral_row(
    values: np.ndarray,
    sampling_rate: float,
    chosen: list[str],
    band_edges: list[float],
    harmonic_tolerance: float,
) -> dict[str, float]:
    """计算一个窗口的全部频域指标，并只返回 ``chosen`` 里要的列。

    指标含义：主频与主幅值（幅值谱峰值）、谱质心与谱展宽（以能量为权重的一阶/二阶矩）、
    谱熵（能量分布的集中度，已按 ``log(箱数)`` 归一化到 0~1）、
    高频能量比（Nyquist 一半以上）、谐波能量比（主频的 2~5 次谐波附近能量占比，
    容差由 ``harmonic_tolerance`` 给出）、频带能量比（``band_edges`` 是 Nyquist 的分数边界）。
    """
    frequencies, amplitude, spectrum, taper = _spectrum(values, sampling_rate)
    power = np.abs(spectrum) ** 2
    # The mean is removed before the FFT, so the DC bin carries no fault-relevant energy.
    shape = np.where(frequencies > 0, power, 0.0)
    energy = float(shape.sum())
    if energy <= 0:
        # 能量为 0 说明整个窗口没有任何变化（平窗口的极端情形），频域特征无定义。
        raise ValueError("Spectral features need a signal with non-zero variance")
    weights = shape / energy
    peak = int(np.argmax(np.where(frequencies > 0, amplitude, -np.inf)))
    centroid = float(np.sum(frequencies * weights))
    positive = weights[weights > 0]
    nyquist = sampling_rate / 2
    harmonics = np.zeros_like(frequencies, dtype=bool)
    for order in range(2, 6):
        # 只统计 2~5 次谐波：更高次通常已接近噪声，且容易被混叠干扰。
        target = order * frequencies[peak]
        if 0 < target < nyquist:
            harmonics |= np.abs(frequencies - target) <= harmonic_tolerance * target
    band_energy = []
    boundaries = [0.0, *band_edges, 1.0]
    for low, high in zip(boundaries[:-1], boundaries[1:], strict=True):
        # 边界以 Nyquist 的分数给出，因此同一套参数在任意采样率下含义一致。
        band = (frequencies > low * nyquist) & (frequencies <= high * nyquist)
        band_energy.append(float(shape[band].sum() / energy))
    row = {
        "dominant_frequency": float(frequencies[peak]),
        "dominant_amplitude": float(amplitude[peak]),
        "spectral_centroid": centroid,
        "spectral_spread": float(np.sqrt(np.sum((frequencies - centroid) ** 2 * weights))),
        "spectral_entropy": float(-np.sum(positive * np.log(positive)) / np.log(positive.size)),
        "spectral_rms": float(np.sqrt(_signal_power(spectrum, taper))),
        "high_frequency_ratio": float(shape[frequencies > nyquist / 2].sum() / energy),
        "harmonic_ratio": float(shape[harmonics].sum() / energy),
        "band_energy_ratio": band_energy,
    }
    flat: dict[str, float] = {}
    for name in chosen:
        if name == "band_energy_ratio":
            flat.update({f"band_energy_ratio_{i}": value for i, value in enumerate(band_energy)})
        else:
            flat[name] = row[name]
    return flat


def _flat_columns(values: dict[str, np.ndarray], flat_threshold: float) -> list[str]:
    """Columns whose window cannot carry a spectrum.

    Two real cases: the window is constant (held or quantised tag), or its only variation
    sits where the Hann taper is zero — typically an isolated spike in the first or last
    sample. The second case leaves no windowed energy at all, so a spectrum is undefined.
    """
    flat: list[str] = []
    for name, window in values.items():
        if float(np.ptp(window)) <= flat_threshold:
            flat.append(name)
            continue
        taper = np.hanning(window.size)
        centred = window - window.mean()
        scale = float(np.max(np.abs(centred))) or 1.0
        energy = float(np.sum((centred * taper) ** 2))
        # 幅值不为 0 但"加窗后几乎没能量"的窗口同样没有可用频谱：
        # 典型是孤立尖峰恰好落在窗两端（Hann 窗在那里为 0）。
        # 阈值用相对量（scale² × 窗长 × 1e-18）而不是绝对值，保证与信号幅度无关。
        if energy <= (1e-9 * scale) ** 2 * window.size:
            flat.append(name)
    return flat


def spectral(
    data: pd.DataFrame,
    columns: list[str],
    sampling_rate: float,
    features: list[str] | None = None,
    band_edges: list[float] | None = None,
    harmonic_tolerance: float = 0.02,
    flat_policy: str = "nan",
    flat_threshold: float = 0.0,
    asset_column: str | None = None,
    group_column: str | None = None,
    label_column: str | None = None,
    time_column: str | None = None,
    window_size: int = 0,
    step: int = 0,
    label_policy: str = "strict",
    window_span: str | float = "",
    step_span: str | float = "",
    prediction_horizon: str | float = "",
    prediction_gap: str | float = "",
    current_fault_policy: str = "drop",
    normal_label: str = "0",
) -> dict[str, Any]:
    """频域特征提取：每个窗口做一次加窗 FFT，产出的标签与其它窗口组件同构。

    参数校验先于任何计算：``sampling_rate`` 必须为正的有限数（**没有默认值**，
    因为物理频率完全取决于它）；``band_edges`` 必须是 (0,1) 内递增的 Nyquist 分数；
    ``flat_policy`` 取 ``nan``（默认，保持行对齐）/``skip``/``error``。

    平窗口（恒值，或只有窗两端有变化）按策略处理：``nan`` 会输出与正常行同构的 NaN 行，
    以保证后续 ``feature.merge`` 与标签对齐不被破坏；``skip`` 会真正丢行（只在没有其它分支
    需要合并时才安全）；``error`` 恢复硬失败。无论哪种策略，warning 都会报告每个通道的平窗口数。
    """
    if not isinstance(sampling_rate, (int, float)) or isinstance(sampling_rate, bool):
        raise ValueError("Sampling rate must be numeric")
    if not np.isfinite(sampling_rate) or sampling_rate <= 0:
        raise ValueError("Sampling rate must be a positive, finite number of samples per second")
    if not 0 <= harmonic_tolerance <= 0.5:
        raise ValueError("Harmonic tolerance must be between 0 and 0.5")
    edges = [float(edge) for edge in ([0.25, 0.5] if band_edges is None else band_edges)]
    if any(not np.isfinite(edge) or not 0 < edge < 1 for edge in edges):
        raise ValueError("Band edges must be fractions of the Nyquist frequency between 0 and 1")
    if edges != sorted(set(edges)):
        raise ValueError("Band edges must increase without duplicates")
    if flat_policy not in {"nan", "skip", "error"}:
        raise ValueError("flat_policy must be one of nan, skip, error")
    if not np.isfinite(flat_threshold) or flat_threshold < 0:
        raise ValueError("flat_threshold must be a non-negative, finite number")
    chosen = features or ["dominant_frequency", "spectral_centroid", "spectral_entropy", "band_energy_ratio"]
    unknown = set(chosen) - set(SPECTRAL)
    if unknown:
        raise ValueError(f"Unknown spectral features: {sorted(unknown)}")
    cols = numeric_columns(data, columns)
    asset_of = group_assets(data, group_column, asset_column) if asset_column and group_column else None
    if label_column in cols or group_column in cols:
        raise ValueError("Label/group columns cannot be feature inputs")
    if not np.isfinite(data[cols].to_numpy(dtype=float, copy=False)).all():
        raise ValueError("Spectral extraction requires finite numeric values")
    span, stride, horizon, gap = window_arguments(
        window_size=window_size,
        window_span=window_span,
        step_span=step_span,
        label_policy=label_policy,
        prediction_horizon=prediction_horizon,
        prediction_gap=prediction_gap,
        current_fault_policy=current_fault_policy,
        time_column=time_column,
        label_column=label_column,
    )
    counters = {"current_fault": 0, "unknown_future": 0}
    rows, labels, keys, group_ids, coverage = [], [], [], [], []
    flat_counts = {col: 0 for col in cols}
    window_count = 0
    for key, chunk, group, source_rows, ready_label in prepared_windows(
        data,
        group_column,
        label_column,
        time_column,
        window_size,
        step,
        label_policy,
        span,
        stride,
        horizon,
        gap,
        current_fault_policy,
        normal_label,
        counters,
    ):
        if len(chunk) < MIN_SPECTRAL_SAMPLES:
            # 样本太少的窗口分辨不出有意义的频谱，直接拒绝而不是给出噪声结果。
            raise ValueError(f"Spectral extraction needs at least {MIN_SPECTRAL_SAMPLES} samples per window")
        window_count += 1
        series = {col: chunk[col].to_numpy(dtype=float) for col in cols}
        flat_here = _flat_columns(series, flat_threshold)
        if flat_here:
            for col in flat_here:
                flat_counts[col] += 1
            if flat_policy == "error":
                raise ValueError(
                    f"Flat (constant) window for {flat_here}; a spectrum needs variation. "
                    "Use flat_policy=nan (keeps row alignment) or skip, or drop quantised channels."
                )
            if flat_policy == "skip":
                continue
        row: dict[str, float] = {}
        for col in cols:
            values = series[col]
            if col in flat_here:
                # NaN keeps row alignment with the other feature branches, so merge and
                # validation still line up; a skipped row would silently break them.
                # 中文注：这一点是刻意的设计——平窗口保留、值置 NaN，
                # 让"哪些窗口没有频谱"以数据的形式可见，而不是消失无踪。
                for name in _spectral_names(chosen, edges):
                    row[f"{col}__{name}"] = float("nan")
                continue
            row.update(
                {
                    f"{col}__{name}": value
                    for name, value in _spectral_row(
                        values, float(sampling_rate), chosen, edges, harmonic_tolerance
                    ).items()
                }
            )
        if label_column:
            labels.append(
                ready_label if ready_label is not None else _window_label(chunk, label_column, label_policy)
            )
        rows.append(row)
        keys.append(key)
        group_ids.append(group)
        coverage.append(source_rows)
    assemble_size, assemble_step, overlapping = window_shape(span, stride, window_size, step)
    attrs = window_attrs(
        data.attrs,
        label_policy=label_policy,
        span=span,
        stride=stride,
        horizon=horizon,
        gap=gap,
        current_fault_policy=current_fault_policy,
        counters=counters,
    )
    attrs["warnings"] = _flat_warnings(flat_counts, window_count)
    outputs = _assemble(
        rows,
        keys,
        labels,
        group_ids,
        coverage,
        attrs,
        group_column,
        label_column,
        assemble_size,
        assemble_step,
        overlapping=overlapping,
    )
    outputs["warnings"] = _flat_warnings(flat_counts, window_count)
    attach_assets(outputs, asset_of, group_ids)
    return outputs


def _categorical_values(data: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """取类别列并统一成字符串，缺失值填成 ``<missing>``。

    统一成字符串是为了让"类别集合"在训练与推理之间可比（1 与 "1" 不应算两个类别）；
    缺失值显式编码而不是丢弃，因为"这条记录缺类别"本身就是信息。
    """
    if not columns or len(columns) != len(set(columns)):
        raise ValueError("Select at least one unique categorical column")
    missing = set(columns) - set(data.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")
    return data[columns].fillna(MISSING_CATEGORY).astype(str)


@dataclass(frozen=True)
class CategoricalEncoder:
    """Fitted categorical mappings with a deterministic inference schema.

    The object contains plain Python state, so it survives the platform's deepcopy,
    checkpoint and pickle-based artifact spill paths. ``transform`` never refits on
    inference data: missing training categories keep their columns, and unseen values
    follow ``handle_unknown``.

    内部只保存纯 Python 结构（tuple/dict/float），因此可以被 deepcopy、检查点快照与
    pickle 磁盘溢写安全地搬运；推理期只做 transform、绝不重新 fit，
    输出列严格按训练时的顺序与集合重建，保证同一模型在不同批次上的列模式一致。
    """

    columns: tuple[str, ...]
    method: str
    handle_unknown: str
    categories: dict[str, tuple[str, ...]]
    output_columns: tuple[str, ...]
    frequencies: dict[str, dict[str, float]]
    counts: dict[str, dict[str, float]]
    target_means: dict[str, dict[str, float]]
    target_global_mean: float | None = None

    def signature(self) -> tuple[Any, ...]:
        """用于去重的身份签名：同一组输入列 + 方法 + 输出列视为同一个编码器。"""
        return self.columns, self.method, self.output_columns

    def describe(self) -> dict[str, Any]:
        """给 summarize()/UI 用的可读描述（不包含大字典）。"""
        return {
            "method": self.method,
            "input_columns": list(self.columns),
            "output_columns": list(self.output_columns),
            "category_counts": {column: len(values) for column, values in self.categories.items()},
            "handle_unknown": self.handle_unknown,
        }

    def _unknown(self, values: pd.DataFrame) -> dict[str, list[str]]:
        """找出训练时没见过的类别值（每列最多列出 10 个，够定位问题即可）。"""
        unknown: dict[str, list[str]] = {}
        for column in self.columns:
            extra = sorted(set(values[column].unique()) - set(self.categories[column]))
            if extra:
                unknown[column] = extra[:10]
        return unknown

    def transform(self, data: pd.DataFrame) -> pd.DataFrame:
        """按已拟合的映射编码新数据（绝不重新拟合）。

        ``handle_unknown="error"`` 时遇到新类别直接报错；``"ignore"`` 时按各方法的默认值处理：
        独热为全 0、序数为 -1、频率/计数为 0、目标编码为训练集全局均值。
        最后按 ``output_columns`` 重排，确保列集合与顺序和训练时完全一致。
        """
        values = _categorical_values(data, list(self.columns))
        unknown = self._unknown(values)
        if unknown and self.handle_unknown == "error":
            raise ValueError(f"Unknown categories at transform time: {unknown}")

        if self.method == "onehot":
            result = pd.get_dummies(values, dtype=float)
            # 用训练列集合 reindex：训练中出现过但本批未出现的类别补 0，
            # 本批新出现的类别（handle_unknown=ignore）在这里被自然丢弃。
            if not result.columns.is_unique:
                raise ValueError("Categorical column names and values produce duplicate one-hot features")
            result = result.reindex(columns=list(self.output_columns), fill_value=0.0).astype(float)
        else:
            result = pd.DataFrame(index=data.index)
            for column in self.columns:
                series = values[column]
                if self.method == "ordinal":
                    # 序数映射沿用训练时的类别排序（categories 已 sorted），保证跨批次一致。
                    mapping = {value: index for index, value in enumerate(self.categories[column])}
                    result[f"{column}__ordinal"] = series.map(mapping).fillna(-1).astype("int64")
                elif self.method in {"frequency", "category_statistics"}:
                    result[f"{column}__frequency"] = (
                        series.map(self.frequencies[column]).fillna(0.0).astype(float)
                    )
                    if self.method == "category_statistics":
                        result[f"{column}__count"] = series.map(self.counts[column]).fillna(0.0).astype(float)
                elif self.method == "target":
                    result[f"{column}__target"] = (
                        series.map(self.target_means[column]).fillna(self.target_global_mean).astype(float)
                    )
                else:  # defensive guard for manually constructed/persisted objects
                    raise ValueError(f"Unknown categorical method: {self.method}")
            result = result.loc[:, list(self.output_columns)]

        result.attrs = dict(data.attrs)
        # 把自身登记进 attrs：合并/验证器据此把编码器嵌进训练好的模型。
        encoders = list(result.attrs.get("categorical_encoders", []))
        known = {encoder.signature() for encoder in encoders if isinstance(encoder, CategoricalEncoder)}
        if self.signature() not in known:
            encoders.append(self)
        result.attrs["categorical_encoders"] = encoders
        return result


def fit_categorical_encoder(
    data: pd.DataFrame,
    columns: list[str],
    method: str = "onehot",
    target_column: str | None = None,
    handle_unknown: str = "ignore",
) -> CategoricalEncoder:
    """在训练数据上拟合编码器（各类方法的输出列名在下面确定）。

    * ``onehot``：每个（列, 取值）组合一列，列名由 pandas 生成；
    * ``ordinal``：``<列>__ordinal``，取值按类别名排序后映射为序号；
    * ``frequency``：``<列>__frequency``，取该类别在训练集中的频率；
    * ``category_statistics``：频率 + 出现次数两列；
    * ``target``：``<列>__target``，类别对应的目标均值（全局均值为未见类别的回退），
      属于有监督编码，需要在训练折内拟合——见 :func:`fit_categorical` 的 OOF 处理。
    """
    if method not in CATEGORICAL_METHODS:
        raise ValueError(f"Unknown categorical method: {method}")
    if handle_unknown not in UNKNOWN_CATEGORY_POLICIES:
        raise ValueError(f"handle_unknown must be one of {UNKNOWN_CATEGORY_POLICIES}")
    if target_column in columns:
        raise ValueError("Target cannot be encoded as an input")
    values = _categorical_values(data, columns)
    # 类别集合排序后保存：让序号映射与输出列顺序在任何批次上都可复现。
    categories = {column: tuple(sorted(values[column].unique())) for column in columns}
    frequencies = {
        column: {str(key): float(value) for key, value in values[column].value_counts(normalize=True).items()}
        for column in columns
    }
    counts = {
        column: {str(key): float(value) for key, value in values[column].value_counts().items()}
        for column in columns
    }
    target_means: dict[str, dict[str, float]] = {}
    target_global_mean = None

    if method == "onehot":
        encoded = pd.get_dummies(values, dtype=float)
        if not encoded.columns.is_unique:
            raise ValueError("Categorical column names and values produce duplicate one-hot features")
        output_columns = tuple(str(column) for column in encoded.columns)
    elif method == "ordinal":
        output_columns = tuple(f"{column}__ordinal" for column in columns)
    elif method == "frequency":
        output_columns = tuple(f"{column}__frequency" for column in columns)
    elif method == "category_statistics":
        output_columns = tuple(
            name for column in columns for name in (f"{column}__frequency", f"{column}__count")
        )
    else:
        if not target_column or len(data) < 4:
            # 目标编码在样本过少时几乎等于把标签直接抄进特征，因此设最低样本量。
            raise ValueError("Target encoding requires a numeric target and at least four rows")
        if target_column not in data.columns:
            raise ValueError(f"Missing target column: {target_column}")
        target = pd.to_numeric(data[target_column], errors="raise")
        if not np.isfinite(target.to_numpy(dtype=float, copy=False)).all():
            raise ValueError("Target encoding requires a finite numeric target")
        target_global_mean = float(target.mean())
        for column in columns:
            means = target.groupby(values[column]).mean()
            target_means[column] = {str(key): float(value) for key, value in means.items()}
        output_columns = tuple(f"{column}__target" for column in columns)

    return CategoricalEncoder(
        tuple(columns),
        method,
        handle_unknown,
        categories,
        output_columns,
        frequencies,
        counts,
        target_means,
        target_global_mean,
    )


def fit_categorical(
    data: pd.DataFrame,
    columns: list[str],
    method: str = "onehot",
    target_column: str | None = None,
    random_state: int = 42,
    handle_unknown: str = "ignore",
) -> dict[str, Any]:
    """拟合一次，返回训练特征与可复用的编码器。

    非目标编码直接 ``encoder.transform(data)``；目标编码用 **K 折 OOF（out-of-fold）**
    生成训练特征——每个折的均值只用其它折算，避免"用自己的标签编码自己"。
    编码器本身仍保存全量映射，供将来对推理数据做纯 transform。
    返回值整体标记为探索性（``mark_fitted``），因为编码过程看过全部标签。
    """
    encoder = fit_categorical_encoder(data, columns, method, target_column, handle_unknown)
    if method != "target":
        result = encoder.transform(data)
    else:
        values = _categorical_values(data, columns)
        target = pd.to_numeric(data[target_column], errors="raise")
        result = pd.DataFrame(index=data.index)
        for column in columns:
            encoded = pd.Series(index=data.index, dtype=float)
            # Preserve the previous out-of-fold training behaviour while the encoder
            # retains full training mappings for future transform-only inference.
            for train, test in KFold(min(5, len(data)), shuffle=True, random_state=random_state).split(data):
                means = target.iloc[train].groupby(values[column].iloc[train]).mean()
                encoded.iloc[test] = values[column].iloc[test].map(means).fillna(target.iloc[train].mean())
            result[f"{column}__target"] = encoded
        result.attrs = dict(data.attrs)
        result.attrs["categorical_encoders"] = [encoder]
    return {"features": mark_fitted(result, f"categorical.{method}"), "encoder": encoder}


def categorical(
    data: pd.DataFrame,
    columns: list[str],
    method: str = "onehot",
    target_column: str | None = None,
    random_state: int = 42,
    handle_unknown: str = "ignore",
) -> pd.DataFrame:
    """只返回训练特征的兼容入口（等价于 ``fit_categorical(...)["features"]``）。"""
    return fit_categorical(data, columns, method, target_column, random_state, handle_unknown)["features"]


def carried_columns_notice(columns: list[str]) -> str:
    """``keep_columns`` 的警告原文（核心库与组件层共用一份，避免两处说法漂移）。"""
    return (
        f"keep_columns={list(columns)} are carried through unchanged so a window component can "
        "group/time/label by them; they are NOT model inputs — sending this table straight to a "
        "validation component would leak the label or the equipment identity."
    )


def carry_columns(encoded: pd.DataFrame, source: pd.DataFrame, columns: list[str] | None) -> pd.DataFrame:
    """把原始列**原样**带进编码后的表，供下游窗口组件按它们分组/切窗。

    为什么需要这一步：``feature.categorical`` 的输出只有编码列（独热/序数/频率…），
    实体列、时间列、标签列全被丢掉——于是"先编码、再按窗口聚合"这条路在平台里是断的
    （2026-09-18 实测：`feature.merge` 也合不了，因为行数与来源对不上）。把需要的列带过去，
    窗口组件就有得可依。

    两条硬规矩：

    * 列必须真的存在，且不能与编码列重名——重名会静默覆盖掉编码结果；
    * 索引必须对齐（编码保持原索引），否则 pandas 会悄悄填 NaN。

    带过去的列只该用于下游的 ``group_column``/``time_column``/``label_column``。它们会被写进
    ``evaluation_warnings`` 一路传到模型指标里：直接把它们当特征喂给验证组件就是标签泄漏。
    """
    if not columns:
        return encoded
    missing = [name for name in columns if name not in source.columns]
    if missing:
        raise ValueError(f"keep_columns not found in the data: {missing}")
    clashes = [name for name in columns if name in encoded.columns]
    if clashes:
        raise ValueError(f"keep_columns collide with encoded columns: {clashes}")
    if not encoded.index.equals(source.index):
        raise ValueError("keep_columns requires the encoded table to share the source row index")
    for name in columns:
        encoded[name] = source[name]
    encoded.attrs["evaluation_warnings"] = [
        *encoded.attrs.get("evaluation_warnings", []),
        carried_columns_notice(list(columns)),
    ]
    return encoded


def merge_features(left: pd.DataFrame, right: pd.DataFrame) -> pd.DataFrame:
    """按列合并两条特征分支（横向拼接），并强制校验来源一致。

    校验顺序与理由：

    1. 索引必须完全相同——否则拼出来的行对不上；
    2. ``source_rows`` 等 provenance 必须一致——否则两边的窗口不是同一批窗口；
    3. ``groups``/``assets``/``source_path``/``source_id`` 必须一致——否则分组或资产划分将失真；
    4. 列名不能重复（重叠即报错，让使用者显式选择或改名）。

    合并后的 attrs 取左侧为基准，并去重合并两侧的探索性警告与类别编码器。
    """
    if not left.index.equals(right.index):
        raise ValueError("Feature indices must match exactly")
    if not provenance_matches(left.attrs, right.attrs):
        raise ValueError("Feature provenance differs: source_rows")
    for key in ("groups", "assets", "source_path", "source_id"):
        if left.attrs.get(key) != right.attrs.get(key):
            raise ValueError(f"Feature provenance differs: {key}")
    if set(left.columns) & set(right.columns):
        raise ValueError("Feature names overlap; rename before merging")
    result = pd.concat([left, right], axis=1)
    result.attrs = dict(left.attrs)
    # 两侧的泄漏警告都要保留：任何一侧做过全量拟合，合并结果就带探索性。
    result.attrs["evaluation_warnings"] = list(
        dict.fromkeys(left.attrs.get("evaluation_warnings", []) + right.attrs.get("evaluation_warnings", []))
    )
    encoders: list[CategoricalEncoder] = []
    # 按 signature 去重：同一编码器出现在两侧时只保留一份。
    signatures: set[tuple[Any, ...]] = set()
    for encoder in [
        *left.attrs.get("categorical_encoders", []),
        *right.attrs.get("categorical_encoders", []),
    ]:
        if not isinstance(encoder, CategoricalEncoder) or encoder.signature() in signatures:
            continue
        encoders.append(encoder)
        signatures.add(encoder.signature())
    if encoders:
        result.attrs["categorical_encoders"] = encoders
    return result
