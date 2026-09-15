# 实施与验证记录

环境：Windows、Python 3.11.7、Node.js 24.15.0。验证日期：初版 2026-09-14，最后一轮（第十六轮）2026-09-15。

## 结果

| 检查 | 结果 |
| --- | --- |
| Python / pytest | 161 项通过（1 项按可选依赖跳过） |
| 前端 DOM 集成测试 | 8 项通过，连接真实临时 HTTP 服务 |
| 真实浏览器验收 | Google Chrome headless + DevTools 协议，16 组检查通过（含实时同步、三块分隔条方向、连线正交性与"特征分支挂概览可检验"，见下） |
| 实时同步（SSE） | Agent 改动 74 ms 内出现在打开的页面；Agent 触发执行时页面看到 RUNNING→SUCCESS 与节点耗时（见下） |
| Random Forest / SVM / XGBoost 示例 | 全部执行成功，使用同一批测试设备 |
| MCP | 真实 stdio 初始化和工具调用；HTTP 能读到 MCP 创建的节点 |
| MCP 闭环冒烟 | `scripts/mcp_smoke.py --from-config` 按客户端配置启动 bridge，38 个工具完成建图、校验、执行、结果、XML 与检查点，见下 |
| XML | XSD、Registry 语义验证、Graph → XML → Graph 等价 |
| Ruff | 静态检查通过 |
| JavaScript | node --check 通过 |
| Agent Skill | `tests/test_skill_guide.py` 23 项 + `tests/test_mcp_bridge.py` 5 项通过（工具描述、组件引用、闸门与入口不膨胀、参考可达、等待类超时与错误分类），skill-creator quick_validate 通过 |
| Python wheel 构建 | dist/fault_prediction_platform-0.1.0-py3-none-any.whl |
| 依赖一致性 | pip check 无冲突 |

Python 测试覆盖每个内置组件，以及参数/端口错误、环、拓扑顺序、标签与窗口来源对齐、原始数据文件变更失效、部分执行、失败传播与重试、检查点深复制、输入路径、请求类型、并发编辑拒绝、时间窗口去重叠和有界预览。

新增批次另外验证：64 Hz 单音的主频、峰值幅值（1.0）与谱 RMS（1/√2）恢复，含谐波信号的谐波能量比与谱质心分离，三段频带比之和为 1，采样率必填与频带边界/窗口长度/常量信号的拒绝；方差、相关性、互信息和模型重要性的选择结果与来源保留；主成分投影保持索引与窗口来源并能与统计特征合并；一条同时包含统计特征、频域特征、合并、特征评分和随机森林的 Graph 端到端成功执行。

DOM 测试覆盖组件库、参数表单提交、连线、复制、删除、撤销/重做、示例运行、模型指标/曲线渲染，以及不可信节点名称转义。

测试过程中出现两条第三方弃用提示：Starlette 的 httpx 测试客户端适配和 AnyIO BlockingPortal 别名。测试均通过；并非平台功能错误。

## 浏览器验收

`npm run browser-check` 会启动临时服务与 Chrome headless，通过 DevTools 协议执行验收，并把截图写入 `.fault-platform/screenshots`。本次结果：

| 检查 | 实测 |
| --- | --- |
| 组件库渲染 | 26 个组件、5 个分类，含 feature.spectral、feature.score_select、feature.pca |
| 布局尺寸（CSS 像素） | 组件库 235×530、画布 1144×471、检查器 277×262 |
| 示例图渲染 | 11 个节点、14 条贝塞尔连线，节点 218×115 画布单位，最短路径 143 字符 |
| 真实指针拖拽 | 拖动 overview 264×170 画布单位，服务端保存位置 (864.1, 199.8) 与画布一致 |
| 缩放与适应 | 53% → 64% → 适应 62% |
| 组件放置与参数表单 | 从组件库放入 feature.spectral，检查器渲染 11 个参数字段并标注 sampling_rate 必填；删除后服务端图恢复 11 个节点 |
| 方案执行与结果 | 浏览器内 status=SUCCESS；随机森林指标 100%×5；折线图 233 个点；日志与 XML 页签渲染 10098 字符 XML |
| Agent 改动实时同步 | 通过 HTTP（与 MCP 同一条控制通道）新增 `feature.spectral` 节点，页面在 **74 ms** 内自行出现该节点；远端删除后页面同步移除，全程无页面操作 |
| Agent 触发执行的进度 | 由服务端触发执行，页面状态芯片进入 RUNNING、节点徽章由 PENDING 变为 SUCCESS，并渲染耗时与缓存标注（如 `SUCCESS · 10 ms`） |

真实服务上的事件流时序（`GET /api/events`，另一次独立验证）：

| 相对时间 | 事件 |
| --- | --- |
| +574 ms | `graph_changed create_pipeline`（连接后补发历史） |
| +621 ms | `graph_changed add_component`（编辑后约 47 ms 到达） |
| +924 ms | `run_started` |
| +925 / +926 ms | `pipeline_status VALIDATING / RUNNING` |
| +926 / +936 ms | `node_status source RUNNING / SUCCESS` + `history` |
| +936 ms | `pipeline_status SUCCESS` |

该脚本覆盖真实引擎下的渲染、命中测试、拖拽、缩放、执行与结果查看。本次会话无法使用 Codex 浏览器扩展（认证方式为 API key），因此没有人工像素评审；截图文件可供查看，窄屏布局和下载弹窗仍未自动校验。

## MCP 闭环

本机已在 `~/.codex/config.toml` 注册 `[mcp_servers.fault-prediction]`（原文件已备份为 `config.toml.bak-<时间戳>`），并用 `scripts/mcp_smoke.py --from-config` 按该配置原样拉起 bridge 验证：

| 环节 | 实测 |
| --- | --- |
| 工具发现 | 29 个工具 | 
| 组件检索 | category=feature 返回 8 个组件；`feature.spectral` 的 sampling_rate 类型 float 且必填 |
| 建图 | 5 个节点、6 条连线；`Dataset → FeatureDataset` 被拒绝并返回 `Incompatible port types` |
| 执行 | status=SUCCESS，5 条执行历史 |
| 结果 | 随机森林 accuracy=1.0、test_count=92、首要特征 temperature__rms（合成数据） |
| 频域节点 | 输出 2 个特征列（主频、谱 RMS） |
| XML 与检查点 | XML 5392 字符；检查点保存并恢复，completed_nodes 覆盖全部 5 个节点 |

环境限制：本机 npm 版 `codex` CLI（0.130.0）读取全局配置时，会因 `cc-switch-model-catalog.json` 中 `model_reasoning_effort = "max"` 不被支持而报 `unknown variant max`，导致 `codex mcp list` / `codex mcp add` 不可用。这属于本机 Codex 配置问题，与本工程无关；本次改用直接编辑 `config.toml` 并用 MCP 客户端实测的方式完成注册与验证。

## 内存模型（P0 优化前后实测）

用 153 MB 载荷（2,000,000 行 × 10 列）与一个 3 节点图（316 MB CSV：数据输入 → 过滤 → 标准化／概览）实测工作集峰值：

| 操作 | 优化前 | 优化后 |
| --- | --- | --- |
| `store.put()`（每个节点产出） | +153 MB | **0**（按引用保存） |
| 预览 20 行（`get_node_result`） | 触发一次整表拷贝，峰值 844 MB | **0**（`peek` + 预览行算缺失率） |
| `store_outputs` | +153 MB | **0** |
| `snapshot()`（检查点） | +153 MB | **0**（只存元数据与引用） |
| `store.get(copy=True)`（消费点） | +153 MB | +153 MB（保留隔离语义） |
| 端到端 3 节点图 | 峰值 **1616 MB（10.5×载荷）**，5.8 s | 峰值 **1140 MB（7.4×载荷）**，5.3 s |

