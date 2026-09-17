"""Additional tabular preparation operations used by built-in components.

补齐 ``data.*`` 组件需要的表格操作：填充、重采样、切分、邻近值、二值化。
与 :mod:`fault_core.data` 的分工是——那里是"行列基本操作"，这里涉及时间轴、
分组边界与训练/测试划分，因此每处都需要显式声明分组列与时间列。

统一约定：返回新表并继承 ``attrs``（见 :func:`_with_attrs`），
这样下游的 provenance 校验与探索性警告都能继续传递。
"""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd
from sklearn.model_selection import GroupShuffleSplit, train_test_split
from sklearn.preprocessing import KBinsDiscretizer, PolynomialFeatures

from fault_core.data import numeric_columns
from fault_core.preprocessing import mark_fitted


def _with_attrs(source: pd.DataFrame, result: pd.DataFrame) -> pd.DataFrame:
    """把源表的 ``attrs`` 复制到结果表上。

    pandas 的多数运算会丢掉 ``attrs``，而它承载着窗口来源与警告信息，
    因此凡是"新建"过 DataFrame 的分支都要显式调一次这里。
    """
    result.attrs = dict(source.attrs)
    return result


def impute(
    data: pd.DataFrame,
    columns: list[str] | None = None,
    method: str = "mean",
    group_column: str | None = None,
    interpolation_method: str = "linear",
) -> pd.DataFrame:
    """填充数值列缺失值。

    ``mean``：按 ``group_column`` 分组求均值填充（同一台设备的均值），组内仍缺则回退到整列
    均值；这是"全表拟合"操作，因此打上探索性警告。
    ``max``/``min``：同样的回退顺序，但用组内（再退到整列）极值填充——适合"越限即异常"的
    通道：宁可用边界值兜底，也不要用均值把缺口抹平；同样属于拟合操作，会被标记。
    ``interpolate``：按行序在组内做线性/最近邻插值（``limit_direction="both"`` 允许向两端
    外推边界缺失）；插值只依赖邻点，不引入全表统计量，因此不标记。
    整列都是缺失值时无法填充，直接报错让使用者自己决定删列。
    """
    cols = numeric_columns(data, columns)
    result = data.copy()
    if group_column and group_column not in data.columns:
        raise ValueError(f"Missing group column: {group_column}")
    if method in {"mean", "max", "min"}:
        fallback = data[cols].mean() if method == "mean" else getattr(data[cols], method)()
        if group_column:
            # transform 保持原索引与行数，可以直接参与 fillna。
            values = data.groupby(group_column, sort=False, dropna=False)[cols].transform(method)
            result[cols] = data[cols].fillna(values).fillna(fallback)
        else:
            result[cols] = data[cols].fillna(fallback)
        if result[cols].isna().any().any():
            raise ValueError(f"{method.capitalize()} imputation cannot fill an all-missing column")
        return mark_fitted(_with_attrs(data, result), f"{method} imputation")
    if method != "interpolate":
        raise ValueError(f"Unknown imputation method: {method}")
    if interpolation_method not in {"linear", "nearest"}:
        raise ValueError(f"Unknown interpolation method: {interpolation_method}")
    if group_column:
        # 逐组插值：跨组插值会把上一台设备的尾值接到下一台的开头，属于静默的数据污染。
        for _, indices in data.groupby(group_column, sort=False, dropna=False).groups.items():
            result.loc[indices, cols] = data.loc[indices, cols].interpolate(
                method=interpolation_method, limit_direction="both"
            )
    else:
        result[cols] = data[cols].interpolate(method=interpolation_method, limit_direction="both")
    if result[cols].isna().any().any():
        raise ValueError("Interpolation cannot fill a column containing only missing values")
    return _with_attrs(data, result)


