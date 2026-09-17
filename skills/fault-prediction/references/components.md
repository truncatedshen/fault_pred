# 组件地图（人工整理）

全部 88 个内置组件，按你遇到它们的阶段分组。这份文件是**指南**：某个组件干什么、什么时候**不要**用、哪些参数真的会改变结果。权威 schema 永远是 `get_component_schema(component_type)`（类型、默认值、取值范围、枚举）；`docs/components.md` 是同一份数据的批量版本。

端口记法：`输入:类型 → 输出:类型`。面向模型的组件，必须 `features` 与 `labels` 都接上才有意义。

---

## 数据 (20)

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

### data.polynomial_features — 多项式特征
`dataset:Dataset → dataset:Dataset`
`columns`、`degree`、`interaction_only`、`include_bias`、`keep_original`、`max_columns`。生成平方项与交互项（`signal^2`、`signal*noise`）。**必须在窗口切分之前用**：它改变列结构，放到窗口之后含义完全不同。项数按 `C(n+d, d)` 增长，超过 `max_columns` 直接报错而不是把内存吃光。

### data.discretize — 离散化分箱
`dataset:Dataset → dataset:Dataset`
`columns`、`n_bins`、`strategy`（`uniform`、`quantile`、`kmeans`）、`encode`（`ordinal`、`onehot-dense`）、`keep_original`、`suffix`。箱边界在**整表**上拟合，所以结果带探索性警告。工业数据优先用 `quantile`（等频）——`uniform` 会被极值支配。

### data.seasonal_difference — 同期差分
`dataset:Dataset → dataset:Dataset`
`columns`、`period`、`mode`（`difference`、`ratio`）、`group_column`、`time_column`、`keep_original`、`suffix`、`drop_missing`。就是"同比"口径：`y_t - y_{t-period}` 或 `y_t / y_{t-period}`。比值模式要求基线严格为正（否则直接报错）。**每组前 `period` 行没有可比对象**：默认保留为 NaN 并写警告，`drop_missing=true` 才删行——删掉的是数据，不是噪声。

### data.concat — 数据拼接
`first + second（+ third / fourth，可选）:Dataset → dataset:Dataset`
把多条分支按顺序纵向拼成一份：几个 `data.input`（每台设备/每批一个文件）各接一个端口即可，下游窗口/特征/验证器完全不用改。**`first`/`second` 必填**——只接一条会被 `validate_pipeline` 拦下（半接线的图不该等到运行才炸）；超过四个源就串联下一个 concat。`source_column` 设了会给每行加一列"来自哪个输入端口"（想知道具体文件，用上游 `data.input` 的 `source_column`，它写的是文件路径）。列集合必须一致、列顺序按 `first` 对齐、索引重排 `0..N-1`；空输入直接报错（那通常是"过滤条件什么都没匹配到"）。**什么时候不要用**：数据本来就在一个文件里（用 `data.input.paths` 更省节点）；按列拼是 `feature.merge`，不是它。

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

## 探索 (14)，全部是终端分支

这些组件回答的是"数据里有什么"的问题。它们的输出（`StatisticsResult`、`CorrelationMatrix`、`Prediction`）不是模型输入。`explore.central_tendency`、`explore.dispersion`、`explore.correlation`、`explore.distribution`、`explore.anomaly`、`explore.peaks`、`explore.normality`、`explore.acf`、`explore.isotonic`、`explore.gbr_fit` 吃的是表，所以**同时接受 `FeatureDataset`**：把它们指向特征分支，是检查"特征算得对不对"最快的办法。`explore.periodicity`、`explore.cross_relation` 只吃原始信号，`explore.concept_drift`、`explore.kl_divergence` 需要 `reference` + `current` 两份输入。

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

