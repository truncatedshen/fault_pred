# 组件地图（人工整理）

全部 56 个内置组件，按你遇到它们的阶段分组。这份文件是**指南**：某个组件干什么、什么时候**不要**用、哪些参数真的会改变结果。权威 schema 永远是 `get_component_schema(component_type)`（类型、默认值、取值范围、枚举）；`docs/components.md` 是同一份数据的批量版本。

端口记法：`输入:类型 → 输出:类型`。面向模型的组件，必须 `features` 与 `labels` 都接上才有意义。

---

## 数据 (16)

### data.input — 数据输入
`— → dataset:Dataset`
读 CSV 或 Parquet，路径相对 `data_root`。**关键参数：** `path`（必填，相对路径）、`format`、`separator`、`encoding`、`columns`（投影）、`max_rows`、`filters`（Parquet 谓词下推）、`streaming`、`chunk_rows`。每张图都应该由它起头；文件大时，第一件事就是给它加边界。

### data.filter — 条件过滤
`dataset:Dataset → dataset:Dataset`
行过滤。**关键参数：** `column`、`operator`（`gt`、`ge`、`lt`、`le`、`eq`、`ne`、`in`、`between`）、`value`，多条条件用 `conditions` + `logical_operator`。用来圈定数据集（一口设备、一个类别、一段时间）。注意：**窗口之后**再过滤会破坏合并来源，过滤要放在窗口组件之前。

### data.row_operation — 行操作
`dataset:Dataset → dataset:Dataset`
**`operation`：** `head`、`tail`、`range`、`sample`、`sort`、`drop_rows`、`remove_duplicates`、`drop_missing`。`data.quality` 报出重复行时用 `remove_duplicates`；流式之前要 `sort`（同一组的行必须连续）。这些操作都会破坏特征的合并来源，留在窗口之前。

### data.column_operation — 列操作
`dataset:Dataset → dataset:Dataset`
**`operation`：** `select`、`drop`、`rename`、`reorder`、`create`、`cast_type`、`drop_empty`、`drop_constant`、`trim_extrema`。`drop_constant` 与 `drop_empty` 是质量预检里便宜的那部分；`create`/`expression_text` 造派生列；`cast_type` 修"数字被读成文本"的列。

### data.time_resample — 按时间重采样
`dataset:Dataset → dataset:Dataset`
**关键参数：** `time_column`、`frequency`、`columns`、`aggregation`、`group_column`、`fill_method`。原始采样间隔不规则时用它。它会改变行集合，所以必须在窗口**之前**；永远不要放在两条准备合并的分支之间。

### data.split — 数据切分
`dataset:Dataset → train:Dataset, test:Dataset`
验证器之外的行级切分。**`method`：** `random`、`temporal`、`group`；另有 `group_column`、`stratify_column`、`test_size`、`random_state`。拿它做快速体检可以；正经评估请用验证器自己的 `split_method`，那里带泄漏护栏。

### data.neighbor_features — 临近数据纳入
`dataset:Dataset → dataset:Dataset`
按 `group_column` 分组、按 `time_column` 排序，为 `columns` 加上 `offsets` 处的滞后/超前值，`drop_missing` 控制是否丢弃。用于窗口之前的短时上下文。

### data.imputation — 缺失值填充
`dataset:Dataset → dataset:Dataset`
原始帧填补。**`method`：** `mean`、`interpolate`（加 `interpolation_method`：`linear`/`nearest`），配合 `columns` 与 `group_column`。时序优先 `interpolate`；如果 NaN 只在特征提取之后才出现，改用 `feature.imputation`。

### data.normalization — 规范化
`dataset:Dataset → dataset:Dataset`
**`method`：** `minmax`、`l1`、`l2`、`maxabs`（加 `feature_range`）。在整帧上拟合缩放：探索分支上可以，模型前有泄漏风险。SVM 验证器是在自己的训练切分内部缩放的。

### data.standardization — 标准化
`dataset:Dataset → dataset:Dataset`
**`method`：** `zscore`、`robust`；`with_mean`、`with_std`。泄漏注意事项同上；`robust` 抗离群。

### data.transformation — 数值转换
`dataset:Dataset → dataset:Dataset`
**`method`：** `log`、`log1p`、`sqrt`、`power`、`box-cox`、`yeo-johnson`、`expression`。让通道更接近对称之后再算统计/拟合特征。

