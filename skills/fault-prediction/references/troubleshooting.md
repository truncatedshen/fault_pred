# 排错目录

按阶段分组，给出平台真实产出的报错原文、这条护栏为什么存在、以及最小的修法。引号里的报错都是逐字抄的；工具名、参数名与报错文本保持英文，便于你直接对照。

---

## 1. 服务与工作区

| 报错 / 症状 | 原因 | 修法 |
| --- | --- | --- |
| `Unknown operation: …` | 这个版本里没有这个工具 | 重新读一遍工具列表；名字是稳定的，先核对拼写 |
| 连 MCP 桥时报连接错误 | HTTP 服务没在跑 | 桥是个薄代理：先启动服务（`python -m fault_platform serve`），并把这件事报告给操作者 |
| 等待工具返回 `CONTROL_API_TIMEOUT` | 客户端等超时了，**服务端可能还在算** | 先 `get_pipeline_status` 看真实状态；**不要重启服务**，内存状态会全丢 |
| 等待工具返回 `CONTROL_API_UNAVAILABLE` | 桥确实连不上服务 | 让操作者启动服务后重试；只有这一步才需要重启 |
| 重启之后一切为空 | 方案/工作区/检查点都在内存里 | 重新载入 `storage_root` 下的 XML；重新上传原先不在 `data_root` 里的数据 |
| `Reading workspace … which is not the pipeline's latest (…).` | 你传了更旧的 `workspace_id` | 省略 `workspace_id` 去读最新那次运行 |
| 改了参数结果却没变 | 读到了缓存结果或过期工作区 | 查 `get_pipeline_status`；重跑那个节点 |
| 运行成功了节点却显示 `PENDING` | 之后编辑过图（"Graph changed; run to refresh results"），或产物被驱逐 | 重跑；增量复用让它很便宜 |
| `Checkpoint outputs were released; affected nodes recompute.` | 检查点引用的产物已经不在了 | 重跑受影响节点，或重新存一次检查点 |
| 出现重名工具 `list_pipelines_1`、`save_pipeline_2` | 客户端发现层的别名，不是服务端功能 | 用不带后缀的名字 |
| 产物缓存报了意外的 `evictions`/`spills` | 字节预算（或没有溢出目录）在起作用 | 如实报出统计；和操作者一起调 `--artifact-cache-mb` 或 `--artifact-spill-dir` |

## 2. 数据访问

| 报错 / 症状 | 原因 | 修法 |
| --- | --- | --- |
| 路径被拒，或数据集为空 | `data.input.path` 必须**相对 `data_root`**；绝对路径与 `..` 都会被拒 | `list_datasets()` → 用列出来的相对路径 |
| 文件不在 `list_datasets` 里 | 它在 `data_root` 之外 | 请操作者上传（网页按钮写到 `data_root/uploads/`），或用 `--data-root` 重启服务 |
| `Parquet needs the pyarrow extra` | 服务安装时没带 Parquet 支持 | 告诉操作者要装哪个 extra，或先转成 CSV |
| dtype 莫名其妙 / 解析报错 | 二进制或空白分隔的文件被改名成 `.csv` | 老老实实转换（见 `SKILL.md` §2） |
| 读文件时内存暴涨 | 整份文件被读进来了 | 给读取加边界：`columns`、`max_rows`、Parquet `filters`，或 `streaming` |

## 3. 图编辑

