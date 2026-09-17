# Fault Prediction Component Platform

**故障预测组件化建模与执行平台 · Fault Studio v0.1**

把故障预测方案拆成可复用、可连接、可配置的原子组件，像搭 Simulink 一样搭建：在网页里拖拽连线，或让 AI Agent 通过 MCP 自动搭建，两者编辑的是**同一个 ComponentGraph**，由同一个 Runtime 执行，并能保存为 XML。

```
拖入组件 → 配置参数 → 连接端口 → ComponentGraph → XML → ExecutionEngine → Workspace → 查看中间结果与最终结果
```

- **人工入口**：Visual Designer（网页，无需前端构建）
- **Agent 入口**：MCP stdio bridge + `skills/fault-prediction/SKILL.md`
- **脚本入口**：`fault_core` 纯数值 API 与 `fault_platform` Graph API

底层 `fault_core` 是独立数值库，不知道 Graph、MCP 和 Agent；正确性（端口类型、参数、依赖、执行）由 Runtime 负责，LLM 只负责理解目标、规划方案和解释结果。

---

## 1. 快速开始

### 1.1 安装（需要 Python 3.11+）

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,mcp,xgboost]"
```

Windows 也可以直接运行 `.\setup.ps1`。Linux / macOS 把 `.venv\Scripts\python.exe` 换成 `.venv/bin/python`。

依赖按需选择：

| 安装方式 | 用途 |
| --- | --- |
| `pip install -e .` | 只跑网页与 Runtime |
| `pip install -e ".[mcp]"` | 让 Agent 通过 MCP 操作 |
| `pip install -e ".[xgboost]"` | 使用 XGBoost 组件 |
| `pip install -e ".[statsmodels]"` | 使用 ARIMA 组件（未安装时该组件会给出可执行的安装提示） |
| `pip install -e ".[parquet]"` | 读取 Parquet 数据源（大文件列裁剪/谓词下推） |
| `pip install -e ".[dev]"` | 运行测试与静态检查 |

安装后可用的命令：`python -m fault_platform ...` 或等价的 `fault-platform ...`。网页与 Runtime 不需要 Node.js，npm 只用于可选的前端测试。

### 1.2 启动服务

```powershell
.\.venv\Scripts\python.exe -m fault_platform serve --port 8765
# 或
.\start.ps1
```

浏览器打开 **http://127.0.0.1:8765**。Ctrl+C 停止；端口被占用时改用 `--port 8766`。

服务只绑定本机 127.0.0.1，面向单用户开发。参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--port` | 8765 | 网页与 HTTP 控制 API 端口 |
| `--data-root` | examples/data | CSV 数据目录，数据输入组件的 path 相对此目录 |
| `--storage-root` | .fault-platform/pipelines | 「保存」写入的 XML 目录 |

### 1.3 两分钟跑通示例

1. 点顶部 **加载示例**（生成合成设备数据与示例方案）。
2. 点 **▶ 运行方案**，等状态变为 `SUCCESS`。
3. 依次点击节点查看输出：数据概览（列、类型、缺失率、**标签构成**）、统计特征（特征表）、随机森林（指标、**训练/测试的正负样本构成**、混淆矩阵、特征重要性）、折线图。

   正负样本比例是读其它指标的前提：测试集里一个故障样本都没有时，`accuracy=1.0` 只说明"模型全判正常"，
   平台会为这种情况直接给出一条警告（`The test set contains no positive (1) rows`），别只抄 accuracy。
4. 切换结果区页签：**执行日志** 看每个节点的状态、耗时与缓存命中，**XML 方案** 看完整配置。
5. 点 **保存** 写入 `.fault-platform/pipelines`，或 **导出 XML** 下载文件。

示例数据是固定随机种子的合成三分类设备数据，指标只证明工程闭环，不代表真实工业效果。

---

## 2. 网页使用（人工搭建）

### 2.1 准备数据

- 点「＋ 上传 CSV」上传 UTF-8 CSV（25 MB 以内），或把文件放进 `--data-root` 目录（默认 `examples/data`）。
- 左侧「数据文件」下拉会列出目录里的 CSV / Parquet 文件，选择后自动添加一个数据输入节点。
- 输入组件的 `path` 是相对 `--data-root` 的路径，例如 `synthetic_equipment.csv` 或 `uploads/dataset_ab12cd34ef56.csv`。
- **文件很大时**：数据输入组件支持列裁剪与行数上限，先在小切片上把流程跑通再放量——
  `columns` 只读需要的列、`max_rows` 限制读取行数（0 表示全读）、`format` 可选 `csv`/`parquet`，
  对 Parquet 还可以用 `filters` 做谓词下推（例如 `[["equipment", ">", 10]]`，需装 `.[parquet]`）。
  任何裁剪都会带一条"结果只描述该子集"的警告，并一路传到模型指标里。
- **输入大于内存时**：给数据输入打开 `streaming`（配合 `chunk_rows`，默认 20 万行）——行留在磁盘上，窗口组件按块读、逐组算特征，峰值内存由"输入大小"变成"输出特征表大小"。
  要求每个设备的行在文件里连续（按分组列排好序）；`time_column` 存在时组内需按时间有序。不支持的组件（如相关性、散点图、行排序/去重）会明确报错并提示插入 `data.materialize` 先物化——物化会带警告，因为内存又重新随输入增长。

### 2.2 搭建五步

| 步骤 | 操作 |
| --- | --- |
| 1. 新建 / 载入 | 「＋ 新建」建空方案；「导入 XML」载入已有方案；顶部下拉切换已保存方案 |
| 2. 放入组件 | 从左侧组件库拖到画布，或拖动已选节点调整位置 |
| 3. 连线 | 先点输出端口（节点右侧），再点输入端口（节点左侧）；按 Esc 取消待连线 |
| 4. 配置参数 | 选中节点，右侧参数面板按组件 Parameter Schema 自动生成，必填项标注「必填」，填好保存 |
| 5. 校验与运行 | 「✓ 校验」做静态检查，「▶ 运行方案」执行；运行中禁止编辑同一方案，「停止」在组件之间生效 |

运行后点击任意节点即可查看该节点的中间数据、图形、指标或模型信息；失败节点会显示错误类型、消息与输入摘要。

### 2.3 画布操作

