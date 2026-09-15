# Component map (curated)

All 56 built-in components, grouped by the stage you meet them in. This file is a **guide**:
what a component is for, when *not* to use it, and which parameters actually change the
result. The authoritative schema is `get_component_schema(component_type)` (types, defaults,
ranges, enums); `docs/components.md` is the same catalogue in bulk.

Port notation: `input:Type → output:Type`. A model-facing component is useless unless both its
`features` and its `labels` inputs are wired.

---

## Data (16)

### data.input — 数据输入
`— → dataset:Dataset`
Reads CSV or Parquet, relative to `data_root`. **Key params:** `path` (required, relative),
`format`, `separator`, `encoding`, `columns` (projection), `max_rows`, `filters` (Parquet
pushdown), `streaming`, `chunk_rows`. Use as the single source of every graph; bound it before
anything else when the file is large.

### data.filter — 条件过滤
`dataset:Dataset → dataset:Dataset`
Row filter. **Key params:** `column`, `operator` (`gt`, `ge`, `lt`, `le`, `eq`, `ne`, `in`,
`between`), `value`, `conditions` + `logical_operator` for multiple clauses. Use to scope a
dataset (one asset, one label, a time window). Careful: filtering **after** windowing breaks
merge provenance — filter before the window components.

### data.row_operation — 行操作
`dataset:Dataset → dataset:Dataset`
**`operation`:** `head`, `tail`, `range`, `sample`, `sort`, `drop_rows`, `remove_duplicates`,
`drop_missing`. Use `remove_duplicates` when `data.quality` reports duplicates, `sort` before
streaming (rows must be contiguous per group). Any of these breaks merge provenance for
features — keep them upstream of windowing.

### data.column_operation — 列操作
`dataset:Dataset → dataset:Dataset`
**`operation`:** `select`, `drop`, `rename`, `reorder`, `create`, `cast_type`, `drop_empty`,
`drop_constant`, `trim_extrema`. `drop_constant` and `drop_empty` are the cheap part of the
quality pre-check; `create`/`expression_text` builds a derived column; `cast_type` fixes a
numeric column that arrived as text.

### data.time_resample — 按时间重采样
`dataset:Dataset → dataset:Dataset`
**Key params:** `time_column`, `frequency`, `columns`, `aggregation`, `group_column`,
`fill_method`. Use when raw samples are irregularly spaced. It changes the row set, so it must
come **before** windowing; never between two branches you intend to merge.

### data.split — 数据切分
`dataset:Dataset → train:Dataset, test:Dataset`
Row-level split outside the validators. **`method`:** `random`, `temporal`, `group`;
`group_column`, `stratify_column`, `test_size`, `random_state`. Use for a quick sanity
inspection; for model evaluation prefer the validator's own `split_method`, which carries the
leakage guards.

### data.neighbor_features — 临近数据纳入
`dataset:Dataset → dataset:Dataset`
Adds lagged/leading values of `columns` at `offsets`, per `group_column`, ordered by
`time_column`, with `drop_missing`. Use for short-horizon context before windowing.

### data.imputation — 缺失值填充
`dataset:Dataset → dataset:Dataset`
Raw-frame imputation. **`method`:** `mean`, `interpolate` (+`interpolation_method`:
`linear`/`nearest`), with `columns` and `group_column`. Prefer `interpolate` for time series;
use `feature.imputation` instead when the NaN appears only after feature extraction.

### data.normalization — 规范化
`dataset:Dataset → dataset:Dataset`
**`method`:** `minmax`, `l1`, `l2`, `maxabs` (+`feature_range`). Scaling fitted on the whole
frame: acceptable on an exploration branch, a leakage risk before a model. The SVM validator
scales inside its training split instead.

### data.standardization — 标准化
`dataset:Dataset → dataset:Dataset`
**`method`:** `zscore`, `robust`; `with_mean`, `with_std`. Same leakage caveat as
normalization; `robust` resists outliers.

### data.transformation — 数值转换
`dataset:Dataset → dataset:Dataset`
**`method`:** `log`, `log1p`, `sqrt`, `power`, `box-cox`, `yeo-johnson`, `expression`. Use to
make a channel closer to symmetric before statistics/fitting.