def time_resample(
    data: pd.DataFrame,
    time_column: str,
    frequency: str,
    columns: list[str] | None = None,
    aggregation: str = "mean",
    group_column: str | None = None,
    fill_method: str = "none",
) -> pd.DataFrame:
    """把数值信号重采样到固定时间格点上，可按设备分组。

    流程是：解析时间列 → （分组）按时间排序 → ``resample(frequency).agg(aggregation)`` →
    可选缺口填充 → 还原为普通列。``fill_method`` 支持 ``none``/``interpolate``（按时间插值）/
    ``ffill``（前向填充后再后向补齐）。

    重采样会改变行集合，所以必须在**窗口切分之前**做；跨组的行不会被合并，
    空输入与非法频率都会报错而不是返回空表。
    """
    if time_column not in data.columns:
        raise ValueError(f"Missing time column: {time_column}")
    if group_column and group_column not in data.columns:
        raise ValueError(f"Missing group column: {group_column}")
    cols = numeric_columns(data, columns)
    if time_column in cols:
        # 时间列本身不参与聚合，否则它的数值形式会被一起求均值。
        cols.remove(time_column)
    if group_column in cols:
        cols.remove(group_column)
    if not cols:
        raise ValueError("Select at least one numeric value column to resample")
    work = data[[*([group_column] if group_column else []), time_column, *cols]].copy()
    work[time_column] = pd.to_datetime(work[time_column], errors="raise")

    def one(frame: pd.DataFrame) -> pd.DataFrame:
        """对单组做排序 + 重采样 + 填充，返回带时间列的表。"""
        sampled = (
            frame.sort_values(time_column, kind="stable")
            .set_index(time_column)[cols]
            .resample(frequency)
            .agg(aggregation)
        )
        if fill_method == "interpolate":
            sampled = sampled.interpolate(method="time", limit_direction="both")
        elif fill_method == "ffill":
            sampled = sampled.ffill().bfill()
        elif fill_method != "none":
            raise ValueError(f"Unknown fill method: {fill_method}")
        return sampled.reset_index()

    try:
        if group_column:
            # 逐组重采样后拼回一张表：避免把不同设备的时间点混到同一个格点上。
            frames = []
            for group, frame in work.groupby(group_column, sort=False, dropna=False):
                sampled = one(frame)
                sampled.insert(0, group_column, group)
                frames.append(sampled)
            result = pd.concat(frames, ignore_index=True) if frames else work.iloc[0:0].copy()
        else:
            result = one(work)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid resampling configuration: {exc}") from exc
    if result.empty:
        raise ValueError("Resampling produced no rows")
    return _with_attrs(data, result)


def split_data(
    data: pd.DataFrame,
    method: str = "random",
    test_size: float = 0.25,
    random_state: int = 42,
    group_column: str | None = None,
    stratify_column: str | None = None,
) -> dict[str, pd.DataFrame]:
    """按三种方式切分数据，返回带划分元数据的 ``{"train": …, "test": …}``。

    ``temporal``：按现有行序取前段训练、后段测试（调用方需先按业务时间排好序）；
    ``group``：``GroupShuffleSplit`` 按 ``group_column`` 整组划分，避免同一设备同时出现在两侧；
    ``random``：``train_test_split``，可选 ``stratify_column`` 分层。

    每个输出表都会写入 ``split_method`` / ``split_role`` / ``source_indices``，
    便于事后核对"哪些行进了测试集"。注意：这是数据层面的快速切分，
    模型评估应优先使用验证器自带的 ``split_method``（它会额外处理重叠窗口与资产留出）。
    """
    if len(data) < 2:
        raise ValueError("Data split requires at least two rows")
    positions = np.arange(len(data))
    if method == "temporal":
        boundary = int(len(data) * (1 - test_size))
        train, test = positions[:boundary], positions[boundary:]
    elif method == "group":
        if not group_column or group_column not in data.columns:
            raise ValueError("Group split requires group_column")
        train, test = next(
            GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=random_state).split(
                data, groups=data[group_column]
            )
        )
    elif method == "random":
        stratify = None
        if stratify_column:
            if stratify_column not in data.columns:
                raise ValueError(f"Missing stratify column: {stratify_column}")
            stratify = data[stratify_column]
        train, test = train_test_split(
            positions, test_size=test_size, random_state=random_state, stratify=stratify
        )
    else:
        raise ValueError(f"Unknown split method: {method}")
    outputs = {}
    for role, selected in (("train", train), ("test", test)):
        frame = data.iloc[selected].copy()
        frame.attrs = {
            **data.attrs,
            "split_method": method,
            "split_role": role,
            "source_indices": frame.index.tolist(),
        }
        outputs[role] = frame
    return outputs


