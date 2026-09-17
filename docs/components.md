# 组件参考

由 scripts/export_catalog.py 从 Registry 自动生成。

## data.input · 数据输入

Read a local CSV or Parquet file, optionally with a column projection and row limit

**输入**：无

**输出**：dataset : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| path | string | null | 是 |  |
| paths | list | [] | 否 |  |
| source_column | string | "" | 否 |  |
| format | enum | "csv" | 否 | csv, parquet |
| encoding | string | "utf-8-sig" | 否 |  |
| separator | string | "," | 否 |  |
| columns | column_list | [] | 否 |  |
| max_rows | integer | 0 | 否 |  min=0, max=None |
| filters | list | [] | 否 |  |
| streaming | boolean | false | 否 |  |
| chunk_rows | integer | 200000 | 否 |  min=100, max=None |

## data.materialize · 物化数据

Load a streamed dataset fully into memory so global operations (sort, dedup, plots) can run

**输入**：dataset : Dataset

**输出**：dataset : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |

## data.asset_key · 资产标识

Derive the owning asset (well/machine) from an instance key so validation can hold out whole assets

**输入**：dataset : Dataset

**输出**：dataset : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| column | column | null | 是 |  |
| target | string | "asset" | 否 |  |
| mode | enum | "split" | 否 | split, regex |
| separator | string | "_" | 否 |  |
| index | integer | 0 | 否 |  min=0, max=None |
| pattern | expression | "" | 否 |  |

## data.quality · 数据质量预检

Per-group constant/zero columns, flat-window ratio, duplicate rows and mixed-label windows

**输入**：dataset : Dataset

**输出**：report : Visualization

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| group_column | column | null | 否 |  |
| label_column | column | null | 否 |  |
| time_column | column | null | 否 |  |
| window_size | integer | 0 | 否 |  min=0, max=None |
| step | integer | 0 | 否 |  min=0, max=None |
| max_groups | integer | 20 | 否 |  min=1, max=200 |
| flat_threshold | float | 0.0 | 否 |  min=0, max=None |

## data.filter · 条件过滤

Select rows using a typed predicate

**输入**：dataset : Dataset

**输出**：dataset : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| column | column | null | 是 |  |
| operator | enum | "gt" | 否 | gt, ge, lt, le, eq, ne, >, >=, <, <=, ==, !=, in, between |
| value | any | 0 | 是 |  |
| conditions | list | [] | 否 |  |
| logical_operator | enum | "and" | 否 | and, or |

## data.row_operation · 行操作

Sample, sort, slice, drop or deduplicate rows

**输入**：dataset : Dataset

**输出**：dataset : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| operation | enum | "head" | 否 | head, tail, range, sample, sort, drop_rows, remove_duplicates, drop_missing |
| count | integer | 10 | 否 |  min=1, max=None |
| start | integer | 0 | 否 |  min=0, max=None |
| columns | column_list | [] | 否 |  |
| ascending | boolean | true | 否 |  |
| indices | list | [] | 否 |  |
| random_state | integer | 42 | 否 |  min=0, max=None |

## data.column_operation · 列操作

Select, drop, rename, reorder, create or cast columns

**输入**：dataset : Dataset

**输出**：dataset : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| operation | enum | "select" | 否 | select, drop, rename, reorder, create, cast_type, drop_empty, drop_constant, trim_extrema |
| columns | column_list | [] | 否 |  |
| mapping | object | {} | 否 |  |
| name | string | "" | 否 |  |
| expression_text | expression | "" | 否 |  |
| dtype | enum | "float64" | 否 | float64, int64, string, bool |
| extrema_count | integer | 1 | 否 |  min=1, max=None |

## data.time_resample · 按时间重采样

Aggregate numeric signals onto a regular datetime grid

**输入**：dataset : Dataset

**输出**：dataset : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| time_column | column | null | 是 |  |
| frequency | string | null | 是 |  |
| columns | column_list | [] | 否 |  |
| aggregation | enum | "mean" | 否 | mean, median, min, max, sum, first, last |
| group_column | column | null | 否 |  |
| fill_method | enum | "none" | 否 | none, interpolate, ffill |

## data.split · 数据切分

Create reproducible random, temporal or group train/test datasets

**输入**：dataset : Dataset

**输出**：train : Dataset，test : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| method | enum | "random" | 否 | random, temporal, group |
| test_size | float | 0.25 | 否 |  min=0.05, max=0.5 |
| random_state | integer | 42 | 否 |  min=0, max=None |
| group_column | column | null | 否 |  |
| stratify_column | column | null | 否 |  |

## data.neighbor_features · 临近数据纳入

Append lag and lead values without crossing equipment groups

**输入**：dataset : Dataset

**输出**：dataset : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| offsets | list | [1] | 是 |  |
| group_column | column | null | 否 |  |
| time_column | column | null | 否 |  |
| drop_missing | boolean | false | 否 |  |

## data.imputation · 缺失值填充

Fill numeric missing values by mean or interpolation

**输入**：dataset : Dataset

**输出**：dataset : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| method | enum | "mean" | 否 | mean, max, min, interpolate |
| group_column | column | null | 否 |  |
| interpolation_method | enum | "linear" | 否 | linear, nearest |

## data.normalization · 规范化

Min-Max, L1, L2 or MaxAbs scaling

**输入**：dataset : Dataset

**输出**：dataset : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| method | enum | "minmax" | 否 | minmax, l1, l2, maxabs |
| feature_range | list | [0, 1] | 否 |  |