### explore.peaks — 山峰检测
`columns`、`prominence`、`distance`、`group_column`。逐列找局部极大值。真实信号里的量化台阶会造出大量假峰：把 `prominence` 设成最小量化刻度、`distance` 设成半个预期周期，峰个数才是可用的。**多实例数据一定要给 `group_column`**：不给的话"设备 A 结尾 + 设备 B 开头"的拼接处会被数成一个峰。分组后 `rows` 是"一组 × 一列"，`peaks` 的键是 `"<组>:<列>"`。

### explore.normality — 正态性校验
`columns`、`method`（`normaltest` 需 ≥8 行、`shapiro` 需 3~5000 行）、`alpha`。结论只有两种："没有足够证据拒绝正态"与"拒绝正态"。**`p > alpha` 不等于数据服从正态**，返回值里的 `caveat` 必须照抄进汇报；样本量大时它会指出毫无工程意义的微小偏离。

### explore.kl_divergence — KL 散度度量
`reference:Dataset + current:Dataset → statistics`；`columns`、`bins`、`epsilon`、`js_threshold`。KL 有方向（返回的是 `KL(current || reference)`），JS 对称且有界，跨列比较请用 JS。与 `explore.concept_drift` 的分工：那边给 PSI/KS + 漂移判定，这边给两条散度曲线，适合"到底差多少"。

### explore.acf — ACF 自相关函数
`columns`、`max_lag`、`alpha`、`group_column`。给出 0..max_lag 的整条自相关曲线与置信带（`±z/sqrt(n)`），`white_noise=true` 表示 1..max_lag 内没有滞后超出带外。`explore.periodicity` 只回答"主周期是多少"，想知道"记忆有多长"用这个。**同样要给 `group_column`**——ACF 假设序列连续，跨设备的拼接会凭空造出一段"长程相关"；分组后置信带按各组样本量单独计算，`max_lag` 会被最短的一组削短。

### explore.isotonic — 保序回归
`x_column`、`y_column`、`increasing`、`out_of_bounds`、`max_points`。只约束单调性、不放函数形式。**保序回归的 R² 是样本内的，而且比直线灵活得多**，不要拿它跟线性/多项式比 R²；真正有信息的是 `blocks`（平台段数）与 `spearman`。

### explore.gbr_fit — 量化相关性拟合 GBR
`columns`、`target_column`、`n_estimators`、`learning_rate`、`max_depth`、`min_samples_leaf`、`subsample`、`test_size`、`random_state → statistics, importance`。量化"这些列能解释多少目标"。默认 `test_size=0` 只报**样本内** R²（会明确警告）；设一个留出集也只是随机切分、无视窗口重叠，要下结论请走 `validation.*`。

### explore.hp_filter — HP 趋势过滤
`column`、`lamb`、`group_column`、`time_column`、`max_points`。Hodrick–Prescott 把序列拆成趋势与周期，`cycle_share` 是周期项占总方差的比例。**λ 是选择，不是事实**：周期 24 的正弦在 λ=1600 处约 1/4 的方差会被算进趋势。`lamb` 必须写进报告；行数超过 20000 会直接报错（HP 是全局耦合的拟合，不能偷偷分块）。

### explore.stationarity — 平稳性检查
`column`、`max_lag`（0 = Schwert 经验规则）、`regression`（`c`/`ct`/`n`）、`group_column`、`time_column`。ADF 单位根检验，输出统计量与 1%/5%/10% **渐近**临界值。**故意不给 p 值**——完整 p 值需要整套 MacKinnon 响应面；请用统计量与三条临界线判断，并说明样本短时临界值偏保守。

### explore.dtw — DTW 距离
`first_column`、`second_column`、`band`（Sakoe–Chiba 半径，0 = 不限）、`normalize`。允许时间轴伸缩的形状距离，还会给出 `path_length` 与按路径长度归一化的距离。**DTW 没有天然阈值**：要么和同批数据的分布比，要么改用自带归一化的 `explore.sbd`；这里只给距离，不给"异常/正常"的结论。

