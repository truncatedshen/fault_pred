"""Thin component adapters; numerical implementations live in fault_core.

这个文件里的 87 个类都是**薄适配层**：声明元数据、端口与参数 schema，
``execute`` 里只做"取参数 → 调用 ``fault_core`` 的对应函数 → 包装成
:class:`ComponentResult`"。真正的计算在 ``src/fault_core``，因此：

* 组件本身没有状态、也不持有大数据，可以随便实例化（画布上拖几个都行）；
* 想在脚本或 notebook 里复用算法时直接调 ``fault_core``，不必经过图与运行时；
* 新增一个组件通常只是"一段 Meta + 一段参数表 + 三行 execute"。

文件顶部的 ``COLS``/``WINDOW``/``VALIDATION_PARAMS`` 等常量是多个组件的共享参数定义：
它们保证统计、拟合、熵、频域这些窗口组件拥有一模一样的参数名与语义，
这是它们的结果能通过 ``feature.merge`` 合并的前提。
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, ClassVar

import pandas as pd

from fault_core import (
    advanced_analysis,
    advanced_data,
    advanced_models,
    change_detection,
    data,
    exploration,
    features,
    forecasting,
    model_selection,
    models,
    preprocessing,
    quality,
    reduction,
    selection,
    sequence_features,
    series_analysis,
    visualization,
)
from fault_core import (
    assets as asset_tools,
)
from fault_platform.components.base import (
    BaseComponent,
)
from fault_platform.components.base import (
    ComponentMetadata as Meta,
)
from fault_platform.components.base import (
    ComponentResult as Result,
)
from fault_platform.components.base import (
    DataType as T,
)
from fault_platform.components.base import (
    InputPort as In,
)
from fault_platform.components.base import (
    OutputPort as Out,
)
from fault_platform.components.base import (
    ParameterDefinition as P,
)
from fault_platform.streaming import StreamedDataset

if TYPE_CHECKING:
    from fault_platform.runtime import ExecutionContext


def enum(name: str, default: str, options: tuple[str, ...], description: str = "") -> P:
    """枚举参数的简写；选项会写进 schema，前端与 Agent 据此知道能填什么。"""
    return P(name, "enum", default, options=options, description=description)


#: 可选数值列：留空表示"自动取所有数值列"。
COLS = P(
    "columns",
    "column_list",
    [],
    description="Column names; numeric components default to all numeric columns",
)
#: 必填数值列：窗口组件要求显式指定测点，因为"用了哪些通道"必须写进报告。
REQUIRED_COLS = P("columns", "column_list", None, required=True)
DATA_IN = (In("dataset", T.DATASET),)
#: 检查类输入：概览/绘图/探索在原始表与特征表上语义相同（运行时两者都是 DataFrame）。
#: "宽容"只放在接收端，因此这些组件可以直接挂到特征分支上，用来检验中间产物。
TABLE_IN = (In("dataset", T.DATASET, accepts=(T.FEATURE_DATASET,)),)
DATA_OUT = (Out("dataset", T.DATASET),)
FEATURES_IN = (In("features", T.FEATURE_DATASET),)
#: 四个窗口组件的公共参数：**必须完全一致**才能合并，也才能共享标签与来源信息。
WINDOW = (
    REQUIRED_COLS,
    P("group_column", "column", None, description="Equipment/run identifier"),
    P(
        "asset_column",
        "column",
        None,
        description="Owning asset (well/machine); lets validation hold out whole assets",
    ),
    P("label_column", "column", None, description="Generates labels aligned with feature windows"),
    P("time_column", "column", None),
    P("window_size", "integer", 0, min=0, description="0 = entire group; otherwise complete windows"),
    P("step", "integer", 0, min=0, description="0 = non-overlapping windows"),
    P(
        "window_span",
        "string",
        "",
        description="Time window such as 7d / 12h / 30m; use instead of window_size, needs time_column",
    ),
    P("step_span", "string", "", description="Time step such as 1d; empty = non-overlapping"),
    P(
        "prediction_horizon",
        "string",
        "",
        description="With label_policy=horizon: a fault inside this time after the window makes the label 1",
    ),
    P(
        "prediction_gap",
        "string",
        "",
        description="Embargo between the window end and the horizon; keeps the label from leaking the onset",
    ),
    enum(
        "current_fault_policy",
        "drop",
        ("drop", "positive", "negative"),
        "Windows that already contain a fault: drop them (they belong to detection), or label them 1/0",
    ),
    P(
        "normal_label",
        "string",
        "0",
        description="Label value that counts as normal; anything else counts as a fault in the horizon",
    ),
    enum("label_policy", "strict", ("strict", "last", "mode", "horizon")),
)
#: 监督验证器的公共参数：切分方式、留出比例、随机种子，以及"哪一类算故障"。
VALIDATION_PARAMS = (
    enum(
        "split_method",
        "stratified",
        ("stratified", "group", "asset", "temporal"),
        "group = by instance, asset = hold out whole assets (needs asset_column upstream)",
    ),
    P("test_size", "float", 0.25, min=0.05, max=0.5),
    P("random_state", "integer", 42, min=0),
    P(
        "positive_class",
        "string",
        "",
        description="Label counted as the fault class for miss_rate; empty = last sorted class",
    ),
)


class DataInputComponent(BaseComponent):
    """数据输入：读取 CSV/Parquet，支持列裁剪、行数上限、谓词下推与流式分块。"""

    metadata = Meta(
        "data.input",
        "数据输入",
        "data",
        "Read a local CSV or Parquet file, optionally with a column projection and row limit",
        tags=("csv", "parquet", "source"),
    )
    output_ports = DATA_OUT
    parameter_schema = (
        P(
            "path",
            "string",
            None,
            required=True,
            description="First source: CSV or Parquet path relative to the server data directory",
        ),
        P(
            "paths",
            "list",
            [],
            description=(
                "Additional sources appended after `path`, in order (same columns required); "
                "rows are concatenated and the index is renumbered"
            ),
        ),
        P(
            "source_column",
            "string",
            "",
            description="When set, add a column holding the relative path each row came from",
        ),
        enum("format", "csv", ("csv", "parquet"), "Parquet needs the pyarrow extra"),
        P("encoding", "string", "utf-8-sig"),
        P("separator", "string", ","),
        COLS,
        P(
            "max_rows",
            "integer",
            0,
            min=0,
            description="0 reads every row; a positive value bounds the read (big-file mode)",
        ),
        P(
            "filters",
            "list",
            [],
            description='Parquet predicate pushdown, e.g. [["equipment", ">", 10]]',
        ),
        P(
            "streaming",
            "boolean",
            False,
            description="Keep rows on disk and hand chunked reads to components that support streaming",
        ),
        P("chunk_rows", "integer", 200_000, min=100, description="Rows per chunk when streaming"),
    )

    def preflight(self, context: ExecutionContext) -> None:
        """执行前的资源检查：**每个**源都要落在数据目录内且存在；Parquet 还需要 pyarrow。"""
        for source in self._sources(context):
            context.resolve_data_path(source)
            if self._format_for(context, source) == "parquet":
                try:
                    import pyarrow  # noqa: F401
                except ImportError as exc:
                    raise ValueError(
                        "Parquet input requires pyarrow: pip install 'fault-prediction-platform[parquet]'"
                    ) from exc

    def external_fingerprint(self, context: ExecutionContext) -> str:
        """对**全部源文件**的内容取 SHA-256（顺序敏感）：任一文件变了，下游自动重算。

        单源时返回的仍是那一个文件的摘要——与旧行为逐字一致，因此已有方案的缓存不会失效。
        多源时把各文件摘要按顺序串起来再哈希，所以"换了其中一个文件"或"换了顺序"都会变。
        """
        digests = []
        for source in self._sources(context):
            with context.resolve_data_path(source).open("rb") as handle:
                digests.append(hashlib.file_digest(handle, "sha256").hexdigest())
        if len(digests) == 1:
            return digests[0]
        return hashlib.sha256("\n".join(digests).encode()).hexdigest()

    def _sources(self, context: ExecutionContext) -> list[str]:
        """本节点这次要读的**相对路径列表**：执行期覆盖优先，否则 ``path`` + ``paths``。

        走上下文而不是改图参数，是为了让"同一张图换一批同构数据跑"变成**执行参数**而不是**编辑**：
        图不变（可追溯）、已有结果不失效；而 `external_fingerprint` 读同一个上下文，
        所以指纹会跟着实际文件变，增量复用不会把上一批数据的结果当成本次结果。
        """
        return context.effective_dataset_paths(
            self.component_id, self.parameters["path"], self.parameters["paths"]
        )

    def _format_for(self, context: ExecutionContext, source: str) -> str:
        """按后缀优先判断格式（``.parquet``/``.pq`` 直接当 Parquet，避免参数写错读崩）。"""
        suffix = context.resolve_data_path(source).suffix.lower()
        return "parquet" if suffix in {".parquet", ".pq"} else self.parameters["format"]

    @staticmethod
    def _require_same_schema(source: str, reference: list[str], actual: list[str]) -> None:
        """多源拼接要求列集合一致；缺列/多列都直接报错，而不是补 NaN 或丢列。

        静默对齐是这里最危险的选项：少了一列就补 0/NaN 会让"两台机器数据不一致"这件事
        一路漂到模型里，而报告上看不出任何异常。
        """
        missing = [name for name in reference if name not in actual]
        extra = [name for name in actual if name not in reference]
        if missing or extra:
            raise ValueError(
                f"Data source {source} has a different schema than the first source "
                f"(missing {missing}, extra {extra}); multi-source input needs the same columns"
            )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """读取数据：单源直接读；多源按顺序纵向拼成一份表（列集合必须一致）。

        多个同构文件（例如每台设备一份导出）在这里合并成一份 ``Dataset``，后续组件看到的
        始终是"一份数据"，因此下游窗口、特征与验证器完全不用改。
        """
        sources = self._sources(context)
        path = context.resolve_data_path(sources[0])
        columns = self.parameters["columns"] or None
        max_rows = self.parameters["max_rows"] or None
        warnings: list[str] = []
        configured = self.parameters["path"]
        configured_sources = [configured, *self.parameters["paths"]]
        if sources != configured_sources:
            # 用了覆盖就必须说出来：否则"这份报告读的是哪个文件"只能靠猜。
            warnings.append(
                f"Reading {sources} from a dataset override; the graph says {configured_sources}."
            )
        if self.parameters["streaming"]:
            if len(sources) > 1:
                # 流式描述符只描述一个文件；默默地只读第一个是错的，直接拒绝。
                raise ValueError(
                    "Streamed input supports a single source; remove `paths` (or the override) "
                    "or set streaming=false"
                )
            # 流式分支：不读数据，只把"怎么读"（路径、列、块大小、来源指纹）打包传下去。
            identifier = self.external_fingerprint(context)
            streamed = StreamedDataset(
                path=path,
                format=self._format_for(context, sources[0]),
                chunk_rows=int(self.parameters["chunk_rows"]),
                columns=columns,
                encoding=self.parameters["encoding"],
                separator=self.parameters["separator"],
                filters=self.parameters["filters"] or None,
                total_rows=self._total_rows(path, self._format_for(context, sources[0])),
                attrs={
                    "source_path": str(path),
                    "source_paths": [str(path)],
                    "source_id": identifier,
                    "source_projection": {
                        "columns": columns,
                        "max_rows": max_rows,
                        "filters": self.parameters["filters"],
                    },
                },
            )
            if max_rows:
                # 流式路径无法在读取时截断行数，必须明确告知使用者"限制没有生效"。
                warnings.append("Streamed input cannot enforce max_rows; the whole file is read.")
            return Result({"dataset": streamed}, warnings)
        frames: list[tuple[str, pd.DataFrame]] = []
        for source in sources:
            resolved = context.resolve_data_path(source)
            if self._format_for(context, source) == "parquet":
                frame = self._read_parquet(resolved, columns, max_rows, self.parameters["filters"])
            else:
                frame = pd.read_csv(
                    resolved,
                    encoding=self.parameters["encoding"],
                    sep=self.parameters["separator"],
                    usecols=columns,
                    nrows=max_rows,
                )
            if not frame.columns.is_unique or frame.empty:
                raise ValueError(f"Data source must contain rows and unique column names: {source}")
            frames.append((source, frame))
        reference = frames[0][1].columns.tolist()
        for source, frame in frames[1:]:
            self._require_same_schema(source, reference, frame.columns.tolist())
        if self.parameters["source_column"]:
            name = self.parameters["source_column"]
            if name in reference:
                raise ValueError(f"source_column {name!r} already exists in the data")
            for source, frame in frames:
                frame.insert(0, name, source)
            reference = [name, *reference]
        if len(frames) > 1:
            # 各文件的原始索引都是从 0 开始的，直接拼会出现重复索引；重排成 0..N-1。
            frame = pd.concat([item[1].loc[:, reference] for item in frames], ignore_index=True)
            warnings.append(
                f"Combined {len(frames)} sources into one dataset ({len(frame)} rows, index renumbered): "
                + ", ".join(item[0] for item in frames)
            )
        else:
            frame = frames[0][1]
        if max_rows or columns or self.parameters["filters"] or len(frames) > 1:
            # 裁剪过输入就必须标注：结论只适用于这个子集，不能当成全量结论。
            if max_rows or columns or self.parameters["filters"]:
                warnings.append(
                    "Input was limited (rows/columns/filter); results and metrics describe that subset only."
                )
            frame.attrs["evaluation_warnings"] = list(warnings)
        frame.attrs["source_path"] = str(path)
        frame.attrs["source_paths"] = [str(context.resolve_data_path(item)) for item in sources]
        frame.attrs["source_id"] = self.external_fingerprint(context)
        frame.attrs["source_projection"] = {
            "columns": list(frame.columns),
            "max_rows": max_rows,
            "filters": self.parameters["filters"],
        }
        return Result({"dataset": frame}, warnings)

    @staticmethod
    def _total_rows(path, file_format: str) -> int | None:
        if file_format != "parquet":
            return None
        try:
            import pyarrow.parquet as pq

            return int(pq.ParquetFile(path).metadata.num_rows)
        except Exception:  # pragma: no cover - metadata is optional
            return None

    @staticmethod
    def _read_parquet(
        path, columns: list[str] | None, max_rows: int | None, filters: list[Any]
    ) -> pd.DataFrame:
        """Parquet 有界读取：有谓词时一次性下推，无谓词时逐批读取以真正限制峰值内存。"""
        import pyarrow.parquet as pq

        if max_rows:
            if filters:
                # Row-group pruning through the predicate; the result is already narrowed.
                table = pq.read_table(path, columns=columns, filters=filters)
                return table.slice(0, max_rows).to_pandas().reset_index(drop=True)
            # No predicate: stream batches so the row limit really bounds peak memory.
            batches, collected = [], 0
            for batch in pq.ParquetFile(path).iter_batches(columns=columns):
                batches.append(batch.to_pandas())
                collected += batch.num_rows
                if collected >= max_rows:
                    break
            if not batches:
                raise ValueError("Parquet source produced no rows")
            return pd.concat(batches, ignore_index=True).head(max_rows)
        table = pq.read_table(path, columns=columns, filters=filters or None)
        return table.to_pandas().reset_index(drop=True)


class MaterializeComponent(BaseComponent):
    """物化：把流式数据集整体读进内存，恢复排序/去重/画图等全局操作（会带内存警告）。"""

    metadata = Meta(
        "data.materialize",
        "物化数据",
        "data",
        "Load a streamed dataset fully into memory so global operations (sort, dedup, plots) can run",
        tags=("streaming", "materialize"),
    )
    accepts_streaming = True
    input_ports, output_ports = DATA_IN, DATA_OUT

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """把惰性数据集整体读进内存；已经是普通表时原样透传。"""
        value = inputs["dataset"]
        if not isinstance(value, StreamedDataset):
            return Result({"dataset": value})
        frame = value.materialize()
        return Result(
            {"dataset": frame},
            [f"Materialised {len(frame)} rows into memory; peak memory grows with the input size."],
        )


class AssetKeyComponent(BaseComponent):
    """资产标识：从实例键派生资产列（``WELL-00001_2017…`` → ``WELL-00001``）。"""

    metadata = Meta(
        "data.asset_key",
        "资产标识",
        "data",
        "Derive the owning asset (well/machine) from an instance key so validation can hold out whole assets",
        subcategory="预检",
        tags=("asset", "well", "instance", "资产", "留一井"),
        search_keywords=("asset key", "leave one well out", "资产划分", "实例标识"),
    )
    input_ports, output_ports = DATA_IN, DATA_OUT
    parameter_schema = (
        P("column", "column", None, required=True, description="Source key, e.g. the instance id"),
        P("target", "string", "asset", description="Name of the derived asset column"),
        enum("mode", "split", ("split", "regex")),
        P("separator", "string", "_", description="split mode: separator between asset and event"),
        P("index", "integer", 0, min=0, description="split mode: which part is the asset"),
        P("pattern", "expression", "", description="regex mode: pattern with one capture group"),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        frame = asset_tools.asset_key(inputs["dataset"], **self.parameters)
        assets = frame[self.parameters["target"]].nunique()
        return Result(
            {"dataset": frame},
            [f"Derived {assets} assets from {self.parameters['column']} into '{self.parameters['target']}'."],
        )


class QualityComponent(BaseComponent):
    """Report per-group constants, flat windows and label changes before modelling."""

    metadata = Meta(
        "data.quality",
        "数据质量预检",
        "data",
        "Per-group constant/zero columns, flat-window ratio, duplicate rows and mixed-label windows",
        subcategory="预检",
        tags=("quality", "预检", "flat", "constant"),
    )
    input_ports = DATA_IN
    output_ports = (Out("report", T.VISUALIZATION),)
    parameter_schema = (
        COLS,
        P("group_column", "column", None, description="Instance/asset id used for per-group checks"),
        P("label_column", "column", None, description="Counts windows whose label changes inside"),
        P("time_column", "column", None),
        P("window_size", "integer", 0, min=0),
        P("step", "integer", 0, min=0),
        P("max_groups", "integer", 20, min=1, max=200),
        P("flat_threshold", "float", 0.0, min=0),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """跑质量预检，并把 ``findings`` 提升成节点警告（让发现直接出现在报告里）。"""
        report = quality.quality_report(inputs["dataset"], **self.parameters)
        return Result({"report": report}, list(report["findings"]))


class FilterComponent(BaseComponent):
    """条件过滤：按一个或多个条件筛行，多条件之间用 and/or 组合。"""

    metadata = Meta(
        "data.filter", "条件过滤", "data", "Select rows using a typed predicate", tags=("filter", "清洗")
    )
    input_ports, output_ports = DATA_IN, DATA_OUT
    parameter_schema = (
        P("column", "column", None, required=True),
        enum(
            "operator",
            "gt",
            ("gt", "ge", "lt", "le", "eq", "ne", ">", ">=", "<", "<=", "==", "!=", "in", "between"),
        ),
        P("value", "any", 0, required=True),
        P("conditions", "list", []),
        enum("logical_operator", "and", ("and", "or")),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"dataset": data.filter_data(inputs["dataset"], **self.parameters)})


class RowOperationComponent(BaseComponent):
    """行操作：截取、抽样、排序、去重、去空值行。"""

    metadata = Meta(
        "data.row_operation",
        "行操作",
        "data",
        "Sample, sort, slice, drop or deduplicate rows",
        tags=("去除空值行", "去除重复行", "排序数据"),
    )
    input_ports, output_ports = DATA_IN, DATA_OUT
    parameter_schema = (
        enum(
            "operation",
            "head",
            ("head", "tail", "range", "sample", "sort", "drop_rows", "remove_duplicates", "drop_missing"),
        ),
        P("count", "integer", 10, min=1),
        P("start", "integer", 0, min=0),
        COLS,
        P("ascending", "boolean", True),
        P("indices", "list", []),
        P("random_state", "integer", 42, min=0),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"dataset": data.row_operation(inputs["dataset"], **self.parameters)})


class ColumnOperationComponent(BaseComponent):
    """列操作：选列/删列/改名/改序/派生新列/转类型/清理全空与常量列/裁剪极值。"""

    metadata = Meta(
        "data.column_operation",
        "列操作",
        "data",
        "Select, drop, rename, reorder, create or cast columns",
        tags=(
            "保留需要的列",
            "删除空值列",
            "去除最大值和最小值",
            "删除列",
            "去掉列值都相同的列",
            "两列之间计算",
        ),
    )
    input_ports, output_ports = DATA_IN, DATA_OUT
    parameter_schema = (
        enum(
            "operation",
            "select",
            (
                "select",
                "drop",
                "rename",
                "reorder",
                "create",
                "cast_type",
                "drop_empty",
                "drop_constant",
                "trim_extrema",
            ),
        ),
        COLS,
        P("mapping", "object", {}),
        P("name", "string", ""),
        P("expression_text", "expression", ""),
        enum("dtype", "float64", ("float64", "int64", "string", "bool")),
        P("extrema_count", "integer", 1, min=1, description="Rows removed at each extreme per column"),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"dataset": data.column_operation(inputs["dataset"], **self.parameters)})


class TimeResampleComponent(BaseComponent):
    """按时间重采样：把不规则采样对齐到固定时间格点（可分组）。"""

    metadata = Meta(
        "data.time_resample",
        "按时间重采样",
        "data",
        "Aggregate numeric signals onto a regular datetime grid",
        subcategory="行操作",
        tags=("resample", "time", "时序"),
    )
    input_ports, output_ports = DATA_IN, DATA_OUT
    parameter_schema = (
        P("time_column", "column", None, required=True),
        P("frequency", "string", None, required=True, description="Pandas frequency such as 1s, 5min or 1h"),
        COLS,
        enum("aggregation", "mean", ("mean", "median", "min", "max", "sum", "first", "last")),
        P("group_column", "column", None),
        enum("fill_method", "none", ("none", "interpolate", "ffill")),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"dataset": advanced_data.time_resample(inputs["dataset"], **self.parameters)})


class DataSplitComponent(BaseComponent):
    """数据切分：在数据层面按随机/时间/分组切成 train/test（带划分元数据）。

    模型评估优先用验证器自带的 ``split_method``——它还会检查窗口重叠与资产留出。
    """

    metadata = Meta(
        "data.split",
        "数据切分",
        "data",
        "Create reproducible random, temporal or group train/test datasets",
        subcategory="行操作",
        tags=("split", "train", "test"),
    )
    input_ports = DATA_IN
    output_ports = (Out("train", T.DATASET), Out("test", T.DATASET))
    parameter_schema = (
        enum("method", "random", ("random", "temporal", "group")),
        P("test_size", "float", 0.25, min=0.05, max=0.5),
        P("random_state", "integer", 42, min=0),
        P("group_column", "column", None),
        P("stratify_column", "column", None),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result(advanced_data.split_data(inputs["dataset"], **self.parameters))


class NeighborFeatureComponent(BaseComponent):
    """邻近数据纳入：为每列追加滞后/超前值，按分组阻断跨设备平移。"""

    metadata = Meta(
        "data.neighbor_features",
        "临近数据纳入",
        "data",
        "Append lag and lead values without crossing equipment groups",
        subcategory="行操作",
        tags=("lag", "lead", "neighbor", "时序"),
    )
    input_ports, output_ports = DATA_IN, DATA_OUT
    parameter_schema = (
        REQUIRED_COLS,
        P("offsets", "list", [1], required=True, description="Positive=lag, negative=lead; zero is invalid"),
        P("group_column", "column", None),
        P("time_column", "column", None),
        P("drop_missing", "boolean", False),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"dataset": advanced_data.neighbor_features(inputs["dataset"], **self.parameters)})


class ImputationComponent(BaseComponent):
    """原始表缺失填充：按组均值填充或组内插值（均值填充属全表拟合，带警告）。"""

    metadata = Meta(
        "data.imputation",
        "缺失值填充",
        "data",
        "Fill numeric missing values by mean or interpolation",
        subcategory="变换与替换",
        tags=("missing", "mean", "interpolate", "填充"),
    )
    input_ports, output_ports = DATA_IN, DATA_OUT
    parameter_schema = (
        COLS,
        enum("method", "mean", ("mean", "max", "min", "interpolate")),
        P("group_column", "column", None),
        enum("interpolation_method", "linear", ("linear", "nearest")),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        frame = advanced_data.impute(inputs["dataset"], **self.parameters)
        return Result({"dataset": frame}, list(frame.attrs.get("evaluation_warnings", [])))


class BinarizeComponent(BaseComponent):
    """特征二值化：按阈值把数值列转成 0/1 指示列（可保留原列）。"""

    metadata = Meta(
        "data.binarize",
        "特征二值化",
        "data",
        "Convert numeric values above a threshold to one and the rest to zero",
        subcategory="转换",
        tags=("binary", "threshold", "二值化"),
    )
    input_ports, output_ports = DATA_IN, DATA_OUT
    parameter_schema = (
        REQUIRED_COLS,
        P("threshold", "float", 0.0),
        P("keep_original", "boolean", True),
        P("suffix", "string", "_binary"),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"dataset": advanced_data.binarize(inputs["dataset"], **self.parameters)})


class PolynomialFeatureComponent(BaseComponent):
    """多项式特征：在选定列上生成平方项与交互项（**必须在窗口切分之前**使用）。"""

    metadata = Meta(
        "data.polynomial_features",
        "多项式特征",
        "data",
        "Generate polynomial and interaction terms from numeric columns before window cutting",
        subcategory="规范化",
        tags=("polynomial", "interaction", "多项式", "交互项"),
        search_keywords=(
            "polynomial features",
            "interaction terms",
            "生成多项式特征",
            "二次项",
            "交叉项",
        ),
    )
    input_ports, output_ports = DATA_IN, DATA_OUT
    parameter_schema = (
        REQUIRED_COLS,
        P("degree", "integer", 2, min=2, max=5),
        P("interaction_only", "boolean", False),
        P("include_bias", "boolean", False),
        P("keep_original", "boolean", True),
        P("max_columns", "integer", 512, min=2, max=10000),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"dataset": advanced_data.polynomial_features(inputs["dataset"], **self.parameters)})


class DiscretizeComponent(BaseComponent):
    """离散化分箱：等宽/等频/一维聚类切箱，输出箱序号或独热指示（箱边界在整表上拟合）。"""

    metadata = Meta(
        "data.discretize",
        "离散化分箱",
        "data",
        "KBins discretization into bin indices or one-hot flags, with a full-data bound warning",
        subcategory="转换",
        tags=("discretize", "binning", "分箱", "离散化"),
        search_keywords=(
            "discretization",
            "kbins",
            "k箱离散化",
            "分位数分箱",
            "离散化分箱",
        ),
    )
    input_ports, output_ports = DATA_IN, DATA_OUT
    parameter_schema = (
        REQUIRED_COLS,
        P("n_bins", "integer", 5, min=2, max=200),
        enum("strategy", "quantile", ("uniform", "quantile", "kmeans")),
        enum("encode", "ordinal", ("ordinal", "onehot-dense")),
        P("keep_original", "boolean", True),
        P("suffix", "string", "_bin"),
        P("random_state", "integer", 42, min=0),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        frame = advanced_data.discretize(inputs["dataset"], **self.parameters)
        return Result({"dataset": frame}, list(frame.attrs.get("evaluation_warnings", [])))


class ConcatComponent(BaseComponent):
    """数据拼接：把多条 `Dataset` 分支按顺序纵向拼成一份（多数据源在图里合并）。

    典型用法：画布上摆几个 `data.input`（每台设备/每批各一个文件），各自接进这里，
    后续窗口、特征、验证器看到的是**一份**数据，完全不用改。超过四个源就串联下一个 concat
    （链式合并的语义是平的：顺序即拼接顺序）。
    """

    metadata = Meta(
        "data.concat",
        "数据拼接",
        "data",
        "Concatenate several Dataset branches into one table; same columns required",
        subcategory="变换与替换",
        tags=("concat", "merge sources", "拼接", "多数据源", "合并"),
        search_keywords=(
            "concatenate",
            "union rows",
            "combine data sources",
            "数据拼接",
            "合并数据源",
            "多数据源",
            "多个数据源",
        ),
    )
    input_ports = (
        In("first", T.DATASET),
        In("second", T.DATASET),
        In("third", T.DATASET, False),
        In("fourth", T.DATASET, False),
    )
    output_ports = DATA_OUT
    parameter_schema = (
        P(
            "source_column",
            "string",
            "",
            description="When set, add a column holding the input port each row came from",
        ),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """按端口声明顺序拼接；只有真正连上的端口参与（可选端口没接就跳过）。"""
        frames = [(port.name, inputs[port.name]) for port in self.input_ports if port.name in inputs]
        frame = advanced_data.concat_by_rows(frames, source_column=self.parameters["source_column"])
        notes = list(frame.attrs.get("evaluation_warnings", []))
        return Result({"dataset": frame}, notes)


class SeasonalDifferenceComponent(BaseComponent):
    """同期差分/同期比值（"同比"口径）：``y_t - y_{t-period}`` 或 ``y_t / y_{t-period}``。"""

    metadata = Meta(
        "data.seasonal_difference",
        "同期差分",
        "data",
        "Seasonal differencing or ratio against the same phase one period earlier",
        subcategory="变换与替换",
        tags=("seasonal", "year over year", "同比", "同期差分"),
        search_keywords=("seasonal difference", "year over year", "同比口径", "同期差分", "季节差分"),
    )
    input_ports, output_ports = DATA_IN, DATA_OUT
    parameter_schema = (
        REQUIRED_COLS,
        P("period", "integer", 24, min=1, description="Samples per cycle (24 = daily series hourly)"),
        enum("mode", "difference", ("difference", "ratio"), "ratio needs a strictly positive baseline"),
        P("group_column", "column", None),
        P("time_column", "column", None),
        P("keep_original", "boolean", True),
        P("suffix", "string", ""),
        P(
            "drop_missing",
            "boolean",
            False,
            description="Drop the first period of every group instead of keeping it as NaN",
        ),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        frame = series_analysis.seasonal_difference(inputs["dataset"], **self.parameters)
        return Result({"dataset": frame}, list(frame.attrs.get("evaluation_warnings", [])))


class NormalizationComponent(BaseComponent):
    """规范化：minmax / l1 / l2 / maxabs；除 l1、l2 外都在全表上拟合，带探索性警告。"""

    metadata = Meta("data.normalization", "规范化", "data", "Min-Max, L1, L2 or MaxAbs scaling")
    input_ports, output_ports = DATA_IN, DATA_OUT
    parameter_schema = (
        COLS,
        enum("method", "minmax", ("minmax", "l1", "l2", "maxabs")),
        P("feature_range", "list", [0, 1]),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"dataset": preprocessing.scale(inputs["dataset"], **self.parameters)})


class StandardizationComponent(BaseComponent):
    """标准化：zscore 或 robust；属全表拟合，因此带探索性警告。"""

    metadata = Meta(
        "data.standardization",
        "标准化",
        "data",
        "Z-score or robust scaling; full-data fit is marked exploratory",
    )
    input_ports, output_ports = DATA_IN, DATA_OUT
    parameter_schema = (
        COLS,
        enum("method", "zscore", ("zscore", "robust")),
        P("with_mean", "boolean", True),
        P("with_std", "boolean", True),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"dataset": preprocessing.scale(inputs["dataset"], **self.parameters)})


class TransformationComponent(BaseComponent):
    """数值转换：log / log1p / sqrt / power / Box-Cox / Yeo-Johnson / 自定义表达式。"""

    metadata = Meta(
        "data.transformation",
        "数值转换",
        "data",
        "Log, power and bounded arithmetic expressions",
        tags=("log", "对数变换"),
    )
    input_ports, output_ports = DATA_IN, DATA_OUT
    parameter_schema = (
        COLS,
        enum("method", "log1p", preprocessing.TRANSFORMATION_METHODS),
        P("power", "float", 2, min=-10, max=10),
        P("expression_text", "expression", "x"),
        P(
            "n_quantiles",
            "integer",
            1000,
            min=1,
            description="quantile_uniform/quantile_normal only; capped at the row count",
        ),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"dataset": preprocessing.transform(inputs["dataset"], **self.parameters)})


class CentralTendencyComponent(BaseComponent):
    """集中趋势：均值/中位数/众数/加权均值（终端分支，不进模型）。"""

    metadata = Meta("explore.central_tendency", "集中趋势", "explore", "Mean, median, mode and weighted mean")
    input_ports = TABLE_IN
    output_ports = (Out("statistics", T.STATISTICS),)
    parameter_schema = (
        COLS,
        enum("method", "mean", ("mean", "median", "mode", "weighted_mean")),
        P("weight_column", "column", None),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"statistics": exploration.central_tendency(inputs["dataset"], **self.parameters)})


class DispersionComponent(BaseComponent):
    """离散度量：标准差/方差/极差/IQR/MAD/变异系数（终端分支）。"""

    metadata = Meta(
        "explore.dispersion", "离散度量", "explore", "Variance, standard deviation, range, IQR, MAD and CV"
    )
    input_ports = TABLE_IN
    output_ports = (Out("statistics", T.STATISTICS),)
    parameter_schema = (COLS, enum("method", "std", ("std", "variance", "range", "iqr", "mad", "cv")))

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"statistics": exploration.dispersion(inputs["dataset"], **self.parameters)})


class CorrelationComponent(BaseComponent):
    """相关性度量：pearson / spearman / kendall 相关系数矩阵（终端分支）。"""

    metadata = Meta(
        "explore.correlation", "相关性度量", "explore", "Pearson, Spearman or Kendall correlation"
    )
    input_ports = TABLE_IN
    output_ports = (Out("matrix", T.CORRELATION),)
    parameter_schema = (COLS, enum("method", "pearson", ("pearson", "spearman", "kendall")))

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"matrix": exploration.correlation(inputs["dataset"], **self.parameters)})


class DistributionCheckComponent(BaseComponent):
    """分布检查：逐列直方图与偏度/峰度/分位数摘要。"""

    metadata = Meta(
        "explore.distribution",
        "分布检查",
        "explore",
        "Quantiles, shape statistics and bounded histograms for numeric columns",
        subcategory="集中趋势",
        tags=("distribution", "histogram", "分布"),
    )
    input_ports = TABLE_IN
    output_ports = (Out("statistics", T.STATISTICS),)
    parameter_schema = (COLS, P("bins", "integer", 20, min=2, max=200))

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result(
            {"statistics": advanced_analysis.distribution_check(inputs["dataset"], **self.parameters)}
        )


class PeriodicityCheckComponent(BaseComponent):
    """周期性检查：用自相关峰值给出主周期（有采样率时换算成秒与赫兹）。"""

    metadata = Meta(
        "explore.periodicity",
        "周期性检查",
        "explore",
        "Inspect lag autocorrelation and report the strongest candidate period",
        subcategory="离散度量",
        tags=("period", "autocorrelation", "周期"),
    )
    input_ports = DATA_IN
    output_ports = (Out("statistics", T.STATISTICS),)
    parameter_schema = (
        COLS,
        P("max_lag", "integer", 100, min=1, max=10000),
        P("sampling_rate", "float", None, min=0.000000001),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result(
            {"statistics": advanced_analysis.periodicity_check(inputs["dataset"], **self.parameters)}
        )


class ConceptDriftComponent(BaseComponent):
    """概念漂移：比较参考集与当前集，用 PSI 与 KS 检验判断哪些通道发生漂移。"""

    metadata = Meta(
        "explore.concept_drift",
        "概念漂移",
        "explore",
        "Compare reference and current numeric distributions with PSI and the KS test",
        subcategory="离散度量",
        tags=("drift", "psi", "ks", "漂移"),
    )
    input_ports = (In("reference", T.DATASET), In("current", T.DATASET))
    output_ports = (Out("statistics", T.STATISTICS),)
    parameter_schema = (
        COLS,
        P("bins", "integer", 10, min=2, max=100),
        P("psi_threshold", "float", 0.2, min=0),
        P("alpha", "float", 0.05, min=0.000001, max=1),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result(
            {
                "statistics": advanced_analysis.concept_drift(
                    inputs["reference"], inputs["current"], **self.parameters
                )
            }
        )


class CrossRelationComponent(BaseComponent):
    """互相关与互协方差：随滞后展开，用于发现两个通道之间的时延关系。"""

    metadata = Meta(
        "explore.cross_relation",
        "互相关与互协方差",
        "explore",
        "Lagged cross-correlation or cross-covariance between two numeric signals",
        subcategory="相关性度量",
        tags=("cross-correlation", "cross-covariance", "互相关", "互协方差"),
    )
    input_ports = DATA_IN
    output_ports = (Out("matrix", T.CORRELATION),)
    parameter_schema = (
        P("first_column", "column", None, required=True),
        P("second_column", "column", None, required=True),
        enum("method", "cross_correlation", ("cross_correlation", "cross_covariance")),
        P("max_lag", "integer", 20, min=1, max=10000),
        P("normalize", "boolean", True),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"matrix": advanced_analysis.cross_relation(inputs["dataset"], **self.parameters)})


class AnomalyExplorationComponent(BaseComponent):
    """异常探索：箱线图（IQR）、动态阈值、双曲平滑三种可解释方法，逐行输出标记与分数。"""

    metadata = Meta(
        "explore.anomaly",
        "异常探索",
        "explore",
        "Boxplot bounds, rolling dynamic thresholds or robust hyperbolic-tangent smoothing",
        subcategory="异常探索",
        tags=("boxplot", "dynamic threshold", "tanh", "异常", "双曲线平滑"),
    )
    input_ports = TABLE_IN
    output_ports = (Out("prediction", T.PREDICTION),)
    parameter_schema = (
        COLS,
        enum("method", "boxplot", ("boxplot", "dynamic_threshold", "hyperbolic_smoothing")),
        P("window", "integer", 20, min=2),
        P("threshold", "float", 3.0, min=0),
        P("iqr_multiplier", "float", 1.5, min=0),
        P("group_column", "column", None),
        P("time_column", "column", None),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result(
            {"prediction": advanced_analysis.anomaly_exploration(inputs["dataset"], **self.parameters)}
        )


class PeaksComponent(BaseComponent):
    """山峰检测：逐列找局部极大值，可按突出度与最小间距过滤量化台阶造成的假峰。"""

    metadata = Meta(
        "explore.peaks",
        "山峰检测",
        "explore",
        "Local maxima detection with prominence and minimum-distance filtering",
        subcategory="集中趋势",
        tags=("peak", "find peaks", "山峰", "峰值"),
        search_keywords=("peak detection", "local maxima", "寻找山峰", "山峰数", "峰值检测"),
    )
    input_ports = TABLE_IN
    output_ports = (Out("statistics", T.STATISTICS),)
    parameter_schema = (
        COLS,
        P("prominence", "float", 0.0, min=0),
        P("distance", "integer", 1, min=1),
        P("group_column", "column", None, description="Find peaks within each group; never across"),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"statistics": advanced_analysis.peak_summary(inputs["dataset"], **self.parameters)})


class NormalityCheckComponent(BaseComponent):
    """正态性校验：D'Agostino 或 Shapiro 检验，结论只说明"没有拒绝正态"。"""

    metadata = Meta(
        "explore.normality",
        "正态性校验",
        "explore",
        "Per-column normality tests (D'Agostino-Pearson or Shapiro-Wilk) with an explicit caveat",
        subcategory="离散度量",
        tags=("normality", "shapiro", "正态分布", "正态性"),
        search_keywords=(
            "normality test",
            "gaussian check",
            "正态分布校验",
            "正态性检验",
            "分布检验",
        ),
    )
    input_ports = TABLE_IN
    output_ports = (Out("statistics", T.STATISTICS),)
    parameter_schema = (
        COLS,
        enum("method", "normaltest", ("normaltest", "shapiro")),
        P("alpha", "float", 0.05, min=0.000001, max=0.999999),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"statistics": advanced_analysis.normality_check(inputs["dataset"], **self.parameters)})


