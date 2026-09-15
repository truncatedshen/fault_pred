# 实施与验证记录

环境：Windows、Python 3.11.7、Node.js 24.15.0。验证日期：2026-09-14。

## 结果

| 检查 | 结果 |
| --- | --- |
| Python / pytest | 95 项通过（1 项按可选依赖跳过） |
| 前端 DOM 集成测试 | 4 项通过，连接真实临时 HTTP 服务 |
| 真实浏览器验收 | Google Chrome headless + DevTools 协议，9 组检查通过（含实时同步，见下） |
| 实时同步（SSE） | Agent 改动 74 ms 内出现在打开的页面；Agent 触发执行时页面看到 RUNNING→SUCCESS 与节点耗时（见下） |
| Random Forest / SVM / XGBoost 示例 | 全部执行成功，使用同一批测试设备 |
| MCP | 真实 stdio 初始化和工具调用；HTTP 能读到 MCP 创建的节点 |
| MCP 闭环冒烟 | `scripts/mcp_smoke.py --from-config` 按客户端配置启动 bridge，29 个工具完成建图、校验、执行、结果、XML 与检查点，见下 |
| XML | XSD、Registry 语义验证、Graph → XML → Graph 等价 |
| Ruff | 静态检查通过 |
| JavaScript | node --check 通过 |
| Agent Skill | skill-creator quick_validate 通过 |
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

## 第四轮：可持久化类别编码（2026-09-15）

`feature.categorical` 现在输出训练特征和 `FeatureTransformer`。编码器保存训练类别、固定输出列、频率/计数/目标映射及 `handle_unknown` 策略；`feature.categorical_transform` 对新数据只做 transform。合并后的特征会保留编码器来源，验证器把它嵌入 `TrainedClassifier`，因此模型经过 pickle、Artifact 磁盘溢写或检查点恢复后，可以直接接收原始类别列。

未知类别的默认策略为 `ignore`：one-hot 为全零、ordinal 为 `-1`、频率/计数为 `0`、目标编码为训练集全局均值；`error` 模式会列出未知值并拒绝预测。缺失类别继续使用 `<missing>`，且推理输出列严格按训练顺序重建。

本轮验收结果：Python **104 passed, 1 skipped**；前端 DOM **4/4**；Chrome **9/9**（组件库 29 个）；部署/MCP **12/12**（36 个 MCP 工具，端到端方案成功）。`tests/test_categorical_encoder.py` 的 9 项测试覆盖 schema 固定、未知类别两种策略、五类编码语义、数值型类别、组件复用、完整 DAG 传递、模型内嵌和 pickle 往返。

手工验收步骤：

1. 启动服务，打开 http://127.0.0.1:8765，加载示例并运行。
2. 点击统计特征、随机森林、曲线节点，核对表格、指标和曲线。
3. 在新方案中拖入数据输入和过滤组件，连接端口并应用参数。
4. 验证缩放、平移、Shift 多选、复制、删除及撤销重做。
5. 导出 XML，再导入，核对节点配置、位置和连接。
6. 执行后保存检查点；修改并重跑；恢复检查点核对旧结果。

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
