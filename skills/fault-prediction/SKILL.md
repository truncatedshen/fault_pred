---
name: fault-prediction
description: 通过 MCP 驱动故障预测组件平台：侦察服务、准备并定位数据、质量预检、构建对齐的窗口与标签、做特征工程、做诚实的留出验证（实例／资产／时序）、执行与排错、读取结果、把方案持久化成可复用的 XML。适用于本平台上的设备故障检测、退化与起始点建模、异常探索与模型对比，也包括修改已有方案。
---

# 故障预测组件平台 — Agent 操作手册

这里的一张方案就是一张**带类型的组件图**：`data.input →（质量预检）→ 窗口/标签 → 特征 → 验证`，
外加探索与可视化分支。你（通过 MCP）、网页设计器和 HTTP API 编辑的是**同一张图**，所以人能实时
看着你的节点出现并运行。

按顺序走这条环路：**侦察 → 数据准备 → 质量预检 → 窗口 → 特征 → 验证 → 执行 → 读结果 → 修正 →
持久化 → 汇报**。每一阶段都消费上一阶段的契约；实践中代价最大的两个失误（数据路径写错、通道
死掉或恒定）都在前两个阶段用几次调用就能拦住。

## 0. 操作规则

### 0.1 去哪儿找

| 你正要…… | 去看 |
| --- | --- |
| 开始任何任务，或不清楚服务状态 | §1 侦察 |
| 搞清楚数据在哪、怎么进来 | §2 工作区与数据准备 |
| 判断数据能不能用 | §3 质量预检 |
| 切窗口、选标签策略 | §4 窗口、分组与标签 |
| 加特征、合并或清洗特征 | §5 特征 |
| 选切分方式、读指标 | §6 验证 |
| 排错、重试某个节点 | §7 执行与排错 |
| 读结果但不想把数组灌进上下文 | §8 读取结果 |
| 把方案交给别人 | §9 持久化与交接 |
| 内存告急，或文件超过 1 GB | §10 大数据与内存 |
| 有报错原文要对原因 | §11 症状，或 `references/troubleshooting.md` |
| 不确定这一阶段是不是漏了某类能力 | §3~§6 末尾的**阶段自检** |
| 需要参数表、实测数字或检查清单 | `references/stages.md` |
| 想照抄一段已知可用的调用序列 | `references/recipes.md` |
| 想知道某个组件是干什么的、什么时候不该用 | `references/components.md` |

### 0.2 硬约束（违反会立刻失败）

1. **端口有类型。** `Dataset`、`FeatureDataset`、`LabelVector`、`FeatureTransformer`、
   `Prediction`、`Model`、`Metrics`、`FeatureImportance`、`StatisticsResult`、
   `CorrelationMatrix`、`Visualization`、`PlotArtifact`。类型不兼容会被拒绝，报错形如
   `Incompatible port types: LabelVector -> Dataset | FeatureDataset`。窗口特征是
   `FeatureDataset`；**绝不要把原始 `Dataset` 直接接进模型**。注意输入端口可以声明兼容类型：
   检查类组件（概览、绘图、探索）同时接受 `Dataset` 与 `FeatureDataset`，所以特征分支能直接挂概览。
2. **一个输入只有一个生产者。** 扇出免费（一个输出接多个下游，这就是分支的方式）；扇入必须显式：
   特征用 `feature.merge`，原始行没有扇入组件。往已占用的输入再连一条会失败：
   `Target input already connected`。
3. **成环会被拒绝**（`Connection creates a cycle`）：图必须是有向无环图。
4. **数据路径相对于服务端的 `data_root`。** 绝对路径与 `..` 逃逸都会被拒绝，且 `data_root` 不能在
   运行期更改。
5. **状态在进程内。** 方案、工作区、产物与检查点都属于正在运行的服务；重启即清空。只有 `data_root`
   下的文件和写在 `storage_root` 的 XML 会留下。这是单用户本地服务。