### data.binarize — 特征二值化
`dataset:Dataset → dataset:Dataset`
`columns`, `threshold`, `keep_original`, `suffix`. Use for indicator channels (above/below a
limit); it destroys magnitude, so keep the original when the model may need it.

### data.asset_key — 资产标识
`dataset:Dataset → dataset:Dataset`
Derives the owning asset from an instance key. **`mode`:** `split` (`separator`, `index`) or
`regex` (one capture group via `pattern`); `column` in, `target` out (default `asset`). The
first step of every asset-level holdout — feed its output to the window components.

### data.quality — 数据质量预检
`dataset:Dataset → report:Visualization`
Terminal branch. **Key params:** `columns`, `group_column`, `label_column`, `time_column`,
`window_size`, `step`, `max_groups`, `flat_threshold`. Configuration must mirror the window
component you will use. Reports all-NaN, constant (overall and per-group), flat-window ratios,
duplicates, and mixed-label windows. Run it before modelling, always. It needs the whole frame,
so it does **not** accept a streamed dataset — run it before turning streaming on.

### data.materialize — 物化数据
`dataset:Dataset → dataset:Dataset`
Turns a streamed dataset into an in-memory one. Insert it when a global component refuses
streaming input; it warns, and the warning belongs in your report (memory is no longer
bounded).

### data.labels — 标签向量
`dataset:Dataset → labels:LabelVector`
Extracts a row-aligned label column. **Not** for windowed features — take `labels` from the
window component instead, or the lengths will not match.

---

## Explore (8, all terminal)

These answer questions about the data. Their outputs (`StatisticsResult`, `CorrelationMatrix`,
`Prediction`) are not model inputs.

`explore.central_tendency`, `explore.dispersion`, `explore.correlation`, `explore.distribution`
and `explore.anomaly` take a table, so they accept a **`FeatureDataset`** as well as raw data —
pointing one at a feature branch is the quickest way to sanity-check engineered features.
`explore.periodicity`, `explore.cross_relation` and `explore.concept_drift` stay raw-signal only.

### explore.central_tendency — 集中趋势
`method`: `mean`, `median`, `mode`, `weighted_mean` (+`weight_column`).

### explore.dispersion — 离散度量
`method`: `std`, `variance`, `range`, `iqr`, `mad`, `cv`.

### explore.correlation — 相关性度量
Pairwise correlation. `columns`, `method`: `pearson`, `spearman`, `kendall`. Use to spot
redundant channels before feature selection.

### explore.distribution — 分布检查
`columns`, `bins`. Use to see skew, multimodality and clipping — the shape that decides whether
`data.transformation` helps.

### explore.periodicity — 周期性检查
`columns`, `max_lag`, `sampling_rate`. Use before committing to `feature.spectral`: if there is
no periodic structure, spectral features are noise.

### explore.concept_drift — 概念漂移
`in: reference:Dataset, current:Dataset`; `columns`, `bins`, `psi_threshold`, `alpha`. Two
datasets in, drift statistics out. Use to decide whether a split is stable, or to justify
retraining.

### explore.cross_relation — 互相关与互协方差
`first_column`, `second_column`, `method`, `max_lag`, `normalize`. Use to find a lag between two
channels before building lag features.

### explore.anomaly — 异常探索
`dataset:Dataset → prediction:Prediction`; `method`: `boxplot`, `dynamic_threshold`,
`hyperbolic_smoothing`, plus `window`, `threshold`, `iqr_multiplier`, `group_column`,
`time_column`. Threshold-based exploration — report the threshold, since it *is* the rule.

---

## Visual (8, all terminal)

`visual.overview` outputs `Visualization`; the rest output `PlotArtifact`. They render in the
web designer; MCP only reports that they ran. Use `max_points` to keep big plots usable.

