# 阶段手册（`SKILL.md` 背后的细节）

`SKILL.md` 只放决策、硬约束与阶段自检；这个文件放每个阶段背后的参数表、实测数字和检查清单。
**只读你当前所在的那一节**，快速冒烟一次不需要它。章节编号与 `SKILL.md` 的 §号一一对应。

## 阶段 1 — 工作区与数据准备（§2）

### 平台接受哪些格式

只有 `.csv`、`.parquet`、`.pq`。其它格式必须在平台**之外**先转换，而且你要说明你转换过：

| 来源 | 用什么转 |
| --- | --- |
| `.mat`（CWRU、SEU） | `scipy.io.loadmat` → DataFrame → `to_parquet` |
| `.txt`（C-MAPSS） | `pandas.read_csv(sep=空白, header=None)` → `to_parquet` |
| MDF/BLF（车载日志） | MATLAB `mdfRead`/`blfread`，或 Python `asammdf`/`python-can` |
| 历史库／数据库 | 导出一段列式切片成 Parquet |

转换时目标是**一行一个采样**：`asset_id`、`instance_id`、`time`、测量列、标签。窗口组件就认这个形状。

### 检查清单

 - [ ] 已经知道 `data_root`；文件能在 `list_datasets` 里看到，或者你已明确请操作者放进来
 - [ ] 实例／资产／时间／标签／测量列都已确认（问出来的，或推断出来并说明了推断依据）
 - [ ] `data.input.path` 是相对路径且能解析（不确定就单独跑一下 `data.input`）
 - [ ] 文件超过约 1 GB？先选有界读取（`columns`、`max_rows`、Parquet `filters`）或 `streaming`

### 注意事项

 - 猜路径。每条错的路径都是一次白跑的运行；`list_datasets` 只要一次调用。
 - 以为文件不用转换。`.mat`/`.txt`/MDF 都不被接受；把 `.mat` 改名成 `.csv` 这种半成品会在后面以莫名其妙的 dtype 报错。
 - 把实例当成资产。趁原始数据还在眼前，把两列都记下来。

## 阶段 2 — 质量预检（§3）

### `data.quality` 报告什么

| 发现 | 含义 | 处理 |
| --- | --- | --- |
| `all_nan_columns` | 整列没有数据 | 从 `columns` 里去掉 |
| `constant_columns` | 整列恒定 | 去掉——零方差零信息 |
| `groups_with_constant_columns` | 在某个资产内部恒定，但跨资产会变 | 典型的"保持值/未投用"点位；通常该丢，保留就必须说明理由 |
| 重复行 | 重复采样 | 先查清楚再信指标——重复会跨切分泄漏 |
| 混合标签窗口、标签跳变 | 窗口内标签发生变化 | 直接告诉你 `label_policy=strict` 能不能活 |
| 每列平窗口比例 | 窗口内完全没有变化的比例 | `feature.spectral` 在那里默认输出 NaN；把这个比例写进汇报 |

用**与窗口特征完全一致**的 `columns`、`group_column`、`time_column`、`window_size`、`step` 去跑，这样它的计数才描述真实的东西。

### 实测例子

在一份 3W 风格的工业数据上，`P-TPT` 有 19.4% 的窗口完全平直、`T-TPT` 7.8%，而它们的
`missing_rate = 0`。`visual.overview` 什么也看不出来，`data.quality` 一眼就看出来了。

### 检查清单

 - [ ] `data.quality` 用最终窗口参数跑过
 - [ ] 死列／常数列／平窗口列已丢弃，或保留并写明理由
 - [ ] 打算用的标签策略与混合标签计数不矛盾
 - [ ] 已经告诉了用户这次预检发现了什么

### 注意事项

 - `missing_rate = 0` 不等于"可用"。
 - 全局统计会掩盖组内恒定：一列可以缺失率 0%、整列非常数，但在每一口井里都是常数。
 - 跳过这一阶段不会让运行失败，但会让**结论**失败。

## 阶段 3 — 窗口、分组与标签（§4）

窗口生产者是 `feature.statistical`、`feature.fitting`、`feature.spectral`、`feature.entropy`。它们共享这些参数：