class DivergenceComponent(BaseComponent):
    """KL / JS 散度：逐列比较参考集与当前集的分布差异，JS 对称可用于跨列比较。"""

    metadata = Meta(
        "explore.kl_divergence",
        "KL散度度量",
        "explore",
        "Per-column Kullback-Leibler and Jensen-Shannon divergence between reference and current data",
        subcategory="离散度量",
        tags=("kl divergence", "js divergence", "散度", "相对熵"),
        search_keywords=(
            "divergence",
            "relative entropy",
            "KL散度度量",
            "分布差异",
            "JS散度",
        ),
    )
    input_ports = (In("reference", T.DATASET), In("current", T.DATASET))
    output_ports = (Out("statistics", T.STATISTICS),)
    parameter_schema = (
        COLS,
        P("bins", "integer", 10, min=2, max=200),
        P("epsilon", "float", 0.000000001, min=0),
        P("js_threshold", "float", 0.1, min=0),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result(
            {
                "statistics": advanced_analysis.divergence(
                    inputs["reference"], inputs["current"], **self.parameters
                )
            }
        )


class AcfComponent(BaseComponent):
    """ACF 自相关函数：给出整条自相关曲线与置信带，回答"记忆有多长/是否白噪声"。"""

    metadata = Meta(
        "explore.acf",
        "ACF自相关函数",
        "explore",
        "Autocorrelation curve with a 95% confidence band and per-lag significance flags",
        subcategory="集中趋势",
        tags=("autocorrelation", "acf", "自相关", "白噪声"),
        search_keywords=(
            "autocorrelation function",
            "ACF自相关函数",
            "自相关",
            "记忆长度",
            "白噪声检验",
        ),
    )
    input_ports = TABLE_IN
    output_ports = (Out("statistics", T.STATISTICS),)
    parameter_schema = (
        COLS,
        P("max_lag", "integer", 50, min=1),
        P("alpha", "float", 0.05, min=0.000001, max=0.999999),
        P("group_column", "column", None, description="Compute one ACF per group; never across"),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result(
            {"statistics": advanced_analysis.autocorrelation_function(inputs["dataset"], **self.parameters)}
        )


class IsotonicComponent(BaseComponent):
    """保序回归：只约束单调性，输出拟合曲线与平台段数（样本内 R² 不可与其他模型比）。"""

    metadata = Meta(
        "explore.isotonic",
        "保序回归",
        "explore",
        "Monotone (isotonic) regression between two columns, reporting blocks and Spearman rho",
        subcategory="相关性度量",
        tags=("isotonic", "monotonic", "保序回归", "单调"),
        search_keywords=(
            "isotonic regression",
            "monotonic fit",
            "保序回归",
            "单调回归",
            "量化相关性拟合",
        ),
    )
    input_ports = TABLE_IN
    output_ports = (Out("statistics", T.STATISTICS),)
    parameter_schema = (
        P("x_column", "column", None, required=True),
        P("y_column", "column", None, required=True),
        P("increasing", "boolean", True),
        enum("out_of_bounds", "clip", ("clip", "nan")),
        P("max_points", "integer", 500, min=10, max=5000),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"statistics": advanced_analysis.isotonic_fit(inputs["dataset"], **self.parameters)})


class GbrFitComponent(BaseComponent):
    """梯度提升拟合（GBR）：量化特征对目标的解释力并给出特征重要性。"""

    metadata = Meta(
        "explore.gbr_fit",
        "量化相关性拟合GBR",
        "explore",
        "Gradient-boosting fit that quantifies how much the columns explain the target",
        subcategory="相关性度量",
        tags=("gbr", "gradient boosting", "相关性", "特征重要性"),
        search_keywords=(
            "gbr fit",
            "gradient boosting regression",
            "量化相关性拟合GBR",
            "相关性拟合",
            "非线性关系",
        ),
    )
    input_ports = TABLE_IN
    output_ports = (Out("statistics", T.STATISTICS), Out("importance", T.IMPORTANCE))
    parameter_schema = (
        REQUIRED_COLS,
        P("target_column", "column", None, required=True),
        P("n_estimators", "integer", 100, min=1, max=2000),
        P("learning_rate", "float", 0.1, min=0.0001, max=1),
        P("max_depth", "integer", 3, min=1, max=32),
        P("min_samples_leaf", "integer", 1, min=1),
        P("subsample", "float", 1.0, min=0.01, max=1),
        P(
            "test_size",
            "float",
            0.0,
            min=0,
            max=0.9,
            description="0 = in-sample only (exploratory); a holdout here ignores window overlap",
        ),
        P("random_state", "integer", 42, min=0),
        P("max_points", "integer", 500, min=10, max=5000),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        outputs = advanced_analysis.gbr_fit(inputs["dataset"], **self.parameters)
        # importance 同时作为独立端口给出（可以接 validation.compare 那类展示），
        # statistics 端口只放"拟合结论 + 曲线"，避免同一份表在两个端口里重复出现。
        statistics = {key: value for key, value in outputs.items() if key != "importance"}
        return Result(
            {"statistics": statistics, "importance": outputs["importance"]},
            list(outputs["metrics"]["warnings"]),
        )


class HpFilterComponent(BaseComponent):
    """HP 趋势过滤（Hodrick–Prescott）：把序列拆成趋势与周期，并给出周期项占比。"""

    metadata = Meta(
        "explore.hp_filter",
        "HP趋势过滤",
        "explore",
        "Hodrick-Prescott trend/cycle separation with the cycle variance share",
        subcategory="离散度量",
        tags=("hodrick prescott", "hp filter", "趋势", "周期分离"),
        search_keywords=(
            "hodrick prescott filter",
            "hp filter",
            "HP过滤器",
            "趋势周期分解",
            "趋势滤波",
        ),
    )
    input_ports = TABLE_IN
    output_ports = (Out("statistics", T.STATISTICS),)
    parameter_schema = (
        P("column", "column", None, required=True),
        P("lamb", "float", 1600.0, min=0.0001, description="Smoothing penalty; larger = smoother trend"),
        P("group_column", "column", None),
        P("time_column", "column", None),
        P("max_points", "integer", 500, min=10, max=5000),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"statistics": series_analysis.hp_filter(inputs["dataset"], **self.parameters)})


class StationarityComponent(BaseComponent):
    """平稳性检查：ADF 单位根检验，给出统计量与三档渐近临界值（不给 p 值）。"""

    metadata = Meta(
        "explore.stationarity",
        "平稳性检查",
        "explore",
        "Augmented Dickey-Fuller unit-root test with asymptotic critical values",
        subcategory="离散度量",
        tags=("adf", "stationarity", "unit root", "平稳性"),
        search_keywords=("stationarity check", "adf test", "平稳性检查", "单位根检验"),
    )
    input_ports = TABLE_IN
    output_ports = (Out("statistics", T.STATISTICS),)
    parameter_schema = (
        P("column", "column", None, required=True),
        P("max_lag", "integer", 0, min=0, description="0 = Schwert rule of thumb"),
        enum("regression", "c", ("c", "ct", "n"), "c = constant, ct = constant + trend, n = none"),
        P("group_column", "column", None),
        P("time_column", "column", None),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"statistics": series_analysis.adf_test(inputs["dataset"], **self.parameters)})