结论：P0 去掉了"存一份、预览一份、检查点再一份"这三类纯浪费的拷贝，并把缓存从"永不回收"改为可设预算（`--artifact-cache-mb`，LRU + pin）。剩余倍数来自组件内部的必要拷贝（过滤/标准化会生成新表）与消费点的隔离副本；把它降到常数级需要 P1 的流式输入与磁盘 ArtifactStore。缓存预算只在两次运行之间生效，DAG 执行期间暂停淘汰，因此配置过小的预算不会让运行失败，只会让结果在运行后释放并把状态降级为 `READY`、下次重算。

## 大文件与落盘（P1 实测）

同一套基准（`scripts/memory_bench.py`，1,000,000 行 × 22 列，CSV 151 MB / 内存中 92 MB，缓存预算 32 MB，产出落盘目录）：

| 模式 | 峰值 RSS | 读入行数 | 落盘 | 运行后 RAM | 耗时 |
| --- | --- | --- | --- | --- | --- |
| `full`（全量读） | **810 MB** | 1,000,000 | 1 个 artifact → 168 MB | **4 MB** | 18.7 s |
| `bounded`（`columns` 投影 + `max_rows=200000`） | **218 MB** | 200,000 | 0 | 8 MB | 3.5 s |

解读：

- 落盘让"运行后保留的结果"不再占用内存：168 MB 在磁盘、RAM 剩 4 MB，而 `evictions=0`、模型指标仍能从磁盘读回（说明可用性没有被牺牲）。
- 受限输入把大文件的探索与建模压到可接受范围：峰值 810 MB → 218 MB（后者已接近 Python + pandas + scikit-learn 的空载底噪），耗时 18.7 s → 3.5 s。
- `full` 的 8.8× 峰值说明**单次运行的工作集仍随输入线性增长**——裁剪能规避，但不能消除；要真正跑 2 GB 级输入需要分块流式特征提取（P2），这也是当前 README 里明确列出的边界。

新增测试：`tests/test_artifact_cache.py`（落盘优先于淘汰、pin 不落盘、落盘结果可读且删除/清理回收文件）与 `tests/test_bounded_input.py`（CSV 投影与限行、无限读无警告、Parquet 投影+谓词+限行、扩展名白名单、缺 pyarrow 时的安装提示）。

## 分块流式特征提取（P2 实测）

`scripts/memory_bench.py`，同一图（数据输入 → 统计特征 → 随机森林），全量读 vs 流式读（`chunk_rows=100000`）：

| 输入规模 | CSV | 内存中 | 全量读峰值 | 流式峰值 | 耗时（全量 / 流式） | 准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| 1,000,000 行 | 154 MB | 99 MB | **830 MB** | **235 MB** | 20.3 s / **8.5 s** | 完全一致 |
| 2,000,000 行 | 308 MB | 198 MB | **1486 MB** | **262 MB** | 41.3 s / **17.5 s** | 完全一致 |

输入再放大时流式侧仍然平缓（同一管线，仅特征提取+模型，另一次测量）：

| 输入规模 | CSV | 流式峰值（提取） | 流式峰值（含模型） | 特征表行数 |
| --- | --- | --- | --- | --- |
| 1,000,000 行 | 154 MB | 237 MB | 235 MB | 15,625 |
| 2,000,000 行 | 308 MB | 261 MB | 263 MB | 31,250 |
| 4,000,000 行 | 617 MB | 307 MB | 335 MB | 62,500 |

要点：4,000,000 行（617 MB）的流式峰值 335 MB，**低于文件本身**；全量读在同规模下约为其 4 倍以上。流式的内存由输出特征表与窗口数决定，不再由输入行数决定。窗口键、特征值、标签与来源在批量/流式两条路径上逐位一致（`tests/test_streaming.py` 用 `assert_allclose(atol=1e-12)` 与 `provenance_matches` 校验）。

新增测试：`tests/test_streaming.py`（分块后的全局行号、三种窗口特征与批量逐位一致、非连续分组被拒绝、流式概览与批量统计一致、整图流式 vs 物化结果一致、不支持流式的组件给出明确错误、`data.materialize` 恢复全局操作）。

## 部署到另一台机器（实测）

`scripts/export_release.py` 产出的部署包在一个模拟的“干净机器”上跑通（全新的 `CODEX_HOME` 与安装目录，且 `config.toml` 里预置了一个第三方 `mcp_servers.node_repl` 条目）：

| 步骤 | 结果 |
| --- | --- |
| 解压部署包 → `deploy.ps1` | 建 venv、装 wheel（`[mcp,parquet]`）、拷贝脚本、安装 skill、写 MCP 配置、写 `start.ps1`，全部完成 |
| `config.toml` 安全性 | 原有 `model`、`[mcp_servers]`、`node_repl` 逐字保留；新增 `fault-prediction`；写入前生成 `config.toml.bak-<时间戳>`；顺带规范化了文件里原有的 UTF-8 BOM |
| `verify_deploy.py --from-config` | **14/14 通过**：环境、可选项、skill 形态、MCP 配置与解释器、服务与首页/静态资源/SSE、MCP 端到端（29 工具、status=SUCCESS、accuracy=1.0） |

过程中修掉两个真实缺陷：`re.sub` 会把替换文本里的反斜杠当转义（Windows 路径直接报 `bad escape \p`），以及带 BOM 的 `config.toml` 会让 `tomllib` 拒绝解析（Windows 编辑器常见）。两者都补了回归测试（`tests/test_deploy_tools.py`，10 项）。

## 第三轮：真实使用反馈的改进

两轮真实数据使用（合成 + 3W 实例数据）暴露的问题与对应改动：

| 反馈 | 改动 | 证据 |
| --- | --- | --- |
| 平窗口（`T-TPT`、`P-PDG` 这类保持/量化通道）让 `feature.spectral` 直接抛异常、整图失败 | 新增 `flat_policy=nan/skip/error` 与 `flat_threshold`，默认 NaN 且**保持行对齐**，回传每通道平窗口计数警告 | `tests/test_feature_extension.py::test_spectral_parameter_validation` 覆盖三种策略 |
| 恒零/常量列在 `visual.overview` 里看不出来（missing_rate=0） | 新增 `data.quality` 预检组件：逐组常数列、全 NaN/全零列、平窗口比例、重复行、混标签窗口、标签变化次数，并把发现写进 warnings | 实测合成样本一次报出"20 组含常数列、T-TPT 100% 平窗口、1 次标签变化" |
| 变更类调用每次回吐整张图、没有批量接口 | `include_graph` 参数（批量默认 false）+ `add_components` / `connect_many` / `configure_components` | `tests/test_agent_ergonomics.py`：3 个节点 1 次调用、3 条连线 1 次调用，响应不含 graph |
| `get_node_result` 被 `train_indices`/`test_indices` 淹没 | 长数组折叠为 `*_count` + 5 条预览，`include_indices=true` 才展开 | 同上：紧凑响应里 `train_indices_count == train_count`，accuracy/混淆矩阵仍完整 |
| 没有删除、没有运行期配置、没有等待语义、旧 workspace 静默返回 | 新增 `delete_pipeline` / `get_server_info` / `list_datasets` / `wait_for_pipeline`，非最新 workspace 返回警告 | 同上：删除后图与工作区一并回收；`wait_for_pipeline` 阻塞到终态并带 `timed_out`；陈旧 workspace 触发警告 |
| skill 缺"工作空间与数据准备"、格式说窄、score_select 自相矛盾、label_policy 后果写轻、无质量预检清单、未区分资产/实例划分、未说何时用 CLI | 重写 `skills/fault-prediction/SKILL.md`：新增 §1 工作空间与数据准备（`get_server_info`/`list_datasets`/格式转换表）、§2 质量预检、§3 标签策略表（onset 用 mode）、§4 明确 selection/PCA 为探索性、§5 资产 vs 实例划分、§0 token 纪律、§8 何时改用 CLI | skill-creator `quick_validate` 通过；部署验收里"skill installed and well formed"仍通过 |