### 0.3 省 token 的纪律

 - 按意图检索（`retrieve_components`），不要列全目录。
 - 成批组装：`add_components` + `connect_many` + `configure_components`，一张 10 节点的图 3~5 次调用
   就够。批量操作默认 `include_graph=false`，保持这个默认，只读
   `{pipeline_id, version, node_count, edge_count, added}`，不要让它回吐整张图。
 - `get_component_schema` 只在真要配置某个组件时取，一次取几个。
 - `get_node_result` 保持紧凑（`include_indices=false`），长数组会被折叠成 `*_count`。
 - 等待用 `wait_for_pipeline`；不要 sleep 轮询。
 - 批量操作是**全成或全不成**：`add_components`/`connect_many`/`configure_components` 里任一条被拒都会回滚整批，并在报错里点名是第几条、哪个组件——失败后不必再 `get_pipeline` 去猜哪些已经生效。

### 0.4 诚实规则

 - **警告是结果的一部分，不是噪音。** 泄漏、平窗口、缓存驱逐、子集与过期工作区的提示，必须连同
   它限定的那个数字一起交给用户。
 - 永远不要把探索性分数（全量拟合的特征选择或 PCA、合成数据）说成验证过的性能。说清楚切分方式
   与覆盖情况。
 - 如果你做了转换、抽稀、删列或填补，说明做了并且说明为什么。
 - 如果某个阶段被跳过（没跑质量预检、没做资产留出），也要说明。**缺一次检查本身就是结论。**

### 0.5 服务与桥的故障

 - `error_code=CONTROL_API_TIMEOUT`：**这只是客户端等超时，不等于服务死了。** 服务端很可能还在算。
   先 `get_pipeline_status` 看真实状态；**绝不要因为一次超时就去重启服务**——内存里的图和产物会全丢。
 - `error_code=CONTROL_API_UNAVAILABLE`：这时桥确实连不上服务。让操作者启动
   `python -m fault_platform serve`，然后重试。
 - 长任务（真实数据上几分钟很正常）用 `wait_for_pipeline(timeout_seconds=...)`；不确定要等多久时，
   用 `get_pipeline_status` 轮询（它返回逐节点状态），这比误判失败要好。

### 0.6 用用户的语言回答

 - 用户用什么语言提问，就用什么语言写结论、警告与汇报（默认中文）。
 - 工具名、组件类型名、参数名、平台报错原文保持英文原样，不要翻译——它们是接口标识符，翻错了
   就没法照着调用。

## 1. 侦察 — 三次调用，加一份能力清单

| 调用 | 它回答什么 |
| --- | --- |
| `get_server_info()` | 平台版本、`data_root`、`storage_root`、数据文件数与前 50 个路径、组件数量、产物缓存统计（字节、驱逐、溢出、溢出目录） |
| `list_datasets()` | `data_root` 下所有可读的 `.csv`/`.parquet`/`.pq` 及其字节大小 |
| `get_component_facets()` | 现存的分类、子分类、标签与版本——够你浏览一遍，而不用拉 schema |

然后按意图检索：`retrieve_components(intent=..., category=..., input_type=..., output_type=..., source_component_type=..., target_component_type=...)`。它按词面加权打分（含中文二元组），并能只保留"可以合法夹在两个已有节点之间"的组件——这是往已有图中间插节点时最好用的能力。需要穷举视图时用 `list_components`（`category`、`tags`、`input_type`、`include_schema` 等过滤器），纯关键词用 `search_components`。**优先用 `retrieve_components`**：列全目录只会白费上下文。

侦察要产出两样东西：上面那些环境事实，以及一份 3~6 行的**能力清单**（这次任务可能用得上的手段）。做法是 `get_component_facets()` 加一两次 `retrieve_components`，只针对你不确定平台是否具备的能力（漂移、周期性、无监督异常、类别编码……）各查一次。**不要枚举全部 56 个组件**：那是目录，不是清单，只会烧上下文而不会改变任何决定。

`create_example(include_xgboost=false)` 会在合成数据上造一张能端到端跑通的示例图。用它学图的形状或给服务做冒烟测试；**永远不要用它回答关于用户数据的问题**。

## 2. 阶段 1 — 工作区与数据准备