## data.standardization · 标准化

Z-score or robust scaling; full-data fit is marked exploratory

**输入**：dataset : Dataset

**输出**：dataset : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| method | enum | "zscore" | 否 | zscore, robust |
| with_mean | boolean | true | 否 |  |
| with_std | boolean | true | 否 |  |

## data.transformation · 数值转换

Log, power and bounded arithmetic expressions

**输入**：dataset : Dataset

**输出**：dataset : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| method | enum | "log1p" | 否 | log, log1p, sqrt, power, box-cox, yeo-johnson, quantile_uniform, quantile_normal, expression |
| power | float | 2 | 否 |  min=-10, max=10 |
| expression_text | expression | "x" | 否 |  |
| n_quantiles | integer | 1000 | 否 |  min=1, max=None |

## data.binarize · 特征二值化

Convert numeric values above a threshold to one and the rest to zero

**输入**：dataset : Dataset

**输出**：dataset : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| threshold | float | 0.0 | 否 |  |
| keep_original | boolean | true | 否 |  |
| suffix | string | "_binary" | 否 |  |

## data.polynomial_features · 多项式特征

Generate polynomial and interaction terms from numeric columns before window cutting

**输入**：dataset : Dataset

**输出**：dataset : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| degree | integer | 2 | 否 |  min=2, max=5 |
| interaction_only | boolean | false | 否 |  |
| include_bias | boolean | false | 否 |  |
| keep_original | boolean | true | 否 |  |
| max_columns | integer | 512 | 否 |  min=2, max=10000 |

## data.discretize · 离散化分箱

KBins discretization into bin indices or one-hot flags, with a full-data bound warning

**输入**：dataset : Dataset

**输出**：dataset : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| n_bins | integer | 5 | 否 |  min=2, max=200 |
| strategy | enum | "quantile" | 否 | uniform, quantile, kmeans |
| encode | enum | "ordinal" | 否 | ordinal, onehot-dense |
| keep_original | boolean | true | 否 |  |
| suffix | string | "_bin" | 否 |  |
| random_state | integer | 42 | 否 |  min=0, max=None |

## data.seasonal_difference · 同期差分

Seasonal differencing or ratio against the same phase one period earlier

**输入**：dataset : Dataset

**输出**：dataset : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| period | integer | 24 | 否 |  min=1, max=None |
| mode | enum | "difference" | 否 | difference, ratio |
| group_column | column | null | 否 |  |
| time_column | column | null | 否 |  |
| keep_original | boolean | true | 否 |  |
| suffix | string | "" | 否 |  |
| drop_missing | boolean | false | 否 |  |

## data.concat · 数据拼接

Concatenate several Dataset branches into one table; same columns required

**输入**：first : Dataset，second : Dataset，third : Dataset（可选），fourth : Dataset（可选）

**输出**：dataset : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| source_column | string | "" | 否 |  |

## explore.central_tendency · 集中趋势

Mean, median, mode and weighted mean

**输入**：dataset : Dataset | FeatureDataset

**输出**：statistics : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| method | enum | "mean" | 否 | mean, median, mode, weighted_mean |
| weight_column | column | null | 否 |  |

## explore.dispersion · 离散度量

Variance, standard deviation, range, IQR, MAD and CV

**输入**：dataset : Dataset | FeatureDataset

**输出**：statistics : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| method | enum | "std" | 否 | std, variance, range, iqr, mad, cv |

## explore.correlation · 相关性度量

Pearson, Spearman or Kendall correlation

**输入**：dataset : Dataset | FeatureDataset

**输出**：matrix : CorrelationMatrix

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| method | enum | "pearson" | 否 | pearson, spearman, kendall |

## explore.distribution · 分布检查

Quantiles, shape statistics and bounded histograms for numeric columns

**输入**：dataset : Dataset | FeatureDataset

**输出**：statistics : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| bins | integer | 20 | 否 |  min=2, max=200 |

## explore.periodicity · 周期性检查

Inspect lag autocorrelation and report the strongest candidate period

**输入**：dataset : Dataset

**输出**：statistics : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| max_lag | integer | 100 | 否 |  min=1, max=10000 |
| sampling_rate | float | null | 否 |  min=1e-09, max=None |

## explore.concept_drift · 概念漂移

Compare reference and current numeric distributions with PSI and the KS test

**输入**：reference : Dataset，current : Dataset

**输出**：statistics : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| bins | integer | 10 | 否 |  min=2, max=100 |
| psi_threshold | float | 0.2 | 否 |  min=0, max=None |
| alpha | float | 0.05 | 否 |  min=1e-06, max=1 |

## explore.cross_relation · 互相关与互协方差

Lagged cross-correlation or cross-covariance between two numeric signals

**输入**：dataset : Dataset

**输出**：matrix : CorrelationMatrix

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| first_column | column | null | 是 |  |
| second_column | column | null | 是 |  |
| method | enum | "cross_correlation" | 否 | cross_correlation, cross_covariance |
| max_lag | integer | 20 | 否 |  min=1, max=10000 |
| normalize | boolean | true | 否 |  |

## explore.anomaly · 异常探索

Boxplot bounds, rolling dynamic thresholds or robust hyperbolic-tangent smoothing

**输入**：dataset : Dataset | FeatureDataset