### explore.sbd — SBD 相关
`first_column`、`second_column`、`max_lag_fraction`、`normalize`。`1 - max(NCC)`，取值 `[0, 2]`，0 就是形状相同，因此**可以直接跨样本对比较**，适合做通道相似度筛选。会回报 `best_lag`（最优平移量）——先确认这个平移在物理上说得通，再信距离。

### explore.slope_cosine — 斜率与余弦夹角
`first_column`、`second_column`、`window`、`threshold`、`group_column`、`time_column → prediction`。窗口内两条**增量向量**的余弦：`+1` 同向、`-1` 反向，同时给出各自的滚动斜率；`cosine < -threshold` 的行标 `opposite=true`。用增量而不是原值是有意的：原值上的余弦会被各自的均值与量级支配。

---

## 可视化 (8)，全部是终端分支

`visual.overview` 输出 `Visualization`，其余输出 `PlotArtifact`。它们在网页设计器里渲染；MCP 只会告诉你它跑过了。用 `max_points` 让大图保持可用。

`visual.overview`（`time_column`、`label_column`，以及可选的 `labels` 输入端口）—— 一眼看到列、类型、缺失情况与**标签的正负样本比例**。标签有两个来源：表里的标签列（`label_column`）或接进来的标签向量（`labels`，特征分支上用 `stat.labels → overview.labels`）。它**不会**显示恒定或保持值通道，那是 `data.quality` 的活。
`visual.line`（`time_column`、`value_columns`、`group`、`title`）—— 信号的主力图。
`visual.scatter`（`x`、`y`、`group`、`x_label`、`y_label`）—— 看类别可分性。
`visual.subplot`（`x`、`value_columns`、`kind`、`max_points`）—— 多通道，一张图。
`visual.histogram`（`columns`、`bins`、`density`、`title`）—— 取值分布。
`visual.compare`（`first:Dataset`、`second:Dataset`；`columns`、`x_column`）—— 过滤或修复前后对比。
`visual.relationship`（`columns`、`method`、`threshold`）—— 相关性热图。
`visual.anomaly`（`dataset:Dataset`、`prediction:Prediction`；`x`、`y`、`anomaly_column`）—— 把检测结果叠回原始信号，是向人解释检测器最快的方式。

其中 `visual.overview`、`visual.line`、`visual.scatter`、`visual.subplot`、`visual.histogram`、`visual.relationship` **同时接受 `FeatureDataset` 与 `Dataset`**：任何特征分支都能就地检查，这也是你向人展示"流水线到底产出了什么"的方式。`visual.compare` 与 `visual.anomaly` 只吃原始信号。

---

## 特征 (14)

### 窗口生产者（原始 `Dataset` → `FeatureDataset` + `LabelVector`）

四个组件共享 `columns`、`group_column`、`asset_column`、`label_column`、`time_column`、`window_size`、`step`、`label_policy`。**要合并它们的输出，就必须在这些参数上完全一致。** 另外还有时间窗口与预测参数：`window_span`/`step_span`（按时间切窗，如 `7d`/`12h`）与 `prediction_horizon`/`prediction_gap`/`current_fault_policy`/`normal_label`（配合 `label_policy=horizon` 做"预测未来会不会故障"）。

**feature.statistical** — 统计
`features`：水平/形状类 `mean`、`std`、`variance`、`min`、`max`、`median`、`rms`、`skewness`、`kurtosis`、`quantile`（配 `quantile`）、`range`、`iqr`、`mad`、`peak`、`crest_factor`；结构类 `count`、`argmax_first`/`argmax_last`、`argmin_first`/`argmin_last`（位置按 0..1 归一化，跨窗口长度可比）、`count_above_mean`/`count_below_mean`、`longest_above_mean`/`longest_below_mean`、`mean_delta`/`mean_abs_delta`、`mean_second_derivative`、`duplicate_point_ratio`、`repeated_value_ratio`、`duplicate_sum`、`time_reversal_asymmetry`，以及四个字面比较项 `std_gt_range`、`variance_gt_std`、`max_repeated`、`min_repeated`（0/1）。水平/形状类特征默认就从这里开始；结构类里 `repeated_value_ratio`、`duplicate_sum` 对"保持值/卡死"通道特别有用，但比较项是否对模型有用需要单独评估。