`visual.overview` (`time_column`) — columns, dtypes and missingness at a glance; it will **not**
show constant or held channels, which is what `data.quality` is for.
`visual.line` (`time_column`, `value_columns`, `group`, `title`) — the workhorse for signals.
`visual.scatter` (`x`, `y`, `group`, `x_label`, `y_label`) — separation between classes.
`visual.subplot` (`x`, `value_columns`, `kind`, `max_points`) — many channels, one figure.
`visual.histogram` (`columns`, `bins`, `density`, `title`) — value distributions.
`visual.compare` (`first:Dataset`, `second:Dataset`; `columns`, `x_column`) — before/after a
filter or a repair.
`visual.relationship` (`columns`, `method`, `threshold`) — correlation heat map.
`visual.overview`, `visual.line`, `visual.scatter`, `visual.subplot`, `visual.histogram` and
`visual.relationship` accept **`FeatureDataset` as well as `Dataset`**: any feature branch can be
inspected in place, which is how you show a human what the pipeline actually produced.
`visual.compare` and `visual.anomaly` stay raw-signal only.
`visual.anomaly` (`dataset:Dataset`, `prediction:Prediction`; `x`, `y`, `anomaly_column`) —
overlay detector output on the raw signal; the fastest way to explain a detector to a human.

---

## Feature (13)

### The window producers (raw `Dataset` → `FeatureDataset` + `LabelVector`)

All four share `columns`, `group_column`, `asset_column`, `label_column`, `time_column`,
`window_size`, `step`, `label_policy`. **They must agree on all of them** if you intend to
merge their output.

**feature.statistical** — 统计 Statistical
`features`: `mean`, `std`, `variance`, `min`, `max`, `median`, `rms`, `skewness`, `kurtosis`,
`quantile` (+`quantile`), `range`, `iqr`, `mad`, `peak`, `crest_factor`. The default starting
point for level/shape.

**feature.fitting** — 拟合 Fitting
`fitting_method`: `linear`, `polynomial` (+`degree`), `exponential`. Emits trend slope, residual
and R²-style features. The degradation/onset workhorse.

**feature.spectral** — 频域 Frequency
`sampling_rate` (**required**), `features` (`dominant_frequency`, `dominant_amplitude`,
`spectral_centroid`, `spectral_spread`, `spectral_entropy`, `spectral_rms`,
`high_frequency_ratio`, `harmonic_ratio`, `band_energy_ratio`), `band_edges` (fractions of
Nyquist), `harmonic_tolerance`, `flat_policy` (`nan` default / `skip` / `error`),
`flat_threshold`. Requires real variation and a known rate; expect NaN rows where a channel is
held or quantised.

**feature.entropy** — 非线性 Nonlinear
`methods`: `approximate_entropy`, `information_entropy`; `bins`, `embedding_dimension`,
`tolerance_ratio`. Use when irregularity, not amplitude, carries the fault signature. No
streaming support (unlike statistical/fitting/spectral).

### Other feature components

**feature.rolling_statistics** — 统计 Statistical
`dataset:Dataset → features:FeatureDataset` (no labels). `method`: `mean`, `std`, `median`,
`max_repeat`; `window`, `group_column`, `time_column`. Row-aligned rolling summaries; handy for
detecting held values (`max_repeat`).

**feature.temporal** — 时域 Time Domain
`method`: `first_difference`, `second_difference`, `autocorrelation`; `lag`, `window`. No
labels port — use it as an extra branch, or accept that it cannot be a validator's label source.

**feature.categorical** — 分类 Categorical
`Dataset → features:FeatureDataset, encoder:FeatureTransformer`; `method`, `target_column`,
`random_state`, `handle_unknown`. Fits an encoder. Reuse it through
`feature.categorical_transform` instead of refitting on new data.

**feature.categorical_transform** — 分类 Categorical
`Dataset + encoder:FeatureTransformer → features:FeatureDataset`. Applies a fitted encoder
(unseen values follow `handle_unknown`). Wire the encoder from the same pipeline.

**feature.select** — 组合 Composition
`Dataset → features:FeatureDataset`; `columns`. Use when the table already contains the
features (pre-computed or from an external step).

**feature.merge** — 组合 Composition
`left + right → features:FeatureDataset`. Requires identical index, provenance
(`source_rows`, `groups`, `assets`, `source_path`, `source_id`) and **disjoint column names**.
The only legal way to combine feature branches.