| 参数 | 含义 | 说明 |
| --- | --- | --- |
| `columns` | 测量列 | **必填**；只放测量值，绝不要放 id／时间／标签列 |
| `group_column` | 窗口分组（实例或一次运行） | 窗口永不跨组；不设时 `window_size=0` 等于"整张表" |
| `label_column` | 为窗口生成标签 | 把它的 `labels` 输出接到每个验证器 |
| `time_column` | 排序依据 | 只要行是按时间排列的，就设上它 |
| `asset_column` | 所属资产（井、机器） | `split_method=asset` 必需 |
| `window_size` | 每个窗口多少行 | `0` = 整组（每组只出一行） |
| `step` | 步长 | `0` = 不重叠；`step < window_size` = 重叠窗口 |
| `window_span` | 时间窗口，如 `7d`/`12h`/`180s` | 与 `window_size` 二选一；按**时间**切窗，采样不规则时每个窗口行数可以不同 |
| `step_span` | 时间步长，如 `1d`/`60s` | 留空 = 不重叠；小于窗口跨度就是重叠窗口 |
| `prediction_horizon` | 预测视野，如 `2d` | 只在 `label_policy=horizon` 下有效：窗口结束之后这么久内出现故障就标 1 |
| `prediction_gap` | 预测间隔（禁入带） | 把视野整体推后，避免贴着故障起始的样本过易 |
| `current_fault_policy` | 窗口自身已故障时怎么办：`drop`/`positive`/`negative` | 默认 `drop`：这些样本属于检测而不是预测，丢弃**并计数** |
| `normal_label` | 哪个标签值算正常（默认 `0`） | 其它取值都算故障；标签是字符串时按字符串比较 |
| `label_policy` | `strict`（默认）/ `last` / `mode` | 见下表 |
特征行数 = **窗口数**：每组约"组内时长 / 步长"（按行切窗则是"组内行数 / 步长"），最后一个不完整的窗口丢弃。这与输入行数无关——48.9 万行原始数据配 `180s`/`60s` 得到 8081 行特征，步长换成 `180s` 就只剩 2700 行。

### `label_policy`

| 策略 | 行为 | 什么时候用 |
| --- | --- | --- |
| `strict` | 窗口内标签**变化**就让整次运行失败 | 按构造标签在每个窗口内恒定 |
| `mode` | 取窗口内多数标签 | 起始点／退化数据——真实场景的常态 |
| `last` | 取窗口末端标签 | 你明确要预测窗口结束时刻的状态 |
| `horizon` | 用**未来视野**打标签：窗口结束加 `prediction_gap` 之后、`prediction_horizon` 之内出现故障就标 1 | 预测任务（故障发生前报警）——需要 `time_column`、`label_column`、`window_span` 与 `prediction_horizon` |

如果标签随时间变化（几乎每一份真实故障数据都是），`strict` **一定**会失败——这是正确行为不是 bug，而且它恰好发生在故障起始处，通常还是数据里最有意思的位置。改用 `mode`（或 `last`），并说明你选了哪个。

### 检查清单

 - [ ] 每个窗口组件（以及 `data.quality`）的 `columns`、`group_column`、`time_column`、`window_size`、`step` 完全一致
 - [ ] 每个验证器的 `features` **与** `labels` 都来自同一个窗口组件
 - [ ] `window_size`/`step` 是按真实采样率定的，不是从示例抄来的
 - [ ] 标签策略与所选理由都写出来了

### 注意事项

 - 行级标签不是窗口标签。`data.labels` 给的是每原始行一个标签；和窗口特征一起喂给模型会长度不匹配而失败。
 - `window_size=0` 会把每组悄悄压成一行——整段分类正确，起始点检测错误。
 - 一个窗口组件扇出给多个消费者没问题；把两条窗口分支合起来必须走 `feature.merge`，且来源一致。

### 预测任务长什么样

按时间窗口加未来视野：窗口 `[t, t+span)` 是"手上的证据"，标签取自 `(t+span+gap, t+span+gap+horizon]` 这段未来。真实 3W 数据实测（489,456 行 / 28 个实例，窗口 `180s`、步长 `60s`、视野 `1h`）：得到 **3370 个窗口、正类占 26.9%**，同时丢弃 **4380 个"自身已故障"**与 **331 个"视野超出数据"**的窗口。两个丢弃数必须写进汇报，否则"样本为什么变少、为什么不能用"就没人知道。