| 操作 | 方式 |
| --- | --- |
| 平移 | 在空白处按住拖动 |
| 缩放 | 滚轮，或右下角 `＋ / −`；「适应画布」自动缩放到全部节点 |
| 多选 | Shift + 点击 |
| 复制 / 删除 | 工具栏按钮，或 Ctrl+C / Delete |
| 撤销 / 重做 | Ctrl+Z / Ctrl+Y（或 Ctrl+Shift+Z） |
| 全选 / 保存 | Ctrl+A / Ctrl+S |
| 布局 | 「自动布局」按依赖关系分层重排 |
| 连线样式 | 正交折线（水平/竖直段 + 折角圆角），两个端口同一高度时就是一条直线 |
| 收藏与搜索 | 组件库上方搜索框按名称/描述/标签过滤，可只看收藏 |

### 2.4 面板与组件库

组件数量增长到 88 个（后续还会更多），所以左栏做成了"目录 + 分级折叠"，三栏宽窄也都可调：

| 操作 | 方式 |
| --- | --- |
| 折叠分组 | 点击「分类标题」或「子分类标题」即折叠/展开；组件总数超过 24 时子分类默认折叠，先给目录 |
| 全部折叠 / 展开 | 组件库上方的「▾ 折叠全部 / ▸ 展开全部」按钮 |
| 搜索与筛选 | 输入关键词或切换分类/收藏/最近时自动展开命中的分组，不会出现"搜到了却看不见" |
| 记忆状态 | 折叠状态存在浏览器本地（`fault-library-collapsed`），刷新后保持 |
| 调整左栏宽度 | 拖动组件库右边缘的分隔条（向右拖=变宽）；键盘 `←/→` 微调；双击分隔条或「↺ 恢复布局」复位 |
| 调整右栏宽度 | 拖动节点配置左边缘的分隔条（向左拖=变宽，键盘 `←/→` 同理） |
| 调整结果面板高度 | 拖动结果面板上方的分隔条（向上拖=变高，键盘 `↑/↓` 同理）；上限为视口高度的 45% |

宽度同时受视口限制（侧栏不超过视口宽度的 34%），窄窗口下三栏都不会被挤没；面板宽高记录在 `fault-layout`，刷新后保持。

**字号改哪里**：`style.css` 上半部分是压缩成一行、彼此耦合的原始规则，**不要在那里改字号**；文件末尾有一段集中的「字号基线」块（正文类、组件库、画布/右栏、结果面板四组），调字号只动那一段。`scripts/browser_check.cjs` 会实测计算样式并断言下限（例如组件名 ≥ 13px、组件名不得被裁切），所以字号被改回去会直接让浏览器验收变红。

### 2.5 端口类型与连线规则

组件之间只能通过声明了数据类型的端口连接，默认两端类型完全一致才能连。**输入端口可以额外声明兼容类型**：16 个检查类组件（`visual.overview`、`visual.line`、`visual.scatter`、`visual.subplot`、`visual.histogram`、`visual.relationship`、`explore.central_tendency`、`explore.dispersion`、`explore.correlation`、`explore.distribution`、`explore.anomaly`、`explore.peaks`、`explore.normality`、`explore.acf`、`explore.isotonic`、`explore.gbr_fit`）既接受 `Dataset` 也接受 `FeatureDataset`，所以特征分支可以直接挂概览——中间产物随时可查。数据转换类组件不放宽，仍然只吃原始 `Dataset`；`explore.concept_drift` / `explore.kl_divergence` 需要两份输入，因此只吃 `Dataset`。

| 数据类型 | 运行时对象 | 典型来源 |
| --- | --- | --- |
| `Dataset` | DataFrame | 数据输入、过滤、行/列操作、规范化、标准化、数值转换 |
| `TimeSeries` | DataFrame | 时间序列数据 |
| `FeatureDataset` | DataFrame | 统计/拟合/分类/频域特征、特征列选择、特征合并、评分选择、PCA |
| `LabelVector` | Series | 窗口组件的 labels 输出、标签向量组件 |
| `StatisticsResult` / `Metrics` / `Visualization` / `PlotArtifact` | 结构化对象 / JSON 图形规格 | 集中趋势、离散度量、数据概览、散点图、折线图、模型指标、模型对比 |
| `CorrelationMatrix` / `Prediction` / `FeatureImportance` | DataFrame | 相关性、预测结果、特征重要性 |
| `Model` | 带 predict 的训练对象 | 随机森林、SVM、XGBoost |

其他规则：一个输入端口最多接一条连线；连接不能形成环；图必须是有向无环图（DAG），运行时按拓扑顺序调度。

### 2.6 与 Agent 并行工作（实时同步）

网页通过 `GET /api/events`（Server-Sent Events）订阅当前服务的事件流，因此 Agent（MCP）或其它标签页的动作会立刻反映到你打开的页面上：

| 场景 | 页面行为 |
| --- | --- |
| Agent 加/删节点、改参数、连线 | 自动载入新版本并提示「已同步其他端的修改（vN）」，画布立即出现变化 |
| Agent 触发执行 | 顶部状态变为 RUNNING，节点逐个从 PENDING 变为 RUNNING/SUCCESS/FAILED/SKIPPED |
| 节点执行完成 | 节点底部显示本次耗时，命中增量缓存时显示「复用」 |
| Agent 新建/导入方案 | 方案下拉列表自动刷新 |
| Agent 保存检查点 | 检查点下拉列表自动刷新 |
| 你本地有未保存修改，而对方改了方案 | 顶部出现黄色横幅，可选「重新加载」或「保留我的修改」；即使忽略，下次保存也会因版本冲突被拒绝并再次提示 |

断线后浏览器自动重连，并用 `Last-Event-ID` 补齐断线期间的事件；原有的 500 ms 轮询保留作为兜底。

---

## 3. 组件库（88 个）

组件定义由 Registry 统一提供，网页组件库、MCP `list_components` 和 XML 校验读取同一份定义。

### 数据处理 Data Processing