**目标：** 原始文件已经在 `data_root` 里，而且你知道哪几列是实例、资产、时间、测量值和标签。

1. `get_server_info()` → 记下 `data_root`，它就是沙箱根目录。
2. `list_datasets()` → 文件已经在里面了吗？在的话，直接用那个相对路径。
3. 不在的话得先弄进来，而**通过 MCP 你无法复制文件进去**。两条路：网页的"上传 CSV"按钮（写到 `data_root/uploads/dataset_<hex>.<ext>`，会改名，所以之后要重新 `list_datasets`；上限 25 MB，只收 `.csv`/`.parquet`/`.pq`），或者让操作者把文件放进 `data_root`／用 `--data-root <dir>` 重启服务。说清楚你要哪一条、为什么，**不要猜路径，也不要自己去翻文件系统**。
4. 配置 `data.input`：`path`（相对路径，**必填**）、`format`（默认 `csv`，或 `parquet`），CSV 还要 `separator`/`encoding`。

平台只认 CSV 和 Parquet：`.mat`、`.txt`、MDF/BLF 都必须在平台外先转换，而且你要说明你转了。转换配方与阶段检查清单在 `references/stages.md` 的阶段 1。转换时目标是**一行一个采样**（`asset_id`、`instance_id`、`time`、测量列、标签），窗口组件就认这个形状；同时把实例列和资产列都记下来——趁数据还在你眼前。

## 3. 阶段 2 — 质量预检

**目标：** 在任何通道变成"意外的特征"之前，先知道哪些通道是死的。

把 `data.quality` 挂在同一个 `data.input` 输出上，并且用**与后面窗口特征完全一致**的 `columns`、`group_column`、`time_column`、`window_size`、`step`。它输出 `Visualization`，是**终端分支**：永远不会喂给模型，这也正是扇出存在的理由。

有两类发现会改变决定，而 `visual.overview` 看不见它们：一列可以"缺失率 0%、整列非常数，但在每个组内恒定"（传感器未投用、历史库拿 0 填充）；以及被量化或保持的通道会产生**平窗口**（`feature.spectral` 在那里默认输出 NaN）。把每列的平窗口比例写进汇报。完整的发现表在 `references/stages.md` 的阶段 2。

**阶段自检 — 这一步有问题吗？** 逐条自问，**只有真的命中才动手**。一道闸门命中，代价是加一个终端分支，不是重做方案，所以判断标准是"这个答案会改变我下一步吗"。

| 自检 | 命中时 |
| --- | --- |
| 通道看起来高度冗余 | 在同一个 `data.input` 输出上挂 `explore.correlation`（终端分支） |
| 一个文件里有多段时期／批次／资产 | 挂 `explore.concept_drift`——它要**两个**输入（`reference`、`current`），用两个 `data.filter` 切早段与晚段分别接进去 |
| 怀疑有周期性或反复出现的工况 | 挂 `explore.periodicity`（采样率未知就先问，不要猜） |
| 各资产的量级差得很多 | 挂 `explore.central_tendency` + `explore.dispersion`，按资产列分组 |
| 完全没有标签，只想先看"异常长什么样" | 挂 `explore.anomaly`（输出 `Prediction`，终端分支） |

**不要为了"用上组件"而加节点。** 上面每一条都是终端分支：它服务人的报告，不会进模型。

## 4. 阶段 3 — 窗口、分组与标签

**目标：** 得到一张 `FeatureDataset`，它的每一行与标签一一对应，并且验证阶段能诚实地切分。

窗口生产者是 `feature.statistical`、`feature.fitting`、`feature.spectral`、`feature.entropy`。如果你打算合并它们，它们必须在 `columns`、`group_column`、`time_column`、`asset_column`、`label_column`、`window_size`、`step`、`label_policy` 上完全一致；参数语义见 `references/stages.md` 的阶段 3。 窗口有两种定义：**按行**（`window_size`/`step`，固定采样率的短窗）与**按时间**（`window_span`/`step_span`，如 `"7d"`——"用最近 7 天的数据"就该这么写，采样不规则时每个窗口的行数可以不同）。预测任务（故障发生前报警）用 `label_policy=horizon` + `prediction_horizon`，标签取自窗口**之后**的视野。