**feature.fitting** — 拟合
`fitting_method`：`linear`、`polynomial`（配 `degree`）、`exponential`。输出趋势斜率、残差与 R² 类特征。退化/起始点任务的主力。

**feature.spectral** — 频域
`sampling_rate`（**必填**）、`features`（`dominant_frequency`、`dominant_amplitude`、`spectral_centroid`、`spectral_spread`、`spectral_entropy`、`spectral_rms`、`high_frequency_ratio`、`harmonic_ratio`、`band_energy_ratio`）、`band_edges`（Nyquist 的比例）、`harmonic_tolerance`、`flat_policy`（默认 `nan`，另有 `skip`/`error`）、`flat_threshold`。要有真实波动与已知采样率；通道被保持或量化时，那些行会是 NaN。

**feature.entropy** — 非线性
`methods`：`approximate_entropy`、`information_entropy`、`binned_entropy`（后两个是同一实现的两个叫法，都产出 `<列名>__information_entropy`）；`bins`、`embedding_dimension`、`tolerance_ratio`。当故障特征是"不规则程度"而不是幅度时用它。**不支持流式**（statistical/fitting/spectral 支持）。

### 其它特征组件

**feature.rolling_statistics** — 统计
`dataset:Dataset → features:FeatureDataset`（无 labels）。`method`：`mean`、`std`、`variance`（与 std 同口径 ddof=0）、`median`、`max`、`min`、`max_repeat`；`window`、`group_column`、`time_column`。保持行对齐的滚动汇总；查"保持值"用 `max_repeat` 很顺手。

**feature.temporal** — 时域
`method`：`first_difference`、`second_difference`、`autocorrelation`、`sum_abs_change`（窗口内 |Δ| 之和）、`peak_count`（窗口内山峰数，可用 `prominence` 过滤量化台阶）；`lag`、`window`、`prominence`。没有 labels 端口——当额外分支用，或者接受它不能做验证器的标签来源。

**feature.wavelet** — 时域
`Dataset → features:FeatureDataset`（无 labels，行数与输入一致）。`window`、`levels`（≤ log2(window)）、`peak_sigma`、`group_column`、`time_column`。输出每层的 Haar 细节能量占比、主尺度与细节峰个数——清单里的"连续小波变换的山峰数"落在这里，但它是**离散 Haar 的近似**，不声称与 Morlet CWT 数值一致；换小波基，峰个数会变，报告里要写清用的是哪一种。

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

## 验证 (27)

### 有监督分类器 —— `features:FeatureDataset + labels:LabelVector`

公共参数：`split_method`（默认 `stratified`，另有 `group`、`asset`、`temporal`）、`test_size`、`random_state`、`positive_class`、`class_weight`。输出包括 `model`、`prediction`、`metrics`（有的还有 `importance`）。

**validation.random_forest** —— `n_estimators`、`max_depth`、`min_samples_split`、`min_samples_leaf`、`class_weight`（`balanced`、`balanced_subsample`）。默认基线；`importance` 是 `FeatureImportance`。

**validation.svm** —— `kernel`（`linear`、`poly`、`rbf`、`sigmoid`）、`C`、`gamma`（`scale`、`auto`）、`class_weight`、`probability`。标准化只在训练行上拟合。想要 ROC/PR 指标就保持 `probability=true`（默认）。

**validation.xgboost** —— `n_estimators`、`max_depth`、`learning_rate`、`subsample`、`colsample_bytree`、`objective`。需要可选依赖；缺失时如实报告，不要悄悄把这第三个模型删掉。

