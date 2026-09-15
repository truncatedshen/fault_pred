# 配方

针对最常见的几类任务，给出可以直接照抄的调用序列。参数名就是真实的 MCP 参数名；`…` 表示你要从数据里填的值。

每个配方都假设你已经做完了 `SKILL.md` §1 的侦察调用：`get_server_info()`、`list_datasets()`，以及针对所需组件的一两次 `retrieve_components(...)`。

---

## 配方 A —— 表格数据、按实例留出、分类基线

什么时候用：表本身已经像特征表或已按实例切好窗口，行之间不重叠，问题是"这些已知单元里哪个是坏的？"

```jsonc
// 1. 建方案
create_pipeline({"name": "设备故障基线"})

// 2. 一次调用把节点建完
add_components({"pipeline_id": "p_…", "components": [
  {"component_type": "data.input", "node_id": "source",
   "parameters": {"path": "…/data.csv"}},
  {"component_type": "data.quality", "node_id": "quality",
   "parameters": {"columns": ["…measurements…"], "group_column": "equipment",
                  "label_column": "label", "window_size": 64, "step": 0}},
  {"component_type": "feature.statistical", "node_id": "stat",
   "parameters": {"columns": ["…measurements…"], "group_column": "equipment",
                  "label_column": "label", "time_column": "time",
                  "window_size": 64, "step": 0, "features": ["mean","std","rms","kurtosis"]}},
  {"component_type": "feature.imputation", "node_id": "clean", "parameters": {"method": "median"}},
  {"component_type": "validation.random_forest", "node_id": "forest",
   "parameters": {"split_method": "group", "test_size": 0.25, "random_state": 42,
                  "n_estimators": 300, "class_weight": "balanced"}}
]})

// 3. 一次调用把线连完（注意 quality 是终端分支，扇出是允许的）
connect_many({"pipeline_id": "p_…", "connections": [
  {"source_node": "source", "source_port": "dataset", "target_node": "quality",   "target_port": "dataset"},
  {"source_node": "source", "source_port": "dataset", "target_node": "stat",      "target_port": "dataset"},
  {"source_node": "stat",   "source_port": "features","target_node": "clean",     "target_port": "features"},
  {"source_node": "clean",  "source_port": "features","target_node": "forest",    "target_port": "features"},
  {"source_node": "stat",   "source_port": "labels",  "target_node": "forest",    "target_port": "labels"}
]})

validate_pipeline({"pipeline_id": "p_…"})
execute_pipeline({"pipeline_id": "p_…"})
wait_for_pipeline({"pipeline_id": "p_…", "timeout_seconds": 600})
get_node_result({"pipeline_id": "p_…", "node_id": "forest"})   // 指标与警告
```

汇报：切分方式是 `group`、留出规模、`coverage`、指标表，以及每一条警告。`window_size` 必须是一个真实的决定——写 `0` 会把每组压成一行。

---

## 配方 B —— 起始点/退化数据 + 诚实的资产留出

什么时候用：标签随时间变化，采样单元是"实例"而实例属于"资产"（井、机器、产线），问题是"这套东西能推广到没见过的设备上吗？"