新增测试文件：`tests/test_agent_ergonomics.py`（7 项）与质量/平窗口相关断言；控制操作从 29 增至 36，组件从 27 增至 28。

## 第四轮：资产级留出与真实 3W 复算

延续 `test_3w` 的三条平台限制与"下一步建议"，本轮改动与**在同一份真实数据上的复算结果**：

| 反馈 | 改动 |
| --- | --- |
| 窗口 ID（`g3_w1080`）无法回溯到井/实例，只能自己拼映射 | 窗口组件新增 `asset_column`，资产随特征写入 `attrs["assets"]`（批量、频谱、流式、熵特征四条路径一致）；`feature.merge` 与验证器把 `assets` 纳入 provenance 校验 |
| group 划分按实例、不等于按井留出 | 新增 `split_method=asset`（真正的留一井），并要求上游提供 `asset_column`；新增 `data.asset_key` 从实例名派生资产 |
| `/api/data` 不列 Parquet，上传只收 CSV | 列表与上传均支持 `.csv/.parquet/.pq`，上传预览按格式解析 |
| 类别不平衡任务需要成本敏感指标 | 指标新增 `balanced_accuracy`、`average_precision`（PR-AUC）、`per_class_recall`、`train/test_class_counts`（原始类别名）、`miss_rate`（可用 `positive_class` 指定故障类）与 `coverage`（实例/资产、未见资产数） |
| 平窗口 NaN 无法进入模型 | 新增 `feature.imputation`（mean/median/zero/drop_columns）；模型遇到 NaN 时报错会点名具体列并提示插入该组件 |
| 平窗口判定漏检"只在窗首尾变化"的窗口 | `feature.spectral` 的平窗口判定改为**加窗后能量**判定：Hann 窗在两端为 0，若窗口的起伏只出现在首尾采样点，加窗后能量恒为 0——此类窗口现在按 `flat_policy` 处理（默认 NaN），不再抛 `non-zero variance` |

**真实 3W 数据复算**（`test_3w/data/3w_events.parquet`，489,456 行、28 个实例、10 口井；窗口 180 s / 步长 60 s，共 8,091 个窗口；统计 33 + 拟合 18 + 频域 21 = 72 个特征；随机森林 300 棵，`label_policy=mode`）：

| 划分 | Accuracy | Balanced | ROC-AUC | PR-AUC | 漏报率 | 正常类召回 | 测试资产未见 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `group`（按实例，原设置） | 0.6379 | 0.6210 | 0.7159 | 0.8043 | 0.2306 | 0.4726 | **0 / 6**（测试井全部在训练集出现过） |
| `asset`（留一井，新增） | 0.6062 | 0.5682 | **0.5275** | 0.6108 | 0.2700 | 0.4064 | **3 / 3** |

解读：

- 同一份数据、同一个模型，**按实例划分会把"同井不同事件"算进泛化能力**：AUC 0.716 里有相当一部分是"记住了这口井"。严格留一井后 AUC 掉到 0.528，接近随机——这正是报告里怀疑的问题，现在由平台直接给出并报告了未见资产数。
- 频域分支这次跑通了：13.1% 的频域取值为 NaN（P-TPT 19.5% 的窗口无可用频谱，与 3W 报告独立统计的 19.4% 吻合），经 `feature.imputation` 后进入模型；各通道的平窗口数由组件随运行返回。
- 资产级划分下最重要的特征从 `T-TPT` 统计量变为 `P-TPT__range/std/iqr`，说明井间差异主要体现在压力的波动幅度上。

新增测试：`tests/test_asset_split.py`（7 项：资产派生、四条路径携带资产、资产划分 vs 实例划分、缺少资产列/单一资产的拒绝、特征缺失填充与"整列全 NaN"处理、指标语义与原类别名、`/api/data` 与上传的 Parquet 支持）。

## 第五轮：组件规模（对应需求文档三十九～四十一）

需求要求 Registry 支持分类/子分类/标签/搜索关键词/版本/兼容性，`list_components` 支持多维过滤，并为"组件检索"预留意图检索。逐条核对结果：

| 需求 | 状态 | 证据 |
| --- | --- | --- |
| Category / Subcategory / Tags / Search Keywords / Version / Compatibility | ✅ 已实现 | `ComponentMetadata` 六个字段齐全；56 个组件的 subcategory 覆盖"统计/频域/时域/拟合/分类/表征学习/选择/组合/非线性"等 |
| Registry 按上述维度查询 | ✅ 已实现 | `list(category, subcategory, tags, query, input_type, output_type, version, compatibility, limit, offset)` + `count()` + `facets()` |
| `list_components` 支持 category/query/tags/input_type/output_type/limit | ✅ 已实现 | 服务层签名与 MCP 工具一致，另返回 `total/returned/offset/limit/has_more`；`include_schema=false` 用于只浏览不取 Schema |
| Visualizer：分类筛选 / 搜索 / 收藏 / 最近使用 | ✅ 已实现 | 前端 `#category-filter`、搜索框、`libraryView`（全部/收藏/最近）、localStorage 持久化收藏与最近 12 个，并按子分类分组显示 |
| 组件检索（Component Retrieval） | ✅ 已实现（词法排序 + 端口兼容） | `registry.retrieve(intent, category, tags, input_type, output_type, source_component_type, target_component_type)`：字段加权打分（名称 18、类型 15、关键词 14、标签 12、子分类 7、分类 5、描述 4）、中文 bigram 匹配、端口兼容 +12 并过滤不兼容项，返回 `score` 与 `match_reasons`；MCP 工具 `retrieve_components` |
| Embedding 检索 | ⬜ 未实现 | 需求文档本身写作"未来可以增加"，当前为词法检索 |

本轮补掉的三处缺口：

1. `facets()` 原本是**死代码**（实现了但没有任何入口）。新增 `get_component_facets` 操作（返回 categories / subcategories / tags / versions / compatibility 与总数、平台版本），MCP 与 HTTP 都可调用。
2. `compatibility` 原本只作为元数据与过滤条件，**从未被校验**。新增 `fault_platform/version.py`（版本比较 + 区间解析），执行前校验每个组件的兼容区间，不满足时报错并给出人名话的原因（`requires fault-platform>=9.0 but this platform is 0.1.0`）；同时把散落的 "0.1.0" 字面量统一为 `PLATFORM_VERSION`。
3. skill 未教 Agent 使用 `retrieve_components` / `get_component_facets`，现在补上（含"用 source/target_component_type 找可插入的组件"这一用法）。

实测（56 个组件）：`retrieve("统计特征")` → `feature.statistical`（71 分）；`retrieve("extract frequency content")` → `feature.spectral`；`retrieve("留一井 评估")` → `data.asset_key`；`retrieve("", category="feature", source_component_type="data.input", target_component_type="feature.merge")` → 只返回能"吃 Dataset、吐 FeatureDataset"的特征组件。新增测试 `tests/test_catalogue_scale.py`（4 项）覆盖 facets 一致性、过滤与分页、检索排序与端口兼容、版本区间校验。

## 第四轮：可持久化类别编码（2026-09-15）

`feature.categorical` 现在输出训练特征和 `FeatureTransformer`。编码器保存训练类别、固定输出列、频率/计数/目标映射及 `handle_unknown` 策略；`feature.categorical_transform` 对新数据只做 transform。合并后的特征会保留编码器来源，验证器把它嵌入 `TrainedClassifier`，因此模型经过 pickle、Artifact 磁盘溢写或检查点恢复后，可以直接接收原始类别列。

未知类别的默认策略为 `ignore`：one-hot 为全零、ordinal 为 `-1`、频率/计数为 `0`、目标编码为训练集全局均值；`error` 模式会列出未知值并拒绝预测。缺失类别继续使用 `<missing>`，且推理输出列严格按训练顺序重建。