| type | 名称 | 输入 → 输出 |
| --- | --- | --- |
| `data.input` | 数据输入 | 无 → dataset : Dataset |
| `data.materialize` | 物化数据 | dataset : Dataset → dataset : Dataset |
| `data.asset_key` | 资产标识 | dataset : Dataset → dataset : Dataset |
| `data.quality` | 数据质量预检 | dataset : Dataset → report : Visualization |
| `data.filter` | 条件过滤 | dataset : Dataset → dataset : Dataset |
| `data.row_operation` | 行操作 | dataset : Dataset → dataset : Dataset |
| `data.column_operation` | 列操作 | dataset : Dataset → dataset : Dataset |
| `data.time_resample` | 按时间重采样 | dataset : Dataset → dataset : Dataset |
| `data.split` | 数据切分 | dataset : Dataset → train / test : Dataset |
| `data.neighbor_features` | 临近数据纳入 | dataset : Dataset → dataset : Dataset |
| `data.imputation` | 缺失值填充 | dataset : Dataset → dataset : Dataset |
| `data.normalization` | 规范化 | dataset : Dataset → dataset : Dataset |
| `data.standardization` | 标准化 | dataset : Dataset → dataset : Dataset |
| `data.transformation` | 数值转换 | dataset : Dataset → dataset : Dataset |
| `data.binarize` | 特征二值化 | dataset : Dataset → dataset : Dataset |
| `data.polynomial_features` | 多项式特征 | dataset : Dataset → dataset : Dataset |
| `data.discretize` | 离散化分箱 | dataset : Dataset → dataset : Dataset |
| `data.seasonal_difference` | 同期差分 | dataset : Dataset → dataset : Dataset |
| `data.labels` | 标签向量 | dataset : Dataset → labels : LabelVector |

### 数据探索 Data Exploration

| type | 名称 | 输入 → 输出 |
| --- | --- | --- |
| `explore.central_tendency` | 集中趋势 | dataset : Dataset → statistics : StatisticsResult |
| `explore.dispersion` | 离散度量 | dataset : Dataset → statistics : StatisticsResult |
| `explore.correlation` | 相关性度量 | dataset : Dataset → matrix : CorrelationMatrix |
| `explore.distribution` | 分布检查 | dataset : Dataset → statistics : StatisticsResult |
| `explore.periodicity` | 周期性检查 | dataset : Dataset → statistics : StatisticsResult |
| `explore.concept_drift` | 概念漂移 | reference / current : Dataset → statistics : StatisticsResult |
| `explore.cross_relation` | 互相关与互协方差 | dataset : Dataset → matrix : CorrelationMatrix |
| `explore.anomaly` | 异常探索 | dataset : Dataset → prediction : Prediction |
| `explore.peaks` | 山峰检测 | dataset : Dataset → statistics : StatisticsResult |
| `explore.normality` | 正态性校验 | dataset : Dataset → statistics : StatisticsResult |
| `explore.kl_divergence` | KL 散度度量 | reference / current : Dataset → statistics : StatisticsResult |
| `explore.acf` | ACF 自相关函数 | dataset : Dataset → statistics : StatisticsResult |
| `explore.isotonic` | 保序回归 | dataset : Dataset → statistics : StatisticsResult |
| `explore.gbr_fit` | 量化相关性拟合 GBR | dataset : Dataset → statistics : StatisticsResult, importance : FeatureImportance |
| `explore.hp_filter` | HP 趋势过滤 | dataset : Dataset → statistics : StatisticsResult |
| `explore.stationarity` | 平稳性检查（ADF） | dataset : Dataset → statistics : StatisticsResult |
| `explore.dtw` | DTW 距离 | dataset : Dataset → statistics : StatisticsResult |
| `explore.sbd` | SBD 相关 | dataset : Dataset → statistics : StatisticsResult |
| `explore.slope_cosine` | 斜率与余弦夹角 | dataset : Dataset → prediction : Prediction |

### 数据可视化 Data Visualization

| type | 名称 | 输入 → 输出 |
| --- | --- | --- |
| `visual.overview` | 数据概览 | dataset : Dataset → overview : Visualization |
| `visual.scatter` | 散点图 | dataset : Dataset → plot : PlotArtifact |
| `visual.line` | 折线图 | dataset : Dataset → plot : PlotArtifact |
| `visual.subplot` | 子图 | dataset : Dataset → plot : PlotArtifact |
| `visual.histogram` | 直方图 | dataset : Dataset → plot : PlotArtifact |
| `visual.compare` | 对比画图 | first / second : Dataset → plot : PlotArtifact |
| `visual.anomaly` | 异常点可视化 | dataset : Dataset, prediction（可选）→ plot : PlotArtifact |
| `visual.relationship` | 关系图 | dataset : Dataset → plot : PlotArtifact |

### 特征提取 Feature Extraction

| type | 名称 | 输入 → 输出 |
| --- | --- | --- |
| `feature.statistical` | 统计特征 | dataset : Dataset → features : FeatureDataset, labels : LabelVector |
| `feature.fitting` | 拟合特征 | dataset : Dataset → features : FeatureDataset, labels : LabelVector |
| `feature.rolling_statistics` | 滚动统计特征 | dataset : Dataset → features : FeatureDataset |
| `feature.temporal` | 差分与自相关特征 | dataset : Dataset → features : FeatureDataset |
| `feature.wavelet` | 小波特征 | dataset : Dataset → features : FeatureDataset |
| `feature.entropy` | 熵特征 | dataset : Dataset → features : FeatureDataset, labels : LabelVector |
| `feature.spectral` | 频域特征 | dataset : Dataset → features : FeatureDataset, labels : LabelVector |
| `feature.categorical` | 分类特征 | dataset : Dataset → features : FeatureDataset, encoder : FeatureTransformer |
| `feature.categorical_transform` | 分类特征变换 | dataset : Dataset, encoder : FeatureTransformer → features : FeatureDataset |
| `feature.select` | 选择已有特征 | dataset : Dataset → features : FeatureDataset |
| `feature.merge` | 合并特征 | left / right : FeatureDataset → features : FeatureDataset |
| `feature.imputation` | 特征缺失处理 | features : FeatureDataset → features : FeatureDataset |
| `feature.score_select` | 特征评分选择 | features : FeatureDataset, labels（可选）→ features : FeatureDataset, scores : FeatureImportance |
| `feature.pca` | 主成分分析 | features : FeatureDataset → features : FeatureDataset, variance : StatisticsResult |
窗口族（`feature.statistical` / `feature.fitting` / `feature.spectral` / `feature.entropy`）共享同一套窗口与预测参数：按行 `window_size`/`step`，或按时间 `window_span`/`step_span`（如 `7d`/`1d`）；`label_policy=horizon` 配合 `prediction_horizon`/`prediction_gap` 就能从"检测"切到"预测"。用法见 §4.4，参数细节见 §5。