```jsonc
add_components({"pipeline_id": "p_…", "components": [
  {"component_type": "data.input", "node_id": "source",
   "parameters": {"path": "…/events.parquet", "format": "parquet"}},
  {"component_type": "data.asset_key", "node_id": "asset",
   "parameters": {"column": "instance", "target": "asset", "mode": "split",
                  "separator": "_", "index": 0}},
  {"component_type": "data.quality", "node_id": "quality",
   "parameters": {"columns": ["…"], "group_column": "instance", "label_column": "label",
                  "time_column": "time", "window_size": 128, "step": 32}},
  {"component_type": "feature.statistical", "node_id": "stat",
   "parameters": {"columns": ["…"], "group_column": "instance", "asset_column": "asset",
                  "label_column": "label", "time_column": "time",
                  "window_size": 128, "step": 32, "label_policy": "mode",
                  "features": ["mean","std","rms","kurtosis","crest_factor"]}},
  {"component_type": "feature.fitting", "node_id": "fit",
   "parameters": {"columns": ["…"], "group_column": "instance", "asset_column": "asset",
                  "label_column": "label", "time_column": "time",
                  "window_size": 128, "step": 32, "label_policy": "mode",
                  "fitting_method": "linear"}},
  {"component_type": "feature.merge", "node_id": "merge"},
  {"component_type": "feature.imputation", "node_id": "clean", "parameters": {"method": "median"}},
  {"component_type": "validation.random_forest", "node_id": "forest",
   "parameters": {"split_method": "asset", "positive_class": "…故障标签…",
                  "n_estimators": 300, "class_weight": "balanced"}}
]})

connect_many({"connections": [
  {"source_node": "source", "source_port": "dataset",  "target_node": "asset",   "target_port": "dataset"},
  {"source_node": "asset",  "source_port": "dataset",  "target_node": "quality", "target_port": "dataset"},
  {"source_node": "asset",  "source_port": "dataset",  "target_node": "stat",    "target_port": "dataset"},
  {"source_node": "asset",  "source_port": "dataset",  "target_node": "fit",     "target_port": "dataset"},
  {"source_node": "stat",   "source_port": "features", "target_node": "merge",   "target_port": "left"},
  {"source_node": "fit",    "source_port": "features", "target_node": "merge",   "target_port": "right"},
  {"source_node": "merge",  "source_port": "features", "target_node": "clean",   "target_port": "features"},
  {"source_node": "clean",  "source_port": "features", "target_node": "forest",  "target_port": "features"},
  {"source_node": "stat",   "source_port": "labels",   "target_node": "forest",  "target_port": "labels"}
]})
```

决定这个配方是否诚实的地方：

 - `label_policy="mode"`：起始点窗口跨在标签切换上，`strict` 会在最有意思的那一刻中止。
 - `asset_column` 要出现在**每一个**窗口生产者上——合并会检查两条分支的资产是否一致，`split_method="asset"` 也从特征里读它。
 - 两条窗口分支共享 `columns`/`group_column`/`time_column`/`window_size`/`step`，合并才能成功。任意一条改了其中一项，合并就会拒绝。
 - 读 `metrics.coverage`；如果 `test_assets_unseen` 是 0，那这个切分并没有做你以为的事。

---

## 配方 C —— 无监督检测（没有可用标签）

什么时候用：没有可靠标签，或者标签本身正是你要发现、而不是用来评估的东西。

```jsonc
add_components({"pipeline_id": "p_…", "components": [
  {"component_type": "data.input", "node_id": "source", "parameters": {"path": "…"}},
  {"component_type": "data.quality", "node_id": "quality", "parameters": {"columns": ["…"]}},
  {"component_type": "validation.isolation_forest_detector", "node_id": "iforest",
   "parameters": {"columns": ["vibration", "temperature"], "contamination": 0.05,
                  "n_estimators": 200, "random_state": 42}},
  {"component_type": "validation.persistence_detector", "node_id": "persist",
   "parameters": {"column": "vibration", "threshold": 2.0, "direction": "above",
                  "min_consecutive": 3, "group_column": "instance"}},
  {"component_type": "visual.anomaly", "node_id": "plot",
   "parameters": {"x": "time", "y": "vibration", "anomaly_column": "…", "max_points": 2000}}
]})
```

`contamination` **就是模型本身**：它决定把多少个点标成异常，所以这里"准确率"这类数字没有意义。要汇报的是你要求的异常比例，以及你在 `data.quality` 里看到的明显失效模式（阈值窗口、尖峰、保持值）。`validation.persistence_detector` 需要一个操作者能为之辩护的阈值——那是"卡住值"的工艺规则，不是统计规则。

---

## 配方 D —— 大文件（超过约 1 GB）

```jsonc
// 先试有界读取：在打开流式之前先这么做，通常就够了
{"component_type": "data.input", "parameters": {"path": "events.parquet", "format": "parquet",
  "columns": ["instance","time","asset","label","vibration","temperature"],
  "filters": [["asset", "in", ["WELL-00001","WELL-00002"]]]}}

// 或者整体流式，让窗口组件分批处理
{"component_type": "data.input", "parameters": {"path": "events.parquet", "format": "parquet",
  "streaming": true, "chunk_rows": 200000}}
```