本轮验收结果：Python **104 passed, 1 skipped**；前端 DOM **4/4**；Chrome **9/9**（组件库 29 个）；部署/MCP **12/12**（36 个 MCP 工具，端到端方案成功）。`tests/test_categorical_encoder.py` 的 9 项测试覆盖 schema 固定、未知类别两种策略、五类编码语义、数值型类别、组件复用、完整 DAG 传递、模型内嵌和 pickle 往返。

## 第五轮：完整组件清单接入 MCP（2026-09-15）

Registry 从 29 个扩展到 54 个组件，补齐时间重采样、数据切分、邻近值、填充、二值化、分布/周期/漂移/异常探索、五种图形、滚动/差分/熵特征，以及线性回归、ARMA、KNN、隔离森林、Persist、决策树和水库机分类。列操作同时增加删除全空列、删除常量列和裁剪极值选项。MCP 仍保留 36 个高层控制工具，新增组件通过 `list_components`、`search_components`、`get_component_schema` 和图编辑工具发现与执行。

本轮验收结果：Python **110 passed, 1 skipped**；前端 DOM **4/4**；Ruff 与前端 JavaScript 语法检查通过；真实 MCP stdio 会话可用中文“水库机”检索到 `validation.reservoir_classifier`。按本轮要求未执行部署、安装包和发布物校验。

手工验收步骤：

1. 启动服务，打开 http://127.0.0.1:8765，加载示例并运行。
2. 点击统计特征、随机森林、曲线节点，核对表格、指标和曲线。
3. 在新方案中拖入数据输入和过滤组件，连接端口并应用参数。
4. 验证缩放、平移、Shift 多选、复制、删除及撤销重做。
5. 导出 XML，再导入，核对节点配置、位置和连接。
6. 执行后保存检查点；修改并重跑；恢复检查点核对旧结果。

## 第六轮：Agent Skill 重写（2026-09-15）

触发原因：真实使用反馈指出 skill 太薄——只说"按这个顺序做"，没说每个阶段怎么配、有哪些坑。Agent 于是要猜数据路径、猜 `label_policy`、猜哪个组件能流式，而且 skill 内部还有自相矛盾的一处（装配一节让 `feature.score_select` 进模型，执行一节又说探索输出是终端分支）。

改法：`SKILL.md` 从 84 行重写为 569 行的阶段手册，每个阶段固定回答四件事——**目标**、**怎么配（真实参数名）**、**注意事项（护栏与常见坑）**、**进入下一阶段的检查清单**；阶段为 recon → 数据准备 → 质量预检 → 窗口与标签 → 特征 → 验证 → 执行排错 → 读结果 → 持久化 → 汇报。另附三份参考：`references/recipes.md`（可照抄的调用序列：常规分类、onset 数据、资产留出、无监督、超大文件、失败后重跑、三模型对比）、`references/troubleshooting.md`（报错原文 → 原因 → 修法 + 每条护栏为什么存在）、`references/components.md`（56 个组件的用途、端口、关键参数与"什么时候不要用"）。矛盾处改为单一说法：`feature.score_select` / `feature.pca` 只在探索分支使用，或进模型但必须带着泄漏警告汇报。

写文档时核对源码，纠正了三处常见误解并写进 skill：`/api/data/upload` 只接受 CSV/Parquet 且**上限 25 MB**（更大的文件必须由操作者放进 `data_root` 或用 `--data-root` 指过去）；流式只被 `feature.statistical`、`feature.fitting`、`feature.spectral`、`visual.overview`、`data.materialize` 接受，`data.quality` 与熵特征都要先物化；`validation.linear_regression` 的 `split_method` 是 `random/group/temporal`，**没有** asset 留出。

防漂移：新增 `tests/test_skill_guide.py`（18 项），校验 38 个工具全部被文档化、技能里不得出现不存在的工具调用或组件名、附录 B 的分类计数与 Registry 一致、阶段章节与三份参考文件存在且体量达标（防止回退成薄摘要）。`scripts/export_release.py` 的发布清单改为列出 skill 目录下全部文件，避免只声明 `SKILL.md` 而漏掉参考文件。

本轮验收：Python **140 passed, 1 skipped**；`ruff check` + `ruff format --check` 通过；`scripts/verify_deploy.py --from-config` **14/14**（其中 skill 检查项通过）；`scripts/mcp_smoke.py --from-config` 成功（38 工具、13 个 feature 组件可检索、XML 6157 字符）；发布包重新导出为 `dist/fault-prediction-platform-0.1.0-deploy.zip`（248 KB，含三份参考文件）。

## 第七轮：源码注释补齐（2026-09-15）

目标：把 `src` 下全部模块补成"可读、可维护"的状态。改动**只涉及注释与 docstring，没有修改任何一行逻辑**——测试数量与结果前后完全一致（140 passed, 1 skipped），可作为"零行为变更"的证据。

做法分三层，避免把注释写成复述代码：

1. **模块级 docstring**：这个模块负责什么、在整个数据流里的位置、有哪些贯穿全文的约定（例如 `fault_core` 的"索引即身份"、`fault_platform` 的"图只存配置"）。
2. **函数／类 docstring**：参数语义、返回值形状、失败条件，以及"为什么这样设计"（例如窗口组件的四个参数必须一致才能合并；验证器为什么拒绝 `stratified` 处理重叠窗口）。
3. **行内注释**：只写代码本身读不出来的信息——数值细节（Hann 窗相干增益归一化、MAD 的 1.4826 系数、区间双指针扫描）、护栏原因（为什么拒绝覆盖已有列、为什么要 purge 共享原始行的训练窗口）、以及性能取舍（流式覆盖用区间而不是行号列表，400 万行能省下数百 MB）。

语言约定：保留原有英文 docstring 摘要（与既有代码风格、工具描述一致），新增的说明用中文；这样 IDE 悬浮提示仍是英文短句，深入阅读时得到中文解释。

量化前后对比（`src` 下 33 个 `.py` 文件）：

| 指标 | 改动前 | 改动后 |
| --- | --- | --- |
| 总行数 | 7,737 | 10,311 |
| 行内注释行 | 43（集中在 4 个文件，21 个文件为 0） | 217（每个文件都有） |
| docstring 段数 | ~100（多数模块只有一行） | 447 |
| docstring 文本行 | ~600 | 5,022 |

覆盖到的关键点举例：`fault_core/features.py` 的窗口/来源/标签三条不变量与流式区间算法；`models.py` 的四种切分语义与指标字段；`quality.py` 每类发现对应的动作；`fault_platform/runtime.py` 的指纹、增量复用、失败传播与取消；`workspace.py` 的按引用存储、LRU 淘汰/溢写与有界观测；`service.py` 的控制面契约、全局锁例外（`wait_for_pipeline`）与"编辑即失效"；`xml_io` 的 XXE 与端口一致性校验；`builtin.py` 每个组件的用途、端口与坑（56 个组件全部有类级说明）。

配套更新：README 增加注释约定与目录说明，并把过期的"53 项通过"改为当前数字；发布包重新构建（wheel 与 deploy zip 均含带注释的源码）。

本轮验收：pytest **140 passed, 1 skipped**；`ruff check` + `ruff format --check` 通过；`pip check` 干净；DOM 测试 **4/4**；`scripts/verify_deploy.py --from-config` **14/14**；`scripts/mcp_smoke.py --from-config` 退出码 0（38 工具、status=SUCCESS）。

## 第八轮：组件库折叠与可调整布局（2026-09-15）

触发原因：使用反馈指出组件库是"平铺"的——56 个组件全部直接展开，组件变多后很难找；而且左中右三栏宽度固定，画布或参数面板不能按需放大。

改了什么：