### data.binarize — 特征二值化
`dataset:Dataset → dataset:Dataset`
`columns`、`threshold`、`keep_original`、`suffix`。用于指示类通道（超过/低于某个限值）；它会毁掉幅度信息，所以模型可能需要幅度时保留原列。

### data.asset_key — 资产标识
`dataset:Dataset → dataset:Dataset`
从实例键推出所属资产。**`mode`：** `split`（用 `separator`、`index`）或 `regex`（用 `pattern` 一个捕获组）；`column` 进、`target` 出（默认 `asset`）。资产留出的第一步：把它的输出接给窗口组件。

### data.quality — 数据质量预检
`dataset:Dataset → report:Visualization`
终端分支。**关键参数：** `columns`、`group_column`、`label_column`、`time_column`、`window_size`、`step`、`max_groups`、`flat_threshold`。配置必须镜像你后面要用的窗口组件。它报告全 NaN 列、常数列（整体与按组）、平窗口比例、重复行、混合标签窗口。**建模前永远先跑它。**它需要整帧，所以**不**接受流式数据集，打开流式之前先跑。

### data.materialize — 物化数据
`dataset:Dataset → dataset:Dataset`
把流式数据集变成内存中的数据集。全局组件拒绝流式输入时插它；它会警告，而这条警告属于你的汇报（内存不再有界了）。

### data.labels — 标签向量
`dataset:Dataset → labels:LabelVector`
抽取与行对齐的标签列。**不要**用于窗口特征：从窗口组件取 `labels`，否则长度对不上。

---

## 探索 (8)，全部是终端分支

这些组件回答的是"数据里有什么"的问题。它们的输出（`StatisticsResult`、`CorrelationMatrix`、`Prediction`）不是模型输入。`explore.central_tendency`、`explore.dispersion`、`explore.correlation`、`explore.distribution`、`explore.anomaly` 吃的是表，所以**同时接受 `FeatureDataset`**：把它们指向特征分支，是检查"特征算得对不对"最快的办法。`explore.periodicity`、`explore.cross_relation`、`explore.concept_drift` 只吃原始信号。

### explore.central_tendency — 集中趋势
`method`：`mean`、`median`、`mode`、`weighted_mean`（加 `weight_column`）。

### explore.dispersion — 离散度量
`method`：`std`、`variance`、`range`、`iqr`、`mad`、`cv`。

### explore.correlation — 相关性度量
两两相关系数。`columns`、`method`：`pearson`、`spearman`、`kendall`。用来在做特征选择之前发现冗余通道。

### explore.distribution — 分布检查
`columns`、`bins`。看偏度、多峰与截断——这些形状决定了 `data.transformation` 是否有帮助。

### explore.periodicity — 周期性检查
`columns`、`max_lag`、`sampling_rate`。在决定上 `feature.spectral` 之前先跑它：没有周期结构，频域特征就是噪音。

### explore.concept_drift — 概念漂移
输入是 `reference:Dataset` 与 `current:Dataset` 两个数据集；参数 `columns`、`bins`、`psi_threshold`、`alpha`。用来判断切分稳不稳，或者为"该重训了"提供依据。

### explore.cross_relation — 互相关与互协方差
`first_column`、`second_column`、`method`、`max_lag`、`normalize`。在造滞后特征之前，先看清两个通道之间的时延。

### explore.anomaly — 异常探索
`dataset:Dataset → prediction:Prediction`；`method`：`boxplot`、`dynamic_threshold`、`hyperbolic_smoothing`，另有 `window`、`threshold`、`iqr_multiplier`、`group_column`、`time_column`。基于阈值的探索——**把阈值报出来**，因为它就是规则本身。

---

## 可视化 (8)，全部是终端分支

`visual.overview` 输出 `Visualization`，其余输出 `PlotArtifact`。它们在网页设计器里渲染；MCP 只会告诉你它跑过了。用 `max_points` 让大图保持可用。