### 算法验证 Algorithm Validation

| type | 名称 | 输入 → 输出 |
| --- | --- | --- |
| `validation.random_forest` | 随机森林 | features, labels → model / prediction / metrics / importance |
| `validation.svm` | 支持向量机 | features, labels → model / prediction / metrics |
| `validation.xgboost` | XGBoost | features, labels → model / prediction / metrics / importance |
| `validation.decision_tree` | 决策树 | features, labels → model / prediction / metrics / importance |
| `validation.reservoir_classifier` | 水库机分类 | features, labels → model / prediction / metrics |
| `validation.linear_regression` | 线性回归 | features, target → model / prediction / metrics / importance |
| `validation.ridge` | 岭回归 | features, target → model / prediction / metrics / importance |
| `validation.arma` | ARMA | dataset → model / prediction / metrics |
| `validation.knn_detector` | KNN 检测 | dataset → model / prediction / metrics |
| `validation.isolation_forest_detector` | 隔离森林检测 | dataset → model / prediction / metrics |
| `validation.dbscan_detector` | DBSCAN 检测 | dataset → model / prediction / metrics |
| `validation.pca_detector` | PCA 检测器 | dataset → model / prediction / metrics |
| `validation.min_cluster_detector` | Mincluster 探测器 | dataset → model / prediction / metrics |
| `validation.persistence_detector` | Persist 检测器 | dataset → model / prediction / metrics |
| `validation.level_shift_detector` | LevelShift 检测器 | dataset → model / prediction / metrics |
| `validation.volatility_shift_detector` | VolatilityShift 检测器 | dataset → model / prediction / metrics |
| `validation.seasonal_detector` | Seasonal 检测器 | dataset → model / prediction / metrics |
| `validation.autoregression_detector` | AutoRegression 检测器 | dataset → model / prediction / metrics |
| `validation.esd_detector` | GeneralizedESD 检测器 | dataset → model / prediction / metrics |
| `validation.nsigma_detector` | Nsigma 检测 | dataset → model / prediction / metrics |
| `validation.mean_drift_detector` | 均值漂移检测 | dataset → model / prediction / metrics |
| `validation.one_class_svm` | 单类 SVM 检测 | dataset → model / prediction / metrics |
| `validation.kmeans` | KMeans 聚类 | dataset → model / prediction / metrics |
| `validation.exponential_smoothing` | 指数平滑 | dataset → model / prediction / metrics |
| `validation.arima` | ARIMA（可选依赖） | dataset → model / prediction / metrics |
| `validation.grid_search` | 超参搜索 | features, labels → model / metrics / importance |
| `validation.compare` | 模型对比 | first / second / third : Metrics → comparison : StatisticsResult |

每个组件的完整参数表（类型、默认值、必填、取值范围）见 [docs/components.md](docs/components.md)。

分类特征组件执行 `fit + transform`，并把固定类别词表、频率/目标映射、输出列顺序和未知类别策略保存在 `encoder` 中。后续数据通过 `feature.categorical_transform` 只做变换；验证器也会把上游编码器嵌入模型对象，因此 pickle、Artifact 磁盘溢写和检查点恢复后，模型仍可接收带原始类别列的数据。`handle_unknown=ignore` 默认把新类别变为 one-hot 全零、ordinal 的 `-1`、频率的 `0` 或目标编码的训练全局均值；设为 `error` 可严格拒绝。

---

## 4. 典型方案模板

### 4.1 最小验证方案

```
data.input → visual.overview
          ↘ feature.statistical → validation.random_forest → validation.compare
```

### 4.2 工业时序方案（推荐起点）

```
data.input → data.filter → ┬─ feature.statistical ─┐
                           ├─ feature.fitting  ────┼→ feature.merge → feature.score_select →
                           └─ feature.spectral ────┘
                                                      ┬─ validation.random_forest ─┐
                                                      ├─ validation.svm ───────────┼→ validation.compare
                                                      └─ validation.xgboost ───────┘
```

三条特征分支必须设置相同的 `columns / group_column / label_column / time_column / window_size / step`，这样窗口索引与来源一致，`feature.merge` 才能合并；标签由窗口组件生成的 `labels` 端口接到每个模型，避免逐行标签与聚合特征错位。

### 4.3 探索分支

数据概览、统计、相关性、散点图、折线图是终端分支：接上就能看，不要把它们接到模型输入。示例把「标准化 → 折线图」放在探索分支，建模使用原始窗口特征，避免全量缩放造成的数据泄漏。

### 4.4 预测方案（用历史窗口预测未来故障）

检测回答"现在坏没坏"，预测回答"接下来会不会坏"。区别全在窗口与标签：窗口按**时间**切，标签取自窗口**之后**的视野。

```
data.input → feature.statistical(window_span="7d", step_span="1d",
                                 prediction_horizon="2d", prediction_gap="1h",
                                 label_policy="horizon")
                                    ├→ validation.random_forest(split_method="temporal")
                                    └→ visual.overview（看特征表长什么样）
```

```jsonc
{"component_type": "feature.statistical", "parameters": {
  "columns": ["…测点…"], "group_column": "instance", "asset_column": "asset",
  "label_column": "fault", "time_column": "time_s",
  "window_span": "7d", "step_span": "1d",   // 用最近 7 天，每天一个样本
  "prediction_horizon": "2d",               // 往后看 2 天
  "prediction_gap": "1h",                   // 先隔 1 小时，避免贴着故障起始
  "label_policy": "horizon",                // 标签来自未来视野
  "current_fault_policy": "drop",           // 已经坏了的窗口交给检测任务
  "normal_label": "0"}}
```

必须向用户交代的四件事：**窗口与视野**（`window_span`/`step_span`/`prediction_horizon`/`prediction_gap`）；**丢了多少窗口、为什么**（`attrs` 的 `horizon_dropped_current_fault` 与 `horizon_dropped_unknown_future`）；**正类比例**；**切分方式**（时间窗口通常重叠，用 `temporal` 或 `group`/`asset`，`stratified` 会被拒绝）。