两条规则直接决定这次运行能不能成：

 - `label_policy=strict`（默认）在**一个窗口内的标签发生变化时会让整次运行失败**——而真实故障数据里，变化点恰好就在故障起始处，也就是最有意思的地方。改用 `mode`（多数标签）或 `last`，并说明你选了哪个、为什么。
 - `window_size=0` 的含义是"整组当成一个窗口"：整段分类正确，起始点检测错误。`window_size`/`step` 要按真实采样率定，不要照抄示例。

**阶段自检 — 这一步有问题吗？**

| 自检 | 命中时 |
| --- | --- |
| 标签会在窗口内变化 | 用 `label_policy=mode`；真实数据上 `strict` 必然失败 |
| 要回答"未来会不会故障"（预测，而不是检测） | 用 `window_span`（如 `7d`）+ `prediction_horizon`（如 `2d`）+ `label_policy=horizon`：窗口结束加 `prediction_gap` 之后、视野之内出现故障就标 1；窗口自身已故障的样本按 `current_fault_policy` 处理（默认丢掉并计数，它们属于检测任务） |
| 要回答的是"退化从什么时候开始" | 用窗口末端标签配 `mode`，并预期正类稀少 |
| 打算整体留出资产 | 现在就把资产列接上（`data.asset_key`，或窗口组件的 `asset_column`）；否则 `split_method=asset` 会拒绝运行 |
| 某个组比一个窗口还短 | 缩短 `window_size`，或者回到阶段 2 丢掉这个组并说明 |

## 5. 阶段 4 — 特征

**目标：** 一条或多条对齐的 `FeatureDataset` 分支，合并好、清洗好，并且不含你已经知道是死的通道。

"哪类信号问题该用哪个分支"（水平与形状、趋势、旋转与共振、不规则性、短时动态、离散属性）已经列在 `references/stages.md` 的阶段 4。照着挑，不要默认只挂统计分支；如果特征已经在表里，用 `feature.select`。

下面几条是**被强制**的，不是风格建议：

 - **合并要求来源完全一致。** `feature.merge` 要求索引相同、来源行相同、分组相同，并且**列名不相交**（重叠会报 `Feature names overlap; rename before merging`）。用同样的 columns/window/step/group 建出来的两条分支永远能合；被过滤或重采样过的那条合不了。
 - **NaN 进不了模型。** 频域平窗口、过短的组、原始缺失都会产生 NaN；在特征与模型之间插 `feature.imputation`，并报告填了多少。
 - **类别特征是"拟合出来的配对"。** `feature.categorical` 会产出一个 `encoder`；新数据上要用 `feature.categorical_transform` 复用它，而不是重新拟合。
 - **`sampling_rate` 是必填**，没有默认值：没人知道就**问**，不要猜。
 - **`feature.score_select` 与 `feature.pca` 在全部行上拟合**，因此见过留出集：只能当探索用，且必须把泄漏警告写进汇报。

**阶段自检 — 特征够不够？**

| 自检 | 命中时 |
| --- | --- |
| 模型能看到的列全是"水平／形状"量（均值、标准差、极值类） | 回到 `references/stages.md` 阶段 4 的那张表逐行自问；某一行的问题在这个信号上成立、却没有建分支，就是你漏掉了一类能力 |
| 某个通道是离散档位／等级，却被当成连续量 | `feature.categorical`（拟合出 `encoder`）→ 之后用 `feature.categorical_transform` 复用 |
| 波形是冲击性、不规则的 | `feature.entropy`（`methods` 必填） |
| 需要变化率，但不想切窗 | `feature.temporal`（`columns` 必填）或 `feature.rolling_statistics` |
| 某条探索分支产生了 NaN | 在模型前插 `feature.imputation`，并报告填充比例 |

## 6. 阶段 5 — 验证

**目标：** 一个真正回答用户问题的分数。