def neighbor_features(
    data: pd.DataFrame,
    columns: list[str],
    offsets: list[int] | None = None,
    group_column: str | None = None,
    time_column: str | None = None,
    drop_missing: bool = False,
) -> pd.DataFrame:
    """为数值列追加滞后/超前值，按分组边界阻断跨设备平移。

    ``offsets`` 里正数表示滞后（lag，取前面第 n 行），负数表示超前（lead）；
    生成列名形如 ``vibration_lag_1`` / ``temperature_lead_2``。内部先按
    ``group_column``/``time_column`` 排序再 ``groupby().shift()``，最后用原索引顺序还原，
    因此输出与输入逐行对齐；``drop_missing=True`` 会删掉平移产生的缺口行。
    """
    cols = numeric_columns(data, columns)
    chosen = offsets or [1]
    if not chosen or any(
        isinstance(offset, bool) or not isinstance(offset, int) or offset == 0 for offset in chosen
    ):
        raise ValueError("offsets must contain non-zero integers")
    if len(set(chosen)) != len(chosen):
        raise ValueError("offsets must be unique")
    if group_column and group_column not in data.columns:
        raise ValueError(f"Missing group column: {group_column}")
    if time_column and time_column not in data.columns:
        raise ValueError(f"Missing time column: {time_column}")
    order = list(data.index)
    # 排序只用于计算：结果最后会按原始索引顺序还原（result.loc[order]）。
    sort_columns = [column for column in (group_column, time_column) if column]
    result = data.sort_values(sort_columns, kind="stable").copy() if sort_columns else data.copy()
    generated = []
    for offset in chosen:
        shifted = (
            result.groupby(group_column, sort=False, dropna=False)[cols].shift(offset)
            if group_column
            else result[cols].shift(offset)
        )
        direction, distance = ("lag", offset) if offset > 0 else ("lead", abs(offset))
        for column in cols:
            name = f"{column}_{direction}_{distance}"
            result[name] = shifted[column]
            generated.append(name)
    result = result.loc[order]
    if drop_missing:
        result = result.dropna(subset=generated)
    return _with_attrs(data, result)


def binarize(
    data: pd.DataFrame,
    columns: list[str],
    threshold: float = 0.0,
    keep_original: bool = True,
    suffix: str = "_binary",
) -> pd.DataFrame:
    """按阈值把数值列转成 0/1 指示列。

    ``keep_original=True`` 时新增 ``<列名><suffix>`` 列并保留原列（模型可能同时需要幅度与
    指示量）；``False`` 时原地替换。目标列已存在会报错，避免静默覆盖已有特征。
    使用 ``int8`` 存储，减少宽表的特征表体积。
    """
    cols = numeric_columns(data, columns)
    result = data.copy() if keep_original else data.drop(columns=cols).copy()
    for column in cols:
        name = f"{column}{suffix}" if keep_original else column
        if name in result.columns and name != column:
            raise ValueError(f"Binarized column already exists: {name}")
        result[name] = (data[column] > threshold).astype("int8")
    return _with_attrs(data, result)


def _polynomial_names(columns: list[str], transformer: PolynomialFeatures) -> list[str]:
    """把 sklearn 的 ``powers_`` 矩阵翻译成可读列名，例如 ``vibration*temperature``。"""
    names = []
    for powers in transformer.powers_:
        parts = [
            column if power == 1 else f"{column}^{power}" for column, power in zip(columns, powers) if power
        ]
        names.append("*".join(parts) if parts else "bias")
    return names