实测参考（3W 真实数据：489,456 行 / 28 个实例，窗口 `180s`、步长 `60s`、视野 `1h`）：得到 **3370 个窗口、正类 26.9%**，丢弃 4380 个"自身已故障"与 331 个"视野超出数据"的窗口，特征提取 1.0 秒。注意**特征行数只由窗口与步长决定，与输入行数无关**。

边界：时间列按**秒**解释（数值列）或时间戳；流式输入不支持预测模式（按块看不到未来），要么关掉 `streaming`，要么插 `data.materialize`。

### 4.5 建模注意事项

| 主题 | 说明 |
| --- | --- |
| 标签对齐 | 必须使用窗口组件的 `labels` 输出；混标签窗口默认拒绝（`label_policy=strict`），可选 `last` 或 `mode` |
| 划分方式 | `split_method=stratified` 分层随机、`group` 按设备分组、`temporal` 按特征行顺序的时间划分 |
| 资产级留出 | `data.asset_key` 从实例名派生资产 → 窗口组件填 `asset_column` → 验证器用 `split_method=asset`，真正留出整口井/整台设备；`metrics.coverage` 报告未见资产数 |
| 重叠窗口 | 重叠窗口不能随机划分，必须用 `group` 或 `temporal` |
| 泄漏检查 | Runtime 会检查训练与测试窗口是否共享原始数据行，发现即报错 |
| 全量预处理 | 全量缩放/编码会带探索性警告并传递到指标；SVM 的标准化与概率校准只在训练集内拟合 |
| 频域前置条件 | 需要真实采样率；平台不重采样、不推断转速 |

---

## 5. 数据与参数约定

CSV 需要唯一列名、非空行，第一行为表头：

```csv
equipment,time,label,vibration,temperature,pressure
0,0,0,2.076,38.617,9.997
0,1,0,1.740,35.900,9.876
1,0,0,2.310,37.204,10.118
```

窗口类组件的参数：

| 参数 | 含义 |
| --- | --- |
| `columns` | 参与特征计算的数值列，留空默认全部数值列；标签列与分组列不能作为特征输入 |
| `group_column` | 设备/批次标识，每个分组内部独立切窗口 |
| `time_column` | 时间列，存在时按它排序后再切窗口（拟合特征也用它作自变量） |
| `label_column` | 生成与窗口对齐的标签 |
| `window_size` | 窗口长度，`0` 表示整组一段 |
| `step` | 滑动步长，`0` 表示不重叠 |
| `window_span` | **时间窗口**，如 `7d`/`12h`/`180s`；与 `window_size` 二选一，按时间切窗（采样不规则时行数可不固定） |
| `step_span` | 时间步长，如 `1d`；留空表示不重叠 |
| `prediction_horizon` | **预测视野**，如 `2d`：配合 `label_policy=horizon`，窗口结束之后这么久内出现故障就标 1 |
| `prediction_gap` | 预测间隔（禁入带），把视野整体推后，避免贴着故障起始的样本过易 |
| `current_fault_policy` | 窗口自身已故障时：`drop`（默认，属于检测任务）/`positive`/`negative`；丢弃数量会写进 `attrs` 与警告 |
| `normal_label` | 哪个标签值算正常（默认 `0`），其它取值都算故障 |
| `label_policy` | 混标签窗口的处理：`strict` 拒绝、`last` 取最后一个、`mode` 取众数；`horizon` 表示**预测**——标签取自窗口之后的未来视野 |
`window_size`/`step`（或 `window_span`/`step_span`）决定的是**特征行数**：每组大约"组内时长 ÷ 步长"行，尾部不足一个窗口的丢弃；与输入行数无关。48.9 万行原始数据配 `180s`/`60s` 得到 8081 行特征，步长改成 `180s` 只剩 2700 行。
| `sampling_rate` | 仅频域特征：原始样本采样率（Hz），必填，窗口至少 8 个样本 |

频域特征说明：使用 Hann 窗与相干增益归一化，`dominant_frequency` / `dominant_amplitude` / `spectral_rms` 对单音准确；`band_edges` 用 Nyquist 比例表示，`band_energy_ratio_i` 之和为 1；`harmonic_ratio` 统计 2–5 倍主频附近的能量占比。谱质心、谱展宽、谱熵受窗主瓣宽度影响，适合在同一流程内比较样本。

平窗口（保持值 / 量化值）：真实过程点位的恒值通道会让频谱失去意义。默认 `flat_policy=nan`——该窗口的频域特征记为 NaN，**行索引保持对齐**（否则 merge 与验证会错位），同时回传"多少窗口是平的"的警告；`skip` 丢弃这些窗口（只在没有其它分支需要合并时安全），`error` 恢复硬失败。跑特征前建议先用 `data.quality` 查看逐组常数列与平窗口比例。

---

## 6. Agent 使用（MCP）

### 6.1 启动服务

MCP bridge 只是转发到本地 HTTP 控制 API，所以**必须先启动服务**：

```powershell
.\.venv\Scripts\python.exe -m fault_platform serve --port 8765
```

### 6.2 配置 MCP 客户端

```json
{
  "mcpServers": {
    "fault-prediction": {
      "command": "D:/codespace/python/fault_pred/.venv/Scripts/python.exe",
      "args": ["-m", "fault_platform", "mcp", "--url", "http://127.0.0.1:8765"]
    }
  }
}
```

工程移动后要修改 `command` 的绝对路径；配置文件位置由客户端决定，本工程不改全局配置。也可以手动运行 bridge：`python -m fault_platform mcp --url http://127.0.0.1:8765`。

配好后建议先跑一次冒烟脚本：它会按配置里的命令真实拉起 bridge，只用 MCP 工具完成「发现组件 → 建图 → 校验 → 执行 → 取结果 → 导出 XML → 检查点」的闭环，并打印每一步的结果。

```powershell
.\.venv\Scripts\python.exe scripts\mcp_smoke.py --from-config
```

`--from-config` 直接读取 `~/.codex/config.toml` 的 `[mcp_servers.fault-prediction]`；去掉该参数则用当前解释器和 `--url` 启动，方便 CI 或其它客户端复用。

### 6.3 工具清单（39 个高层操作）