流式的规则违反时会直接炸：

 - 行必须按组**连续**排列——导出前先按组列排序；
 - 组内必须按时间有序；
 - 只有 `feature.statistical`、`feature.fitting`、`feature.spectral`、`visual.overview` 与 `data.materialize` 接受流式数据集；其它（包括 `data.quality`）会报 `… cannot consume streamed input; insert data.materialize or turn streaming off on data.input`；
 - `data.materialize` 会恢复全内存行为，并且（正确地）给出警告；
 - 一个组里塞了几百万行，仍然会被整体载入——把它拆成实例。

质量预检要在**非流式**分支上先跑（或者在有限样本上跑）：它读不了流。

最后查 `get_server_info().artifact_cache` 里的 `evictions`/`spills`，并说明这次运行是否装进了内存。

---

## 配方 E —— 修正一次失败的运行

```jsonc
get_pipeline_status({"pipeline_id": "p_…"})          // 当前工作区是哪一个
get_pipeline_result({"pipeline_id": "p_…"})          // 一次调用拿到每个节点的状态
get_node_result({"pipeline_id": "p_…", "node_id": "stat"})   // 报错 + 输入预览
get_history({"pipeline_id": "p_…", "limit": 20})

configure_components({"pipeline_id": "p_…", "updates": [
  {"node_id": "stat", "parameters": {"label_policy": "mode"}}
]})
retry_node({"pipeline_id": "p_…", "node_id": "stat"})        // 重跑 stat 及其下游
wait_for_pipeline({"pipeline_id": "p_…"})
```

值得认出来的几种模式：

1. **多个节点同时失败** —— 去找那个共同的上游原因（过滤条件什么都没匹配到、整列常数）。修好源头，尾部自然都好了。
2. **只有一个节点失败，其余都成功** —— `retry_node` 会复用上游产物，很便宜；不要整图重跑。
3. **运行之后又改过图** —— 结果会读成 `PENDING` 并提示 "Graph changed; run to refresh results"。这不是数据丢失，只是失效。
4. **数字来路不明** —— 读 `warnings`：驱逐、过期工作区或子集提示都会解释它。

放弃一次尝试时，`delete_pipeline` 删掉它，别让服务一直背着它的工作区和溢出文件。

---

## 配方 F —— 对比三个模型

```jsonc
// 三个模型的特征/标签/切分必须完全一致，只有估计器不同
{"node_id": "forest", "parameters": {"split_method": "asset", "test_size": 0.25, "random_state": 42}}
{"node_id": "svm",    "parameters": {"split_method": "asset", "test_size": 0.25, "random_state": 42,
                                     "kernel": "rbf", "C": 10, "class_weight": "balanced"}}
{"node_id": "xgb",    "parameters": {"split_method": "asset", "test_size": 0.25, "random_state": 42}}

connect_many({"connections": [
  {"source_node": "forest", "source_port": "metrics", "target_node": "cmp", "target_port": "first"},
  {"source_node": "svm",    "source_port": "metrics", "target_node": "cmp", "target_port": "second"},
  {"source_node": "xgb",    "source_port": "metrics", "target_node": "cmp", "target_port": "third"}
]})
```

`validation.compare` 会拒绝测试索引不一致的输入，这正是它的价值：如果对比被拒绝，说明两次运行没有共享同一个留出集，任何排名都没有意义。`validation.xgboost` 需要可选依赖；缺失时要说出来，而不是悄悄少一个模型。

---

## 配方 G —— 先探索再建模（还没有模型）

```jsonc
add_components({"components": [
  {"component_type": "visual.overview", "node_id": "overview", "parameters": {"time_column": "time"}},
  {"component_type": "data.quality", "node_id": "quality",
   "parameters": {"group_column": "instance", "label_column": "label", "time_column": "time",
                  "window_size": 128, "step": 32}},
  {"component_type": "explore.correlation", "node_id": "corr", "parameters": {"method": "pearson"}},
  {"component_type": "explore.distribution", "node_id": "dist", "parameters": {"bins": 40}},
  {"component_type": "explore.periodicity", "node_id": "period",
   "parameters": {"columns": ["vibration"], "max_lag": 200, "sampling_rate": 1000}}
]})
```

这些全都是挂在 `data.input` 上的**终端**分支（扇出）。它们的输出是 `Visualization`/`StatisticsResult`/`CorrelationMatrix`，没有一个能接进模型——这是类型端口强制的，不是约定。