1. **两级折叠目录**。组件库按「分类 → 子分类」折叠：分类标题显示图标与数量，子分类标题显示数量；点击标题即折叠/展开。组件总数超过 24 时子分类默认折叠（先给目录），且折叠只加 `.collapsed` 隐藏内容，**组件节点仍留在 DOM 中**——收藏、拖拽、计数与前端测试都依赖它们存在。另有「▾ 折叠全部 / ▸ 展开全部」按钮，折叠状态存 `localStorage.fault-library-collapsed`。
2. **搜索/筛选强制展开**。输入关键词、切换分类或切到收藏/最近视图时，命中的分组自动展开——避免"搜到了却看不见"这类折叠式目录最常见的坑。
3. **三栏可拖拽**。左栏宽度、右栏宽度、结果面板高度各自有一条分隔条：拖动调整、方向键微调、双击（或「↺ 恢复布局」）复位；尺寸存 `localStorage.fault-layout`，刷新后保持。分隔条绝对定位在 `.workspace` 内并按其**实际渲染边界**定位，因此对 CSS 钳制与响应式断点同样成立（放进可滚动面板会随内容滚走）。
4. **视口钳制**。侧栏不超过视口宽度的 34%、结果面板不超过高度的 45%，画布始终留得下可用空间；窄窗口下三栏都不会被挤没。

本轮验收：Python **140 passed, 1 skipped**（本轮未改 Python）；`ruff` 通过；DOM 集成测试 **6/6**（新增 2 项：折叠/搜索/持久化、面板尺寸钳制与复位）；`scripts/browser_check.cjs` **13/13**（新增 3 项，均在真实 Chrome 里用真实指针事件驱动）：

| 新增检查 | 实测 |
| --- | --- |
| 组件库折叠为目录且可搜索 | DOM 中仍是 56 个组件；默认 24 个分组折叠、仅 8 个可见；点开频域子分类后可见 9 个；搜索"频域"时 0 个分组折叠、计数 `1/56`；全部折叠后 0 个可见且目录**无需滚动**（`fitsWithoutScroll`）且无标题被裁切 |
| 分隔条拖拽与持久化 | 左栏 235px → 326px（CSS 变量与 `localStorage` 一致），刷新后仍为 326px，画布保持 1054×471 |
| 窄窗口（960×720）三栏 | 三栏均 ≥100px，横向溢出 0px，分隔条与面板边缘对齐误差 ≤4px |

截图：`.fault-platform/screenshots/` 下的默认目录视图、全部展开视图，以及第九轮补的两张 3x 放大图。该轮只做了可量化的几何断言（尺寸、可见项数、滚动/裁切、溢出、对齐），**没有做人眼像素评审**——当时的会话不具备图像输入能力，这一点如实记录；下一轮补做了目视评审，并因此发现了一个真 bug。

## 第九轮：人眼放大评审与拖拽方向修复（2026-09-15）

触发原因：第八轮的可用性改动（折叠目录 + 可拖拽面板）只经过了可量化的几何断言，没有人真正看过渲染结果。本会话恢复图像输入能力后，用真实 Chrome 截图补做了目视评审，并把"把代表问题的区域放大 3 倍再看"固定成做法。

方法：无头 Chrome + DevTools 协议，`Page.captureScreenshot` 带 `clip` 与 `scale=3`，只截关键区域（组件库目录、树形引导线、三块分隔条的静止与悬停态）。放大图已固化进 `scripts/browser_check.cjs`，每次跑验收都会重新生成：

| 放大图 | 内容 |
| --- | --- |
| `01c-library-tree-zoom.png` | 分类标题、子分类引导线、数量徽标 |
| `01d-panel-splitter-zoom.png` | 左栏与画布之间的分隔条（悬停高亮） |

评审发现（按严重程度）：

