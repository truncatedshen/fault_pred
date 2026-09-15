"""Thin component adapters; numerical implementations live in fault_core.

这个文件里的 56 个类都是**薄适配层**：声明元数据、端口与参数 schema，
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
    data,
    exploration,
    features,
    models,
    preprocessing,
    quality,
    reduction,
    selection,
    sequence_features,
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
    enum("label_policy", "strict", ("strict", "last", "mode")),
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
            description="CSV or Parquet path relative to the server data directory",
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
        """执行前的资源检查：路径必须落在数据目录内且文件存在；Parquet 还需要 pyarrow。"""
        context.resolve_data_path(self.parameters["path"])
        if self._format(context) == "parquet":
            try:
                import pyarrow  # noqa: F401
            except ImportError as exc:
                raise ValueError(
                    "Parquet input requires pyarrow: pip install 'fault-prediction-platform[parquet]'"
                ) from exc

    def external_fingerprint(self, context: ExecutionContext) -> str:
        """对文件内容取 SHA-256：源文件一改，下游结果会自动重算，而不是复用旧结果。"""
        with context.resolve_data_path(self.parameters["path"]).open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()

    def _format(self, context: ExecutionContext) -> str:
        """按后缀优先判断格式（``.parquet``/``.pq`` 直接当 Parquet，避免参数写错读崩）。"""
        suffix = context.resolve_data_path(self.parameters["path"]).suffix.lower()
        return "parquet" if suffix in {".parquet", ".pq"} else self.parameters["format"]

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        """读取数据：流式模式下只返回惰性描述符，普通模式按裁剪参数读进内存。"""
        path = context.resolve_data_path(self.parameters["path"])
        columns = self.parameters["columns"] or None
        max_rows = self.parameters["max_rows"] or None
        warnings: list[str] = []
        if self.parameters["streaming"]:
            # 流式分支：不读数据，只把"怎么读"（路径、列、块大小、来源指纹）打包传下去。
            identifier = self.external_fingerprint(context)
            streamed = StreamedDataset(
                path=path,
                format=self._format(context),
                chunk_rows=int(self.parameters["chunk_rows"]),
                columns=columns,
                encoding=self.parameters["encoding"],
                separator=self.parameters["separator"],
                filters=self.parameters["filters"] or None,
                total_rows=self._total_rows(path, self._format(context)),
                attrs={
                    "source_path": str(path),
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
        if self._format(context) == "parquet":
            frame = self._read_parquet(path, columns, max_rows, self.parameters["filters"])
        else:
            frame = pd.read_csv(
                path,
                encoding=self.parameters["encoding"],
                sep=self.parameters["separator"],
                usecols=columns,
                nrows=max_rows,
            )
        if not frame.columns.is_unique or frame.empty:
            raise ValueError("Data source must contain rows and unique column names")
        if max_rows or columns or self.parameters["filters"]:
            # 裁剪过输入就必须标注：结论只适用于这个子集，不能当成全量结论。
            warnings.append(
                "Input was limited (rows/columns/filter); results and metrics describe that subset only."
            )
            frame.attrs["evaluation_warnings"] = list(warnings)
        frame.attrs["source_path"] = str(path)
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
        enum("method", "mean", ("mean", "interpolate")),
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
        enum("method", "log1p", ("log", "log1p", "sqrt", "power", "box-cox", "yeo-johnson", "expression")),
        P("power", "float", 2, min=-10, max=10),
        P("expression_text", "expression", "x"),
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
    """数据概览：行数/列数/类型/缺失率/唯一值数/时间范围，也支持流式单遍统计。"""

    metadata = Meta(
        "visual.overview", "数据概览", "visual", "Shape, dtypes, missing rates, summary and time range"
    )
    input_ports = TABLE_IN
    output_ports = (Out("overview", T.VISUALIZATION),)
    parameter_schema = (P("time_column", "column", None),)
    accepts_streaming = True

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        dataset = inputs["dataset"]
        if isinstance(dataset, StreamedDataset):
            overview = visualization.overview_stream(dataset.chunks(), **self.parameters)
            return Result({"overview": overview})
        return Result({"overview": visualization.overview(dataset, **self.parameters)})


class StatisticalFeatureComponent(BaseComponent):
    """统计特征：窗口内的均值/标准差/RMS/峰度/波峰因数等（默认 mean/std/rms）。"""

    metadata = Meta(
        "feature.statistical",
        "统计特征",
        "feature",
        "Grouped/windowed statistics and aligned window labels",
        subcategory="统计 Statistical",
        tags=("window", "rms", "时序"),
        search_keywords=("statistical features", "feature extraction", "统计特征", "窗口特征"),
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
        "Linear, polynomial or exponential trends with residual and R² features",
        subcategory="拟合 Fitting",
        search_keywords=("curve fitting", "trend features", "趋势拟合", "拟合特征"),
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
        enum("method", "mean", ("mean", "std", "median", "max_repeat")),
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
        enum("method", "first_difference", ("first_difference", "second_difference", "autocorrelation")),
        P("lag", "integer", 1, min=1),
        P("window", "integer", 20, min=3),
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
        "Windowed approximate entropy and histogram-based information entropy",
        subcategory="非线性 Nonlinear",
        tags=("entropy", "approximate entropy", "近似熵", "信息熵"),
        search_keywords=("nonlinear features", "complexity", "熵特征"),
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
            options=("approximate_entropy", "information_entropy"),
        ),
        P("bins", "integer", 16, min=2, max=200),
        P("embedding_dimension", "integer", 2, min=1, max=5),
        P("tolerance_ratio", "float", 0.2, min=0),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result(sequence_features.entropy_features(inputs["dataset"], **self.parameters))


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
        "FFT amplitude features per window: dominant frequency, centroid, entropy, band and harmonic ratios",
        subcategory="频域 Frequency",
        tags=("fft", "spectrum", "时序"),
        search_keywords=("frequency features", "frequency domain", "频率特征", "频域特征"),
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
    CentralTendencyComponent,
    DispersionComponent,
    CorrelationComponent,
    DistributionCheckComponent,
    PeriodicityCheckComponent,
    ConceptDriftComponent,
    CrossRelationComponent,
    AnomalyExplorationComponent,
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
    ARMAComponent,
    KNNDetectorComponent,
    IsolationForestDetectorComponent,
    PersistenceDetectorComponent,
    LabelComponent,
    SelectFeatureComponent,
    FeatureImputationComponent,
    MergeFeatureComponent,
    CompareModelsComponent,
)