| 报错 / 症状 | 原因 | 修法 |
| --- | --- | --- |
| `Incompatible port types: X -> Y` | 类型化端口契约 | 补上缺的那一步（`feature.select`、窗口组件、`data.materialize`） |
| `Target input already connected` | 一个输入端口只能有一个生产者 | 删掉旧连线，或用 `feature.merge` 做扇入 |
| `Unknown source output or target input port` | 端口名写错 | 用 `get_component_schema` 里的端口名（`dataset`、`features`、`labels`、`metrics`、`encoder` 等） |
| `Connection creates a cycle` | 图必须是有向无环图 | 重新组织；这里没有回边 |
| `Graph changed in another client; reload before editing` | `replace_pipeline` 的 `expected_version` 过期 | 重新 `get_pipeline`，再用新版本覆盖 |
| `Save path must be an XML file within the pipeline storage directory` | `save_pipeline` 把写入限制在 `storage_root` | 传一个纯文件名，或 `storage_root` 内的路径 |
| 节点删不掉 | 它还有连线 | 先 `disconnect_components`（或连着边一起重连） |
| `add_components rejected entry 3 of 8 (…)` / `connect_many rejected entry …` / `configure_components rejected entry …` | 批量里某一条非法（参数错、id 重复、端口名错、成环） | 报错已经点名是第几条、哪个组件，并且**整批已回滚**（什么都没生效）；改好那一条再重发整批 |
| 校验时报 `Pipeline is empty` | 还没加节点 | 先加节点再校验 |
| `…: required input not connected` | 某个必填输入没有生产者 | 接上它——报错已经点名节点与端口 |

## 4. 质量与窗口

| 症状 | 原因 | 修法 |
| --- | --- | --- |
| 以为"稀疏处会切出空窗口" | 按时间切窗的锚点是**真实采样**，窗口里至少含那一条 | 窗口不会是空的；要担心的是"**薄**"。看 `attrs.window_thin_windows`（<3 行的窗口数），>10% 时平台会自己警告 |
| 窗口的行数忽多忽少（从 1 行到上千行） | 数据疏密不均，按时间切窗刻意允许这样 | 要么放大 `window_span`、要么设 `min_window_rows` 丢掉薄窗口（**丢弃并计数**，不静默），要么改用按行切窗 |
| 明明写了 `step_span=30m`，窗口却隔了几天 | `start` 跳到"第一条 ≥ 锚点+步长的采样"；采样比步长稀时，步长被顶替成"下一条采样" | 看 `window_id` 里的锚点秒数核对实际间隔；要均匀时间轴就先把数据重采样到规则网格 |
| `data.quality` 发现恒定/保持值通道 | 量化了的工艺点位，或卡死的传感器 | 从 `columns` 里去掉，或保留并说明理由 |
| 平窗口比例很高，而 `missing_rate = 0` | 保持值产生不了频谱 | 预期会出现 NaN 频域特征：填补它们，或丢掉这个通道 |
| `strict` 标签策略让运行中止 | 窗口内标签发生变化——起始点数据的常态 | 改用 `label_policy="mode"`（或 `last`） |
| 窗口特征看起来每组只有一行 | `window_size=0` 的含义是"整组" | 设一个真实的 `window_size` 与 `step` |
| 窗口数远少于预期 | 不完整的尾部窗口被丢弃，`step` 控制重叠程度 | 重算预期值：每组大约 `rows/step`（再减掉尾部） |
| 模型处出现标签长度不匹配 | 把行级标签接到了窗口特征上（`data.labels`） | 从窗口组件取 `labels` |

## 5. 特征