**输出**：prediction : Prediction

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| method | enum | "boxplot" | 否 | boxplot, dynamic_threshold, hyperbolic_smoothing |
| window | integer | 20 | 否 |  min=2, max=None |
| threshold | float | 3.0 | 否 |  min=0, max=None |
| iqr_multiplier | float | 1.5 | 否 |  min=0, max=None |
| group_column | column | null | 否 |  |
| time_column | column | null | 否 |  |

## explore.peaks · 山峰检测

Local maxima detection with prominence and minimum-distance filtering

**输入**：dataset : Dataset | FeatureDataset

**输出**：statistics : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| prominence | float | 0.0 | 否 |  min=0, max=None |
| distance | integer | 1 | 否 |  min=1, max=None |
| group_column | column | null | 否 |  |

## explore.normality · 正态性校验

Per-column normality tests (D'Agostino-Pearson or Shapiro-Wilk) with an explicit caveat

**输入**：dataset : Dataset | FeatureDataset

**输出**：statistics : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| method | enum | "normaltest" | 否 | normaltest, shapiro |
| alpha | float | 0.05 | 否 |  min=1e-06, max=0.999999 |

## explore.kl_divergence · KL散度度量

Per-column Kullback-Leibler and Jensen-Shannon divergence between reference and current data

**输入**：reference : Dataset，current : Dataset

**输出**：statistics : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| bins | integer | 10 | 否 |  min=2, max=200 |
| epsilon | float | 1e-09 | 否 |  min=0, max=None |
| js_threshold | float | 0.1 | 否 |  min=0, max=None |

## explore.acf · ACF自相关函数

Autocorrelation curve with a 95% confidence band and per-lag significance flags

**输入**：dataset : Dataset | FeatureDataset

**输出**：statistics : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| max_lag | integer | 50 | 否 |  min=1, max=None |
| alpha | float | 0.05 | 否 |  min=1e-06, max=0.999999 |
| group_column | column | null | 否 |  |

## explore.isotonic · 保序回归

Monotone (isotonic) regression between two columns, reporting blocks and Spearman rho

**输入**：dataset : Dataset | FeatureDataset

**输出**：statistics : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| x_column | column | null | 是 |  |
| y_column | column | null | 是 |  |
| increasing | boolean | true | 否 |  |
| out_of_bounds | enum | "clip" | 否 | clip, nan |
| max_points | integer | 500 | 否 |  min=10, max=5000 |

## explore.gbr_fit · 量化相关性拟合GBR

Gradient-boosting fit that quantifies how much the columns explain the target

**输入**：dataset : Dataset | FeatureDataset

**输出**：statistics : StatisticsResult，importance : FeatureImportance

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| target_column | column | null | 是 |  |
| n_estimators | integer | 100 | 否 |  min=1, max=2000 |
| learning_rate | float | 0.1 | 否 |  min=0.0001, max=1 |
| max_depth | integer | 3 | 否 |  min=1, max=32 |
| min_samples_leaf | integer | 1 | 否 |  min=1, max=None |
| subsample | float | 1.0 | 否 |  min=0.01, max=1 |
| test_size | float | 0.0 | 否 |  min=0, max=0.9 |
| random_state | integer | 42 | 否 |  min=0, max=None |
| max_points | integer | 500 | 否 |  min=10, max=5000 |

## explore.hp_filter · HP趋势过滤

Hodrick-Prescott trend/cycle separation with the cycle variance share

**输入**：dataset : Dataset | FeatureDataset

**输出**：statistics : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| column | column | null | 是 |  |
| lamb | float | 1600.0 | 否 |  min=0.0001, max=None |
| group_column | column | null | 否 |  |
| time_column | column | null | 否 |  |
| max_points | integer | 500 | 否 |  min=10, max=5000 |

## explore.stationarity · 平稳性检查

Augmented Dickey-Fuller unit-root test with asymptotic critical values

**输入**：dataset : Dataset | FeatureDataset

**输出**：statistics : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| column | column | null | 是 |  |
| max_lag | integer | 0 | 否 |  min=0, max=None |
| regression | enum | "c" | 否 | c, ct, n |
| group_column | column | null | 否 |  |
| time_column | column | null | 否 |  |

## explore.dtw · DTW距离

Dynamic time warping distance between two columns, with an optional band

**输入**：dataset : Dataset | FeatureDataset

**输出**：statistics : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| first_column | column | null | 是 |  |
| second_column | column | null | 是 |  |
| band | integer | 0 | 否 |  min=0, max=None |
| normalize | boolean | true | 否 |  |

## explore.sbd · SBD相关

Shape-based distance from the peak normalised cross-correlation

**输入**：dataset : Dataset | FeatureDataset

**输出**：statistics : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| first_column | column | null | 是 |  |
| second_column | column | null | 是 |  |
| max_lag_fraction | float | 0.5 | 否 |  min=0.01, max=1.0 |
| normalize | boolean | true | 否 |  |

## explore.slope_cosine · 斜率与余弦夹角

Rolling slopes plus the cosine between the two increment vectors

**输入**：dataset : Dataset | FeatureDataset

**输出**：prediction : Prediction

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| first_column | column | null | 是 |  |
| second_column | column | null | 是 |  |
| window | integer | 20 | 否 |  min=3, max=None |
| threshold | float | 0.5 | 否 |  min=0, max=1 |
| group_column | column | null | 否 |  |
| time_column | column | null | 否 |  |

## visual.scatter · 散点图

Bounded scatter plot with optional groups

**输入**：dataset : Dataset | FeatureDataset

**输出**：plot : PlotArtifact

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| x | column | null | 是 |  |
| y | column | null | 是 |  |
| group | column | null | 否 |  |
| title | string | "Scatter plot" | 否 |  |
| x_label | string | "" | 否 |  |
| y_label | string | "" | 否 |  |

## visual.line · 折线图

Time series plot with bounded samples

**输入**：dataset : Dataset | FeatureDataset

**输出**：plot : PlotArtifact

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| time_column | column | null | 是 |  |
| value_columns | column_list | null | 是 |  |
| group | column | null | 否 |  |
| title | string | "Time series" | 否 |  |

## visual.subplot · 子图

Create one bounded line or scatter panel per selected value column

**输入**：dataset : Dataset | FeatureDataset

**输出**：plot : PlotArtifact

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| x | column | null | 是 |  |
| value_columns | column_list | null | 是 |  |
| kind | enum | "line" | 否 | line, scatter |
| title | string | "Subplots" | 否 |  |
| max_points | integer | 500 | 否 |  min=10, max=10000 |

## visual.histogram · 直方图

Histogram counts or density for up to twelve numeric columns

**输入**：dataset : Dataset | FeatureDataset

**输出**：plot : PlotArtifact

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| bins | integer | 20 | 否 |  min=2, max=200 |
| density | boolean | false | 否 |  |
| title | string | "Histogram" | 否 |  |

## visual.compare · 对比画图

Overlay selected columns from two datasets with bounded sampling

**输入**：first : Dataset，second : Dataset

**输出**：plot : PlotArtifact

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| x_column | column | null | 否 |  |
| first_name | string | "first" | 否 |  |
| second_name | string | "second" | 否 |  |
| title | string | "Dataset comparison" | 否 |  |
| max_points | integer | 500 | 否 |  min=10, max=10000 |

## visual.anomaly · 异常点可视化

Separate normal and anomalous rows using a boolean flag column

**输入**：dataset : Dataset，prediction : Prediction（可选）

**输出**：plot : PlotArtifact

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| x | column | null | 是 |  |
| y | column | null | 是 |  |
| anomaly_column | column | null | 是 |  |
| title | string | "Anomalies" | 否 |  |
| max_points | integer | 500 | 否 |  min=10, max=10000 |

## visual.relationship · 关系图

Thresholded numeric feature relationship graph with bounded edges

**输入**：dataset : Dataset | FeatureDataset

**输出**：plot : PlotArtifact

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| method | enum | "pearson" | 否 | pearson, spearman, kendall |
| threshold | float | 0.3 | 否 |  min=0, max=1 |

## visual.overview · 数据概览

Shape, dtypes, missing rates, summary, time range and label/class balance

**输入**：dataset : Dataset | FeatureDataset，labels : LabelVector（可选）

**输出**：overview : Visualization

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| time_column | column | null | 否 |  |
| label_column | column | null | 否 |  |

## feature.statistical · 统计特征

Grouped/windowed statistics with aligned labels; window_span/step_span cut windows by time, label_policy=horizon predicts whether a fault happens inside the prediction horizon

**输入**：dataset : Dataset

**输出**：features : FeatureDataset，labels : LabelVector（可选）

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| group_column | column | null | 否 |  |
| asset_column | column | null | 否 |  |
| label_column | column | null | 否 |  |
| time_column | column | null | 否 |  |
| window_size | integer | 0 | 否 |  min=0, max=None |
| step | integer | 0 | 否 |  min=0, max=None |
| window_span | string | "" | 否 |  |
| step_span | string | "" | 否 |  |
| prediction_horizon | string | "" | 否 |  |
| prediction_gap | string | "" | 否 |  |
| current_fault_policy | enum | "drop" | 否 | drop, positive, negative |
| normal_label | string | "0" | 否 |  |
| label_policy | enum | "strict" | 否 | strict, last, mode, horizon |
| features | feature_list | ["mean", "std", "rms"] | 是 | mean, std, variance, min, max, median, rms, skewness, kurtosis, quantile, range, iqr, mad, peak, crest_factor, count, argmax_first, argmax_last, argmin_first, argmin_last, count_above_mean, count_below_mean, longest_above_mean, longest_below_mean, mean_delta, mean_abs_delta, mean_second_derivative, duplicate_point_ratio, repeated_value_ratio, duplicate_sum, time_reversal_asymmetry, std_gt_range, variance_gt_std, max_repeated, min_repeated |
| quantile | float | 0.75 | 否 |  min=0, max=1 |

## feature.fitting · 拟合特征

Linear, polynomial or exponential trends with residual and R² features; the degradation workhorse, and with label_policy=horizon it feeds failure prediction

**输入**：dataset : Dataset

**输出**：features : FeatureDataset，labels : LabelVector（可选）

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| group_column | column | null | 否 |  |
| asset_column | column | null | 否 |  |
| label_column | column | null | 否 |  |
| time_column | column | null | 否 |  |
| window_size | integer | 0 | 否 |  min=0, max=None |
| step | integer | 0 | 否 |  min=0, max=None |
| window_span | string | "" | 否 |  |
| step_span | string | "" | 否 |  |
| prediction_horizon | string | "" | 否 |  |
| prediction_gap | string | "" | 否 |  |
| current_fault_policy | enum | "drop" | 否 | drop, positive, negative |
| normal_label | string | "0" | 否 |  |
| label_policy | enum | "strict" | 否 | strict, last, mode, horizon |
| fitting_method | enum | "linear" | 否 | linear, polynomial, exponential |
| degree | integer | 2 | 否 |  min=1, max=5 |

## feature.rolling_statistics · 滚动统计特征

Rolling mean, standard deviation, median or repeated-maximum flags

**输入**：dataset : Dataset

**输出**：features : FeatureDataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| method | enum | "mean" | 否 | mean, std, variance, median, max, min, max_repeat |
| window | integer | 5 | 否 |  min=2, max=100000 |
| group_column | column | null | 否 |  |
| time_column | column | null | 否 |  |

## feature.temporal · 差分与自相关特征

Row-aligned first/second differences or rolling autocorrelation

**输入**：dataset : Dataset

**输出**：features : FeatureDataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| method | enum | "first_difference" | 否 | first_difference, second_difference, autocorrelation, sum_abs_change, peak_count |
| lag | integer | 1 | 否 |  min=1, max=None |
| window | integer | 20 | 否 |  min=3, max=None |
| prominence | float | 0.0 | 否 |  min=0, max=None |
| group_column | column | null | 否 |  |
| time_column | column | null | 否 |  |

## feature.wavelet · 小波特征

Rolling Haar multi-scale energies, dominant scale and detail peak counts

**输入**：dataset : Dataset

**输出**：features : FeatureDataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| window | integer | 32 | 否 |  min=4, max=4096 |
| levels | integer | 3 | 否 |  min=1, max=8 |
| peak_sigma | float | 3.0 | 否 |  min=0, max=None |
| group_column | column | null | 否 |  |
| time_column | column | null | 否 |  |

## feature.entropy · 熵特征

Windowed approximate entropy and histogram-based information entropy; with window_span/step_span windows can be cut by time and label_policy=horizon predicts failure inside the horizon

**输入**：dataset : Dataset

**输出**：features : FeatureDataset，labels : LabelVector（可选）

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| group_column | column | null | 否 |  |
| asset_column | column | null | 否 |  |
| label_column | column | null | 否 |  |
| time_column | column | null | 否 |  |
| window_size | integer | 0 | 否 |  min=0, max=None |
| step | integer | 0 | 否 |  min=0, max=None |
| window_span | string | "" | 否 |  |
| step_span | string | "" | 否 |  |
| prediction_horizon | string | "" | 否 |  |
| prediction_gap | string | "" | 否 |  |
| current_fault_policy | enum | "drop" | 否 | drop, positive, negative |
| normal_label | string | "0" | 否 |  |
| label_policy | enum | "strict" | 否 | strict, last, mode, horizon |
| methods | feature_list | ["approximate_entropy", "information_entropy"] | 是 | approximate_entropy, information_entropy, binned_entropy |
| bins | integer | 16 | 否 |  min=2, max=200 |
| embedding_dimension | integer | 2 | 否 |  min=1, max=5 |
| tolerance_ratio | float | 0.2 | 否 |  min=0, max=None |

## feature.categorical · 分类特征

Fit a reusable categorical encoder and produce aligned numeric training features

**输入**：dataset : Dataset

**输出**：features : FeatureDataset，encoder : FeatureTransformer

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| method | enum | "onehot" | 否 | onehot, ordinal, frequency, target, category_statistics |
| target_column | column | null | 否 |  |
| random_state | integer | 42 | 否 |  min=0, max=None |
| handle_unknown | enum | "ignore" | 否 | ignore, error |

## feature.categorical_transform · 分类特征变换

Apply a fitted categorical encoder without learning from inference data

**输入**：dataset : Dataset，encoder : FeatureTransformer

**输出**：features : FeatureDataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |

## feature.spectral · 频域特征

FFT amplitude features per window: dominant frequency, centroid, entropy, band and harmonic ratios; window_span/step_span cut windows by time and label_policy=horizon supports failure prediction

**输入**：dataset : Dataset

**输出**：features : FeatureDataset，labels : LabelVector（可选）

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| group_column | column | null | 否 |  |
| asset_column | column | null | 否 |  |
| label_column | column | null | 否 |  |
| time_column | column | null | 否 |  |
| window_size | integer | 0 | 否 |  min=0, max=None |
| step | integer | 0 | 否 |  min=0, max=None |
| window_span | string | "" | 否 |  |
| step_span | string | "" | 否 |  |
| prediction_horizon | string | "" | 否 |  |
| prediction_gap | string | "" | 否 |  |
| current_fault_policy | enum | "drop" | 否 | drop, positive, negative |
| normal_label | string | "0" | 否 |  |
| label_policy | enum | "strict" | 否 | strict, last, mode, horizon |
| sampling_rate | float | null | 是 |  |
| features | feature_list | ["dominant_frequency", "spectral_centroid", "spectral_entropy", "band_energy_ratio"] | 是 | dominant_frequency, dominant_amplitude, spectral_centroid, spectral_spread, spectral_entropy, spectral_rms, high_frequency_ratio, harmonic_ratio, band_energy_ratio |
| band_edges | list | [0.25, 0.5] | 否 |  |
| harmonic_tolerance | float | 0.02 | 否 |  min=0, max=0.5 |
| flat_policy | enum | "nan" | 否 | nan, skip, error |
| flat_threshold | float | 0.0 | 否 |  min=0, max=None |

## feature.score_select · 特征评分选择

Rank features by variance, correlation pruning, mutual information or model importance

**输入**：features : FeatureDataset，labels : LabelVector（可选）

**输出**：features : FeatureDataset，scores : FeatureImportance

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| method | enum | "variance" | 否 | variance, correlation, mutual_information, model |
| top_k | integer | 0 | 否 |  min=0, max=None |
| threshold | float | null | 否 |  |
| random_state | integer | 42 | 否 |  min=0, max=None |

## feature.pca · 主成分分析

Project features onto principal components and report the explained variance

**输入**：features : FeatureDataset

**输出**：features : FeatureDataset，variance : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| n_components | integer | 2 | 否 |  min=1, max=None |
| columns | column_list | [] | 否 |  |
| whiten | boolean | false | 否 |  |

## validation.random_forest · 随机森林

Random Forest classification with reproducible holdout evaluation

**输入**：features : FeatureDataset，labels : LabelVector

**输出**：model : Model，prediction : Prediction，metrics : Metrics，importance : FeatureImportance

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| split_method | enum | "stratified" | 否 | stratified, group, asset, temporal |
| test_size | float | 0.25 | 否 |  min=0.05, max=0.5 |
| random_state | integer | 42 | 否 |  min=0, max=None |
| positive_class | string | "" | 否 |  |
| n_estimators | integer | 100 | 否 |  min=1, max=2000 |
| max_depth | integer | null | 否 |  min=1, max=100 |
| min_samples_split | integer | 2 | 否 |  min=2, max=None |
| min_samples_leaf | integer | 1 | 否 |  min=1, max=None |
| class_weight | enum | null | 否 | balanced, balanced_subsample |

## validation.svm · 支持向量机

SVM with standardization fitted on training data only

**输入**：features : FeatureDataset，labels : LabelVector

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| split_method | enum | "stratified" | 否 | stratified, group, asset, temporal |
| test_size | float | 0.25 | 否 |  min=0.05, max=0.5 |
| random_state | integer | 42 | 否 |  min=0, max=None |
| positive_class | string | "" | 否 |  |
| kernel | enum | "rbf" | 否 | linear, poly, rbf, sigmoid |
| C | float | 1.0 | 否 |  min=1e-06, max=None |
| gamma | enum | "scale" | 否 | scale, auto |
| class_weight | enum | null | 否 | balanced |
| probability | boolean | true | 否 |  |

## validation.xgboost · XGBoost

XGBoost classification; optional xgboost dependency

**输入**：features : FeatureDataset，labels : LabelVector

**输出**：model : Model，prediction : Prediction，metrics : Metrics，importance : FeatureImportance

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| split_method | enum | "stratified" | 否 | stratified, group, asset, temporal |
| test_size | float | 0.25 | 否 |  min=0.05, max=0.5 |
| random_state | integer | 42 | 否 |  min=0, max=None |
| positive_class | string | "" | 否 |  |
| n_estimators | integer | 100 | 否 |  min=1, max=2000 |
| max_depth | integer | 4 | 否 |  min=1, max=32 |
| learning_rate | float | 0.1 | 否 |  min=0.0001, max=1 |
| subsample | float | 1.0 | 否 |  min=0.01, max=1 |
| colsample_bytree | float | 1.0 | 否 |  min=0.01, max=1 |
| objective | enum | "auto" | 否 | auto, binary:logistic, multi:softprob |

## validation.decision_tree · 决策树

Decision-tree fault classification with reproducible holdout evaluation

**输入**：features : FeatureDataset，labels : LabelVector

**输出**：model : Model，prediction : Prediction，metrics : Metrics，importance : FeatureImportance

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| split_method | enum | "stratified" | 否 | stratified, group, asset, temporal |
| test_size | float | 0.25 | 否 |  min=0.05, max=0.5 |
| random_state | integer | 42 | 否 |  min=0, max=None |
| positive_class | string | "" | 否 |  |
| criterion | enum | "gini" | 否 | gini, entropy, log_loss |
| max_depth | integer | null | 否 |  min=1, max=100 |
| min_samples_split | integer | 2 | 否 |  min=2, max=None |
| min_samples_leaf | integer | 1 | 否 |  min=1, max=None |
| class_weight | enum | null | 否 | balanced |

## validation.reservoir_classifier · 水库机分类

Echo-state feature mapping followed by logistic fault classification

**输入**：features : FeatureDataset，labels : LabelVector

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| split_method | enum | "stratified" | 否 | stratified, group, asset, temporal |
| test_size | float | 0.25 | 否 |  min=0.05, max=0.5 |
| random_state | integer | 42 | 否 |  min=0, max=None |
| positive_class | string | "" | 否 |  |
| reservoir_size | integer | 50 | 否 |  min=5, max=500 |
| spectral_radius | float | 0.9 | 否 |  min=0.01, max=1.5 |
| input_scale | float | 0.5 | 否 |  min=1e-06, max=10 |
| leaking_rate | float | 1.0 | 否 |  min=0.01, max=1 |
| n_steps | integer | 3 | 否 |  min=1, max=50 |
| C | float | 1.0 | 否 |  min=1e-06, max=None |
| max_iter | integer | 1000 | 否 |  min=100, max=10000 |
| class_weight | enum | null | 否 | balanced |

## validation.linear_regression · 线性回归

Numeric target prediction with random, group or temporal holdout metrics

**输入**：features : FeatureDataset，target : LabelVector

**输出**：model : Model，prediction : Prediction，metrics : Metrics，importance : FeatureImportance

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| split_method | enum | "random" | 否 | random, group, temporal |
| test_size | float | 0.25 | 否 |  min=0.05, max=0.5 |
| random_state | integer | 42 | 否 |  min=0, max=None |
| fit_intercept | boolean | true | 否 |  |
| positive | boolean | false | 否 |  |

## validation.ridge · 岭回归

Ridge regression with L2 shrinkage; same metric fields as linear regression

**输入**：features : FeatureDataset，target : LabelVector

**输出**：model : Model，prediction : Prediction，metrics : Metrics，importance : FeatureImportance

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| split_method | enum | "random" | 否 | random, group, temporal |
| test_size | float | 0.25 | 否 |  min=0.05, max=0.5 |
| random_state | integer | 42 | 否 |  min=0, max=None |
| alpha | float | 1.0 | 否 |  min=0, max=None |
| fit_intercept | boolean | true | 否 |  |

## validation.exponential_smoothing · 指数平滑

Holt or Holt-Winters exponential smoothing with a tail holdout forecast

**输入**：dataset : Dataset

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| column | column | null | 是 |  |
| method | enum | "holt" | 否 | holt, holt_winters |
| alpha | float | 0.3 | 否 |  min=1e-06, max=0.999999 |
| beta | float | 0.1 | 否 |  min=1e-06, max=0.999999 |
| gamma | float | 0.1 | 否 |  min=1e-06, max=0.999999 |
| seasonal_periods | integer | 24 | 否 |  min=2, max=None |
| seasonal | enum | "additive" | 否 | additive, multiplicative |
| test_size | float | 0.25 | 否 |  min=0.05, max=0.5 |
| time_column | column | null | 否 |  |
| group_column | column | null | 否 |  |

## validation.arima · ARIMA

ARIMA forecasting; requires the optional statsmodels dependency

**输入**：dataset : Dataset

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| column | column | null | 是 |  |
| order | list | [2, 1, 1] | 否 |  |
| test_size | float | 0.25 | 否 |  min=0.05, max=0.5 |
| trend | string | "c" | 否 |  |
| time_column | column | null | 否 |  |

## validation.arma · ARMA

Autoregressive moving-average fit with a temporal holdout forecast

**输入**：dataset : Dataset

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| column | column | null | 是 |  |
| p | integer | 2 | 否 |  min=0, max=50 |
| q | integer | 1 | 否 |  min=0, max=50 |
| test_size | float | 0.25 | 否 |  min=0.05, max=0.5 |
| iterations | integer | 5 | 否 |  min=1, max=100 |
| time_column | column | null | 否 |  |

## validation.knn_detector · KNN检测

Unsupervised anomaly scores from standardized k-nearest-neighbor distances

**输入**：dataset : Dataset

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| contamination | float | 0.05 | 否 |  min=1e-06, max=0.5 |
| neighbors | integer | 5 | 否 |  min=1, max=None |

## validation.isolation_forest_detector · 隔离森林检测

Unsupervised Isolation Forest scores and contamination-based anomaly flags

**输入**：dataset : Dataset

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| contamination | float | 0.05 | 否 |  min=1e-06, max=0.5 |
| random_state | integer | 42 | 否 |  min=0, max=None |
| n_estimators | integer | 100 | 否 |  min=1, max=2000 |

## validation.dbscan_detector · DBSCAN检测

Density-based detection: points outside every cluster are anomalies (eps controls the rate)

**输入**：dataset : Dataset

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| eps | float | 1.0 | 否 |  min=1e-06, max=None |
| min_samples | integer | 5 | 否 |  min=2, max=None |
| metric | enum | "euclidean" | 否 | euclidean, manhattan, chebyshev |

## validation.pca_detector · PCA检测器

PCA reconstruction-error anomaly detection with a contamination-based threshold

**输入**：dataset : Dataset

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| n_components | integer | 0 | 否 |  min=0, max=None |
| contamination | float | 0.05 | 否 |  min=1e-06, max=0.5 |
| random_state | integer | 42 | 否 |  min=0, max=None |

## validation.min_cluster_detector · Mincluster探测器

MiniBatchKMeans cluster-distance detection; n_clusters=0 picks the count by silhouette

**输入**：dataset : Dataset

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| n_clusters | integer | 0 | 否 |  min=0, max=None |
| contamination | float | 0.05 | 否 |  min=1e-06, max=0.5 |
| max_clusters | integer | 8 | 否 |  min=2, max=64 |
| batch_size | integer | 1024 | 否 |  min=16, max=None |
| silhouette_sample | integer | 5000 | 否 |  min=50, max=None |
| random_state | integer | 42 | 否 |  min=0, max=None |

## validation.kmeans · KMeans聚类

MiniBatchKMeans clustering with silhouette-based cluster-count selection

**输入**：dataset : Dataset | FeatureDataset

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| n_clusters | integer | 0 | 否 |  min=0, max=None |
| max_clusters | integer | 8 | 否 |  min=2, max=64 |
| batch_size | integer | 1024 | 否 |  min=16, max=None |
| silhouette_sample | integer | 5000 | 否 |  min=50, max=None |
| random_state | integer | 42 | 否 |  min=0, max=None |

## validation.one_class_svm · 单类SVM检测

One-class SVM novelty detection learned from normal behaviour only

**输入**：dataset : Dataset

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| nu | float | 0.05 | 否 |  min=1e-06, max=1 |
| kernel | enum | "rbf" | 否 | rbf, linear, poly, sigmoid |
| gamma | enum | "scale" | 否 | scale, auto |

## validation.level_shift_detector · LevelShift检测器

Detect a step change in the mean using a two-sample t statistic around each split point

**输入**：dataset : Dataset

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| column | column | null | 是 |  |
| window | integer | 20 | 否 |  min=2, max=None |
| threshold | float | 4.0 | 否 |  min=0, max=None |
| group_column | column | null | 否 |  |
| time_column | column | null | 否 |  |

## validation.volatility_shift_detector · VolatilityShift检测器

Detect a change in the variance level; the score is the log variance ratio in z units

**输入**：dataset : Dataset

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| column | column | null | 是 |  |
| window | integer | 20 | 否 |  min=2, max=None |
| threshold | float | 4.0 | 否 |  min=0, max=None |
| group_column | column | null | 否 |  |
| time_column | column | null | 否 |  |

## validation.seasonal_detector · Seasonal检测器

Score each row against a seasonal profile fitted on a leading reference segment

**输入**：dataset : Dataset

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| column | column | null | 是 |  |
| period | integer | 24 | 否 |  min=2, max=None |
| threshold | float | 4.0 | 否 |  min=0, max=None |
| reference_fraction | float | 0.5 | 否 |  min=0.1, max=0.9 |
| group_column | column | null | 否 |  |
| time_column | column | null | 否 |  |

## validation.autoregression_detector · AutoRegression检测器

One-step AR(p) residuals on the rows after a leading training segment

**输入**：dataset : Dataset

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| column | column | null | 是 |  |
| order | integer | 2 | 否 |  min=1, max=50 |
| threshold | float | 4.0 | 否 |  min=0, max=None |
| train_fraction | float | 0.5 | 否 |  min=0.1, max=0.9 |
| group_column | column | null | 否 |  |
| time_column | column | null | 否 |  |

## validation.esd_detector · GeneralizedESD检测器

Generalized extreme Studentized deviate test for up to a fraction of outliers

**输入**：dataset : Dataset

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| column | column | null | 是 |  |
| max_outlier_fraction | float | 0.1 | 否 |  min=0.001, max=0.5 |
| alpha | float | 0.05 | 否 |  min=1e-06, max=0.5 |
| group_column | column | null | 否 |  |
| time_column | column | null | 否 |  |

## validation.nsigma_detector · Nsigma检测

Distance from a centre in units of a scale, with global, per-group or rolling centres

**输入**：dataset : Dataset

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| column | column | null | 是 |  |
| sigma | float | 3.0 | 否 |  min=0.1, max=None |
| mode | enum | "global" | 否 | global, group, rolling |
| window | integer | 30 | 否 |  min=2, max=None |
| group_column | column | null | 否 |  |
| time_column | column | null | 否 |  |

## validation.mean_drift_detector · 均值漂移检测

CUSUM drift detector against a reference segment mean, with a slack band

**输入**：dataset : Dataset

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| column | column | null | 是 |  |
| reference_fraction | float | 0.3 | 否 |  min=0.05, max=0.9 |
| slack | float | 0.5 | 否 |  min=0, max=None |
| decision | float | 5.0 | 否 |  min=0.1, max=None |
| group_column | column | null | 否 |  |
| time_column | column | null | 否 |  |

## validation.persistence_detector · Persist检测器

Flag a threshold excursion after it persists for a configured number of rows

**输入**：dataset : Dataset

**输出**：model : Model，prediction : Prediction，metrics : Metrics

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| column | column | null | 是 |  |
| threshold | float | null | 是 |  |
| direction | enum | "above" | 否 | above, below, absolute |
| min_consecutive | integer | 3 | 否 |  min=1, max=None |
| group_column | column | null | 否 |  |

## data.labels · 标签向量

Extract row-aligned labels for tabular features

**输入**：dataset : Dataset

**输出**：labels : LabelVector

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| column | column | null | 是 |  |

## feature.select · 选择已有特征

Explicit Dataset to FeatureDataset conversion

**输入**：dataset : Dataset

**输出**：features : FeatureDataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |

## feature.imputation · 特征缺失处理

Fill or drop NaN feature columns (flat windows yield NaN spectra) before modelling

**输入**：features : FeatureDataset

**输出**：features : FeatureDataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| method | enum | "mean" | 否 | mean, median, zero, drop_columns |
| fill_value | float | 0.0 | 否 |  |

## feature.merge · 合并特征

Join two feature branches with identical row/window provenance

**输入**：left : FeatureDataset，right : FeatureDataset

**输出**：features : FeatureDataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |

## validation.grid_search · 超参搜索

Small-grid cross-validated hyper-parameter search with an explicit optimism warning

**输入**：features : FeatureDataset，labels : LabelVector

**输出**：model : Model，metrics : Metrics，importance : FeatureImportance（可选）

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| algorithm | enum | "random_forest" | 否 | random_forest, decision_tree, svm, logistic, ridge, gradient_boosting |
| param_grid | object | {"n_estimators": [100, 300], "max_depth": [4, 8]} | 是 |  |
| cv_method | enum | "stratified" | 否 | stratified, group, temporal |
| cv_folds | integer | 3 | 否 |  min=2, max=10 |
| scoring | string | "" | 否 |  |
| top_k | integer | 5 | 否 |  min=1, max=20 |

## validation.compare · 模型对比

Compare up to three model metrics on identical holdout rows

**输入**：first : Metrics，second : Metrics，third : Metrics（可选）

**输出**：comparison : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