class DtwComponent(BaseComponent):
    """DTW 距离：允许时间轴伸缩的形状距离，可选 Sakoe–Chiba 带约束与 z 标准化。"""

    metadata = Meta(
        "explore.dtw",
        "DTW距离",
        "explore",
        "Dynamic time warping distance between two columns, with an optional band",
        subcategory="相关性度量",
        tags=("dtw", "dynamic time warping", "形状距离", "DTW"),
        search_keywords=("dynamic time warping", "dtw distance", "DTW距离", "DTW相关分析", "形状相似度"),
    )
    input_ports = TABLE_IN
    output_ports = (Out("statistics", T.STATISTICS),)
    parameter_schema = (
        P("first_column", "column", None, required=True),
        P("second_column", "column", None, required=True),
        P("band", "integer", 0, min=0, description="Sakoe-Chiba radius; 0 = unbounded"),
        P("normalize", "boolean", True, description="Z-normalize both series: compare shape, not level"),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"statistics": series_analysis.dtw_distance(inputs["dataset"], **self.parameters)})


class SbdComponent(BaseComponent):
    """SBD 相关：``1 - max(NCC)``，有界且自带归一化，可直接跨样本对比较。"""

    metadata = Meta(
        "explore.sbd",
        "SBD相关",
        "explore",
        "Shape-based distance from the peak normalised cross-correlation",
        subcategory="相关性度量",
        tags=("sbd", "shape based distance", "形状", "平移相关"),
        search_keywords=("shape based distance", "sbd", "SBD相关", "形状距离", "平移相似度"),
    )
    input_ports = TABLE_IN
    output_ports = (Out("statistics", T.STATISTICS),)
    parameter_schema = (
        P("first_column", "column", None, required=True),
        P("second_column", "column", None, required=True),
        P("max_lag_fraction", "float", 0.5, min=0.01, max=1.0),
        P("normalize", "boolean", True),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result(
            {"statistics": series_analysis.shape_based_distance(inputs["dataset"], **self.parameters)}
        )


class SlopeCosineComponent(BaseComponent):
    """斜率与余弦夹角：两条序列在窗口内的增量向量余弦，判断是否同向变化。"""

    metadata = Meta(
        "explore.slope_cosine",
        "斜率与余弦夹角",
        "explore",
        "Rolling slopes plus the cosine between the two increment vectors",
        subcategory="集中趋势",
        tags=("slope", "cosine", "斜率", "夹角"),
        search_keywords=("slope cosine", "cosine similarity", "斜率与余弦夹角", "同向性分析", "联动分析"),
    )
    input_ports = TABLE_IN
    output_ports = (Out("prediction", T.PREDICTION),)
    parameter_schema = (
        P("first_column", "column", None, required=True),
        P("second_column", "column", None, required=True),
        P("window", "integer", 20, min=3),
        P("threshold", "float", 0.5, min=0, max=1, description="Flag rows with cosine below -threshold"),
        P("group_column", "column", None),
        P("time_column", "column", None),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        frame = series_analysis.slope_cosine(inputs["dataset"], **self.parameters)
        return Result({"prediction": frame}, list(frame.attrs.get("evaluation_warnings", [])))


class ScatterPlotComponent(BaseComponent):
    """散点图：按分组给点着色，用于观察类别可分性（终端分支）。"""

    metadata = Meta("visual.scatter", "散点图", "visual", "Bounded scatter plot with optional groups")
    input_ports = TABLE_IN
    output_ports = (Out("plot", T.PLOT),)
    parameter_schema = (
        P("x", "column", None, required=True),
        P("y", "column", None, required=True),
        P("group", "column", None),
        P("title", "string", "Scatter plot"),
        P("x_label", "string", ""),
        P("y_label", "string", ""),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        p = {**self.parameters, "y": [self.parameters["y"]]}
        return Result({"plot": visualization.plot(inputs["dataset"], "scatter", **p)})


class LinePlotComponent(BaseComponent):
    """折线图：按时间画信号，可指定分组列（终端分支）。"""

    metadata = Meta("visual.line", "折线图", "visual", "Time series plot with bounded samples")
    input_ports = TABLE_IN
    output_ports = (Out("plot", T.PLOT),)
    parameter_schema = (
        P("time_column", "column", None, required=True),
        P("value_columns", "column_list", None, required=True),
        P("group", "column", None),
        P("title", "string", "Time series"),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        p = self.parameters
        return Result(
            {
                "plot": visualization.plot(
                    inputs["dataset"], "line", p["time_column"], p["value_columns"], p["title"], p["group"]
                )
            }
        )


class SubplotComponent(BaseComponent):
    """子图：每个选定列一个面板，适合一次看多个测点。"""

    metadata = Meta(
        "visual.subplot",
        "子图",
        "visual",
        "Create one bounded line or scatter panel per selected value column",
        tags=("subplot", "panel", "子图"),
    )
    input_ports = TABLE_IN
    output_ports = (Out("plot", T.PLOT),)
    parameter_schema = (
        P("x", "column", None, required=True),
        P("value_columns", "column_list", None, required=True),
        enum("kind", "line", ("line", "scatter")),
        P("title", "string", "Subplots"),
        P("max_points", "integer", 500, min=10, max=10000),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"plot": visualization.subplot(inputs["dataset"], **self.parameters)})


class HistogramComponent(BaseComponent):
    """直方图：查看取值分布、偏斜与截断。"""

    metadata = Meta(
        "visual.histogram",
        "直方图",
        "visual",
        "Histogram counts or density for up to twelve numeric columns",
        tags=("histogram", "distribution", "直方图"),
    )
    input_ports = TABLE_IN
    output_ports = (Out("plot", T.PLOT),)
    parameter_schema = (
        REQUIRED_COLS,
        P("bins", "integer", 20, min=2, max=200),
        P("density", "boolean", False),
        P("title", "string", "Histogram"),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"plot": visualization.histogram(inputs["dataset"], **self.parameters)})


class ComparePlotComponent(BaseComponent):
    """对比画图：两份数据（如过滤前后）在同一张图上对比。"""

    metadata = Meta(
        "visual.compare",
        "对比画图",
        "visual",
        "Overlay selected columns from two datasets with bounded sampling",
        tags=("compare", "overlay", "对比"),
    )
    input_ports = (In("first", T.DATASET), In("second", T.DATASET))
    output_ports = (Out("plot", T.PLOT),)
    parameter_schema = (
        REQUIRED_COLS,
        P("x_column", "column", None),
        P("first_name", "string", "first"),
        P("second_name", "string", "second"),
        P("title", "string", "Dataset comparison"),
        P("max_points", "integer", 500, min=10, max=10000),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"plot": visualization.compare(inputs["first"], inputs["second"], **self.parameters)})


class AnomalyPlotComponent(BaseComponent):
    """异常点可视化：把检测出的异常点叠加到原始信号上。"""

    metadata = Meta(
        "visual.anomaly",
        "异常点可视化",
        "visual",
        "Separate normal and anomalous rows using a boolean flag column",
        tags=("anomaly", "fault", "异常点"),
    )
    input_ports = (In("dataset", T.DATASET), In("prediction", T.PREDICTION, False))
    output_ports = (Out("plot", T.PLOT),)
    parameter_schema = (
        P("x", "column", None, required=True),
        P("y", "column", None, required=True),
        P("anomaly_column", "column", None, required=True),
        P("title", "string", "Anomalies"),
        P("max_points", "integer", 500, min=10, max=10000),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        frame = inputs["dataset"]
        if "prediction" in inputs:
            flag = inputs["prediction"][self.parameters["anomaly_column"]]
            if not frame.index.equals(flag.index):
                raise ValueError("Dataset and anomaly prediction indices must align")
            frame = frame.copy()
            frame[self.parameters["anomaly_column"]] = flag
        return Result({"plot": visualization.anomaly_plot(frame, **self.parameters)})


class RelationshipPlotComponent(BaseComponent):
    """关系图：按相关系数阈值输出特征之间的边（只保留强相关的那部分）。"""

    metadata = Meta(
        "visual.relationship",
        "关系图",
        "visual",
        "Thresholded numeric feature relationship graph with bounded edges",
        tags=("relationship", "correlation", "关系图"),
    )
    input_ports = TABLE_IN
    output_ports = (Out("plot", T.PLOT),)
    parameter_schema = (
        COLS,
        enum("method", "pearson", ("pearson", "spearman", "kendall")),
        P("threshold", "float", 0.3, min=0, max=1),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"plot": visualization.relationship(inputs["dataset"], **self.parameters)})


class DataOverviewComponent(BaseComponent):
    """数据概览：行数/列数/类型/缺失率/唯一值数/时间范围/标签构成，也支持流式单遍统计。"""

    metadata = Meta(
        "visual.overview",
        "数据概览",
        "visual",
        "Shape, dtypes, missing rates, summary, time range and label/class balance",
    )
    #: `labels` 是可选端口：特征分支上标签是独立产物（`stat.labels`），
    #: 不接它就只是没有正负比例，接了也不会改变"表"那个入口的宽容范围。
    input_ports = TABLE_IN + (In("labels", T.LABEL_VECTOR, False, "Optional label vector for class balance"),)
    output_ports = (Out("overview", T.VISUALIZATION),)
    parameter_schema = (
        P("time_column", "column", None),
        P("label_column", "column", None, description="Label column in the data; use it or the labels port"),
    )
    accepts_streaming = True

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """跑概览；标签构成（正负样本比例）随报告一起返回，并把结论提升成节点警告。"""
        dataset = inputs["dataset"]
        labels = inputs.get("labels")
        if isinstance(dataset, StreamedDataset):
            overview = visualization.overview_stream(dataset.chunks(), labels=labels, **self.parameters)
        else:
            overview = visualization.overview(dataset, labels=labels, **self.parameters)
        return Result({"overview": overview}, list(overview.get("findings") or []))


class StatisticalFeatureComponent(BaseComponent):
    """统计特征：窗口内的均值/标准差/RMS/峰度/波峰因数等（默认 mean/std/rms）。"""

    metadata = Meta(
        "feature.statistical",
        "统计特征",
        "feature",
        "Grouped/windowed statistics with aligned labels; window_span/step_span cut windows by time, label_policy=horizon predicts whether a fault happens inside the prediction horizon",
        subcategory="统计 Statistical",
        tags=("window", "rms", "时序", "时间窗口", "预测", "prediction"),
        search_keywords=(
            "statistical features",
            "feature extraction",
            "统计特征",
            "窗口特征",
            "时间窗口",
            "滑窗",
            "预测",
            "预测性维护",
            "故障预警",
            "预警",
            "未来",
            "prediction",
            "horizon",
            "forecast",
            "lead time",
            "early warning",
            "prognostics",
        ),
    )
    accepts_streaming = True
    input_ports = DATA_IN
    output_ports = (Out("features", T.FEATURE_DATASET), Out("labels", T.LABEL_VECTOR, False))
    parameter_schema = (
        *WINDOW,
        P("features", "feature_list", ["mean", "std", "rms"], required=True, options=features.STATISTICS),
        P("quantile", "float", 0.75, min=0, max=1),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """统计特征：流式输入走分块路径，普通输入走整表路径，两者输出 schema 相同。"""
        dataset = inputs["dataset"]
        if isinstance(dataset, StreamedDataset):
            return Result(
                features.extract_features_stream(dataset.chunks(), **self.parameters, attrs=dataset.attrs)
            )
        return Result(features.extract_features(dataset, **self.parameters))


class FittingFeatureComponent(BaseComponent):
    """拟合特征：窗口内的线性/多项式/指数趋势及其残差与 R²，刻画退化速率。"""

    metadata = Meta(
        "feature.fitting",
        "拟合特征",
        "feature",
        "Linear, polynomial or exponential trends with residual and R² features; the degradation workhorse, and with label_policy=horizon it feeds failure prediction",
        tags=("trend", "退化", "预测", "prediction"),
        subcategory="拟合 Fitting",
        search_keywords=(
            "curve fitting",
            "trend features",
            "趋势拟合",
            "拟合特征",
            "退化",
            "时间窗口",
            "预测",
            "故障预警",
            "预警",
            "prediction",
            "horizon",
            "forecast",
            "early warning",
        ),
    )
    accepts_streaming = True
    input_ports = DATA_IN
    output_ports = (Out("features", T.FEATURE_DATASET), Out("labels", T.LABEL_VECTOR, False))
    parameter_schema = (
        *WINDOW,
        enum("fitting_method", "linear", ("linear", "polynomial", "exponential")),
        P("degree", "integer", 2, min=1, max=5),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """拟合特征：同样区分流式/整表两条路径，只把 ``kind`` 换成 ``fitting``。"""
        dataset = inputs["dataset"]
        if isinstance(dataset, StreamedDataset):
            return Result(
                features.extract_features_stream(
                    dataset.chunks(), kind="fitting", **self.parameters, attrs=dataset.attrs
                )
            )
        return Result(features.extract_features(dataset, kind="fitting", **self.parameters))


class RollingStatisticsComponent(BaseComponent):
    """滚动统计特征：逐行输出滚动均值/标准差/中位数/重复值指示（行数与输入一致）。"""

    metadata = Meta(
        "feature.rolling_statistics",
        "滚动统计特征",
        "feature",
        "Rolling mean, standard deviation, median or repeated-maximum flags",
        subcategory="统计 Statistical",
        tags=(
            "rolling",
            "smooth",
            "mean",
            "median",
            "均值平滑",
            "标准差平滑",
            "中位数平滑",
            "最大值是否重复",
        ),
        search_keywords=("rolling statistics", "smoothing", "滚动特征", "平滑特征"),
    )
    input_ports = DATA_IN
    output_ports = (Out("features", T.FEATURE_DATASET),)
    parameter_schema = (
        REQUIRED_COLS,
        enum("method", "mean", ("mean", "std", "variance", "median", "max", "min", "max_repeat")),
        P("window", "integer", 5, min=2, max=100000),
        P("group_column", "column", None),
        P("time_column", "column", None),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result(
            {"features": sequence_features.rolling_statistics(inputs["dataset"], **self.parameters)}
        )


class TemporalFeatureComponent(BaseComponent):
    """时域特征：组内一阶/二阶差分或滚动自相关（逐行输出，无标签端口）。"""

    metadata = Meta(
        "feature.temporal",
        "差分与自相关特征",
        "feature",
        "Row-aligned first/second differences or rolling autocorrelation",
        subcategory="时域 Time Domain",
        tags=("difference", "autocorrelation", "一阶差分", "二阶差分", "自相关"),
        search_keywords=("temporal features", "time domain", "时序特征"),
    )
    input_ports = DATA_IN
    output_ports = (Out("features", T.FEATURE_DATASET),)
    parameter_schema = (
        REQUIRED_COLS,
        enum(
            "method",
            "first_difference",
            ("first_difference", "second_difference", "autocorrelation", "sum_abs_change", "peak_count"),
        ),
        P("lag", "integer", 1, min=1),
        P("window", "integer", 20, min=3),
        P(
            "prominence",
            "float",
            0.0,
            min=0,
            description="peak_count only: drop peaks flatter than this prominence (0 = keep every local peak)",
        ),
        P("group_column", "column", None),
        P("time_column", "column", None),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"features": sequence_features.temporal_features(inputs["dataset"], **self.parameters)})


class EntropyFeatureComponent(BaseComponent):
    """熵特征：窗口内的近似熵与信息熵，衡量信号复杂度（单窗口上限 2000 行）。"""

    metadata = Meta(
        "feature.entropy",
        "熵特征",
        "feature",
        "Windowed approximate entropy and histogram-based information entropy; with window_span/step_span windows can be cut by time and label_policy=horizon predicts failure inside the horizon",
        subcategory="非线性 Nonlinear",
        tags=("entropy", "approximate entropy", "近似熵", "信息熵", "预测", "prediction"),
        search_keywords=(
            "nonlinear features",
            "complexity",
            "熵特征",
            "时间窗口",
            "预测",
            "故障预警",
            "prediction",
            "horizon",
            "early warning",
        ),
    )
    input_ports = DATA_IN
    output_ports = (Out("features", T.FEATURE_DATASET), Out("labels", T.LABEL_VECTOR, False))
    parameter_schema = (
        *WINDOW,
        P(
            "methods",
            "feature_list",
            ["approximate_entropy", "information_entropy"],
            required=True,
            options=sequence_features.ENTROPY_METHODS,
        ),
        P("bins", "integer", 16, min=2, max=200),
        P("embedding_dimension", "integer", 2, min=1, max=5),
        P("tolerance_ratio", "float", 0.2, min=0),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result(sequence_features.entropy_features(inputs["dataset"], **self.parameters))


class WaveletFeatureComponent(BaseComponent):
    """小波特征：滚动 Haar 多尺度能量占比、主尺度与细节峰个数（逐行对齐，行数不变）。"""

    metadata = Meta(
        "feature.wavelet",
        "小波特征",
        "feature",
        "Rolling Haar multi-scale energies, dominant scale and detail peak counts",
        subcategory="时域 Time Domain",
        tags=("wavelet", "haar", "小波", "多尺度"),
        search_keywords=(
            "wavelet features",
            "haar wavelet",
            "小波变换",
            "连续小波变换的山峰数",
            "多尺度能量",
            "小波特征",
        ),
    )
    input_ports = DATA_IN
    output_ports = (Out("features", T.FEATURE_DATASET),)
    parameter_schema = (
        REQUIRED_COLS,
        P("window", "integer", 32, min=4, max=4096, description="Samples per rolling window"),
        P("levels", "integer", 3, min=1, max=8, description="Must be <= floor(log2(window))"),
        P("peak_sigma", "float", 3.0, min=0, description="Detail peaks above this robust scale count"),
        P("group_column", "column", None),
        P("time_column", "column", None),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"features": series_analysis.wavelet_features(inputs["dataset"], **self.parameters)})


class CategoricalFeatureComponent(BaseComponent):
    """分类特征：拟合类别编码器并输出训练特征（同时给出可复用的 encoder 端口）。"""

    metadata = Meta(
        "feature.categorical",
        "分类特征",
        "feature",
        "Fit a reusable categorical encoder and produce aligned numeric training features",
        subcategory="分类 Categorical",
        search_keywords=("categorical encoding", "one hot", "类别编码", "离散属性"),
    )
    input_ports = DATA_IN
    output_ports = (
        Out("features", T.FEATURE_DATASET),
        Out("encoder", T.FEATURE_TRANSFORMER, description="Fitted mappings and fixed output schema"),
    )
    parameter_schema = (
        REQUIRED_COLS,
        enum("method", "onehot", features.CATEGORICAL_METHODS),
        P("target_column", "column", None),
        P("random_state", "integer", 42, min=0),
        enum(
            "handle_unknown",
            "ignore",
            features.UNKNOWN_CATEGORY_POLICIES,
            "ignore uses all-zero/-1/zero/global-mean values; error rejects unseen categories",
        ),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """返回训练特征与可复用的 ``encoder``；目标编码走 OOF，结果标记为探索性。"""
        return Result(features.fit_categorical(inputs["dataset"], **self.parameters))


class CategoricalTransformComponent(BaseComponent):
    """分类特征变换：用已有 encoder 编码新数据（不重新拟合，未见类别按策略处理）。"""

    metadata = Meta(
        "feature.categorical_transform",
        "分类特征变换",
        "feature",
        "Apply a fitted categorical encoder without learning from inference data",
        subcategory="分类 Categorical",
        tags=("categorical", "inference", "transform"),
        search_keywords=("categorical inference", "encoding transform", "类别变换"),
    )
    input_ports = (
        In("dataset", T.DATASET),
        In("encoder", T.FEATURE_TRANSFORMER),
    )
    output_ports = (Out("features", T.FEATURE_DATASET),)

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """只用传入的编码器做 transform（不重新拟合），因此可以安全地用于推理阶段。"""
        return Result({"features": inputs["encoder"].transform(inputs["dataset"])})


class SpectralFeatureComponent(BaseComponent):
    """频域特征：加窗 FFT 的主频/谱质心/谱熵/频带比等，平窗口按 flat_policy 处理。"""

    metadata = Meta(
        "feature.spectral",
        "频域特征",
        "feature",
        "FFT amplitude features per window: dominant frequency, centroid, entropy, band and harmonic ratios; window_span/step_span cut windows by time and label_policy=horizon supports failure prediction",
        subcategory="频域 Frequency",
        tags=("fft", "spectrum", "时序", "时间窗口", "预测", "prediction"),
        search_keywords=(
            "frequency features",
            "frequency domain",
            "频率特征",
            "频域特征",
            "时间窗口",
            "预测",
            "故障预警",
            "prediction",
            "horizon",
            "early warning",
        ),
    )
    accepts_streaming = True
    input_ports = DATA_IN
    output_ports = (Out("features", T.FEATURE_DATASET), Out("labels", T.LABEL_VECTOR, False))
    parameter_schema = (
        *WINDOW,
        P(
            "sampling_rate",
            "float",
            None,
            required=True,
            description="Raw samples per second; required to report physical frequencies",
        ),
        P(
            "features",
            "feature_list",
            ["dominant_frequency", "spectral_centroid", "spectral_entropy", "band_energy_ratio"],
            required=True,
            options=features.SPECTRAL,
            description="Every value is produced per selected column",
        ),
        P(
            "band_edges",
            "list",
            [0.25, 0.5],
            description="Band boundaries as fractions of Nyquist; produces band_energy_ratio_i",
        ),
        P("harmonic_tolerance", "float", 0.02, min=0, max=0.5),
        enum(
            "flat_policy",
            "nan",
            ("nan", "skip", "error"),
            "Constant (held/quantised) windows: nan keeps row alignment, skip drops the window, error fails",
        ),
        P(
            "flat_threshold",
            "float",
            0.0,
            min=0,
            description="Peak-to-peak below this counts as a flat window",
        ),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """频域特征：流式与整表两条路径；平窗口计数以警告形式回流到节点状态。"""
        dataset = inputs["dataset"]
        if isinstance(dataset, StreamedDataset):
            outputs = features.spectral_stream(dataset.chunks(), **self.parameters, attrs=dataset.attrs)
        else:
            outputs = features.spectral(dataset, **self.parameters)
        warnings = outputs.pop("warnings", [])
        return Result(outputs, warnings)


class ScoreSelectComponent(BaseComponent):
    """特征评分选择：方差/相关性/互信息/树模型重要性。全表拟合，标记为探索性。"""

    metadata = Meta(
        "feature.score_select",
        "特征评分选择",
        "feature",
        "Rank features by variance, correlation pruning, mutual information or model importance",
        subcategory="选择 Selection",
        tags=("selection", "importance"),
        search_keywords=("feature selection", "feature ranking", "特征选择", "特征排序"),
    )
    input_ports = (In("features", T.FEATURE_DATASET), In("labels", T.LABEL_VECTOR, False))
    output_ports = (Out("features", T.FEATURE_DATASET), Out("scores", T.IMPORTANCE))
    parameter_schema = (
        enum("method", "variance", selection.SELECTION_METHODS),
        P("top_k", "integer", 0, min=0, description="0 keeps every feature above the threshold"),
        P("threshold", "float", None, description="Defaults: 0 for variance, 0.95 for correlation"),
        P("random_state", "integer", 42, min=0),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """选择特征：直接透传 score 结果（``features`` + ``scores``），全表拟合带警告。"""
        return Result(
            selection.select_features(inputs["features"], labels=inputs.get("labels"), **self.parameters)
        )


class PcaComponent(BaseComponent):
    """主成分分析：把特征投影到前若干主成分，同时给出解释方差比。全表拟合，标记为探索性。"""

    metadata = Meta(
        "feature.pca",
        "主成分分析",
        "feature",
        "Project features onto principal components and report the explained variance",
        subcategory="表征学习 Representation Learning",
        tags=("pca", "降维"),
        search_keywords=("representation learning", "dimensionality reduction", "主成分", "表征学习"),
    )
    input_ports = FEATURES_IN
    output_ports = (Out("features", T.FEATURE_DATASET), Out("variance", T.STATISTICS))
    parameter_schema = (
        P("n_components", "integer", 2, min=1),
        COLS,
        P("whiten", "boolean", False),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """PCA：返回投影后的特征与逐主成分的方差解释表（累加列便于判断保留几个成分）。"""
        frame = reduction.pca(inputs["features"], **self.parameters)
        cumulative = 0.0
        rows = []
        for index, ratio in enumerate(frame.attrs["pca_explained_variance_ratio"], start=1):
            cumulative += ratio
            rows.append(
                {"component": f"pc_{index}", "explained_variance_ratio": ratio, "cumulative": cumulative}
            )
        variance = {"rows": rows, "source_columns": frame.attrs["pca_source_columns"]}
        return Result({"features": frame, "variance": variance})


class ValidationComponent(BaseComponent):
    """监督验证器的公共基类：特征 + 标签输入，输出 model / prediction / metrics。"""

    input_ports = (In("features", T.FEATURE_DATASET), In("labels", T.LABEL_VECTOR))
    output_ports = (Out("model", T.MODEL), Out("prediction", T.PREDICTION), Out("metrics", T.METRICS))
    algorithm: ClassVar[str]

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """训练并评估；``metrics["warnings"]``（泄漏、AUC 不可用等）会回流成节点警告。"""
        outputs = models.validate_model(
            inputs["features"], inputs["labels"], self.algorithm, **self.parameters
        )
        return Result(outputs, outputs["metrics"]["warnings"])


class RandomForestComponent(ValidationComponent):
    """随机森林：默认基线模型，输出指标与特征重要性。"""

    metadata = Meta(
        "validation.random_forest",
        "随机森林",
        "validation",
        "Random Forest classification with reproducible holdout evaluation",
        tags=("可诊断性", "自动机器学习"),
    )
    algorithm = "random_forest"
    output_ports = (*ValidationComponent.output_ports, Out("importance", T.IMPORTANCE))
    parameter_schema = (
        *VALIDATION_PARAMS,
        P("n_estimators", "integer", 100, min=1, max=2000),
        P("max_depth", "integer", None, min=1, max=100),
        P("min_samples_split", "integer", 2, min=2),
        P("min_samples_leaf", "integer", 1, min=1),
        P("class_weight", "enum", None, options=("balanced", "balanced_subsample")),
    )


class SVMComponent(ValidationComponent):
    """支持向量机：内部做标准化与可选概率校准（只在训练折上拟合）。"""

    metadata = Meta(
        "validation.svm", "支持向量机", "validation", "SVM with standardization fitted on training data only"
    )
    algorithm = "svm"
    parameter_schema = (
        *VALIDATION_PARAMS,
        enum("kernel", "rbf", ("linear", "poly", "rbf", "sigmoid")),
        P("C", "float", 1.0, min=0.000001),
        enum("gamma", "scale", ("scale", "auto")),
        P("class_weight", "enum", None, options=("balanced",)),
        P("probability", "boolean", True),
    )


class XGBoostComponent(ValidationComponent):
    """XGBoost：需要可选依赖，目标函数按类别数自动选择。"""

    metadata = Meta(
        "validation.xgboost",
        "XGBoost",
        "validation",
        "XGBoost classification; optional xgboost dependency",
        tags=("自动机器学习",),
    )
    algorithm = "xgboost"
    output_ports = (*ValidationComponent.output_ports, Out("importance", T.IMPORTANCE))
    parameter_schema = (
        *VALIDATION_PARAMS,
        P("n_estimators", "integer", 100, min=1, max=2000),
        P("max_depth", "integer", 4, min=1, max=32),
        P("learning_rate", "float", 0.1, min=0.0001, max=1),
        P("subsample", "float", 1.0, min=0.01, max=1),
        P("colsample_bytree", "float", 1.0, min=0.01, max=1),
        enum("objective", "auto", ("auto", "binary:logistic", "multi:softprob")),
    )


class DecisionTreeComponent(ValidationComponent):
    """决策树：可解释性优先的基线，输出指标与特征重要性。"""

    metadata = Meta(
        "validation.decision_tree",
        "决策树",
        "validation",
        "Decision-tree fault classification with reproducible holdout evaluation",
        subcategory="可诊断性",
        tags=("decision tree", "classification", "诊断"),
    )
    algorithm = "decision_tree"
    output_ports = (*ValidationComponent.output_ports, Out("importance", T.IMPORTANCE))
    parameter_schema = (
        *VALIDATION_PARAMS,
        enum("criterion", "gini", ("gini", "entropy", "log_loss")),
        P("max_depth", "integer", None, min=1, max=100),
        P("min_samples_split", "integer", 2, min=2),
        P("min_samples_leaf", "integer", 1, min=1),
        P("class_weight", "enum", None, options=("balanced",)),
    )


class ReservoirClassifierComponent(ValidationComponent):
    """水库机分类：固定随机映射 + 逻辑回归，作为轻量非线性基线。"""

    metadata = Meta(
        "validation.reservoir_classifier",
        "水库机分类",
        "validation",
        "Echo-state feature mapping followed by logistic fault classification",
        subcategory="可诊断性",
        tags=("reservoir", "echo state", "classification", "水库机"),
    )
    algorithm = "reservoir_classifier"
    parameter_schema = (
        *VALIDATION_PARAMS,
        P("reservoir_size", "integer", 50, min=5, max=500),
        P("spectral_radius", "float", 0.9, min=0.01, max=1.5),
        P("input_scale", "float", 0.5, min=0.000001, max=10),
        P("leaking_rate", "float", 1.0, min=0.01, max=1),
        P("n_steps", "integer", 3, min=1, max=50),
        P("C", "float", 1.0, min=0.000001),
        P("max_iter", "integer", 1000, min=100, max=10000),
        P("class_weight", "enum", None, options=("balanced",)),
    )


class LinearRegressionComponent(BaseComponent):
    """线性回归：特征 + 数值目标，输出 R²/MAE/RMSE 与回归系数（无资产切分）。"""

    metadata = Meta(
        "validation.linear_regression",
        "线性回归",
        "validation",
        "Numeric target prediction with random, group or temporal holdout metrics",
        subcategory="可预测性",
        tags=("regression", "predictability", "线性回归"),
    )
    input_ports = (In("features", T.FEATURE_DATASET), In("target", T.LABEL_VECTOR))
    output_ports = (
        Out("model", T.MODEL),
        Out("prediction", T.PREDICTION),
        Out("metrics", T.METRICS),
        Out("importance", T.IMPORTANCE),
    )
    parameter_schema = (
        enum("split_method", "random", ("random", "group", "temporal")),
        P("test_size", "float", 0.25, min=0.05, max=0.5),
        P("random_state", "integer", 42, min=0),
        P("fit_intercept", "boolean", True),
        P("positive", "boolean", False),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """回归验证：输入是特征与数值目标，输出的警告同样回流到节点状态。"""
        outputs = advanced_models.validate_linear_regression(
            inputs["features"], inputs["target"], **self.parameters
        )
        return Result(outputs, outputs["metrics"]["warnings"])


class RidgeComponent(BaseComponent):
    """岭回归：普通最小二乘加 L2 惩罚，用于特征高度相关时的回归验证。"""

    metadata = Meta(
        "validation.ridge",
        "岭回归",
        "validation",
        "Ridge regression with L2 shrinkage; same metric fields as linear regression",
        subcategory="可预测性",
        tags=("ridge", "regression", "岭回归", "正则化"),
        search_keywords=(
            "ridge regression",
            "l2 regularization",
            "岭回归",
            "多重共线性",
            "回归验证",
        ),
    )
    input_ports = (In("features", T.FEATURE_DATASET), In("target", T.LABEL_VECTOR))
    output_ports = (
        Out("model", T.MODEL),
        Out("prediction", T.PREDICTION),
        Out("metrics", T.METRICS),
        Out("importance", T.IMPORTANCE),
    )
    parameter_schema = (
        enum("split_method", "random", ("random", "group", "temporal")),
        P("test_size", "float", 0.25, min=0.05, max=0.5),
        P("random_state", "integer", 42, min=0),
        P("alpha", "float", 1.0, min=0),
        P("fit_intercept", "boolean", True),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        outputs = advanced_models.validate_ridge(inputs["features"], inputs["target"], **self.parameters)
        return Result(outputs, outputs["metrics"]["warnings"])


class ExponentialSmoothingComponent(BaseComponent):
    """指数平滑预测：Holt（水平+趋势）或 Holt–Winters（再加季节），系数由使用者给定。"""

    metadata = Meta(
        "validation.exponential_smoothing",
        "指数平滑",
        "validation",
        "Holt or Holt-Winters exponential smoothing with a tail holdout forecast",
        subcategory="可预测性",
        tags=("exponential smoothing", "holt", "holt winters", "指数平滑", "三阶指数平滑"),
        search_keywords=(
            "exponential smoothing",
            "holt winters",
            "指数平滑",
            "三阶指数平滑",
            "平滑预测",
        ),
    )
    input_ports = DATA_IN
    output_ports = (Out("model", T.MODEL), Out("prediction", T.PREDICTION), Out("metrics", T.METRICS))
    parameter_schema = (
        P("column", "column", None, required=True),
        enum("method", "holt", ("holt", "holt_winters")),
        P("alpha", "float", 0.3, min=0.000001, max=0.999999, description="Level smoothing"),
        P("beta", "float", 0.1, min=0.000001, max=0.999999, description="Trend smoothing"),
        P("gamma", "float", 0.1, min=0.000001, max=0.999999, description="Seasonal smoothing"),
        P("seasonal_periods", "integer", 24, min=2, description="holt_winters only"),
        enum("seasonal", "additive", ("additive", "multiplicative")),
        P("test_size", "float", 0.25, min=0.05, max=0.5),
        P("time_column", "column", None),
        P("group_column", "column", None),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        outputs = forecasting.exponential_smoothing(inputs["dataset"], **self.parameters)
        return Result(outputs, outputs["metrics"]["warnings"])


class ArimaComponent(BaseComponent):
    """ARIMA 预测：需要可选依赖 statsmodels（未安装时报错并给出安装命令）。"""

    metadata = Meta(
        "validation.arima",
        "ARIMA",
        "validation",
        "ARIMA forecasting; requires the optional statsmodels dependency",
        subcategory="可预测性",
        tags=("arima", "sarimax", "time series", "ARIMA"),
        search_keywords=("arima", "sarimax", "ARIMA", "SARIMAX", "时序预测模型"),
    )
    input_ports = DATA_IN
    output_ports = (Out("model", T.MODEL), Out("prediction", T.PREDICTION), Out("metrics", T.METRICS))
    parameter_schema = (
        P("column", "column", None, required=True),
        P("order", "list", [2, 1, 1], description="[p, d, q]"),
        P("test_size", "float", 0.25, min=0.05, max=0.5),
        P("trend", "string", "c"),
        P("time_column", "column", None),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        outputs = forecasting.arima_forecast(inputs["dataset"], **self.parameters)
        return Result(outputs, outputs["metrics"]["warnings"])


class ARMAComponent(BaseComponent):
    """ARMA 预测：对单条序列前段拟合并预测后段，属于时序外推而非窗口分类。"""

    metadata = Meta(
        "validation.arma",
        "ARMA",
        "validation",
        "Autoregressive moving-average fit with a temporal holdout forecast",
        subcategory="可预测性",
        tags=("arma", "forecast", "time series", "预测"),
    )
    input_ports = DATA_IN
    output_ports = (Out("model", T.MODEL), Out("prediction", T.PREDICTION), Out("metrics", T.METRICS))
    parameter_schema = (
        P("column", "column", None, required=True),
        P("p", "integer", 2, min=0, max=50),
        P("q", "integer", 1, min=0, max=50),
        P("test_size", "float", 0.25, min=0.05, max=0.5),
        P("iterations", "integer", 5, min=1, max=100),
        P("time_column", "column", None),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """ARMA：直接吃原始数据表与单个列名，按 ``test_size`` 留出后段做预测。"""
        outputs = advanced_models.validate_arma(inputs["dataset"], **self.parameters)
        return Result(outputs, outputs["metrics"]["warnings"])


class DetectorComponent(BaseComponent):
    """无监督检测器基类：直接吃原始数据表，输出 model / prediction / metrics。"""

    input_ports = DATA_IN
    output_ports = (Out("model", T.MODEL), Out("prediction", T.PREDICTION), Out("metrics", T.METRICS))
    detector_method: ClassVar[str]

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """无监督检测：阈值在完整输入上拟合，因此 metrics 固定带探索性警告。"""
        outputs = advanced_models.fit_anomaly_detector(
            inputs["dataset"], method=self.detector_method, **self.parameters
        )
        return Result(outputs, outputs["metrics"]["warnings"])


class KNNDetectorComponent(DetectorComponent):
    """KNN 检测器：k 近邻平均距离越大越异常；先标准化再算距离。"""

    metadata = Meta(
        "validation.knn_detector",
        "KNN检测",
        "validation",
        "Unsupervised anomaly scores from standardized k-nearest-neighbor distances",
        subcategory="可检测性",
        tags=("knn", "anomaly", "detection", "检测"),
    )
    detector_method = "knn"
    parameter_schema = (
        COLS,
        P("contamination", "float", 0.05, min=0.000001, max=0.5),
        P("neighbors", "integer", 5, min=1),
    )


class IsolationForestDetectorComponent(DetectorComponent):
    """隔离森林检测器：按孤立程度给分，``contamination`` 决定被判异常的比例。"""

    metadata = Meta(
        "validation.isolation_forest_detector",
        "隔离森林检测",
        "validation",
        "Unsupervised Isolation Forest scores and contamination-based anomaly flags",
        subcategory="可检测性",
        tags=("isolation forest", "anomaly", "detection", "隔离森林"),
    )
    detector_method = "isolation_forest"
    parameter_schema = (
        COLS,
        P("contamination", "float", 0.05, min=0.000001, max=0.5),
        P("random_state", "integer", 42, min=0),
        P("n_estimators", "integer", 100, min=1, max=2000),
    )


class DbscanDetectorComponent(BaseComponent):
    """DBSCAN 检测：落在任何簇之外的点判为异常；异常率由 eps 与 min_samples 决定。"""

    metadata = Meta(
        "validation.dbscan_detector",
        "DBSCAN检测",
        "validation",
        "Density-based detection: points outside every cluster are anomalies (eps controls the rate)",
        subcategory="可检测性",
        tags=("dbscan", "density", "anomaly", "密度聚类"),
        search_keywords=(
            "dbscan",
            "density based detection",
            "DBSCAN 检测",
            "密度检测",
            "无监督异常检测",
        ),
    )
    input_ports = DATA_IN
    output_ports = (Out("model", T.MODEL), Out("prediction", T.PREDICTION), Out("metrics", T.METRICS))
    parameter_schema = (
        COLS,
        P("eps", "float", 1.0, min=0.000001),
        P("min_samples", "integer", 5, min=2),
        enum("metric", "euclidean", ("euclidean", "manhattan", "chebyshev")),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        outputs = advanced_models.fit_dbscan_detector(inputs["dataset"], **self.parameters)
        return Result(outputs, outputs["metrics"]["warnings"])


class PcaDetectorComponent(BaseComponent):
    """PCA 检测：用主成分重构每一行，重构误差大的行判为异常。"""

    metadata = Meta(
        "validation.pca_detector",
        "PCA检测器",
        "validation",
        "PCA reconstruction-error anomaly detection with a contamination-based threshold",
        subcategory="可检测性",
        tags=("pca", "reconstruction", "anomaly", "主成分"),
        search_keywords=(
            "pca anomaly detection",
            "reconstruction error",
            "PCA检测器",
            "主成分分析异常检测",
            "重构误差",
        ),
    )
    input_ports = DATA_IN
    output_ports = (Out("model", T.MODEL), Out("prediction", T.PREDICTION), Out("metrics", T.METRICS))
    parameter_schema = (
        COLS,
        P(
            "n_components",
            "integer",
            0,
            min=0,
            description="0 = keep 95% of the variance automatically",
        ),
        P("contamination", "float", 0.05, min=0.000001, max=0.5),
        P("random_state", "integer", 42, min=0),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        outputs = advanced_models.fit_pca_detector(inputs["dataset"], **self.parameters)
        return Result(outputs, outputs["metrics"]["warnings"])


class MinClusterDetectorComponent(BaseComponent):
    """Mincluster 探测器：先聚类正常工况，再按到最近簇心的距离判异常（可自动选簇数）。"""

    metadata = Meta(
        "validation.min_cluster_detector",
        "Mincluster探测器",
        "validation",
        "MiniBatchKMeans cluster-distance detection; n_clusters=0 picks the count by silhouette",
        subcategory="可检测性",
        tags=("kmeans", "cluster", "anomaly", "聚类"),
        search_keywords=(
            "minicluster detector",
            "kmeans anomaly detection",
            "Mincluster探测器",
            "聚类检测",
            "自动给出合理聚类",
        ),
    )
    input_ports = DATA_IN
    output_ports = (Out("model", T.MODEL), Out("prediction", T.PREDICTION), Out("metrics", T.METRICS))
    parameter_schema = (
        COLS,
        P("n_clusters", "integer", 0, min=0, description="0 = choose the count automatically"),
        P("contamination", "float", 0.05, min=0.000001, max=0.5),
        P("max_clusters", "integer", 8, min=2, max=64),
        P("batch_size", "integer", 1024, min=16),
        P("silhouette_sample", "integer", 5000, min=50),
        P("random_state", "integer", 42, min=0),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        outputs = advanced_models.fit_min_cluster_detector(inputs["dataset"], **self.parameters)
        return Result(outputs, outputs["metrics"]["warnings"])


class ChangeDetectorComponent(BaseComponent):
    """结构变化检测器的公共基类：吃原始表，输出 model / prediction / metrics。

    七个检测器共用同一套实现（:func:`fault_core.change_detection.detect`），差异只在**判定
    口径**与参数上，因此端口与执行收敛在基类，每个子类只声明自己的方法与参数。
    共同的语义约定：

    * ``anomaly_score`` 的量纲随方法不同（t 统计量 / 对数比 / z 分数 / sigma 倍数），
      跨方法的分数不可比较，报告里必须同时写方法名与阈值；
    * 窗口或参考段不足的行 ``anomaly_score`` 是 NaN、``is_anomaly`` 为 False，
      用 metrics 里的 ``scored_count`` 说明到底评了多少行；
    * 给了 ``group_column`` 就逐组独立检测，**绝不跨设备边界**。
    """

    input_ports = DATA_IN
    output_ports = (Out("model", T.MODEL), Out("prediction", T.PREDICTION), Out("metrics", T.METRICS))
    detection_method: ClassVar[str]

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        outputs = change_detection.detect(inputs["dataset"], method=self.detection_method, **self.parameters)
        return Result(outputs, outputs["metrics"]["warnings"])


class LevelShiftDetectorComponent(ChangeDetectorComponent):
    """LevelShift 检测：候选点前后各 window 点的均值差的 t 统计量超过阈值即为阶跃。"""

    metadata = Meta(
        "validation.level_shift_detector",
        "LevelShift检测器",
        "validation",
        "Detect a step change in the mean using a two-sample t statistic around each split point",
        subcategory="可检测性",
        tags=("level shift", "changepoint", "阶跃", "均值突变"),
        search_keywords=("level shift detector", "changepoint detection", "均值阶跃", "结构变化", "突变检测"),
    )
    detection_method = "level_shift"
    parameter_schema = (
        P("column", "column", None, required=True),
        P("window", "integer", 20, min=2, description="Points compared before and after each split"),
        P("threshold", "float", 4.0, min=0, description="t statistic cut-off"),
        P("group_column", "column", None),
        P("time_column", "column", None),
    )


class VolatilityShiftDetectorComponent(ChangeDetectorComponent):
    """VolatilityShift 检测：前后两段方差之比的对数，按原假设抽样标准差归一化后比阈值。"""

    metadata = Meta(
        "validation.volatility_shift_detector",
        "VolatilityShift检测器",
        "validation",
        "Detect a change in the variance level; the score is the log variance ratio in z units",
        subcategory="可检测性",
        tags=("volatility shift", "variance change", "波动率", "方差突变"),
        search_keywords=(
            "volatility shift",
            "variance change detection",
            "波动率变化检测",
            "波动率突变",
            "噪声水平变化",
        ),
    )
    detection_method = "volatility_shift"
    parameter_schema = (
        P("column", "column", None, required=True),
        P("window", "integer", 20, min=2),
        P("threshold", "float", 4.0, min=0, description="z units: 0.32 is one sigma for a 20-point window"),
        P("group_column", "column", None),
        P("time_column", "column", None),
    )


class SeasonalDetectorComponent(ChangeDetectorComponent):
    """Seasonal 检测：偏离"参考段季节剖面"的稳健 z 分数超过阈值即为异常。"""

    metadata = Meta(
        "validation.seasonal_detector",
        "Seasonal检测器",
        "validation",
        "Score each row against a seasonal profile fitted on a leading reference segment",
        subcategory="可检测性",
        tags=("seasonal", "seasonality", "季节性", "周期异常"),
        search_keywords=("seasonal detector", "seasonality check", "季节性检测", "周期剖面"),
    )
    detection_method = "seasonal"
    parameter_schema = (
        P("column", "column", None, required=True),
        P("period", "integer", 24, min=2, description="Samples per cycle"),
        P("threshold", "float", 4.0, min=0, description="z units against the robust profile scale"),
        P(
            "reference_fraction",
            "float",
            0.5,
            min=0.1,
            max=0.9,
            description="Leading share used to build the profile; the rest is out-of-sample",
        ),
        P("group_column", "column", None),
        P("time_column", "column", None),
    )


class AutoregressionDetectorComponent(ChangeDetectorComponent):
    """AutoRegression 检测：AR(p) 单步预测残差的稳健 z 分数；只评参考段之后的行。"""

    metadata = Meta(
        "validation.autoregression_detector",
        "AutoRegression检测器",
        "validation",
        "One-step AR(p) residuals on the rows after a leading training segment",
        subcategory="可检测性",
        tags=("autoregression", "ar model", "自回归", "动态变化"),
        search_keywords=("autoregression detector", "ar residual detection", "自回归检测", "残差异常"),
    )
    detection_method = "autoregression"
    parameter_schema = (
        P("column", "column", None, required=True),
        P("order", "integer", 2, min=1, max=50, description="AR order p"),
        P("threshold", "float", 4.0, min=0, description="Robust z-score cut-off"),
        P("train_fraction", "float", 0.5, min=0.1, max=0.9),
        P("group_column", "column", None),
        P("time_column", "column", None),
    )


class EsdDetectorComponent(ChangeDetectorComponent):
    """Generalized ESD（Rosner）检测：迭代剔除最极端点并与 t 分布临界值比较。"""

    metadata = Meta(
        "validation.esd_detector",
        "GeneralizedESD检测器",
        "validation",
        "Generalized extreme Studentized deviate test for up to a fraction of outliers",
        subcategory="可检测性",
        tags=("esd", "rosner", "outlier", "广义ESD"),
        search_keywords=("generalized esd", "rosner test", "ESD检测", "离群点检验"),
    )
    detection_method = "esd"
    parameter_schema = (
        P("column", "column", None, required=True),
        P("max_outlier_fraction", "float", 0.1, min=0.001, max=0.5, description="Upper bound tested"),
        P("alpha", "float", 0.05, min=0.000001, max=0.5),
        P("group_column", "column", None),
        P("time_column", "column", None),
    )


class NSigmaDetectorComponent(ChangeDetectorComponent):
    """Nsigma 检测：偏离中心 sigma 倍尺度即为异常；中心可按整列、整组或滚动窗口取。"""

    metadata = Meta(
        "validation.nsigma_detector",
        "Nsigma检测",
        "validation",
        "Distance from a centre in units of a scale, with global, per-group or rolling centres",
        subcategory="可检测性",
        tags=("nsigma", "sigma", "3sigma", "N倍标准差"),
        search_keywords=("nsigma detector", "sigma threshold", "Nsigma算法", "三倍标准差", "越限检测"),
    )
    detection_method = "nsigma"
    parameter_schema = (
        P("column", "column", None, required=True),
        P("sigma", "float", 3.0, min=0.1, description="How many scales count as an excursion"),
        enum(
            "mode",
            "global",
            ("global", "group", "rolling"),
            "global/group use full-sample statistics (descriptive); rolling follows drift",
        ),
        P("window", "integer", 30, min=2, description="Only used by mode=rolling"),
        P("group_column", "column", None),
        P("time_column", "column", None),
    )


class MeanDriftDetectorComponent(ChangeDetectorComponent):
    """均值漂移检测：Page 的 CUSUM，累积"偏离参考均值超过 slack 倍尺度"的部分。"""

    metadata = Meta(
        "validation.mean_drift_detector",
        "均值漂移检测",
        "validation",
        "CUSUM drift detector against a reference segment mean, with a slack band",
        subcategory="可检测性",
        tags=("cusum", "mean drift", "均值漂移", "缓慢漂移"),
        search_keywords=("mean drift detector", "cusum", "平均值漂移检测", "缓慢漂移检测"),
    )
    detection_method = "mean_drift"
    parameter_schema = (
        P("column", "column", None, required=True),
        P(
            "reference_fraction",
            "float",
            0.3,
            min=0.05,
            max=0.9,
            description="Leading share that defines the target mean",
        ),
        P("slack", "float", 0.5, min=0, description="Slack k in scale units; ignores small deviations"),
        P("decision", "float", 5.0, min=0.1, description="Alarm line h in scale units"),
        P("group_column", "column", None),
        P("time_column", "column", None),
    )


class KMeansComponent(BaseComponent):
    """KMeans 聚类：把记录分到若干工况簇（可自动选簇数）；**不是**异常检测。"""

    metadata = Meta(
        "validation.kmeans",
        "KMeans聚类",
        "validation",
        "MiniBatchKMeans clustering with silhouette-based cluster-count selection",
        subcategory="可诊断性",
        tags=("kmeans", "cluster", "聚类", "工况划分"),
        search_keywords=("kmeans clustering", "cluster analysis", "kmeans聚类", "聚类分析", "工况聚类"),
    )
    input_ports = TABLE_IN
    output_ports = (Out("model", T.MODEL), Out("prediction", T.PREDICTION), Out("metrics", T.METRICS))
    parameter_schema = (
        COLS,
        P("n_clusters", "integer", 0, min=0, description="0 = choose automatically by silhouette"),
        P("max_clusters", "integer", 8, min=2, max=64),
        P("batch_size", "integer", 1024, min=16),
        P("silhouette_sample", "integer", 5000, min=50),
        P("random_state", "integer", 42, min=0),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        outputs = advanced_models.fit_kmeans(inputs["dataset"], **self.parameters)
        return Result(outputs, outputs["metrics"]["warnings"])


class OneClassSvmComponent(BaseComponent):
    """单类 SVM 检测：只学"正常长什么样"，边界之外判为异常（不需要标签）。"""

    metadata = Meta(
        "validation.one_class_svm",
        "单类SVM检测",
        "validation",
        "One-class SVM novelty detection learned from normal behaviour only",
        subcategory="可检测性",
        tags=("one class svm", "novelty", "单类", "边界检测"),
        search_keywords=("one class svm", "novelty detection", "单类SVM", "支持向量机检测", "无标签检测"),
    )
    input_ports = DATA_IN
    output_ports = (Out("model", T.MODEL), Out("prediction", T.PREDICTION), Out("metrics", T.METRICS))
    parameter_schema = (
        COLS,
        P("nu", "float", 0.05, min=0.000001, max=1, description="Upper bound on the outlier fraction"),
        enum("kernel", "rbf", ("rbf", "linear", "poly", "sigmoid")),
        enum("gamma", "scale", ("scale", "auto")),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        outputs = advanced_models.fit_one_class_svm(inputs["dataset"], **self.parameters)
        return Result(outputs, outputs["metrics"]["warnings"])


class PersistenceDetectorComponent(BaseComponent):
    """持续越限检测：连续 N 个点越过阈值才算异常，用来抓卡死/保持值这类故障。"""

    metadata = Meta(
        "validation.persistence_detector",
        "Persist检测器",
        "validation",
        "Flag a threshold excursion after it persists for a configured number of rows",
        subcategory="可检测性",
        tags=("persist", "persistence", "threshold", "检测"),
    )
    input_ports = DATA_IN
    output_ports = (Out("model", T.MODEL), Out("prediction", T.PREDICTION), Out("metrics", T.METRICS))
    parameter_schema = (
        P("column", "column", None, required=True),
        P("threshold", "float", None, required=True),
        enum("direction", "above", ("above", "below", "absolute")),
        P("min_consecutive", "integer", 3, min=1),
        P("group_column", "column", None),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """持续越限检测：阈值来自工艺知识，不使用任何拟合，因此没有探索性警告。"""
        outputs = advanced_models.persistence_detection(inputs["dataset"], **self.parameters)
        return Result(outputs, outputs["metrics"]["warnings"])


class LabelComponent(BaseComponent):
    """标签向量：从原始表取行级标签（窗口标签请用窗口组件的 labels 端口）。"""

    metadata = Meta("data.labels", "标签向量", "data", "Extract row-aligned labels for tabular features")
    input_ports = DATA_IN
    output_ports = (Out("labels", T.LABEL_VECTOR),)
    parameter_schema = (P("column", "column", None, required=True),)

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """取行级标签列；只有"表格本身就是特征表"的场景才该用它。"""
        return Result({"labels": inputs["dataset"][self.parameters["column"]].copy()})


class SelectFeatureComponent(BaseComponent):
    """选择已有特征：把表中已有的数值列声明成 FeatureDataset，接入模型。"""

    metadata = Meta(
        "feature.select",
        "选择已有特征",
        "feature",
        "Explicit Dataset to FeatureDataset conversion",
        subcategory="组合 Composition",
        search_keywords=("select columns as features", "已有特征", "特征输入"),
    )
    input_ports = DATA_IN
    output_ports = (Out("features", T.FEATURE_DATASET),)
    parameter_schema = (REQUIRED_COLS,)

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """把已有列声明为特征：先校验是数值列，再取拷贝，保持索引与 attrs。"""
        columns = data.numeric_columns(inputs["dataset"], self.parameters["columns"])
        return Result({"features": inputs["dataset"][columns].copy()})


class FeatureImputationComponent(BaseComponent):
    """特征缺失处理：mean/median/zero 填充或 drop_columns，放在模型之前。"""

    metadata = Meta(
        "feature.imputation",
        "特征缺失处理",
        "feature",
        "Fill or drop NaN feature columns (flat windows yield NaN spectra) before modelling",
        subcategory="清洗 Cleaning",
        tags=("missing", "nan", "imputation", "特征缺失"),
        search_keywords=("feature imputation", "nan features", "特征缺失填充"),
    )
    input_ports = FEATURES_IN
    output_ports = (Out("features", T.FEATURE_DATASET),)
    parameter_schema = (
        COLS,
        enum("method", "mean", ("mean", "median", "zero", "drop_columns")),
        P("fill_value", "float", 0.0),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """填充或删除 NaN 特征列，并把"动了哪些列/填了什么"作为警告返回。"""
        frame, notes = preprocessing.impute_features(inputs["features"], **self.parameters)
        return Result({"features": frame}, notes)


class MergeFeatureComponent(BaseComponent):
    """合并特征：按列拼接两条分支，强制校验索引与 provenance 一致、列名不重叠。"""

    metadata = Meta(
        "feature.merge",
        "合并特征",
        "feature",
        "Join two feature branches with identical row/window provenance",
        subcategory="组合 Composition",
        search_keywords=("merge features", "combine features", "合并特征"),
    )
    input_ports = (In("left", T.FEATURE_DATASET), In("right", T.FEATURE_DATASET))
    output_ports = (Out("features", T.FEATURE_DATASET),)

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """合并两条特征分支；索引或 provenance 不一致会直接报错（不静默对齐）。"""
        return Result({"features": features.merge_features(inputs["left"], inputs["right"])})


class GridSearchComponent(BaseComponent):
    """超参搜索：在小网格上做交叉验证，返回最优参数、全部候选分数与选出来的模型。"""

    metadata = Meta(
        "validation.grid_search",
        "超参搜索",
        "validation",
        "Small-grid cross-validated hyper-parameter search with an explicit optimism warning",
        subcategory="自动机器学习",
        tags=("grid search", "hyperparameter", "超参搜索", "自动机器学习"),
        search_keywords=(
            "grid search",
            "hyperparameter tuning",
            "超参搜索",
            "自动机器学习",
            "参数寻优",
        ),
    )
    input_ports = (In("features", T.FEATURE_DATASET), In("labels", T.LABEL_VECTOR))
    output_ports = (
        Out("model", T.MODEL),
        Out("metrics", T.METRICS),
        Out("importance", T.IMPORTANCE, False),
    )
    parameter_schema = (
        enum(
            "algorithm",
            "random_forest",
            model_selection.ALGORITHMS,
            "Classifiers take features+labels; regressors take features+target",
        ),
        P(
            "param_grid",
            "object",
            {"n_estimators": [100, 300], "max_depth": [4, 8]},
            required=True,
            description="Parameter name to candidate list, e.g. {'n_estimators': [100, 300]}",
        ),
        enum("cv_method", "stratified", ("stratified", "group", "temporal")),
        P("cv_folds", "integer", 3, min=2, max=10),
        P("scoring", "string", "", description="Empty = f1_weighted for classifiers, r2 for regressors"),
        P("top_k", "integer", 5, min=1, max=20, description="How many candidates to report"),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        outputs = model_selection.grid_search(inputs["features"], inputs["labels"], **self.parameters)
        warnings = list(outputs["metrics"]["warnings"])
        # 候选表放进 metrics（有界的前 top_k 行），不额外加端口：它是一次搜索的审计记录，
        # 不是可以继续接线往下传的数据。
        outputs["metrics"]["top_candidates"] = outputs.pop("candidates").to_dict(orient="records")
        payload = {"model": outputs["model"], "metrics": outputs["metrics"]}
        if "importance" in outputs:
            payload["importance"] = outputs["importance"]
        return Result(payload, warnings)


class CompareModelsComponent(BaseComponent):
    """模型对比：比较最多三个 metrics 产物，要求它们来自同一批测试行。"""

    metadata = Meta(
        "validation.compare",
        "模型对比",
        "validation",
        "Compare up to three model metrics on identical holdout rows",
    )
    input_ports = (In("first", T.METRICS), In("second", T.METRICS), In("third", T.METRICS, False))
    output_ports = (Out("comparison", T.STATISTICS),)

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """对比指标：先校验三个模型的测试行完全一致，再抽出关键字段组成对比表。

        "测试行不一致就拒绝"是刻意的：不同留出集上的分数本来就没有可比性，
        与其给出一张会误导人的表，不如让使用者把切分对齐。
        """
        scores = list(inputs.values())
        if any(s["test_indices"] != scores[0]["test_indices"] for s in scores[1:]):
            raise ValueError("Model comparison requires the same test indices")
        keys = ("algorithm", "accuracy", "precision", "recall", "f1", "roc_auc", "train_count", "test_count")
        return Result({"comparison": {"rows": [{k: score[k] for k in keys} for score in scores]}})


#: 全部内置组件（顺序只影响注册表里的遍历顺序，不影响功能）。
#: 新增组件时把它加进这个元组即可——注册表、前端组件库、MCP 工具与文档都会自动带上它。
BUILTIN_COMPONENTS = (
    DataInputComponent,
    MaterializeComponent,
    AssetKeyComponent,
    QualityComponent,
    FilterComponent,
    RowOperationComponent,
    ColumnOperationComponent,
    TimeResampleComponent,
    DataSplitComponent,
    NeighborFeatureComponent,
    ImputationComponent,
    NormalizationComponent,
    StandardizationComponent,
    TransformationComponent,
    BinarizeComponent,
    PolynomialFeatureComponent,
    DiscretizeComponent,
    SeasonalDifferenceComponent,
    ConcatComponent,
    CentralTendencyComponent,
    DispersionComponent,
    CorrelationComponent,
    DistributionCheckComponent,
    PeriodicityCheckComponent,
    ConceptDriftComponent,
    CrossRelationComponent,
    AnomalyExplorationComponent,
    PeaksComponent,
    NormalityCheckComponent,
    DivergenceComponent,
    AcfComponent,
    IsotonicComponent,
    GbrFitComponent,
    HpFilterComponent,
    StationarityComponent,
    DtwComponent,
    SbdComponent,
    SlopeCosineComponent,
    ScatterPlotComponent,
    LinePlotComponent,
    SubplotComponent,
    HistogramComponent,
    ComparePlotComponent,
    AnomalyPlotComponent,
    RelationshipPlotComponent,
    DataOverviewComponent,
    StatisticalFeatureComponent,
    FittingFeatureComponent,
    RollingStatisticsComponent,
    TemporalFeatureComponent,
    WaveletFeatureComponent,
    EntropyFeatureComponent,
    CategoricalFeatureComponent,
    CategoricalTransformComponent,
    SpectralFeatureComponent,
    ScoreSelectComponent,
    PcaComponent,
    RandomForestComponent,
    SVMComponent,
    XGBoostComponent,
    DecisionTreeComponent,
    ReservoirClassifierComponent,
    LinearRegressionComponent,
    RidgeComponent,
    ExponentialSmoothingComponent,
    ArimaComponent,
    ARMAComponent,
    KNNDetectorComponent,
    IsolationForestDetectorComponent,
    DbscanDetectorComponent,
    PcaDetectorComponent,
    MinClusterDetectorComponent,
    KMeansComponent,
    OneClassSvmComponent,
    LevelShiftDetectorComponent,
    VolatilityShiftDetectorComponent,
    SeasonalDetectorComponent,
    AutoregressionDetectorComponent,
    EsdDetectorComponent,
    NSigmaDetectorComponent,
    MeanDriftDetectorComponent,
    PersistenceDetectorComponent,
    LabelComponent,
    SelectFeatureComponent,
    FeatureImputationComponent,
    MergeFeatureComponent,
    GridSearchComponent,
    CompareModelsComponent,
)