分类器默认 `split_method=stratified`，它会拒绝重叠或重复的数据（`Overlapping windows require group or temporal split`）——这是护栏，不是障碍。切分方式要跟数据结构匹配：窗口 → `group`，整台设备 → `asset`（上游要有 `asset_column`），部署顺序 → `temporal`。`validation.linear_regression` 只有 `random`/`group`/`temporal`，**没有资产留出**，遇到这种情况要如实说明这个限制，而不是假装有。完整的方法表、资产留出配方与指标字典在 `references/stages.md` 的阶段 5。

两件事必须写进每个结论：

 - **`group` 不是资产留出。** 一个资产通常贡献多个实例，所以被留出的"组"仍会把该资产的行为泄漏进训练。真实 3W 数据上，同一套特征与模型从实例留出的 ROC-AUC 0.716 掉到留一井的 0.528。
 - **把 `coverage` 跟分数一起报出来**——"3 个测试井全是训练时没见过的"这句话才让那个数字有意义。`validation.compare` 只接受特征、标签、切分方式、`test_size`、`random_state` 完全一致的两个运行。

**阶段自检 — 验证方式回答得了这个问题吗？**

| 自检 | 命中时 |
| --- | --- |
| 类别极不平衡 | 报 `balanced_accuracy`、`per_class_recall`、`miss_rate`，不要只报 accuracy |
| 没有标签可以验证 | `validation.knn_detector`、`validation.isolation_forest_detector` 或 `validation.persistence_detector`——这三个都吃原始 `Dataset`，不需要 `LabelVector` |
| 要回答"这条序列本身可不可预测" | 用 `validation.arma`（`column` 必填）当基线 |
| 对比里只有一个算法族 | 把 `validation.decision_tree` 或 `validation.reservoir_classifier` 加进 `validation.compare` |
| 留出集里有模型没见过的资产 | 引用 `coverage.unseen_assets`；这才是唯一值得信任的泛化数字 |

## 7. 阶段 6 — 执行与排错

**目标：** 一次跑完，或者对"卡在哪里"给出精确诊断。

```
create_pipeline(name)
  → add_components([...])            # 批量
  → connect_many([...])              # 批量
  → validate_pipeline(pipeline_id)   # 只查结构，很便宜
  → execute_pipeline(pipeline_id)    # 立刻返回 RUNNING
  → wait_for_pipeline(pipeline_id, timeout_seconds=...)
```

`validate_pipeline` 不跑任何计算就能查出：必填输入没接、必填参数没填、类型不匹配、成环、一个输入被两个生产者占用、`compatibility` 约束不满足。**每次执行前都先跑它**，比跑失败便宜得多。

"成功"有两层：控制调用成功（`success: true`）与运行成功（`status`）。查询一次 `FAILED` 的运行本身是一次成功的查询。

节点失败时，除了 `error` 还要看 `get_node_result` 里的 `input_summary` 预览——它显示组件真正收到的前几行，通常答案就在那里（一整列常数、类型不对、过滤条件什么都没匹配到）。改完用 `configure_components`，再用 `retry_node`：增量复用只会重算这个节点及其下游。状态词表与其余迭代手段见 `references/stages.md` 的阶段 6。

**真实数据上一次运行可能要几分钟**（本仓库实测：48.9 万行 / 8091 个窗口 / 三个特征分支 + RF-300 约 150 秒）。所以：等待要留足 `timeout_seconds`；看到 `CONTROL_API_TIMEOUT` 先去 `get_pipeline_status` 确认，**不要重启服务**。

## 8. 阶段 7 — 读取结果

`get_node_result(pipeline_id, node_id)` 返回 `{status, outputs: {端口: {kind, …, value}}, error, warnings}`；`get_pipeline_result` 一次覆盖所有节点（图大时要收小 `limit`）。保持 `include_indices=false`：指标载荷里已经有混淆矩阵、每类召回与类别计数，描述一个结果根本不需要 `train_indices`/`test_indices`——把它们拉出来曾经把 8000 个行号灌进上下文。只有"行本身就是交付物"时才展开索引。工作区 `warnings` 要原样转发。各 `kind` 的载荷形状见 `references/stages.md` 的阶段 7。

## 9. 阶段 8 — 持久化与交接