def concat_by_rows(frames: list[tuple[str, pd.DataFrame]], source_column: str = "") -> pd.DataFrame:
    """把多份同构表按顺序纵向拼成一份（多数据源在**图里**合并的实现）。

    ``frames`` 是 ``[(输入端口名, 表), ...]``，顺序即拼接顺序，**第一个是列顺序与 schema 的基准**。
    与 ``data.input.paths`` 的规则刻意保持一致，因为它们是同一件事的两种入口（一个是节点参数，
    一个是画布上连几条线）：列集合必须相同（缺列/多列直接报错并点名），列顺序按基准对齐，
    索引重排成 ``0..N-1``。

    几个刻意不做的事：

    * **不按列名求并集、不补 NaN**。静默补列会让"两条分支结构不同"一路漂到模型里，
      而报告上看不出任何异常；
    * **不丢空输入**。空表往往是"过滤条件什么都没匹配到"，静默拼进去等于把这个问题藏起来；
    * **单输入时原样返回**（连索引都不动），这样"先接一条、以后再扩"不会改变已有结果。

    ``source_column`` 写的是**输入端口名**（``first``/``second``/…）：concat 坐在数据源之上，
    端口名是唯一一个在"单文件、多文件、覆盖、串联"四种情况下都成立的说法；想知道具体文件，
    看上游 ``data.input`` 的 ``source_column``（它写的是文件路径）。
    """
    if not frames:
        raise ValueError("Concat needs at least one input")
    reference = frames[0][1].columns.tolist()
    prepared: list[pd.DataFrame] = []
    for port, frame in frames:
        if frame.empty:
            raise ValueError(f"Input {port} has no rows; check the upstream filter before concatenating")
        if not frame.columns.is_unique:
            raise ValueError(f"Input {port} has duplicate column names")
        missing = [name for name in reference if name not in frame.columns]
        extra = [name for name in frame.columns if name not in reference]
        if missing or extra:
            raise ValueError(
                f"Input {port} has a different schema than {frames[0][0]} "
                f"(missing {missing}, extra {extra}); concat needs the same columns"
            )
        prepared.append(frame.loc[:, reference])
    if source_column:
        if source_column in reference:
            raise ValueError(f"source_column {source_column!r} already exists in the data")
        prepared = [frame.assign(**{source_column: port}) for (port, _), frame in zip(frames, prepared)]
        reference = [source_column, *reference]
    if len(prepared) == 1:
        result = prepared[0]
    else:
        result = pd.concat([frame.loc[:, reference] for frame in prepared], ignore_index=True)
    # attrs 手工合并：来源路径取并集（保序）、source_id 覆盖全部输入、警告逐条去重。
    source_paths: list[str] = []
    source_ids: list[str] = []
    evaluation: list[str] = []
    for _, frame in frames:
        paths = frame.attrs.get("source_paths")
        if paths is None:
            paths = [frame.attrs["source_path"]] if frame.attrs.get("source_path") else []
        for path in paths:
            if str(path) not in source_paths:
                source_paths.append(str(path))
        if frame.attrs.get("source_id"):
            source_ids.append(str(frame.attrs["source_id"]))
        for note in frame.attrs.get("evaluation_warnings", []):
            if note not in evaluation:
                evaluation.append(note)
    result.attrs = {
        **frames[0][1].attrs,
        "source_paths": source_paths or None,
        "source_path": " + ".join(source_paths) if source_paths else None,
        "source_id": (hashlib.sha256("\n".join(source_ids).encode()).hexdigest() if source_ids else None),
        "concatenated_inputs": [port for port, _ in frames],
    }
    if len(frames) > 1:
        evaluation = [
            *evaluation,
            "Concatenated "
            + f"{len(frames)} inputs ({len(result)} rows, index renumbered): "
            + ", ".join(port for port, _ in frames),
        ]
    result.attrs["evaluation_warnings"] = evaluation
    return result