1. **真 bug：结果面板的分隔条方向反了。** 用真实指针把结果面板分隔条**向上**拖 160px，面板不但没变高，反而缩到了最小高度（实测 252 → 120px）。原因是 `startPanelDrag` 只对 `inspector` 做了方向取反，`results` 沿用了"坐标增大 = 变大"的公式，而结果面板的分隔条在面板**上沿**，方向本应相反。修复：把方向收敛成 `PANEL_KEYS.dragSign`（library `+1`、inspector `-1`、results `-1`）；键盘方向键本来就分开定义，现在指针与键盘一致。
2. **观感缺陷：树形引导线被渲染成一对「(」。** 子分类行直接用 `border-left:2px`，而它又继承了 6px 圆角，Chrome 会把这条边框渲染成两端带弧的括号；6 行重复后视觉噪音很大。修复：改用 `::before` 画一条 2px 直线（`border-radius:1px`），逐行拼起来才是干净的树形引导线。
3. **测试盲区：拖拽逻辑无法被 DOM 测试覆盖。** 分隔条用的是 `handle.onpointerdown = ...` 属性式监听，而 jsdom 不认这个 IDL 属性（实测 `"onpointerdown" in div === false`），于是 jsdom 里拖拽永远不会触发。改成 `addEventListener` 后，同一条逻辑可以同时被 jsdom 与真实浏览器覆盖。
4. **两个"像 bug 但不是"的点，如实记录**：截图里的"灰色竖条 + ▲"是 Chrome 原生滚动条，只因为评审脚本没带 `--hide-scrollbars`；「↺ 恢复布局」按钮变蓝是 `:hover` 态（实测 `matches(":hover") === true`、`color: rgb(37,77,187)`），不是常驻高亮。

本轮验收：Python **140 passed, 1 skipped**（本轮未改 Python）；`ruff check` 与 `ruff format --check src tests scripts` 通过；DOM 集成测试 **6/6**（面板用例新增 3 条拖拽方向断言）；`scripts/browser_check.cjs` **14/14**（新增 1 项方向检查，真实指针事件驱动）。

| 新增断言 | 实测 |
| --- | --- |
| 三块分隔条的方向都跟手 | 往左拖右栏 278 → 358px；往上拖结果面板 252 → 392px；画布仍保有 974×331 |
| 拖拽结果写回布局状态 | 向上拖 170px 后 `localStorage.fault-layout` 为 `{"library":236,"inspector":278,"results":422}`，画布高度仍有 301px |

踩坑记录（写下来避免重复）：`scripts/verify_deploy.py` 必须用配置里的解释器跑（`.\.venv\Scripts\python.exe`）。用 Anaconda 基座 Python 跑会得到 13/14，唯一失败项是 `fault_platform import + registry`——因为 `fault_platform` 只装在这个 venv 里，不是脚本或平台的缺陷。同理，`ruff format --check` 只应作用于 `src tests scripts`：不加路径会连带检查文档里的代码块与 `test_3w/` 等实验目录。

诚实边界：本轮仍然是**抽查**——只看了关键区域放大图与三个交互状态（默认、悬停、拖动后），没有逐屏、逐分辨率、逐浏览器做像素评审；没有做无障碍量化（对比度、色盲模拟、200% 缩放下的字体表现均未测量）；截图来自 1680×1050 的 headless Chrome，未覆盖 Windows 系统缩放下的 DPI 表现。
## 第十轮：连线改为圆角正交折线（2026-09-15）

触发原因：使用反馈指出连线是贝塞尔曲线，而 Simulink 是"直线 + 折线"，曲线看着不够工整。本平台的端口固定为"左侧输入、右侧输出"，是折线最容易做好的拓扑，因此把 `curve()` 的贝塞尔路径换成圆角正交折线。

改了什么：

1. **路由规则**（`orthogonalRoute()`）：出端口先水平走 16px stub，接一条竖直通道，再水平进入目标端口，形成 Simulink 那种 Z 形；两个端口同一高度时自然退化成一条直线。目标在左侧（回边，节点被拖到源节点左边）或两节点几乎重叠时改走 U 形绕行，避免横穿节点本体。
2. **折角圆角**（`edgePathD()`）：折点用 7px 圆角（`Q` 命令），半径按相邻两段中较短的一段自适应收缩，短段也不会被切坏；共线点直接省略，因此 `d` 里只有 `M/L/Q`，没有一条 `C`。
3. **并行分道**（`claimLane()`）：同一条竖直通道上、竖直区间又重叠的连线按 9px 依次错开，避免两条线叠成一条看不清的粗线。
4. **点击热区**：折线只有 1.8px 宽不好点中，因此每条线额外叠一条 15px 宽的透明热区；悬停热区时高亮真正的那条线，热区本身永远透明。
5. **顺带修掉一个真实隐患**：`portPosition()` 原来只要端口元素存在就直接用它的 `getBoundingClientRect()`，量不到尺寸时（尚未渲染、或环境不支持布局）会返回同一个点，导致所有连线退化成零长度路径。现在尺寸为 0 时回退到按节点位置与端口序号推算的坐标。

本轮验收：Python **140 passed, 1 skipped**（本轮未改 Python）；`ruff check` 与 `ruff format --check src tests scripts` 通过；DOM 集成测试 **7/7**（新增 1 项：折线正交性、首尾落在端口、回边走位、并行分道、热区数量）；`scripts/browser_check.cjs` **15/15**（新增 1 项几何检查）。

| 新增检查 | 实测 |
| --- | --- |
| 连线不含曲线命令 | 14 条边，`d` 中 `C/S/A` 命令数 **0** |
| 除圆角外没有斜向行程 | 沿路径抽样累计斜向位移最差 **41.7px**（半径 7、一条边最多 4~6 个圆角，理论上限约 25px；换成贝塞尔会是 300px 量级），阈值 90px |
| 端点落在端口圆心 | 最差偏差 **1.0px**（适应画布后缩放 0.35，约合 2.9 个画布单位），阈值 1.5px |
| 每条线都有点击热区 | **14/14** |

人工目视评审：`02b-edges-zoom.png`（2x 放大连线折角与端口接合处），与 `01c`/`01d` 一样每次跑浏览器验收都会重新生成。**本轮没有人看图**——当前会话的模型不支持图像输入（`view_image` 直接返回 "you do not support image inputs"），所以结论全部来自上述可量化的几何断言，这张放大图留给人工复核。

诚实边界：折线只在"不绕障"的前提下正交——若两条连线必须跨过同一个节点，它们会从节点上压过去（Simulink 会绕开）。回边用固定的两行中线绕行，遇到上下都被节点占满时观感一般。修圆角后的折线在极密集区域仍可能重叠，分道只处理了同一竖直通道这一种情况。

## 第十一轮：把"组件用不上"变成阶段自检（2026-09-15）

触发原因：使用反馈指出 56 个组件里实际只用到了很少几个，"组件优势没发挥出来"。先量化：扫过所有落盘的方案 XML（`test_3w`、`test_mcp_skill` 加冒烟图），真正进过图的组件是 **14 / 56（25%）**；两轮真实使用分别是 9 个和 10 个；把会话记录里"提到过"的算上也只有 29 / 56。缺口集中在 `explore.*`（8 个一个都没进过图）和 `visual.*`（只用过 overview 与 line）。

第一版设想被使用反馈否掉，理由成立：**逐项显式取舍会把任务撑爆**——56 个组件、每个阶段都权衡一遍，等于把组件目录塞进每一步。参照物是 MATLAB 那套 skill：它们全是"触发条件 → 读这个"，不是"把所有工具权衡一遍"。于是改成三条：

1. **Recon 要产出能力清单**（§1）：环境事实之外，再交一份 3~6 行的"这次可能用得上的手段"清单，用 `get_component_facets()` 加 1~2 次 `retrieve_components` 得到；同时写明"不要枚举全部 56 个组件"。
2. **每个问题多发的阶段末尾加一道 Stage gate**（§3~§6）：一张"自检问题 → 命中时用什么"的小表，最多 6 条；命中才动手，没命中就继续。四道闸门覆盖数据理解（`explore.*` 的落点）、窗口与标签、特征够不够、验证方式对不对，并明确写了"不要为了用组件而加节点"。
3. **汇报里加 Stage checks**（§12）：交代哪些闸门触发过、做了什么，以及整场没触及的能力类别为什么可以接受。

顺带修正两处会把 skill 写歪的事实：`explore.concept_drift` 是**双输入**（`reference`/`current` 两个 `Dataset`），单输入挂不上去，闸门里直接写明要用两个 `data.filter` 切早晚两段；`validation.*_detector` 三个无监督组件吃的是原始 `Dataset`、不需要 `LabelVector`，这条写进了验证闸门。

本轮验收：Python **144 passed, 1 skipped**（`tests/test_skill_guide.py` 22 项，新增 4 项：闸门数量、闸门 ≤6 条且必须是自检表、汇报含 Stage checks、Recon 含能力清单）；`ruff check` 与 `ruff format --check src tests scripts` 通过。skill 经 junction 即时生效，`verify_deploy` 的 skill 检查项通过。

刻意没做的事：**没有设"组件使用率"指标**。目标函数是"该问的问题有没有被问"，不是"用了多少个组件"——按覆盖率考核只会催生装饰性分支。量化覆盖率的脚本留在 `.fault-platform/component_coverage.py`（临时脚本），以后想知道又漏了哪类能力直接跑它。

## 第十二轮：Skill 瘦身（2026-09-15）

触发原因：skill-creator 的规范要求入口只放"改变决策、改善工作"的信息，条件性细节放进参考文件按需读取；而 SKILL.md 在第十一轮后涨到 626 行，把参数表、实测数字、检查清单和注意事项全堆在入口里。

改了什么：

- **入口 626 → 385 行**（-39%），只留决策、硬约束、路由、阶段自检闸门、38 个工具的用途表与 5 类能力索引。
- **新增 `references/stages.md`（331 行）**：把每个阶段的参数表、实测数字（3W 的 0.716 → 0.528）、检查清单、「注意事项」与大数据边界搬过去，每节都标了对应 §号便于对照。
- **Appendix B 从"列出全部 56 个组件名"改成"5 类能力 + 数量 + 输入端口"**：完整清单本来就在 `references/components.md`，重复列一遍正是规范点名的 duplication。
- **§11 症状表从 14 行压到 5 行**（最常见的那几条），其余交给本来就更全的 `references/troubleshooting.md`。

没丢内容（逐个抽查）：`Feature names overlap`、`cannot consume streamed input`、`data.asset_key` 资产留出配方、状态词表、3W 数字表、"重启后全空"的处置建议、"重复工具名"的处理，全部仍在 skill 目录内（`stages.md` 或 `troubleshooting.md`）。

新增守卫（`tests/test_skill_guide.py` 22 → 23 项）：`stages.md` 纳入体积下限；每个 `references/*.md` 都必须能从入口被链接；入口行数上限 420 行——谁再把细节堆回 SKILL.md，测试直接红。同时修掉一条措辞脆弱的断言（原来按字面量比对一整句，改行宽就会失败），现在统一按空白规范化后比较，这也正是 skill-creator 明确反对的"只匹配措辞"的测试写法。

本轮验收：Python **145 passed, 1 skipped**；`ruff check` + `ruff format --check src tests scripts` 通过；`skill-creator/scripts/quick_validate.py` 输出 **Skill is valid!**（该脚本在 Windows 上需要 `PYTHONUTF8=1`，否则按 GBK 读中文会崩，这是上游脚本的编码问题）；`verify_deploy` **14/14**（含 skill 检查项）。

诚实边界：这轮只做了结构重排，没有做行为验证——"入口更短是否真的让 Agent 表现更好"没有实测，只符合官方规范并降低每次加载的上下文成本。真正的检验仍是下一轮真实使用：`explore.*` 有没有进图。

## 第十三轮：让中间产物可检验（2026-09-15）

触发原因：使用反馈指出——对真正的故障预测工程师来说，Agent 的产出必须可检验，否则 MCP + skill 就是个黑盒；最核心的是"中间产生的数据长什么样"，比如提取特征之后的表长什么样。而 `visual.overview` 当时只接受 `Dataset`，特征分支挂不上去。

改的是机制，不是给某个组件打补丁：给输入端口加"兼容类型"能力，`accepts` 只写在接收端，输出端永远只有一个类型。

1. **端口模型**：`InputPort.accepts` + `accepted_types`；`ComponentGraph.connect` 改为检查"上游类型是否落在目标端口可接受集合内"，运行时用 `validate_input` 按任一兼容类型校验。报错仍然给出完整集合，例如 `Incompatible port types: LabelVector -> Dataset | FeatureDataset`。
2. **11 个检查类组件放宽**：`visual.overview`、`visual.line`、`visual.scatter`、`visual.subplot`、`visual.histogram`、`visual.relationship`、`explore.central_tendency`、`explore.dispersion`、`explore.correlation`、`explore.distribution`、`explore.anomaly` 现在同时接受 `Dataset` 与 `FeatureDataset`。数据转换类组件（`data.filter` 等）**没有**放宽——宽容只给"看数据"的组件。
3. **可发现性**：`get_component_schema` / `list_components` 返回 `accepted_types`，`input_type=FeatureDataset` 能直接筛出这些检查类组件（Agent 走的正是这条检索）；组件目录 `docs/components.md` 相应显示为 `dataset : Dataset | FeatureDataset`。
4. **网页端**：端口提示与节点检查器显示 `Dataset | FeatureDataset`，工程师一眼知道这个口还能接特征表。
5. **skill**：Stage 4 自检闸门加了一行"说不出特征表长什么样 → 把概览挂上去"；汇报契约新增第 8 项 **Middle evidence**，要求把中间数据的概览作为证据交出来。

本轮验收：Python **147 passed, 1 skipped**；`ruff check` + `ruff format --check src tests scripts` 通过；DOM 测试 **8/8**（新增 1 项：端口提示显示兼容类型）；`scripts/browser_check.cjs` **16/16**（新增 1 项端到端）：给 `stat.features` 挂一个概览节点 → 服务端接受 → 重跑 → 从 `get_node_result` 读出 **90 行 × 15 列**、列名以 `__mean/__std/__rms` 开头 → 浏览器结果面板确实画出了这份概览（截图 `04-feature-overview.png`）。

诚实边界：放宽的只有检查类组件；`explore.periodicity`、`explore.cross_relation`、`explore.concept_drift`、`visual.compare`、`visual.anomaly` 仍只吃原始表，因为它们的语义绑在原始信号与时间上。`data.quality` 也仍然只针对原始窗口——特征表里的"恒零列"要靠 `visual.overview` + `explore.distribution` 人工判断。另外，往已有图上加节点会让该方案的结果失效并需要重跑，这是既有语义（图被编辑即失效），不是本轮引入的行为。

## 第十四轮：P0–P3 修复 + skill 中文化（2026-09-15）

触发原因：一次自查发现四类问题（P0 长跑误报、P1 工具描述缺失、P2 skill 缺口、P3 闸门有效性未验证），使用反馈要求全部修复，并把 skill 换成中文。

### P0 —— 阻塞式等待在第 60 秒必然误报"服务不可达"（实测）

- 桥对所有工具固定 `httpx.AsyncClient(timeout=60)`，而 `wait_for_pipeline` 服务端默认等 300 秒，skill 的配方还写着 600。
- **修复前实测**（3W 真实数据：489,456 行 → 8091 个窗口 → 统计+拟合+频域 → merge → 插补 → RF-300，进程内 149.5 秒）：`execute_pipeline -> RUNNING (0.2s)`，`wait_for_pipeline returned after 60.2 s: {"success": false, "error_code": "CONTROL_API_UNAVAILABLE", "summary": "Start fault-platform serve at ..."}`，而服务端 `status: RUNNING`。
- **修复后实测**（`scripts/mcp_wait_probe.py`）：`wait_for_pipeline returned after 155.1 s` → `{"success": true, "status": "SUCCESS"}`。
- 改动：`call_timeout_seconds()` 让等待类工具的客户端超时跟着 `timeout_seconds` 走；传输层错误拆成 `CONTROL_API_TIMEOUT`（服务可能还在算 → 先查 `get_pipeline_status`，**不要重启**）与 `CONTROL_API_UNAVAILABLE`（桥确实连不上）；非 JSON 响应单独报 `CONTROL_API_BAD_RESPONSE`。
- 新增穿过桥的测试 `tests/test_mcp_bridge.py`（5 项）与手册脚本 `scripts/mcp_wait_probe.py`——这一层此前完全没有测试覆盖。

### P1 —— 38 个工具里 18 个没有描述

补全到 38/38（批量工具明确写出"用我替代逐个调用"），并新增守卫：每个控制操作都必须有描述，且描述不能过短。

### P2 —— skill 全量中文化 + 补两处缺口

- 五个文件中文化：`SKILL.md`（305 行）、`references/stages.md`（253）、`components.md`（223）、`recipes.md`（240）、`troubleshooting.md`（125）。工具名、组件类型、参数名与平台报错原文保留英文——它们是接口标识符。
- 新增 §0.5「服务与桥的故障」（超时 ≠ 服务死了，绝不因此重启）与 §0.6「用用户的语言回答」（默认中文）。
- front-matter 的 `description` 从 493 字符压成一段可读中文，仍保留 `MCP` 关键词供技能路由。
- 守卫测试同步改为中文标记（阶段标题、阶段自检、能力清单、汇报契约、组件计数）。

### P3 —— 闸门有效性：用真实 MCP 走一遍

用真实 MCP 工具按新 skill 的闸门在 3W 数据上建图（`.fault-platform/gate_probe.py`）：阶段 2 自检命中"通道冗余／分布形状"→ 加 `explore.correlation`、`explore.distribution`；阶段 4 自检要求中间产物证据 → 把 `visual.overview` 挂到 `merge.features` 上。覆盖率对比：

| 指标 | 之前 | 现在 |
| --- | --- | --- |
| 落盘方案里用到的组件 | 14 / 56（25%） | **18 / 56（32%）** |
| `explore.*` | 0 / 8 | **2 / 8** |
| 特征分支上的概览 | 无 | `visual.overview` 接在 `merge.features` 上 |
| `data.quality` | 未进任何落盘方案 | 已进图 |

诚实边界：这一轮是**同一个 Agent**（我）按闸门走的，不是另一个独立模型跑出来的；它证明的是"闸门用现有工具可执行、且能带来覆盖"，**不能**证明"换个模型也会这么走"。

### 顺带发现（记录在案，本轮未修）

`add_components` 与 `configure_components` **都不是原子的**：批量中途某条非法时，前面已成功的条目会留下，报错也不说哪一条、已经改了什么（实测：一次批量里 `data.quality` 传了它不接受的 `label_policy`，结果只落了第一个节点，但 `validate_pipeline` 仍然返回 valid=True）。本轮先把新写的工具描述改成实话（不再声称会回滚）并提示失败后先 `get_pipeline` 复核；要做成原子操作需要给 `ComponentGraph` 加快照/回滚，属于行为变更，留待下一轮决定。

本轮验收：Python **152 passed, 1 skipped**；`ruff check` + `ruff format --check src tests scripts` 通过；`quick_validate.py` 输出 **Skill is valid!**（中文 skill 同样通过）；`scripts/browser_check.cjs` **16/16**；`verify_deploy` **14/14**；`mcp_smoke` 成功；`scripts/mcp_wait_probe.py` PASS。

## 第十五轮：批量编辑改为原子操作（2026-09-15）

触发原因：第十四轮的自查发现 `add_components`/`configure_components` **不是原子的**——批量中途某条非法时，前面已成功的条目会留下，报错也不说哪一条、已经改了什么。实测中一次批量里 `data.quality` 传了它不接受的 `label_policy`，结果只落了第一个节点，调用方却以为整批都没进去。使用反馈要求修掉。

改了什么（三条批量操作一起改，`connect_many` 是同一类缺陷）：

1. **全成或全不成**。`add_components` 失败时删掉本次已加的节点，并**把 `graph.version` 退回调用前**——否则一次被拒的调用会留下"版本涨了但内容没变"的状态，让持有旧 `expected_version` 的客户端凭空收到并发冲突。`configure_components` 失败时把已改节点恢复成调用前的参数（`deepcopy` 快照）。`connect_many` 失败时删掉本次已连的边。
2. **报错点名**。统一形如：报错里写明 `entry 3 of 3`（第几条）、括号里给出这一条的组件类型与节点 id、冒号后是平台给出的具体原因，最后一句固定为 `Nothing was added: the graph is exactly as it was before the call.`（另外两条分别是 `Nothing was changed…` 与 `No connections were made.`）
3. **工具描述改成实话**。三条批量工具的 MCP 描述现在明确写 "All or nothing …"；第十四轮临时写的"不会回滚，请自行核对"到此撤销。
4. **skill 同步**。`SKILL.md` §0.3 增加一条：批量是全成或全不成，失败后按报错点名的那条改即可，不必再 `get_pipeline` 猜哪些已生效；`references/troubleshooting.md` 的图编辑一节加了对应症状行。

本轮验收：Python **153 passed, 1 skipped**（新增 `tests/test_agent_ergonomics.py::test_bulk_edits_are_all_or_nothing`：三条批量在失败后节点、参数、边与 `version` 都回到原样）；`ruff check` + `ruff format --check src tests scripts` 通过；浏览器验收 **16/16**、`verify_deploy` **14/14**、`mcp_smoke` 成功、`quick_validate` 通过。

补充说明：第十四轮我顺口提到"`validate_pipeline` 还返回 valid=True"——复核后这一点**不是缺陷**：图里只有一个 `data.input` 节点时结构上确实没有违规（必填输入满足、无环、无重复生产者），`validate_pipeline` 只查结构不查完整性。真正的问题只是"调用方不知道批量被部分应用"，现在这条已经消除。

## 第十六轮：时间窗口与"预测未来故障"（2026-09-15）

触发原因：使用反馈指出，做真正的故障预测需要"用前面几天的数据预测未来是否发生故障"——也就是按**时间**切窗口（例如 7 天），标签来自**未来**，而不是窗口自身。此前窗口只能按行数切（`window_size`/`step`），标签也只能从窗口内部聚合。

改了什么：

1. **时间窗口**：新增 `window_span`/`step_span`（`"7d"`/`"12h"`/`"180s"`），与 `window_size` 互斥。按时间切窗，采样不规则时每个窗口的行数可以不同；时间列支持时间戳与数值（数值按秒解释），组内自动按时间稳定排序。窗口"完整"的判定改为"起点 + 跨度不超过组内最后一个采样时刻"，窗口内的行仍是半开区间 `[起点, 起点+跨度)` 里的全部采样。
2. **未来视野标签**：`label_policy` 新增 `horizon`，配合 `prediction_horizon`/`prediction_gap`/`normal_label`：窗口结束加间隔之后、视野之内出现非正常标签就标 1。`current_fault_policy` 决定"窗口自身已故障"的样本怎么处理（默认 `drop`，它们属于检测任务）。
3. **诚实丢弃**：视野超出可用数据、或视野内压根没有采样的窗口**不标 0**，而是丢弃并计数；连同"已故障窗口"的数量一起写进 `attrs` 与 warnings，汇报时必须带出来。
4. **一份实现**：窗口定义与标签语义收敛到 `fault_core.features` 的 `window_arguments / prepared_windows / window_shape / window_attrs`，`feature.statistical`/`feature.fitting`、`feature.spectral`、`feature.entropy` 三条实现共用。这正是本轮踩到的漂移点：新参数最初只加进了一条实现，另外两个组件直接 `TypeError`——现在由共用函数兜住。
5. **流式明确拒绝**：时间窗口与预测视野需要"整组 + 它的未来"，流式按块看不到未来，因此直接报错让用户关掉 `streaming` 或插 `data.materialize`，而不是给一个标签错了的结果。

真实数据验证（3W：489,456 行 / 28 个实例；窗口 `180s`、步长 `60s`、视野 `1h`、`current_fault_policy=drop`）：**得到 3370 个窗口、正类 905 个（26.9%）**，同时丢弃 **4380 个"自身已故障"**与 **331 个"视野超出数据"**的窗口，特征提取用时 1.0 秒。

本轮验收：Python **160 passed, 1 skipped**（新增 `tests/test_prediction_windows.py` 7 项：视野标签、间隔带、未知未来丢弃、不规则采样与时间窗口、参数组合校验、字符串标签、端到端图跑通）；`ruff check` + `ruff format --check src tests scripts` 通过；skill 守卫 **23 项**通过；浏览器验收 **16/16**；`verify_deploy` **14/14**；`mcp_smoke` 成功。

边界：时间窗口仍假设组内按时间连续（不跨组）；流式不支持预测模式；频域仍要求每个窗口至少 8 个采样点（按行数判断，与时间跨度无关）。
补充（同轮）：使用反馈指出模块文档里"一行一个窗口"的表述有歧义，容易被读成"每个采样点一个窗口"。已改成明确的表述并把这条语义写成回归测试：**特征行数 = 窗口数**，每组约"组内时长 / 步长"（按行切窗则是"组内行数 / 步长"），与输入行数无关。实测同一份 48.9 万行数据：`180s`/`30s` → 16154 行、`180s`/`60s` → 8081 行、`180s`/`180s` → 2700 行、`180s`/`600s` → 818 行、`1h`/`1h` → 114 行。`tests/test_prediction_windows.py::test_feature_row_count_follows_the_window_and_step` 钉住这一点（步长 1d/2d/5d → 8/4/2 行，而输入是 240 行）。
补充（同轮，MCP 接口面）：能力做出来了，但**对 Agent 不可发现**——修复前实测检索结果："未来 7 天 故障"与"滑动时间窗口"返回 0 个组件；"预测未来是否故障"只找到 `validation.arma`（时序基线，不是窗口预测）；关键词 "时间窗口" / "horizon" / "window_span" / "未来" 全部 0 结果。根因是四个窗口生产者的 `description` / `tags` / `search_keywords` 还停留在"窗口统计"。修复后（在同一层 HTTP/MCP 控制面复测）："预测未来是否故障" → `feature.statistical` / `feature.entropy` / `feature.fitting`；"故障预警" → `feature.fitting` / `feature.statistical` / `feature.entropy`；"prediction horizon" → `feature.statistical` / `feature.fitting` / `feature.spectral`；"时间窗口" / "预测" / "horizon" / "window_span" 都能命中四个窗口生产者；`tags=["prediction"]` 返回全部四个。新增守卫 `tests/test_catalogue_scale.py::test_prediction_capability_is_discoverable`（5 条意图 + 5 条关键词 + 标签 + schema 参数与 `label_policy` 枚举），元数据漂移会直接让测试变红。

## 首版边界

- 核心工程和入口完整；单 DAG 串行调度，两个独立方案可同时执行。
- Workspace、模型 artifacts、检查点是内存实现；不会跨进程恢复。
- XML 保存配置，CSV 提供数据；没有磁盘模型存储、增量流式输入或多用户鉴权。
- 参数表单中的列名使用文本/逗号列表输入；尚未提供基于上游列元数据的自动补全。
- 频域特征假设每列样本等间隔且在窗口内连续；平台按提供的采样率解释频率，不做重采样或转速跟踪。
- 频谱使用 Hann 窗与相干增益归一化，峰值幅值和谱 RMS 对单音准确；谱质心、谱展宽、谱熵与频带比受窗主瓣宽度影响，适合比较同一流程下的不同样本。
- 特征评分选择和主成分分析在全部行上拟合，因此携带探索性提示；把选择或降维放进训练折内是后续工作。
- temporal 按现有特征行顺序拆分；调用方必须先建立符合业务时间顺序的特征表。
- 全量缩放、全量编码产生探索性警告；目标编码使用 OOF，仍不能替代嵌套训练内编码。
- 示例是合成的三分类设备数据，不是工业性能验证或真实提前预测实验。
