# 组件参考

由 scripts/export_catalog.py 从 Registry 自动生成。

## data.input · 数据输入

Read a local CSV or Parquet file, optionally with a column projection and row limit

**输入**：无

**输出**：dataset : Dataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| path | string | null | 是 |  |
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
| operation | enum | "select" | 否 | select, drop, rename, reorder, create, cast_type |
| columns | column_list | [] | 否 |  |
| mapping | object | {} | 否 |  |
| name | string | "" | 否 |  |
| expression_text | expression | "" | 否 |  |
| dtype | enum | "float64" | 否 | float64, int64, string, bool |

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
| method | enum | "log1p" | 否 | log, log1p, sqrt, power, box-cox, yeo-johnson, expression |
| power | float | 2 | 否 |  min=-10, max=10 |
| expression_text | expression | "x" | 否 |  |

## explore.central_tendency · 集中趋势

Mean, median, mode and weighted mean

**输入**：dataset : Dataset

**输出**：statistics : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| method | enum | "mean" | 否 | mean, median, mode, weighted_mean |
| weight_column | column | null | 否 |  |

## explore.dispersion · 离散度量

Variance, standard deviation, range, IQR, MAD and CV

**输入**：dataset : Dataset

**输出**：statistics : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| method | enum | "std" | 否 | std, variance, range, iqr, mad, cv |

## explore.correlation · 相关性度量

Pearson, Spearman or Kendall correlation

**输入**：dataset : Dataset

**输出**：matrix : CorrelationMatrix

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | [] | 否 |  |
| method | enum | "pearson" | 否 | pearson, spearman, kendall |

## visual.scatter · 散点图

Bounded scatter plot with optional groups

**输入**：dataset : Dataset

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

**输入**：dataset : Dataset

**输出**：plot : PlotArtifact

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| time_column | column | null | 是 |  |
| value_columns | column_list | null | 是 |  |
| group | column | null | 否 |  |
| title | string | "Time series" | 否 |  |

## visual.overview · 数据概览

Shape, dtypes, missing rates, summary and time range

**输入**：dataset : Dataset

**输出**：overview : Visualization

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| time_column | column | null | 否 |  |

## feature.statistical · 统计特征

Grouped/windowed statistics and aligned window labels

**输入**：dataset : Dataset

**输出**：features : FeatureDataset，labels : LabelVector（可选）

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| group_column | column | null | 否 |  |
| label_column | column | null | 否 |  |
| time_column | column | null | 否 |  |
| window_size | integer | 0 | 否 |  min=0, max=None |
| step | integer | 0 | 否 |  min=0, max=None |
| label_policy | enum | "strict" | 否 | strict, last, mode |
| features | feature_list | ["mean", "std", "rms"] | 是 | mean, std, variance, min, max, median, rms, skewness, kurtosis, quantile, range, iqr, mad, peak, crest_factor |
| quantile | float | 0.75 | 否 |  min=0, max=1 |

## feature.fitting · 拟合特征

Linear, polynomial or exponential trends with residual and R² features

**输入**：dataset : Dataset

**输出**：features : FeatureDataset，labels : LabelVector（可选）

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| group_column | column | null | 否 |  |
| label_column | column | null | 否 |  |
| time_column | column | null | 否 |  |
| window_size | integer | 0 | 否 |  min=0, max=None |
| step | integer | 0 | 否 |  min=0, max=None |
| label_policy | enum | "strict" | 否 | strict, last, mode |
| fitting_method | enum | "linear" | 否 | linear, polynomial, exponential |
| degree | integer | 2 | 否 |  min=1, max=5 |

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

FFT amplitude features per window: dominant frequency, centroid, entropy, band and harmonic ratios

**输入**：dataset : Dataset

**输出**：features : FeatureDataset，labels : LabelVector（可选）

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
| columns | column_list | null | 是 |  |
| group_column | column | null | 否 |  |
| label_column | column | null | 否 |  |
| time_column | column | null | 否 |  |
| window_size | integer | 0 | 否 |  min=0, max=None |
| step | integer | 0 | 否 |  min=0, max=None |
| label_policy | enum | "strict" | 否 | strict, last, mode |
| sampling_rate | float | null | 是 |  |
| features | feature_list | ["dominant_frequency", "spectral_centroid", "spectral_entropy", "band_energy_ratio"] | 是 | dominant_frequency, dominant_amplitude, spectral_centroid, spectral_spread, spectral_entropy, spectral_rms, high_frequency_ratio, harmonic_ratio, band_energy_ratio |
| band_edges | list | [0.25, 0.5] | 否 |  |
| harmonic_tolerance | float | 0.02 | 否 |  min=0, max=0.5 |
| flat_policy | enum | "nan" | 否 | nan, skip, error |
| flat_threshold | float | 0.0 | 否 |  min=0, max=None |

## feature.score_select · 特征评分选择

Rank features by variance, correlation pruning, mutual information or model importance

**输入**：features : FeatureDataset，labels : LabelVector

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
| split_method | enum | "stratified" | 否 | stratified, group, temporal |
| test_size | float | 0.25 | 否 |  min=0.05, max=0.5 |
| random_state | integer | 42 | 否 |  min=0, max=None |
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
| split_method | enum | "stratified" | 否 | stratified, group, temporal |
| test_size | float | 0.25 | 否 |  min=0.05, max=0.5 |
| random_state | integer | 42 | 否 |  min=0, max=None |
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
| split_method | enum | "stratified" | 否 | stratified, group, temporal |
| test_size | float | 0.25 | 否 |  min=0.05, max=0.5 |
| random_state | integer | 42 | 否 |  min=0, max=None |
| n_estimators | integer | 100 | 否 |  min=1, max=2000 |
| max_depth | integer | 4 | 否 |  min=1, max=32 |
| learning_rate | float | 0.1 | 否 |  min=0.0001, max=1 |
| subsample | float | 1.0 | 否 |  min=0.01, max=1 |
| colsample_bytree | float | 1.0 | 否 |  min=0.01, max=1 |
| objective | enum | "auto" | 否 | auto, binary:logistic, multi:softprob |

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

## feature.merge · 合并特征

Join two feature branches with identical row/window provenance

**输入**：left : FeatureDataset，right : FeatureDataset

**输出**：features : FeatureDataset

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |

## validation.compare · 模型对比

Compare up to three model metrics on identical holdout rows

**输入**：first : Metrics，second : Metrics，third : Metrics

**输出**：comparison : StatisticsResult

| 参数 | 类型 | 默认值 | 必填 | 选项 |
| --- | --- | --- | --- | --- |