两条硬规矩：视野超出可用数据的窗口**不能标 0**（"没看到故障"不等于"没有故障"）；窗口自身已经故障的样本也不属于预测任务。两者都由平台丢弃并计数，写进 `attrs` 并以警告形式给出。

## 阶段 4 — 特征（§5）

### 哪类问题用哪个分支

| 关于信号的问题 | 用哪个分支 |
| --- | --- |
| 水平与形状（mean、std、variance、RMS、偏度、峰度、分位数、极差、IQR、MAD、峰值、波峰因数） | `feature.statistical` |
| 趋势／退化速率 | `feature.fitting`（`linear`、`polynomial` + `degree`、`exponential`） |
| 旋转、共振、周期性 | `feature.spectral`——需要采样率，且通道真有变化 |
| 复杂度与不规则性 | `feature.entropy`（`approximate_entropy`、`information_entropy`） |
| 不切窗的短时动态 | `feature.temporal`（差分、滚动自相关） |
| 保持行对齐的滚动汇总 | `feature.rolling_statistics` |
| 离散属性（类别、模式、等级） | `feature.categorical` → `encoder` 端口 |
| 特征已经算好在表里 | `feature.select` |

### 被强制执行的规则

 - **合并要求来源完全一致。** `feature.merge` 需要索引完全相同、来源行相同、`groups`/`assets`/`source_path`/`source_id` 相同，并且**列名不相交**（重叠会报 `Feature names overlap; rename before merging`）。用同样的 columns/window/step/group 建出来的分支永远能合；被过滤、重采样或改过窗口的那条合不了。
 - **NaN 不能进模型。** NaN 的来源：频域平窗口（`flat_policy=nan`）、过短的组、原始缺失值。在特征与模型之间插 `feature.imputation`（`mean`、`median`、`zero`、`drop_columns`，常数用 `fill_value`）。忘了插的话，模型报错会点名具体列。填了多少要报出来。
 - **类别特征是拟合出来的配对。** `feature.categorical` 会拟合并输出 `encoder`；新数据上通过 `feature.categorical_transform` 复用它，不要重新拟合。验证器会把上游的类别编码器内嵌进训练好的模型。
 - **探索类输出是终端。** `visual.*`、`explore.*` 以及 `report`/`plot`/`StatisticsResult`/`CorrelationMatrix` 端口是给人看的，不是模型的输入。
 - **`feature.score_select` 与 `feature.pca` 在全部行上拟合**，因此见过留出集。两种可接受的用法：在检查分支上理解结构；或在模型前使用，但把泄漏警告当作前提条件一起汇报。它们的排序永远不是验证过的证据。
 - **`feature.spectral` 需要已知采样率。** `sampling_rate` 必填且无默认值；没人知道就问，不要猜。保持值或量化值的通道会产生平窗口：默认 `flat_policy=nan` 会保住行对齐、给出 NaN 频谱（之后再填补），`skip` 会丢掉这些窗口（只有当没有东西必须与它们合并时才安全），`error` 恢复硬失败。只在窗口两端出现的波动也算平窗口，因为 Hann 窗在两端为零。

### 检查清单

 - [ ] 每条特征分支窗口参数一致，合并时没有列名冲突
 - [ ] 只要存在 NaN 来源，模型前就有 `feature.imputation`
 - [ ] 探索／可视化节点都挂在终端分支上
 - [ ] 你能说出模型真正会看到哪些列

### 检查中间产物

特征分支可以直接检查：`visual.overview`（行数、列名、类型、缺失率）、`visual.histogram`（分布）、`visual.line`（看几路通道）、`explore.correlation`（冗余度）都同时接受 `FeatureDataset` 与原始 `Dataset`。往你真正建模的那条分支上挂一个，只花一个节点，就把"Agent 说特征没问题"变成故障工程师能直接看的东西；把它的数字写进汇报。

## 阶段 5 — 验证（§6）

`split_method` 出现在 `validation.random_forest`、`validation.svm`、`validation.decision_tree`、`validation.reservoir_classifier` 上（默认 `stratified`，支持 `asset`）。回归是例外：`validation.linear_regression` 只有 `random`（默认）、`group`、`temporal`，没有资产留出——如实说明这个限制，而不是假装有。