**validation.decision_tree** —— `criterion`、`max_depth`、`min_samples_split`、`min_samples_leaf`、`class_weight`。解释性比分数更重要时用它。

**validation.reservoir_classifier** —— `reservoir_size`、`spectral_radius`、`input_scale`、`leaking_rate`、`n_steps`、`C`、`max_iter`。窗口特征上的廉价循环基线。

### 回归

**validation.linear_regression** —— `features + target:LabelVector → model, prediction, metrics, importance`；`split_method`：`random`（默认）、`group`、`temporal`；`fit_intercept`、`positive`。**没有资产切分**——问的是"没见过的设备"时，必须说明这个限制。

**validation.ridge** —— 与线性回归同一套端口、切分与指标字段，多了 `alpha`（L2 惩罚）。窗口统计量几乎总是互相相关，普通最小二乘此时系数会剧烈摆动甚至翻转符号，岭回归用手可调的收缩换稳定。代价是**系数不再是可解释的边际效应**；`alpha=0` 等价于线性回归。

**validation.arma** —— `dataset:Dataset → model, prediction, metrics`；`column`、`p`、`q`、`test_size`、`iterations`、`time_column`。用尾部留出的原始序列预测。

### 无监督检测器 —— 原始 `Dataset`

**validation.pca_detector** —— `columns`、`n_components`（0 = 自动保留 95% 方差）、`contamination`。分数是**重构误差**：多通道同时偏置这类故障比单变量阈值更敏感。

**validation.dbscan_detector** —— `columns`、`eps`、`min_samples`、`metric`。落在所有簇之外的点判为异常。**异常率由 `eps`/`min_samples` 决定，`contamination` 不参与**——这是与其它检测器不同的假设，汇报时必须说清是"密度发现"而不是"比例假设"。

**validation.min_cluster_detector** —— `columns`、`n_clusters`（0 = 用轮廓系数自动选）、`contamination`、`max_clusters`、`batch_size`、`silhouette_sample`。先聚正常工况簇，再按到最近簇心的距离判异常。`silhouette` 很低（例如 < 0.2）说明数据本来就没有清晰簇结构，结论要谨慎引用。

**validation.isolation_forest_detector** —— `columns`、`contamination`、`n_estimators`、`random_state`。`contamination` 定的是异常比例：那是假设，不是发现。

**validation.knn_detector** —— `columns`、`contamination`、`neighbors`。基于距离；量纲不同时先缩放列。

**validation.persistence_detector** —— `column`、`threshold`、`direction`（`above`、`below`、`absolute`）、`min_consecutive`、`group_column`。规则型"信号卡住"检测器；阈值必须来自工艺，不是来自模型。

**validation.level_shift_detector** —— `column`、`window`、`threshold`（t 量纲，默认 4）、`group_column`、`time_column`。候选点前后各 `window` 点的均值差的 Welch t 统计量：同样的跳变在噪声大时就不算阶跃。窗口不足的行 `anomaly_score` 为 NaN 且不计入告警，metrics 的 `scored_count` 说明评了多少行。

**validation.volatility_shift_detector** —— 同上参数。分数是前后两段**方差之比的对数**，并按原假设抽样标准差 `sqrt(2/(window-1))` 归一化成 z 量纲——所以 `threshold=4` 在两个 shift 检测器里含义一致。注意标准差放大 4 倍意味着方差放大 16 倍（z≈8.5）。

**validation.seasonal_detector** —— `column`、`period`、`threshold`、`reference_fraction`、`group_column`。用**前一段**（默认一半）建每相位的季节中位数剖面，再给整条序列打偏离 z 分数。`seasonal_strength` 只在参考段上算，回答"这条序列本身有多季节"，不回答"后来变没变"。

