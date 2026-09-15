"""Thin component adapters; numerical implementations live in fault_core."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, ClassVar

import pandas as pd

from fault_core import (
    data,
    exploration,
    features,
    models,
    preprocessing,
    quality,
    reduction,
    selection,
    visualization,
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
    return P(name, "enum", default, options=options, description=description)


COLS = P(
    "columns",
    "column_list",
    [],
    description="Column names; numeric components default to all numeric columns",
)
REQUIRED_COLS = P("columns", "column_list", None, required=True)
DATA_IN = (In("dataset", T.DATASET),)
DATA_OUT = (Out("dataset", T.DATASET),)
FEATURES_IN = (In("features", T.FEATURE_DATASET),)
WINDOW = (
    REQUIRED_COLS,
    P("group_column", "column", None, description="Equipment/run identifier"),
    P("label_column", "column", None, description="Generates labels aligned with feature windows"),
    P("time_column", "column", None),
    P("window_size", "integer", 0, min=0, description="0 = entire group; otherwise complete windows"),
    P("step", "integer", 0, min=0, description="0 = non-overlapping windows"),
    enum("label_policy", "strict", ("strict", "last", "mode")),
)
VALIDATION_PARAMS = (
    enum("split_method", "stratified", ("stratified", "group", "temporal")),
    P("test_size", "float", 0.25, min=0.05, max=0.5),
    P("random_state", "integer", 42, min=0),
)


class DataInputComponent(BaseComponent):
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
        context.resolve_data_path(self.parameters["path"])
        if self._format(context) == "parquet":
            try:
                import pyarrow  # noqa: F401
            except ImportError as exc:
                raise ValueError(
                    "Parquet input requires pyarrow: pip install 'fault-prediction-platform[parquet]'"
                ) from exc

    def external_fingerprint(self, context: ExecutionContext) -> str:
        with context.resolve_data_path(self.parameters["path"]).open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()

    def _format(self, context: ExecutionContext) -> str:
        suffix = context.resolve_data_path(self.parameters["path"]).suffix.lower()
        return "parquet" if suffix in {".parquet", ".pq"} else self.parameters["format"]

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        path = context.resolve_data_path(self.parameters["path"])
        columns = self.parameters["columns"] or None
        max_rows = self.parameters["max_rows"] or None
        warnings: list[str] = []
        if self.parameters["streaming"]:
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
        value = inputs["dataset"]
        if not isinstance(value, StreamedDataset):
            return Result({"dataset": value})
        frame = value.materialize()
        return Result(
            {"dataset": frame},
            [f"Materialised {len(frame)} rows into memory; peak memory grows with the input size."],
        )


class QualityComponent(BaseComponent):
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
        report = quality.quality_report(inputs["dataset"], **self.parameters)
        return Result({"report": report}, list(report["findings"]))


class FilterComponent(BaseComponent):
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
    metadata = Meta("data.row_operation", "行操作", "data", "Sample, sort, slice, drop or deduplicate rows")
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
    metadata = Meta(
        "data.column_operation", "列操作", "data", "Select, drop, rename, reorder, create or cast columns"
    )
    input_ports, output_ports = DATA_IN, DATA_OUT
    parameter_schema = (
        enum("operation", "select", ("select", "drop", "rename", "reorder", "create", "cast_type")),
        COLS,
        P("mapping", "object", {}),
        P("name", "string", ""),
        P("expression_text", "expression", ""),
        enum("dtype", "float64", ("float64", "int64", "string", "bool")),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"dataset": data.column_operation(inputs["dataset"], **self.parameters)})


class NormalizationComponent(BaseComponent):
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
    metadata = Meta(
        "data.transformation", "数值转换", "data", "Log, power and bounded arithmetic expressions"
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
    metadata = Meta("explore.central_tendency", "集中趋势", "explore", "Mean, median, mode and weighted mean")
    input_ports = DATA_IN
    output_ports = (Out("statistics", T.STATISTICS),)
    parameter_schema = (
        COLS,
        enum("method", "mean", ("mean", "median", "mode", "weighted_mean")),
        P("weight_column", "column", None),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"statistics": exploration.central_tendency(inputs["dataset"], **self.parameters)})


class DispersionComponent(BaseComponent):
    metadata = Meta(
        "explore.dispersion", "离散度量", "explore", "Variance, standard deviation, range, IQR, MAD and CV"
    )
    input_ports = DATA_IN
    output_ports = (Out("statistics", T.STATISTICS),)
    parameter_schema = (COLS, enum("method", "std", ("std", "variance", "range", "iqr", "mad", "cv")))

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"statistics": exploration.dispersion(inputs["dataset"], **self.parameters)})


class CorrelationComponent(BaseComponent):
    metadata = Meta(
        "explore.correlation", "相关性度量", "explore", "Pearson, Spearman or Kendall correlation"
    )
    input_ports = DATA_IN
    output_ports = (Out("matrix", T.CORRELATION),)
    parameter_schema = (COLS, enum("method", "pearson", ("pearson", "spearman", "kendall")))

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"matrix": exploration.correlation(inputs["dataset"], **self.parameters)})


class ScatterPlotComponent(BaseComponent):
    metadata = Meta("visual.scatter", "散点图", "visual", "Bounded scatter plot with optional groups")
    input_ports = DATA_IN
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
    metadata = Meta("visual.line", "折线图", "visual", "Time series plot with bounded samples")
    input_ports = DATA_IN
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


class DataOverviewComponent(BaseComponent):
    metadata = Meta(
        "visual.overview", "数据概览", "visual", "Shape, dtypes, missing rates, summary and time range"
    )
    input_ports = DATA_IN
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
    metadata = Meta(
        "feature.statistical",
        "统计特征",
        "feature",
        "Grouped/windowed statistics and aligned window labels",
        tags=("window", "rms", "时序"),
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
        dataset = inputs["dataset"]
        if isinstance(dataset, StreamedDataset):
            return Result(
                features.extract_features_stream(dataset.chunks(), **self.parameters, attrs=dataset.attrs)
            )
        return Result(features.extract_features(dataset, **self.parameters))


class FittingFeatureComponent(BaseComponent):
    metadata = Meta(
        "feature.fitting",
        "拟合特征",
        "feature",
        "Linear, polynomial or exponential trends with residual and R² features",
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
        dataset = inputs["dataset"]
        if isinstance(dataset, StreamedDataset):
            return Result(
                features.extract_features_stream(
                    dataset.chunks(), kind="fitting", **self.parameters, attrs=dataset.attrs
                )
            )
        return Result(features.extract_features(dataset, kind="fitting", **self.parameters))


class CategoricalFeatureComponent(BaseComponent):
    metadata = Meta(
        "feature.categorical",
        "分类特征",
        "feature",
        "Fit a reusable categorical encoder and produce aligned numeric training features",
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
        return Result(features.fit_categorical(inputs["dataset"], **self.parameters))


class CategoricalTransformComponent(BaseComponent):
    metadata = Meta(
        "feature.categorical_transform",
        "分类特征变换",
        "feature",
        "Apply a fitted categorical encoder without learning from inference data",
        tags=("categorical", "inference", "transform"),
    )
    input_ports = (
        In("dataset", T.DATASET),
        In("encoder", T.FEATURE_TRANSFORMER),
    )
    output_ports = (Out("features", T.FEATURE_DATASET),)

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"features": inputs["encoder"].transform(inputs["dataset"])})


class SpectralFeatureComponent(BaseComponent):
    metadata = Meta(
        "feature.spectral",
        "频域特征",
        "feature",
        "FFT amplitude features per window: dominant frequency, centroid, entropy, band and harmonic ratios",
        subcategory="频域",
        tags=("fft", "spectrum", "时序"),
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
        dataset = inputs["dataset"]
        if isinstance(dataset, StreamedDataset):
            outputs = features.spectral_stream(dataset.chunks(), **self.parameters, attrs=dataset.attrs)
        else:
            outputs = features.spectral(dataset, **self.parameters)
        warnings = outputs.pop("warnings", [])
        return Result(outputs, warnings)


class ScoreSelectComponent(BaseComponent):
    metadata = Meta(
        "feature.score_select",
        "特征评分选择",
        "feature",
        "Rank features by variance, correlation pruning, mutual information or model importance",
        subcategory="选择",
        tags=("selection", "importance"),
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
        return Result(
            selection.select_features(inputs["features"], labels=inputs.get("labels"), **self.parameters)
        )


class PcaComponent(BaseComponent):
    metadata = Meta(
        "feature.pca",
        "主成分分析",
        "feature",
        "Project features onto principal components and report the explained variance",
        subcategory="降维",
        tags=("pca", "降维"),
    )
    input_ports = FEATURES_IN
    output_ports = (Out("features", T.FEATURE_DATASET), Out("variance", T.STATISTICS))
    parameter_schema = (
        P("n_components", "integer", 2, min=1),
        COLS,
        P("whiten", "boolean", False),
    )

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
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
    input_ports = (In("features", T.FEATURE_DATASET), In("labels", T.LABEL_VECTOR))
    output_ports = (Out("model", T.MODEL), Out("prediction", T.PREDICTION), Out("metrics", T.METRICS))
    algorithm: ClassVar[str]

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        outputs = models.validate_model(
            inputs["features"], inputs["labels"], self.algorithm, **self.parameters
        )
        return Result(outputs, outputs["metrics"]["warnings"])


class RandomForestComponent(ValidationComponent):
    metadata = Meta(
        "validation.random_forest",
        "随机森林",
        "validation",
        "Random Forest classification with reproducible holdout evaluation",
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
    metadata = Meta(
        "validation.xgboost", "XGBoost", "validation", "XGBoost classification; optional xgboost dependency"
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


class LabelComponent(BaseComponent):
    metadata = Meta("data.labels", "标签向量", "data", "Extract row-aligned labels for tabular features")
    input_ports = DATA_IN
    output_ports = (Out("labels", T.LABEL_VECTOR),)
    parameter_schema = (P("column", "column", None, required=True),)

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"labels": inputs["dataset"][self.parameters["column"]].copy()})


class SelectFeatureComponent(BaseComponent):
    metadata = Meta(
        "feature.select", "选择已有特征", "feature", "Explicit Dataset to FeatureDataset conversion"
    )
    input_ports = DATA_IN
    output_ports = (Out("features", T.FEATURE_DATASET),)
    parameter_schema = (REQUIRED_COLS,)

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        columns = data.numeric_columns(inputs["dataset"], self.parameters["columns"])
        return Result({"features": inputs["dataset"][columns].copy()})


class MergeFeatureComponent(BaseComponent):
    metadata = Meta(
        "feature.merge",
        "合并特征",
        "feature",
        "Join two feature branches with identical row/window provenance",
    )
    input_ports = (In("left", T.FEATURE_DATASET), In("right", T.FEATURE_DATASET))
    output_ports = (Out("features", T.FEATURE_DATASET),)

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        return Result({"features": features.merge_features(inputs["left"], inputs["right"])})


class CompareModelsComponent(BaseComponent):
    metadata = Meta(
        "validation.compare",
        "模型对比",
        "validation",
        "Compare up to three model metrics on identical holdout rows",
    )
    input_ports = (In("first", T.METRICS), In("second", T.METRICS), In("third", T.METRICS, False))
    output_ports = (Out("comparison", T.STATISTICS),)

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> Result:
        scores = list(inputs.values())
        if any(s["test_indices"] != scores[0]["test_indices"] for s in scores[1:]):
            raise ValueError("Model comparison requires the same test indices")
        keys = ("algorithm", "accuracy", "precision", "recall", "f1", "roc_auc", "train_count", "test_count")
        return Result({"comparison": {"rows": [{k: score[k] for k in keys} for score in scores]}})


BUILTIN_COMPONENTS = (
    DataInputComponent,
    MaterializeComponent,
    QualityComponent,
    FilterComponent,
    RowOperationComponent,
    ColumnOperationComponent,
    NormalizationComponent,
    StandardizationComponent,
    TransformationComponent,
    CentralTendencyComponent,
    DispersionComponent,
    CorrelationComponent,
    ScatterPlotComponent,
    LinePlotComponent,
    DataOverviewComponent,
    StatisticalFeatureComponent,
    FittingFeatureComponent,
    CategoricalFeatureComponent,
    CategoricalTransformComponent,
    SpectralFeatureComponent,
    ScoreSelectComponent,
    PcaComponent,
    RandomForestComponent,
    SVMComponent,
    XGBoostComponent,
    LabelComponent,
    SelectFeatureComponent,
    MergeFeatureComponent,
    CompareModelsComponent,
)