当用户问"这份数据里有什么"、并且希望在模型出现之前先拿到答案时，用这个配方。汇报的是发现（常数列、平窗口比例、混合标签、相关性），不是一张方案的 dump。

---

## 配方 I —— 预测"未来会不会发生故障"（时间窗口 + 未来视野）

什么时候用：用户要的不是"现在坏没坏"（那是检测），而是"接下来这几天会不会坏"（预测）。这时窗口要按**时间**切，标签要取**未来**。

```jsonc
add_components({"pipeline_id": "p_…", "components": [
  {"component_type": "data.input", "node_id": "source",
   "parameters": {"path": "…/events.parquet"}},
  {"component_type": "feature.statistical", "node_id": "stats",
   "parameters": {"columns": ["…"], "group_column": "instance", "asset_column": "asset",
                  "label_column": "fault", "time_column": "time",
                  "window_span": "7d",              // 用最近 7 天的数据
                  "step_span": "1d",                // 每天出一个样本
                  "prediction_horizon": "2d",       // 往后看 2 天
                  "prediction_gap": "1h",           // 先隔 1 小时，避免贴着起始点
                  "label_policy": "horizon",        // 标签来自未来视野
                  "current_fault_policy": "drop",   // 已故障的窗口交给检测任务
                  "normal_label": "0",
                  "features": ["mean", "std", "rms"]}},
  {"component_type": "validation.random_forest", "node_id": "forest",
   "parameters": {"split_method": "temporal", "n_estimators": 300, "class_weight": "balanced"}}
]})
connect_many({"pipeline_id": "p_…", "connections": [
  {"source_node": "source", "source_port": "dataset",  "target_node": "stats",  "target_port": "dataset"},
  {"source_node": "stats",  "source_port": "features", "target_node": "forest", "target_port": "features"},
  {"source_node": "stats",  "source_port": "labels",   "target_node": "forest", "target_port": "labels"}
]})
```

必须向用户交代的四件事：

1. **窗口与视野**：窗口多长（`window_span`）、多久出一次样本（`step_span`）、往后看多远（`prediction_horizon`）、隔了多久（`prediction_gap`）。
2. **丢了多少窗、为什么**：`attrs["horizon_dropped_current_fault"]`（窗口自身已故障，属于检测）与 `attrs["horizon_dropped_unknown_future"]`（视野超出数据，不能标 0），以及随附的 warnings。
3. **正类比例**：预测任务通常正类稀少，报出来才知道模型在学什么。
4. **切分方式**：时间窗口通常重叠（`step_span < window_span`），`stratified` 会被拒绝，用 `temporal`（按时间顺序）或 `group`/`asset`；时间切分才最接近"上线后真的预测未来"。

三条容易踩的坑：窗口跨度必须小于同组数据的可用时长（否则一个窗口都切不出来）；时间列要么是时间戳、要么是**秒**（数值列按秒解释）；流式输入不支持这套（看不到未来），要先把 `streaming` 关掉或插 `data.materialize`。

---

## 配方 H —— 检查中间产物（特征表长什么样）

```jsonc
// 把概览直接挂在你真正建模的那条特征分支上：它同时接受 Dataset 与 FeatureDataset
add_components({"pipeline_id": "p_…", "components": [
  {"component_type": "visual.overview", "node_id": "feature_overview", "parameters": {}}
]})
connect_many({"pipeline_id": "p_…", "connections": [
  {"source_node": "stat", "source_port": "features",
   "target_node": "feature_overview", "target_port": "dataset"}
]})
execute_pipeline({"pipeline_id": "p_…"})
wait_for_pipeline({"pipeline_id": "p_…", "timeout_seconds": 600})
get_node_result({"pipeline_id": "p_…", "node_id": "feature_overview"})
// value.row_count / value.column_names / value.missing_rate 就是特征表的真实形状
```

想看分布就换成 `visual.histogram`，想看冗余就换 `explore.correlation`，想看某几路信号的形状就换 `visual.line`。这些分支都是终端，不会改变建模链路。**注意**：往已有图上加节点会让该方案的结果失效（图被编辑即失效），所以要重新跑一次；但换来的是"人可以直接核对中间数据"，对故障预测这种要被人复核的任务是值得的。