| 方式 | 留出什么 | 什么时候用 |
| --- | --- | --- |
| `stratified` | 随机行，按类别比例 | 行彼此独立：没有窗口、没有重复实例 |
| `group` | `group_column` 的整组（**实例**） | 有重叠窗口，或一个资产只有一个实例 |
| `asset` | 整个资产（上游需要 `asset_column`） | "这套东西在没见过的设备上管用吗？" |
| `temporal` | 靠后的行，并清洗掉与测试集共享原始数据的训练行 | 预测未来、按真实部署顺序评估 |

`stratified` 会拒绝重叠或重复的数据（`Overlapping windows require group or temporal split`）——护栏，不是障碍。

### 资产留出配方

1. `data.asset_key(column="<实例键>", target="asset", mode="split", separator="_", index=0)`，或用 `mode="regex"` 配一个捕获组。它从实例键推出资产（`WELL-00001_20170201…` → `WELL-00001`）。
2. 在**每一个**窗口组件以及 `data.quality` 上设 `asset_column="asset"`。
3. 在每个分类器上设 `split_method="asset"`。它要求资产属性且至少两个资产；报错会点名缺哪一块。

### 为什么 `group` 不是资产留出

一个资产通常贡献多个实例，所以被留出的"组"仍会把该资产的行为泄漏进训练。真实 3W 数据实测（28 个实例、10 口井、8091 个窗口、72 个特征、RF-300）：

| 切分 | accuracy | balanced | ROC-AUC | PR-AUC | miss rate | 测试集未见资产 |
| --- | --- | --- | --- | --- | --- | --- |
| `group`（按实例） | 0.638 | 0.621 | **0.716** | 0.804 | 0.231 | 0 / 6 |
| `asset`（留一井） | 0.606 | 0.568 | **0.528** | 0.611 | 0.270 | 3 / 3 |

同一套模型与特征，在切分变诚实之后掉了约 0.19 AUC。汇报时必须这样给：**永远不要把实例级的数字说成部署性能**。

### 指标字典

| 键 | 为什么要看 |
| --- | --- |
| `accuracy` | 头条数字，类别不平衡时会骗人 |
| `balanced_accuracy` | 按类别规模校正过的准确率——故障稀少时优先看它 |
| `roc_auc`、`average_precision` | 排序质量；稀有故障看 PR-AUC |
| `per_class_recall`、`train_class_counts`、`test_class_counts` | 哪个类被悄悄漏掉了（是原始标签，不是编码后的） |
| `confusion_matrix` | 错在哪里 |
| `positive_class`、`miss_rate` | 故障类的漏报率；标签顺序不明确时显式指定 `positive_class` |
| `coverage` | `train_instances`/`test_instances`/`test_instances_unseen` 及对应的资产口径 |
| `warnings` | 泄漏、AUC 不可用、平窗口、缓存驱逐 |

永远把 `coverage` 和分数一起报："3 个测试井全是训练时没见过的"这句话才让那个数字有意义。

`validation.compare` 比较最多三份指标载荷，前提是**留出行完全相同**，测试索引不一致会直接拒绝；所以只有特征、标签、切分方式、`test_size`、`random_state` 都一致的运行才能互相比较。

### 检查清单

 - [ ] 切分方式与数据结构匹配（有窗口 → group/asset/temporal）
 - [ ] 要回答资产级问题？`asset_column` 到处都设了，且 `split_method=asset`
 - [ ] `coverage` 读了并且引用了
 - [ ] 对比的模型除估计器之外完全一致
 - [ ] 警告都和它的数字一起报出来了

## 阶段 6 — 执行与排错（§7）

### 状态词表

方案级：`CREATED`、`VALIDATING`、`READY`、`RUNNING`、`SUCCESS`、`FAILED`、`CANCELLED`。
节点级：`PENDING`、`READY`、`RUNNING`、`SUCCESS`、`FAILED`、`SKIPPED`（skipped = 上游产物不可用）。

"成功"有两层：控制调用成功（`success: true`）与运行成功（`status`）。查询一次 `FAILED` 的运行本身是一次成功的查询。