| 用途 | 工具 |
| --- | --- |
| 方案管理 | `create_pipeline`、`list_pipelines`、`get_pipeline`、`replace_pipeline`、`load_pipeline`、`save_pipeline`、`create_example` |
| 组件发现 | `list_components`、`search_components`、`get_component_schema` |
| 图编辑 | `add_component`、`remove_component`、`configure_component`、`connect_components`、`disconnect_components`、`validate_pipeline` |
| 执行 | `execute_pipeline`、`execute_node`、`execute_from_node`、`retry_node`、`cancel_pipeline`、`get_pipeline_status`、`get_node_result`、`get_pipeline_result`、`get_history` |
| 检查点与导出 | `save_checkpoint`、`load_checkpoint`、`list_checkpoints`、`get_pipeline_xml`、`export_python` |

大对象不经过 MCP：Agent 只用 `pipeline_id`、`workspace_id`、`node_id` 操作，读回的是有界预览（最多 100 行 / 50 列）和元数据，不返回完整训练矩阵或模型权重。

**给 Agent 的省 token 用法**（真实使用反馈后补充）：

| 做法 | 效果 |
| --- | --- |
| `add_components` / `connect_many` / `configure_components` 批量接口 | 9 个节点从 26 次调用降到 3–4 次 |
| `include_graph=false`（批量接口默认即为 false） | 每次编辑只回 `version + 节点/边数量 + added`，不再回吐整张图 |
| `get_node_result` 默认紧凑 | `train_indices`/`test_indices` 折叠为 `*_count`；确需原始索引时传 `include_indices=true` |
| `wait_for_pipeline(timeout_seconds=…)` | 取代 sleep + 轮询，终态直接返回 `timed_out` |
| `get_server_info` / `list_datasets` | 查 data_root、storage_root、缓存预算与可读文件，无需读进程命令行 |
| `delete_pipeline` | 清理失败的方案、工作区、落盘文件与检查点 |
| 工作区陈旧提示 | 传了非最新的 `workspace_id` 时返回警告，而不是静默给出旧状态 |

### 6.3a 多数据源：同一张图，跑多份同构数据

两份同构数据（例如每台设备各导出一份、或每批一份）有两条路进场，**都不需要重建方案**：

| 方式 | 怎么做 | 什么时候用 |
| --- | --- | --- |
| 入口合并（**持久**） | `data.input` 的 `paths` 参数：`path` 是第一个源，`paths` 按顺序追加，纵向拼成**一份** `Dataset` | 这批数据以后就一起用。下游组件完全不用改，它们只看到一份数据 |
| 运行期整组替换（**临时**） | `execute_pipeline(dataset_overrides={"source": ["a.csv", "b.csv"]})`：字符串=只读这一个文件，列表=只读这几个文件 | 「同一张图，换一批数据跑」。**是执行参数不是编辑**：图不变（可追溯、已有结果不失效），但外部文件指纹会跟着变，所以增量复用不会拿旧数据的结果冒充 |

几条刻意的规则：

- **列集合必须一致**：缺列/多列都直接报错并点名（`missing [...] extra [...]`）。补 NaN 或丢列会让"两台机器数据结构不同"一路漂到模型里，报告上看不出来。列**顺序**可以不同，按第一份的顺序对齐。
- **索引重排**：各文件索引都从 0 开始，直接拼会出现重复索引，所以合并后统一重排为 `0..N-1`（会写进警告）。
- **`source_column`**：设了它就给每行加一列"来自哪个文件"（相对路径），混批排查时很有用；默认不加，表结构保持原样。
- **指纹覆盖全部文件**：`source_id` 是各文件摘要按顺序再哈希，所以改任意一份、或换顺序都会让下游重算；**单源时返回值与旧版逐字一致**，已有方案的缓存不会失效。
- **流式不支持多源**：`streaming=true` 只描述一个文件，配上 `paths` 会直接拒绝，而不是悄悄只读第一个。
- 用了覆盖/合并都会写进警告（节点级 + 方案级 + 模型指标里的 `warnings`），所以报告里能看出"这次读的到底是哪几份"。

### 6.4 典型调用序列

> 用统计特征和随机森林搭一个故障预测方案

```
search_components(query="statistical")
get_component_schema(component_type="feature.statistical")
create_pipeline(name="设备故障预测")
add_component(...) → configure_component(...) → connect_components(...)
validate_pipeline(...)
execute_pipeline(...) → 轮询 get_pipeline_status → get_pipeline_result / get_node_result
get_pipeline_xml(...)
```

`execute_pipeline` 是异步的：返回 RUNNING 后要轮询 `get_pipeline_status`，`SUCCESS / FAILED / CANCELLED` 才是终态。注意区分两层成功：HTTP 200 表示控制请求成功，运行成败看 `status`。

### 6.5 Skill

[skills/fault-prediction/SKILL.md](skills/fault-prediction/SKILL.md) 告诉 Agent 如何按专业顺序搭方案：先探索数据，再决定是否过滤/删列/缩放/转换，然后选择特征与算法、建立并行对比、根据结果决定改哪里。它按阶段组织（recon → 数据准备 → 质量预检 → 窗口与标签 → 特征 → 验证 → 执行排错 → 读结果 → 持久化 → 汇报），每个阶段都给出「要做什么 / 怎么配 / 注意什么 / 何时可以进入下一步」，并把细节拆到四个参考文件：

| 文件 | 内容 |
| --- | --- |
| `SKILL.md` | 入口（385 行）：决策、硬约束、路由、阶段自检闸门、39 个工具的用途表与 5 类能力索引 |
| `references/recipes.md` | 可直接照抄的调用序列（常规分类、onset 数据、资产留出、无监督、超大文件、失败后重跑、三模型对比） |
| `references/stages.md` | 每个阶段的细节：参数表、实测数字、检查清单与「注意事项」（入口把它挪出来，只留决策与闸门） |
| `references/troubleshooting.md` | 报错原文 → 原因 → 修法，以及每条护栏为什么存在 |
| `references/components.md` | 88 个组件的用途、端口、关键参数与「什么时候不要用」 |

把它复制到 Agent 的技能目录，或让 Agent 直接读取（`docs/deploy.md` 的安装脚本会一并安装整个目录）。