| 报错 / 症状 | 原因 | 修法 |
| --- | --- | --- |
| `Feature indices must match exactly` | 要合并的分支来自不同的窗口 | 让两条分支使用完全相同的参数 |
| `Feature provenance differs: source_rows` | 其中一条分支过滤/重采样/改了窗口 | 用同样的方式重建两条分支 |
| `Feature provenance differs: groups` / `assets` / `source_path` / `source_id` | 原因同上，只是换了字段 | 修法同上——合并是在告诉你"这些行不是同一批行" |
| `Feature names overlap; rename before merging` | 两条分支产出了同名列 | 去掉一条分支，或只选不相交的列 |
| 模型报错并点名 NaN 列 | 平窗口／过短的组／原始缺失 | 在模型前插入 `feature.imputation` |
| 有一段行的频域特征是 NaN | 默认 `flat_policy=nan` 为平窗口保住了行对齐 | 填补它们；当没有东西需要合并时可用 `flat_policy=skip` 丢掉这些窗口 |
| 只有窗口两端出现 NaN | Hann 窗两端为零，只在边缘变化的窗口没有频谱 | 这是预期行为，按平窗口同样处理 |
| `sampling_rate` 被拒，或频率结果荒谬 | 它必填，且必须是真实采样率 | 问操作者；**绝不猜** |
| `feature.categorical_transform` 拒绝旧编码器 | 编码器来自不同的列或设置 | 用 `feature.categorical` 重新拟合，或传入同一方案里的编码器 |
| 模型表现好得可疑 | `feature.score_select`/`feature.pca` 在全量行上拟合过 | 挪到探索分支，或把泄漏警告一并汇报 |

## 6. 验证

| 报错 / 症状 | 原因 | 修法 |
| --- | --- | --- |
| `Group split requires feature extraction with group_column` | 上游没有 `group_column` | 在窗口组件上设上它并重跑 |
| `Asset split requires asset_column on the upstream window component (it records which asset each window belongs to)` | 资产信息没有跟着特征走 | `data.asset_key` → 每个窗口组件设 `asset_column` → 重跑 |
| `Asset split needs at least two assets, found N` | 资产列恒定或推导错了 | 检查推导（`separator`、`index`、`pattern`） |
| `Overlapping windows require group or temporal split` | 有重复组却用了 `stratified` | 换成 `group`、`asset` 或 `temporal` |
| `positive_class … is not among the labels […]` | 标签拼写或顺序不对 | 用报错里给出的取值 |
| `ROC-AUC unavailable: the holdout split does not contain all classes.` | 留出集里缺某个类 | 这是数据问题（实例/资产太少），不是代码问题 |
| `ROC-AUC unavailable: this model does not provide probabilities.` | 例如 SVM 设了 `probability=false` | 接受它，或设 `probability=true` |
| `validation.compare` 拒绝输入 | 两次运行的测试索引不同 | 让 features/labels/split/test_size/random_state 完全一致 |
| 从 `group` 换到 `asset` 后分数塌了 | 这才是诚实的数字，实例级那个偏乐观 | 两个都报，并把资产级放在前面 |
| `test_assets_unseen = 0` | 每个测试资产都在训练里出现过 | 这个"留出"不是你要的那个；修切分 |

## 7. 执行

| 报错 / 症状 | 原因 | 修法 |
| --- | --- | --- |
| `execute_pipeline` 只返回 `RUNNING` | 执行是异步的 | `wait_for_pipeline(pipeline_id, timeout_seconds=…)` |
| `timed_out: true` | 运行还在继续 | 用更大的超时再等一次，或查状态/历史 |
| `wait_for_pipeline` 立刻返回 `started: false` | 根本没有在途任务：方案没启动过，或被改图失效 | 回头读 `execute_pipeline` 的返回（`success` 与 `summary` 写着为什么没跑起来），修好后再执行；不要盲目重等 |
| `success: true` 但 `status: FAILED` | 控制成功不等于运行成功 | 读 `errors` / 逐节点状态 |
| 节点 `SKIPPED` 且提示 `Upstream results unavailable` | 上游节点失败了 | 先修上游；下游不会跑 |
| `… cannot consume streamed input; insert data.materialize or turn streaming off on data.input` | 全局组件遇到了流式数据集 | 按报错做；并把这条警告留在汇报里 |
| 改完图立刻运行就失败 | 参数对新图形状非法 | 下次先 `validate_pipeline` |
| `Invalid execution mode` | `mode` 只能是 `all`、`node`、`from` | 用 `execute_node` / `execute_from_node` 包装 |
| 取消之后节点留在 `CANCELLED`/`PENDING` | 取消是协作式的 | 准备好了再重跑 |
| 改一点点就全部重算 | `incremental=false`，或改动节点在所有东西的上游 | 用 `incremental=true`（默认），并改最窄的那个节点 |