### 不整图重算的迭代

 - `incremental=true`（默认）复用指纹未变的节点，所以改一个参数只会重算它和它的下游。
 - `retry_node(pipeline_id, node_id)`（等价 `execute_from_node`）重跑该节点及其下游，复用有效的上游产物。修好失败节点后用这个。
 - `execute_node` 只跑一个节点。
 - `cancel_pipeline` 停止运行；`get_history` 列出每个节点的尝试次数、耗时与 `cached` 标记。
 - 编辑图会让工作区失效：结果被丢弃，节点回到 `PENDING`（"Graph changed; run to refresh results"）。

### 读懂一次失败

`get_node_result` 会返回失败节点的 `error`（异常类型、消息、traceback 行、参数快照），其中的 `input_summary` 预览显示组件真正收到的前几行。**答案通常就在预览里**——一整列常数、类型不对、过滤条件什么都没匹配到。改完用 `configure_components`，再 `retry_node`。

### 检查清单

 - [ ] 执行前 `validate_pipeline` 是干净的
 - [ ] 运行到达 `SUCCESS`，或者你能解释每一个 `SKIPPED`/`FAILED` 节点
 - [ ] 读了 traceback，而不是直接重跑

## 阶段 7 — 读取结果（§8）

各 `kind` 的载荷形状：

| `kind` | 内容 |
| --- | --- |
| `table` | `shape`、`columns`、`dtypes`、预览行、预览范围内的 `missing_rate` |
| `vector` | 长度、名字、前几个值 |
| `array` | 形状、前几个值 |
| `streamed` | 流式数据集的描述（行仍在磁盘上） |
| `model` | 类名、特征列表、内嵌的类别编码器数量 |
| `transformer` | 编码器描述 |
| `object` | 映射本身，长数组折叠成 `*_count` |

指标从验证器的 `metrics` 端口读，并保持 `include_indices=false`。载荷里已经有混淆矩阵、每类召回与类别计数，所以描述一个结果根本不需要 `train_indices`/`test_indices`；把它们拉出来曾经把 8000 个行号灌进上下文。只有"行本身就是交付物"时才展开索引。

## 阶段 8 — 持久化与交接（§9）

 - `save_pipeline(pipeline_id, filename="...xml")` 把图写到 `storage_root` 下（只写 XML，路径被限制在存储目录内）。`get_pipeline_xml` 直接返回同一份文档；`load_pipeline(xml)` 导入为新方案（id 冲突时生成新 id）；`replace_pipeline(graph, expected_version=...)` 是带乐观并发的覆盖，版本过期会被拒绝（`Graph changed in another client; reload before editing`）。
 - `save_checkpoint` / `load_checkpoint` / `list_checkpoints` 快照图**与**工作区（在内存里，随进程消失）。恢复一个产物已被释放的检查点会给出警告并重算受影响节点。
 - `delete_pipeline(pipeline_id)` 删除方案、工作区、溢出文件与检查点。放弃一次失败尝试后用它，别让服务一直背着。
 - 交接时说清楚：方案 id、XML 路径、数据路径、切分方式、标签策略、带覆盖情况的头条指标。

## 大数据与内存（§10）

工作区**按引用**存产物（存储与预览都不复制），可以在字节预算内溢写到磁盘，也可以按要求流式处理——但边界是真的：

 - **先给读取加边界：** `columns`（投影）与 `max_rows`，Parquet 还可以用 `filters`（谓词下推，例如 `[["equipment", ">", 10]]`）。
 - **超过约 1 GB 用流式：** `data.input.streaming=true` + `chunk_rows`。行必须按组**连续**排列（先按组列排序），组内按时间有序。只有 `feature.statistical`、`feature.fitting`、`feature.spectral`、`visual.overview` 与 `data.materialize` 接受流式数据集；其它组件——包括 `data.quality`、熵特征、过滤与绘图——会以 `… cannot consume streamed input; insert data.materialize or turn streaming off on data.input` 拒绝。所以质量预检要么在打开流式之前先做，要么接受一次物化并保留那条警告。
 - **单个超大组**（一口井、上百万行）仍会被整体载入。拆成实例，或者缩短窗口。
 - 缓存预算会驱逐结果。有溢出目录时（服务默认如此）被驱逐的载荷按需重载；没有时拥有它的节点会失效、方案退回 `READY`——这时要重跑，而不是假设数字还在。`get_server_info` 会如实报告 `evictions` 与 `spills`。
 - 如果你只分析了有界子集，结论就只关于这个子集：把产生该数字的行、列与过滤条件写出来。