skill 的正文（含四份参考文件）现在**全中文**，只有工具名、组件类型、参数名与平台报错原文保持英文——它们是接口标识符。第十四轮又补了三件事：Recon 阶段要求产出一份 3~6 行的**能力清单**（这次任务可能用得上的手段，而不是 88 个组件的目录）；每个问题多发的阶段末尾有一道 **阶段自检**（逐条自问，命中才动手，最多 6 条）；汇报契约里有「中间产物证据」一项，要求把概览挂在真正建模的特征分支上并把行列引用出来。另外两处运营性内容：§0.5 讲清「等待超时 ≠ 服务死了，绝不要因此重启服务」，§0.6 要求用用户的语言回答。这些约束由 `tests/test_skill_guide.py`（23 项）与 `tests/test_mcp_bridge.py`（5 项）守住，包括「闸门不许膨胀成组件清单」「入口不许再长回手册」和「每个工具都必须有描述」。

也可以用原始 HTTP：

```http
POST /api/control/create_pipeline
Content-Type: application/json

{"name":"设备故障方案"}
```

---

## 7. Python 使用

### 7.1 只用数值 API（`fault_core`）

`fault_core` 不依赖平台，可以当普通数据分析库用：

```python
import pandas as pd
from fault_core import features, models

frame = pd.read_csv("examples/data/synthetic_equipment.csv")
extracted = features.extract_features(
    frame, ["vibration", "temperature"],
    group_column="equipment", label_column="label", window_size=16,
)
metrics = models.validate_model(
    extracted["features"], extracted["labels"], "random_forest", split_method="group",
)["metrics"]
print(metrics["accuracy"], metrics["test_count"])
```

模块划分：`fault_core.data`（过滤、行/列操作）、`fault_core.preprocessing`（缩放、转换）、`fault_core.exploration`、`fault_core.visualization`、`fault_core.features`（统计/拟合/分类/频域）、`fault_core.selection`、`fault_core.reduction`、`fault_core.models`。

### 7.2 用 Graph API 执行方案

不经过网页、HTTP 和 MCP，直接建图并运行：

```python
from pathlib import Path

from fault_platform.graph import ComponentGraph
from fault_platform.registry import default_registry
from fault_platform.runtime import ExecutionContext, ExecutionEngine
from fault_platform.workspace import FaultWorkspace
from fault_platform.xml_io import XMLSerializer

graph = ComponentGraph(default_registry(), "我的方案")
graph.add_node("data.input", "source", {"path": "synthetic_equipment.csv"}, {"x": 60, "y": 120})
graph.add_node("feature.statistical", "features", {
    "columns": ["vibration", "temperature"], "group_column": "equipment",
    "label_column": "label", "window_size": 16,
}, {"x": 360, "y": 120})
graph.add_node("validation.random_forest", "model",
               {"n_estimators": 50, "split_method": "group"}, {"x": 660, "y": 120})

graph.connect("source", "dataset", "features", "dataset")
graph.connect("features", "features", "model", "features")
graph.connect("features", "labels", "model", "labels")

workspace = FaultWorkspace(graph.pipeline_id)
ExecutionEngine().execute(graph, ExecutionContext(workspace, Path("examples/data")))
print(workspace.status)                                  # SUCCESS
print(workspace.get_output("model", "metrics")["accuracy"])
XMLSerializer().save(graph, Path("examples/my_pipeline.xml"))
```

完整可运行脚本见 [examples/python_api.py](examples/python_api.py)。

---

## 8. 命令行参考

| 命令 | 作用 |
| --- | --- |
| `python -m fault_platform serve --port 8765 --data-root examples/data --storage-root .fault-platform/pipelines` | 启动网页与 HTTP 控制 API |
| `python -m fault_platform demo --output examples --xgboost` | 生成合成数据、示例 XML 并执行，写出 `examples/demo_result.json` |
| `python -m fault_platform run examples/example_pipeline.xml --data-root examples/data` | 无界面执行已有 XML，打印 JSON 摘要，失败退出码 1 |
| `python -m fault_platform mcp --url http://127.0.0.1:8765` | 启动 MCP stdio bridge |

`demo` 只在数据集不存在时生成 CSV，但每次都会重写 `example_pipeline.xml` 与 `demo_result.json`。

服务默认不限制运行结果的内存占用；处理较大数据时用 `--artifact-cache-mb 512` 之类的预算限制缓存：超出后按最近最少使用把未被检查点引用的输出**落盘**到 `--artifact-spill-dir`（默认 `.fault-platform/artifact-spill`，按会话分目录、退出时清理），读回时自动加载；若显式禁用落盘目录，则退化为丢弃并把相关节点标记为待重算。当前缓存与落盘用量可从 `/api/health` 与 `get_pipeline_result` 的 `artifact_cache` 字段读取（含 `bytes / spilled / disk_bytes / spills / loads / evictions`）。

---

## 9. 保存、检查点与数据生命周期

- **XML 保存方案结构**：节点、组件类型、参数、端口、连接、画布位置与 UI 信息，**不保存**大型 DataFrame、模型权重或预测结果。
- **Workspace 保存在内存**：节点输出、指标、模型、执行历史都在服务进程内；「检查点」可保存 Graph + Workspace 的独立快照并恢复。
- **内存模型**：节点输出在 Workspace 中以引用保存（存与预览都不复制）；只有组件真正消费时才会复制一份输入，保证分支互不影响。检查点按引用共享输出并加 pin，不再复制整份数据；`--artifact-cache-mb` 给内存缓存设上限，超限的未 pin 输出落盘（结果仍可读），没有落盘目录时才丢弃并把节点标记为待重算。
- **重启即清空**：服务退出后运行数据、模型、检查点都不保留；只有 XML、上传的 CSV 留在磁盘上，落盘的缓存文件也在退出时清理。
- **运行中禁止编辑**同一方案；「停止」在组件之间生效，正在训练的模型会先跑完；完整图替换带版本冲突检查，避免覆盖并发修改。

---

## 10. 扩展：新增一个组件

新增组件只需要实现并注册，Graph、Runtime、XML、网页与 MCP 都不用改代码：