`visual.overview`（`time_column`）—— 一眼看到列、类型与缺失情况；它**不会**显示恒定或保持值通道，那是 `data.quality` 的活。
`visual.line`（`time_column`、`value_columns`、`group`、`title`）—— 信号的主力图。
`visual.scatter`（`x`、`y`、`group`、`x_label`、`y_label`）—— 看类别可分性。
`visual.subplot`（`x`、`value_columns`、`kind`、`max_points`）—— 多通道，一张图。
`visual.histogram`（`columns`、`bins`、`density`、`title`）—— 取值分布。
`visual.compare`（`first:Dataset`、`second:Dataset`；`columns`、`x_column`）—— 过滤或修复前后对比。
`visual.relationship`（`columns`、`method`、`threshold`）—— 相关性热图。
`visual.anomaly`（`dataset:Dataset`、`prediction:Prediction`；`x`、`y`、`anomaly_column`）—— 把检测结果叠回原始信号，是向人解释检测器最快的方式。

其中 `visual.overview`、`visual.line`、`visual.scatter`、`visual.subplot`、`visual.histogram`、`visual.relationship` **同时接受 `FeatureDataset` 与 `Dataset`**：任何特征分支都能就地检查，这也是你向人展示"流水线到底产出了什么"的方式。`visual.compare` 与 `visual.anomaly` 只吃原始信号。

---

## 特征 (13)

### 窗口生产者（原始 `Dataset` → `FeatureDataset` + `LabelVector`）

四个组件共享 `columns`、`group_column`、`asset_column`、`label_column`、`time_column`、`window_size`、`step`、`label_policy`。**要合并它们的输出，就必须在这些参数上完全一致。** 另外还有时间窗口与预测参数：`window_span`/`step_span`（按时间切窗，如 `7d`/`12h`）与 `prediction_horizon`/`prediction_gap`/`current_fault_policy`/`normal_label`（配合 `label_policy=horizon` 做"预测未来会不会故障"）。

**feature.statistical** — 统计
`features`：`mean`、`std`、`variance`、`min`、`max`、`median`、`rms`、`skewness`、`kurtosis`、`quantile`（配 `quantile`）、`range`、`iqr`、`mad`、`peak`、`crest_factor`。水平/形状类特征默认就从这里开始。

**feature.fitting** — 拟合
`fitting_method`：`linear`、`polynomial`（配 `degree`）、`exponential`。输出趋势斜率、残差与 R² 类特征。退化/起始点任务的主力。

**feature.spectral** — 频域
`sampling_rate`（**必填**）、`features`（`dominant_frequency`、`dominant_amplitude`、`spectral_centroid`、`spectral_spread`、`spectral_entropy`、`spectral_rms`、`high_frequency_ratio`、`harmonic_ratio`、`band_energy_ratio`）、`band_edges`（Nyquist 的比例）、`harmonic_tolerance`、`flat_policy`（默认 `nan`，另有 `skip`/`error`）、`flat_threshold`。要有真实波动与已知采样率；通道被保持或量化时，那些行会是 NaN。

**feature.entropy** — 非线性
`methods`：`approximate_entropy`、`information_entropy`；`bins`、`embedding_dimension`、`tolerance_ratio`。当故障特征是"不规则程度"而不是幅度时用它。**不支持流式**（statistical/fitting/spectral 支持）。

### 其它特征组件

**feature.rolling_statistics** — 统计
`dataset:Dataset → features:FeatureDataset`（无 labels）。`method`：`mean`、`std`、`median`、`max_repeat`；`window`、`group_column`、`time_column`。保持行对齐的滚动汇总；查"保持值"用 `max_repeat` 很顺手。

**feature.temporal** — 时域
`method`：`first_difference`、`second_difference`、`autocorrelation`；`lag`、`window`。没有 labels 端口——当额外分支用，或者接受它不能做验证器的标签来源。

**feature.categorical** — 分类
`Dataset → features:FeatureDataset, encoder:FeatureTransformer`；`method`、`target_column`、`random_state`、`handle_unknown`。它会拟合一个编码器。新数据上请通过 `feature.categorical_transform` 复用，而不是重新拟合。

**feature.categorical_transform** — 分类
`Dataset + encoder:FeatureTransformer → features:FeatureDataset`。套用已拟合的编码器（未知取值的行为由 `handle_unknown` 决定）。编码器要来自同一个方案。

**feature.select** — 组合
`Dataset → features:FeatureDataset`；`columns`。表里已经有特征（预计算或外部步骤产出）时用它。

**feature.merge** — 组合
`left + right → features:FeatureDataset`。要求索引完全相同、来源一致（`source_rows`、`groups`、`assets`、`source_path`、`source_id`），并且**列名不相交**。合并特征分支的唯一合法方式。