## 8. 读取结果

| 症状 | 原因 | 修法 |
| --- | --- | --- |
| 响应特别大 | `include_indices=true`，或 `limit` 太大 | 保持索引折叠；指标里已经有类别计数与混淆矩阵 |
| `preview` 比 `shape` 短 | 这是设计上的上限（`limit` 与 50 列） | 只有当行本身是交付物时才调大 `limit`（最大 100） |
| 拿到 `kind: streamed` 而不是表 | 打开了流式 | 预期如此；统计能流式，原始行留在磁盘上 |
| 图不见了 | `PlotArtifact` 是给设计器用的，不走 MCP | 告诉用户打开网页看这张图 |
| 你怀疑的那次运行 warnings 却是空的 | 有些检查只有你配置了才存在（`data.quality`、资产列、填补） | 补上检查再跑；没有警告不等于没有问题 |

## 9. 性能（大表上哪些节点慢、为什么）

| 症状 | 原因 | 修法 |
| --- | --- | --- |
| 10 万行左右"整体很慢"，`feature.statistical` 尤其慢 | 统计量越多，每个"窗口 × 列"的固定开销越明显（曾经每次调用都现搭 30 个 lambda 的函数表，还无条件先算 rms） | 已在 2026-09-18 修掉：`_stat` 改成模块级查表、偏度/峰度改手写矩公式（与 `scipy.stats` 默认口径一致）、重复类特征改 `np.unique`。**先把统计量收敛到真正需要的几个**，10 个统计量相比 4 个仍要 3.7 倍时间 |
| `visual.overview` / `feature.imputation` 在**小特征表**上也慢 | 曾经 `numeric_columns` 用 `select_dtypes`、覆盖信息用逐窗口行号列表，两者都会让 pandas 每次 `__finalize__` 深拷贝 attrs | 已修（区间化 + 不可变类型）。如果升级后又变慢，先量 `get_node_result` 里该节点的 `execution_time`，再看是不是有人把 attrs 塞进了新的大对象 |
| `validation.*` 慢 | `RandomForestClassifier`/`XGBClassifier` 固定 `n_jobs=1`（服务端并发由运行时统一控制） | 这是设计取舍：想更快就减少 `n_estimators`、或在 `execute_pipeline` 里让多个模型并行（不同方案可并行，同一方案按拓扑顺序） |
| 怀疑慢在"切窗口"本身 | 切窗口是 O(窗口数 × 窗口长度) 的切片，实测 108k 行 / 3580 窗口只要约 0.2s | 不是它。**先量各节点的 `execution_time`**（`get_history` 或网页结果面板），再决定优化谁 |

---

## 护栏，以及它们为什么存在

下面这些是刻意的拒绝，不是 bug。知道它们的意图，就不会去"修"它们：

1. **类型化端口** —— 阻止原始 `Dataset` 被当成特征悄悄喂进模型。
2. **一个输入一个生产者** —— 输入含糊本身就是建模 bug；`feature.merge` 把 join 显式化并校验来源。
3. **校验来源的合并** —— 防止行错位，这是最经典的静默失败。
4. **资产切分的准入要求** —— 平台拒绝把"按实例切分"假装成"按资产切分"。
5. **`strict` 标签策略** —— 让混合标签窗口暴露出来，而不是把一次故障起始悄悄平均掉。
6. **默认 `flat_policy=nan`** —— 保住行对齐（下游合并仍然可用），同时让退化通道可见。
7. **用警告而不是静默成功** —— 泄漏、驱逐、子集与过期工作区的提示，是平台在如实交代自己做了什么。
8. **预览与结果的有界性** —— 让 MCP 载荷小到 Agent 读得动，同时运行时仍能按引用拿到完整产物。