```python
from fault_platform.components.base import (
    BaseComponent, ComponentMetadata, ComponentResult,
    DataType, InputPort, OutputPort, ParameterDefinition,
)


class MyFeatureComponent(BaseComponent):
    metadata = ComponentMetadata("feature.my", "我的特征", "feature", "示例特征组件")
    input_ports = (InputPort("dataset", DataType.DATASET),)
    output_ports = (OutputPort("features", DataType.FEATURE_DATASET),)
    parameter_schema = (
        ParameterDefinition("columns", "column_list", None, required=True),
        ParameterDefinition("threshold", "float", 0.5, min=0, max=1),
    )

    def execute(self, inputs, context):
        return ComponentResult({"features": my_transform(inputs["dataset"], **self.parameters)})
```

把它加入 `fault_platform/components/builtin.py` 的 `BUILTIN_COMPONENTS` 即可：组件库、参数表单、端口校验、XML 读写和 MCP `list_components` 会自动出现该组件。需要读取外部文件的组件额外覆盖 `preflight` 与 `external_fingerprint`。

---

## 11. 项目结构

```
src/fault_core/           纯数值库（数据、预处理、探索、可视化、特征、选择、降维、模型）
src/fault_platform/
  components/             BaseComponent、端口/参数定义、内置组件
  registry.py             组件定义统一来源
  graph.py                ComponentGraph、节点、连接、循环检测、拓扑排序
  runtime.py              ExecutionContext、ExecutionEngine、增量失效、失败传播
  workspace.py            FaultWorkspace、WorkspaceManager、历史、检查点、ArtifactStore
  xml_io/                 XML 序列化/反序列化与 XSD
  service.py              网页与 MCP 共用的控制 API
  api.py / cli.py         本地 HTTP 服务与命令行
  mcp_server.py           MCP stdio bridge
  web/                    可视化编辑器（原生 JS，无构建步骤）
skills/fault-prediction/  Agent 技能
examples/                 合成数据、示例 XML、Python API 示例
docs/                     架构、组件参考、MCP、验证记录
tests/                    pytest 与 DOM 集成测试
scripts/                  组件目录导出、浏览器验收脚本
```

源码注释约定：模块与函数的 docstring 保留英文摘要（与既有代码风格一致），
并补充中文说明——写清"这个模块负责什么、为什么这么设计、有哪些坑"，
关键实现处再用中文行内注释解释数值细节与护栏原因。`fault_core` 不依赖 `fault_platform`，
这条依赖方向不要打破：它保证数值算法可以脱离平台单独测试与复用。

---

## 12. 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q                 # 228 项通过（1 项按可选依赖跳过）
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe -m pip check
node --check src/fault_platform/web/app.js
npm ci; npm test                                        # 4 项 DOM 集成测试
npm run browser-check                                   # Chrome headless 真实浏览器验收
.\.venv\Scripts\python.exe scripts\mcp_smoke.py --from-config   # MCP 闭环（先启动服务）
.\.venv\Scripts\python.exe scripts\memory_bench.py --rows 1000000   # 大文件内存基准
.\.venv\Scripts\python.exe scripts\verify_deploy.py --from-config   # 部署验收（14 项，含 MCP 端到端）
.\.venv\Scripts\python.exe scripts\export_release.py --build        # 打可交付的部署包
```

`npm run browser-check` 会启动临时服务与 Chrome，通过 DevTools 协议验证 16 组检查：组件库渲染、布局尺寸、**组件库折叠与搜索展开**、**分隔条拖拽改变面板宽高、方向跟手、刷新后保持**、**窄窗口下三栏钳制不溢出**、节点与连线绘制、**连线是圆角正交折线（端点误差 ≤1.5px、除圆角外无斜向行程）**、真实指针拖拽持久化、缩放与适应画布、组件放置与参数表单、**Agent 改动实时出现在页面**、**Agent 触发执行时页面显示进度**、方案执行与结果面板、历史与 XML 面板；截图写到 `.fault-platform/screenshots`（`01c`/`01d` 是组件库与分隔条的 3x 放大图，`02b-edges-zoom.png` 是连线折角的 2x 放大图、`04-feature-overview.png` 是特征表概览，供人工目视评审）。

依赖快照见 `requirements-win-py311.lock`（Windows / Python 3.11 验证环境），`dist/` 内含可安装 wheel。详细结果见 [docs/validation.md](docs/validation.md)。

---

## 13. 当前边界与后续计划

首版面向本机单用户开发，已验证：CSV / Parquet 数据源（含列裁剪、行数上限、谓词下推）、**分块流式特征提取**、**按时间切窗与未来视野标签（故障预测）**、图形化 DAG 编辑、以引用为主的 Workspace 与可落盘的缓存、检查点、XML 往返、MCP 控制、分类模型验证。

尚未包含：

- 跨进程持久化（结果可落盘但元数据仍在内存，重启即丢失）
- 流式的**全图**处理：只有窗口特征提取与数据概览支持分块，模型的训练样本仍需一次性驻留（特征表本身已经小得多）；单组行数极大（例如单个设备上百万行）时该组仍需整体缓冲
- 增量/流式摄取、数据库或时序库数据源（当前是文件型 CSV / Parquet）
- RUL、生存分析与回归任务（当前是分类验证）
- 远程多用户、鉴权、分布式队列与生产部署
- 预测已支持"用历史时间窗口预测未来视野内是否故障"（`window_span` + `label_policy=horizon`，见 §4.4）；**还没有**的是按故障类型分别设视野（例如"2 天内会不会发生水合物"），以及把"视野内没有任何故障样本"这类退化情形做成显式警告（现在由 agent 自己看正类比例）

规划中的扩展：按行数上限的分组缓冲（把超大单组也切成流式）、小波 / STFT / 变点特征、交叉验证与超参数搜索、参数面板基于上游列元数据的自动补全。

---

## 14. 文档

- [总体架构](docs/architecture.md)：对象职责、边界、数据流与实现约定
- [项目设计文档](docs/design.md)：完整设计（对象模型、执行语义、端口/参数系统、XML、UI、MCP、领域约定、ADR）
- [组件与参数参考](docs/components.md)：88 个组件的端口与参数表
- [MCP 接入](docs/mcp.md)：bridge 配置与调用约定
- [Agent Skill](skills/fault-prediction/SKILL.md)：Agent 搭方案的专业流程
- [验证记录](docs/validation.md)：测试、浏览器验收与首版边界
- [部署到别人的电脑](docs/deploy.md)：打包、一键安装与四层验收
- [examples/python_api.py](examples/python_api.py)：不依赖网页与 MCP 的运行示例