`save_pipeline(pipeline_id, filename="...xml")` 把图写到 `storage_root` 下；`get_pipeline_xml` 直接返回同一份文档；`load_pipeline(xml)` 会导入成一个新方案；`replace_pipeline(graph, expected_version=...)` 是带乐观并发的覆盖，版本过期会被拒绝。`save_checkpoint`/`load_checkpoint`/`list_checkpoints` 同时快照图**与**工作区（都在内存里，随进程消失）。`delete_pipeline` 会删掉方案、工作区、溢出文件与检查点——放弃一次失败尝试后用它，别让服务一直背着。

交接时给出：方案 id、XML 路径、数据路径、切分方式、标签策略、以及带覆盖情况的头条指标。人可以在 `http://127.0.0.1:8765` 打开同一个图；你的改动会通过 SSE（`GET /api/events`）实时出现，包括运行中的节点状态。

## 10. 大数据与内存

产物是**按引用**存的（存储与预览都不复制），缓存可以在字节预算内溢写到磁盘，也可以按要求流式处理——但边界是真的：

 - **先给读取加边界：** `columns`（投影）与 `max_rows`，Parquet 还可以用 `filters`（谓词下推，例如 `[["equipment", ">", 10]]`）。
 - **超过约 1 GB 用流式：** `data.input.streaming=true` + `chunk_rows`。行必须按组**连续**排列（先按组列排序），组内按时间有序。只有 `feature.statistical`、`feature.fitting`、`feature.spectral`、`visual.overview`、`data.materialize` 接受流式数据集；其它组件（包括 `data.quality`、熵特征、过滤、绘图）会以 `… cannot consume streamed input; insert data.materialize or turn streaming off on data.input` 拒绝。所以要么先在不流式的分支上做质量预检，要么接受物化并保留那条警告。
 - **单个超大组**（一口井、上百万行）仍然会被整体载入。拆成实例，或者缩短窗口。
 - 缓存预算会驱逐产物：有溢出目录时按需重载；没有时下游节点失效、方案退回 `READY`——这时要重跑，而不是假设数字还在。`get_server_info` 会如实报告 `evictions`/`spills`。
 - 如果你只分析了有界子集，结论就只关于这个子集：把行数、列与过滤条件写出来。

## 11. 症状 → 原因 → 修法

| 症状 | 可能原因 | 修法 |
| --- | --- | --- |
| `Incompatible port types: Dataset -> FeatureDataset` | 把原始数据接到了要特征的位置 | 插入窗口/特征组件，或用 `feature.select` |
| `Overlapping windows require group or temporal split` | 用 `stratified` 切了有重复组的数据 | 改 `split_method` |
| `Feature provenance differs: source_rows` | 两条要合并的分支窗口或过滤不同 | 用完全相同的窗口参数重建两条分支 |
| 模型报错并点名 NaN 列 | 上游有平窗口或缺失值 | 在模型前插入 `feature.imputation` |
| 运行在一个窗口上失败，消息提到标签变化 | 起始点数据用了 `label_policy=strict` | 改成 `mode`/`last` |
| 等待返回 `CONTROL_API_TIMEOUT` / `CONTROL_API_UNAVAILABLE` | 客户端超时，或桥真的连不上服务 | 先 `get_pipeline_status` 确认真实状态；**不要重启服务**（内存状态会全丢）。确实是服务没起来才让操作者启动它 |

其余护栏（含原始报错文本与它为什么存在）都在 `references/troubleshooting.md`。

## 12. 汇报契约

任务结束时，回答里必须包含：