def polynomial_features(
    data: pd.DataFrame,
    columns: list[str],
    degree: int = 2,
    interaction_only: bool = False,
    include_bias: bool = False,
    keep_original: bool = True,
    max_columns: int = 512,
) -> pd.DataFrame:
    """在选定数值列上生成多项式项与交互项，例如 ``vibration^2``、``vibration*temperature``。

    **必须在窗口切分之前使用**：它改变表的列结构，而窗口组件按列名取数并把窗口内的列
    压成统计量；放到窗口之后的结果是"在特征上再做二次项"，与这里的目的完全不同。

    生成项数量随列数与阶数组合增长（``C(n + d, d)``），因此设了 ``max_columns`` 上限，
    超出直接报错而不是把内存吃光。``keep_original=True`` 时保留原列，只追加真正的
    新项（一次项就是原列本身，不重复生成）；``include_bias`` 默认关闭，因为常数列在
    窗口特征里没有意义，而且会让线性模型出现多余自由度。
    """
    cols = numeric_columns(data, columns)
    if not cols:
        raise ValueError("Polynomial features need at least one numeric column")
    if isinstance(degree, bool) or not isinstance(degree, int) or degree < 2:
        raise ValueError("Polynomial degree must be an integer >= 2")
    values = data[cols].astype(float)
    if not np.isfinite(values.to_numpy()).all():
        raise ValueError("Polynomial features require finite values; clean missing/infinite first")
    transformer = PolynomialFeatures(
        degree=degree, interaction_only=interaction_only, include_bias=include_bias
    )
    generated = transformer.fit_transform(values)
    names = _polynomial_names(cols, transformer)
    if len(names) > max_columns:
        raise ValueError(
            f"Polynomial features would create {len(names)} columns (limit {max_columns}); "
            "lower the degree or select fewer columns"
        )
    result = data.copy() if keep_original else data.drop(columns=cols).copy()
    for name, column_values in zip(names, generated.T):
        # 一次项就是原列本身，keep_original 时跳过，避免"同名列覆盖"这类静默行为。
        if name in cols and keep_original:
            continue
        if name in result.columns:
            raise ValueError(f"Polynomial column already exists: {name}")
        result[name] = column_values
    result.attrs = {
        **data.attrs,
        "polynomial_degree": degree,
        "polynomial_columns": cols,
    }
    return result


def discretize(
    data: pd.DataFrame,
    columns: list[str],
    n_bins: int = 5,
    strategy: str = "quantile",
    encode: str = "ordinal",
    keep_original: bool = True,
    suffix: str = "_bin",
    random_state: int = 42,
) -> pd.DataFrame:
    """把连续列切成 ``n_bins`` 个箱，输出箱序号或独热指示。

    ``strategy`` 三选一：``uniform`` 等宽（受极值影响大）、``quantile`` 等频（每箱样本数
    相近，工业数据里最常用）、``kmeans`` 一维聚类（箱边界落在数据密度低谷）。

    **箱边界是在整张表上拟合的**，所以结果带探索性警告：同一份映射用在训练集与线上
    会有分布漂移问题。输出列名前缀/后缀固定（``<列名>_bin`` 或 ``<列名>_bin_<序号>``），
    便于下游按名接线。``keep_original=True`` 时保留原列。
    """
    cols = numeric_columns(data, columns)
    if not cols:
        raise ValueError("Discretization needs at least one numeric column")
    if isinstance(n_bins, bool) or not isinstance(n_bins, int) or n_bins < 2:
        raise ValueError("n_bins must be an integer >= 2")
    if strategy not in {"uniform", "quantile", "kmeans"}:
        raise ValueError(f"Unknown discretization strategy: {strategy}")
    if encode not in {"ordinal", "onehot-dense"}:
        raise ValueError(f"Unknown discretization encoding: {encode}")
    values = data[cols].astype(float)
    if not np.isfinite(values.to_numpy()).all():
        raise ValueError("Discretization requires finite values; clean missing/infinite first")
    # subsample=None：默认会随机抽样定箱边界，同一份数据两次运行结果可能不同。
    transformer = KBinsDiscretizer(
        n_bins=n_bins,
        encode=encode,
        strategy=strategy,
        subsample=None,
        random_state=random_state,
    )
    generated = transformer.fit_transform(values)
    if encode == "ordinal":
        names = [f"{column}{suffix}" for column in cols]
    else:
        # 等频/一维聚类会合并空箱，因此每个特征的真实箱数要按 bin_edges 数出来。
        names = [
            f"{column}{suffix}_{index}"
            for column, edges in zip(cols, transformer.bin_edges_)
            for index in range(len(edges) - 1)
        ]
    if len(names) != generated.shape[1]:
        raise ValueError("Discretization produced an unexpected number of columns")
    result = data.copy() if keep_original else data.drop(columns=cols).copy()
    for name, column_values in zip(names, generated.T):
        if name in result.columns:
            raise ValueError(f"Discretized column already exists: {name}")
        result[name] = column_values
    return mark_fitted(_with_attrs(data, result), f"{strategy} discretization")