**validation.autoregression_detector** —— `column`、`order`、`threshold`、`train_fraction`、`group_column`。用前 `train_fraction` 段拟合 AR(p)，之后每一步用真实历史做一步预测、看残差的稳健 z 分数。训练段的分数一律留空（样本内），只有评估段参与告警。

**validation.esd_detector** —— `column`、`max_outlier_fraction`（默认 0.1）、`alpha`、`group_column`。广义 ESD（Rosner）：迭代剔除最极端点并与 t 分布临界值 λ 比较，取**最后一个** R > λ 的步数作为结论；中途不满足就说明前面那些"离群点"只是长尾。metrics 里带 R/λ 对照表供复核。

**validation.nsigma_detector** —— `column`、`sigma`、`mode`（`global`/`group`/`rolling`）、`window`、`group_column`。偏离中心 `sigma` 倍尺度即为异常。`global`/`group` 用全量统计量（描述性），`rolling` 能跟上缓慢漂移；两种口径的结论本来就会不同，所以 `mode` 与中心/尺度都写进 metrics。

**validation.mean_drift_detector** —— `column`、`reference_fraction`、`slack`、`decision`、`group_column`。Page 的 CUSUM：目标是参考段均值，`slack` 是容许的慢漂移（尺度单位），`decision` 是告警线。**警报是锁存的**——越过告警线后一直为真，所以要报的是 `first_alarm_index`（第一次报警在哪一行），而不是报警了多少行。

**validation.one_class_svm** —— `columns`、`nu`、`kernel`、`gamma`。只学"正常长什么样"，边界之外判异常，因此不需要标签。`nu` 是训练时越界比例的**上界**（也是支持向量比例的下界），实际异常比例通常更低——两个数都在 metrics 里，不能把 `nu` 当异常率。

**validation.kmeans** —— `columns`、`n_clusters`（0 = 轮廓系数自动选）、`max_clusters`、`batch_size`、`silhouette_sample → prediction(cluster, distance_to_centre)`。**这不是异常检测**：它不给正常/异常的判决。`silhouette` 低于约 0.2 说明簇结构基本是硬切的，报告里不能只说"分成了 3 类"。

### 预测（原始 `Dataset`）

**validation.exponential_smoothing** —— `column`、`method`（`holt`、`holt_winters`）、`alpha`、`beta`、`gamma`、`seasonal_periods`、`seasonal`（`additive`、`multiplicative`）、`test_size`、`time_column`、`group_column`。留出协议是"前段拟合、后段**从训练段末尾一路外推**"，返回的模型再在全量数据上重拟合，因此 `predict(steps)` 是从"现在"往后。**平滑系数是输入而不是拟合值**：留出分数只对你真正传进去的那组系数有意义。Holt 不含季节项，对有明显周期的序列请用 `holt_winters`（否则 60 步外推的 R² 可以是负的，这是模型不匹配而不是 bug）。乘法季节要求严格正基线。

**validation.arima** —— `column`、`order`（`[p,d,q]`）、`test_size`、`trend`、`time_column`。**需要可选依赖 statsmodels**（`pip install 'fault-prediction-platform[statsmodels]'`）；未安装时直接报错并给出这条命令，不会偷偷换成别的模型。返回 AIC/BIC，但**比较阶数要在留出集上做**，AIC 只用于初筛。

### 自动机器学习

**validation.grid_search** —— `features + labels → model, metrics, importance`；`algorithm`（`random_forest`、`decision_tree`、`svm`、`logistic`、`ridge`、`gradient_boosting`）、`param_grid`（小字典）、`cv_method`（`stratified`/`group`/`temporal`）、`cv_folds`、`scoring`、`top_k`。`param_grid` 里的参数名必须与该算法匹配（写错会列出可用名）。**`best_score` 是交叉验证分数，不是留出分数**——它是在同一份数据上选出来的参数，偏乐观，`warnings` 里会明说。重叠窗口必须用 `cv_method=group` 或 `temporal`。

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