1. **数据** —— 哪个文件、哪些列（实例/资产/时间/标签/测量），以及你做过的任何转换或有界读取。
2. **质量** —— 发现死列/平窗口/常数列了吗，你怎么处理的。
3. **图** —— 节点 id 与组件类型、窗口参数、标签策略及其理由。
4. **验证** —— 切分方式、留出规模、含未见资产的 `coverage`、指标表，以及每一条警告。
5. **边界** —— 哪些是探索性的、哪些是合成的、哪些被跳过了、什么会让这个数字失效。
6. **产物** —— 方案 id、XML 路径、人能打开查看的地址。
7. **阶段自检** —— 哪些闸门命中过、各自做了什么；以及整场没触及的能力类别（一行说明为什么这次可以接受）。
8. **中间产物证据** —— 给出一次对你真正建模的那份数据的直接观察：把 `visual.overview` 挂在特征分支上（问题涉及信号或冗余时再加 `visual.line`/`explore.correlation`），并引用它返回的内容——行数、列名、缺失率——让人不必只凭你的结论。
9. **语言** —— 用用户提问的语言写（默认中文）；工具名、组件类型、参数名与平台报错原文保持英文。

先给结论，再给限定它的那句注意事项。不要把注意事项提前，也永远不要省略它。

## 附录 A — 工具地图（38 个）

| 阶段 | 工具 |
| --- | --- |
| 侦察 | `get_server_info`, `list_datasets`, `get_component_facets`, `list_components`, `search_components`, `retrieve_components`, `get_component_schema` |
| 方案生命周期 | `create_pipeline`, `list_pipelines`, `get_pipeline`, `delete_pipeline`, `replace_pipeline`, `save_pipeline`, `load_pipeline`, `get_pipeline_xml`, `create_example` |
| 图编辑 | `add_component`, `add_components`, `remove_component`, `configure_component`, `configure_components`, `connect_components`, `connect_many`, `disconnect_components` |
| 执行 | `validate_pipeline`, `execute_pipeline` (`mode=all|node|from`, `incremental`), `execute_node`, `execute_from_node`, `retry_node`, `cancel_pipeline` |
| 观察 | `get_pipeline_status`, `wait_for_pipeline`, `get_node_result`, `get_pipeline_result`, `get_history` |
| 持久化 | `save_checkpoint`, `load_checkpoint`, `list_checkpoints` |

`wait_for_pipeline` 是唯一会阻塞的工具，并且刻意跑在服务锁之外。如果客户端显示带后缀的重名工具（`list_pipelines_1`），用不带后缀的那个名字。

## 附录 B — 能力地图

五个族、56 个组件，以及各自期望什么样的输入端口：

| 族 | 干什么用 | 输入端口 |
| --- | --- | --- |
| **数据 (16):** | 读取、过滤、整形、重采样、切分、缩放/编码、派生键、质量预检 | `Dataset` |
| **探索 (8):** | 提问式的诊断——漂移、周期性、相关性、异常；全部是终端分支 | `Dataset`，其中 5 个也接受 `FeatureDataset` |
| **可视化 (8):** | 给人看的图；全部是终端分支 | `Dataset`，其中 6 个也接受 `FeatureDataset` |
| **特征 (13):** | 窗口特征生产者、合并、清洗、选择、降维 | `Dataset` → `FeatureDataset`（+ `LabelVector`） |
| **验证 (11):** | 有监督分类器、回归/ARMA 基线、无监督检测器、模型对比 | `FeatureDataset` + `LabelVector`，无监督检测器用原始 `Dataset` |

每个组件的端口、关键参数与"什么时候不要用"都在 `references/components.md`。

这份清单是**用来查的目录，不是要逐项权衡的表**。哪一族该上场、什么时候上场，由每个阶段末尾的**阶段自检**（§3~§6）决定：那是几个问题，诚实的答案通常是"不"。

## 参考文件

 - `references/stages.md` —— 每个阶段背后的细节：参数表、实测数字、检查清单与注意事项。
 - `references/recipes.md` —— 可直接照抄的调用序列：常规分类、起始点/退化数据、资产留出、无监督检测、大文件流式、失败后重跑、先探索后建模。
 - `references/troubleshooting.md` —— 完整的症状目录，按护栏逐条说明。
 - `references/components.md` —— 组件地图：每个组件干什么、什么时候不该用、关键参数。

参数的权威来源永远是 `get_component_schema(component_type)`；批量目录 `docs/components.md`（由 `scripts/export_catalog.py` 生成）是同一份数据。**如果参考文件与实时 schema 冲突，以 schema 为准——并且那个参考文件就是个待修的 bug。**