**feature.imputation** — 清洗
`features → features`；`method`：`mean`、`median`、`zero`、`drop_columns`；`fill_value`、`columns`。在会产生 NaN 的特征（频域、过短的组）与模型之间的必经一步。

**feature.score_select** — 选择
`features + labels → features, scores`；`method`、`top_k`、`threshold`、`random_state`。在**全部**行上拟合，所以见过留出集：要么只在探索分支上用，要么把泄漏警告一起报出来。

**feature.pca** — 表征学习
`features → features, variance`；`n_components`、`columns`、`whiten`。全量拟合的注意事项同 `feature.score_select`；`variance` 是解释方差结果。

---

## 验证 (11)

### 有监督分类器 —— `features:FeatureDataset + labels:LabelVector`

公共参数：`split_method`（默认 `stratified`，另有 `group`、`asset`、`temporal`）、`test_size`、`random_state`、`positive_class`、`class_weight`。输出包括 `model`、`prediction`、`metrics`（有的还有 `importance`）。

**validation.random_forest** —— `n_estimators`、`max_depth`、`min_samples_split`、`min_samples_leaf`、`class_weight`（`balanced`、`balanced_subsample`）。默认基线；`importance` 是 `FeatureImportance`。

**validation.svm** —— `kernel`（`linear`、`poly`、`rbf`、`sigmoid`）、`C`、`gamma`（`scale`、`auto`）、`class_weight`、`probability`。标准化只在训练行上拟合。想要 ROC/PR 指标就保持 `probability=true`（默认）。

**validation.xgboost** —— `n_estimators`、`max_depth`、`learning_rate`、`subsample`、`colsample_bytree`、`objective`。需要可选依赖；缺失时如实报告，不要悄悄把这第三个模型删掉。

**validation.decision_tree** —— `criterion`、`max_depth`、`min_samples_split`、`min_samples_leaf`、`class_weight`。解释性比分数更重要时用它。

**validation.reservoir_classifier** —— `reservoir_size`、`spectral_radius`、`input_scale`、`leaking_rate`、`n_steps`、`C`、`max_iter`。窗口特征上的廉价循环基线。

### 回归

**validation.linear_regression** —— `features + target:LabelVector → model, prediction, metrics, importance`；`split_method`：`random`（默认）、`group`、`temporal`；`fit_intercept`、`positive`。**没有资产切分**——问的是"没见过的设备"时，必须说明这个限制。

**validation.arma** —— `dataset:Dataset → model, prediction, metrics`；`column`、`p`、`q`、`test_size`、`iterations`、`time_column`。用尾部留出的原始序列预测。

### 无监督检测器 —— 原始 `Dataset`

**validation.isolation_forest_detector** —— `columns`、`contamination`、`n_estimators`、`random_state`。`contamination` 定的是异常比例：那是假设，不是发现。

**validation.knn_detector** —— `columns`、`contamination`、`neighbors`。基于距离；量纲不同时先缩放列。

**validation.persistence_detector** —— `column`、`threshold`、`direction`（`above`、`below`、`absolute`）、`min_consecutive`、`group_column`。规则型"信号卡住"检测器；阈值必须来自工艺，不是来自模型。

### 对比

**validation.compare** —— `first:Metrics`、`second:Metrics`、可选 `third:Metrics → comparison:StatisticsResult`。留出行不同的指标会被拒绝，而这正是你想要的保护。

---

## 值得记住的接线模式

```
data.input ─┬─ data.asset_key ─┬─ windowA ──┐
            │                  ├─ windowB ──┼─ feature.merge ─ feature.imputation ─ model
            │                  └─ data.quality（终端）      ▲
            │                                                   └── windowA.labels
            └─ visual.overview / explore.*   （终端分支）
```

 - 从一个输出扇出就是分支的方式；扇入必须走 `feature.merge`。
 - 一旦有了窗口，`labels` 就来自窗口组件，绝不要用 `data.labels`。
 - `data.quality`、`explore.*`、`visual.*` 都挂在旁边，并且到此为止。
 - 要合并的每条窗口分支，`columns`/`group_column`/`time_column`/`window_size`/`step`/`asset_column` 都必须一致。
 - 特征分支可以直接挂 `visual.overview`（同时接受 `FeatureDataset`），这是让人看见中间产物的标准做法。