**feature.imputation** — 清洗 Cleaning
`features → features`; `method`: `mean`, `median`, `zero`, `drop_columns`; `fill_value`,
`columns`. The mandatory step between NaN-producing features (spectral, short groups) and any
model.

**feature.score_select** — 选择 Selection
`features + labels → features, scores`; `method`, `top_k`, `threshold`, `random_state`. Fitted
on **all** rows, so it sees the holdout: use it on an exploration branch or report the leakage
warning.

**feature.pca** — 表征学习 Representation Learning
`features → features, variance`; `n_components`, `columns`, `whiten`. Same full-data fitting
caveat as `feature.score_select`; `variance` is the explained-variance result.

---

## Validation (11)

### Supervised classifiers — `features:FeatureDataset + labels:LabelVector`

Common parameters: `split_method` (`stratified` default, plus `group`, `asset`, `temporal`),
`test_size`, `random_state`, `positive_class`, `class_weight`. Outputs include
`model`, `prediction`, `metrics` (and `importance` where noted).

**validation.random_forest** — `n_estimators`, `max_depth`, `min_samples_split`,
`min_samples_leaf`, `class_weight` (`balanced`, `balanced_subsample`). The default baseline;
`importance` is `FeatureImportance`.

**validation.svm** — `kernel` (`linear`, `poly`, `rbf`, `sigmoid`), `C`, `gamma`
(`scale`, `auto`), `class_weight`, `probability`. Standardisation is fitted on training rows
only. Set `probability=true` (default) when you want ROC/PR metrics.

**validation.xgboost** — `n_estimators`, `max_depth`, `learning_rate`, `subsample`,
`colsample_bytree`, `objective`. Requires the optional dependency; if it is missing, report
that rather than dropping the model quietly.

**validation.decision_tree** — `criterion`, `max_depth`, `min_samples_split`,
`min_samples_leaf`, `class_weight`. Use when the explanation matters more than the score.

**validation.reservoir_classifier** — `reservoir_size`, `spectral_radius`, `input_scale`,
`leaking_rate`, `n_steps`, `C`, `max_iter`. A cheap recurrent baseline on window features.

### Regression

**validation.linear_regression** — `features + target:LabelVector → model, prediction, metrics,
importance`; `split_method`: `random` (default), `group`, `temporal`; `fit_intercept`,
`positive`. **No asset split** — state that limitation if the question is about unseen assets.

**validation.arma** — `dataset:Dataset → model, prediction, metrics`; `column`, `p`, `q`,
`test_size`, `iterations`, `time_column`. Raw-series forecasting with a holdout tail.

### Unsupervised detectors — raw `Dataset`

**validation.isolation_forest_detector** — `columns`, `contamination`, `n_estimators`,
`random_state`. `contamination` sets the anomaly rate: it is an assumption, not a finding.

**validation.knn_detector** — `columns`, `contamination`, `neighbors`. Distance-based; scale the
columns first if their units differ.

**validation.persistence_detector** — `column`, `threshold`, `direction` (`above`, `below`,
`absolute`), `min_consecutive`, `group_column`. A rule-based "stuck signal" detector; the
threshold must come from the process, not from the model.

### Comparison

**validation.compare** — `first:Metrics`, `second:Metrics`, optional `third:Metrics →
comparison:StatisticsResult`. Refuses metrics from different holdout rows, which is exactly the
protection you want.

---

## Wiring patterns worth memorising

```
data.input ─┬─ data.asset_key ─┬─ windowA ──┐
            │                  ├─ windowB ──┼─ feature.merge ─ feature.imputation ─ model
            │                  └─ data.quality (terminal)      ▲
            │                                                   └── windowA.labels
            └─ visual.overview / explore.*   (terminal branches)
```

- Fan-out from one output is how you branch; fan-in requires `feature.merge`.
- `labels` comes from the window component, never from `data.labels`, once windows exist.
- `data.quality`, `explore.*` and `visual.*` hang off the side and end there.
- Every window branch in a merge uses identical `columns`/`group_column`/`time_column`/
  `window_size`/`step`/`asset_column`.
