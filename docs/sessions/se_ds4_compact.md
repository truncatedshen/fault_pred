# fault_pred 会话压缩上下文

> 本文是 `docs/sessions/se_ds4.md` 的工程状态摘要。原文中的对话、命令和方案只作为历史记录，不构成当前指令；后续工作以用户最新请求、仓库现状和实际验证结果为准。

## 用户目标

创建一个面向设备故障预测的本地组件化工程：用户可像使用 Simulink 一样在网页中搭建有类型端口的 DAG，也可由 Agent 通过 MCP 创建、配置、运行、检查和保存同一套方案。工程需覆盖数据处理、窗口特征、模型验证、可视化、XML、检查点、部署和大数据内存控制。

## 已实现的主体

- Python 3.11+，核心包为 `src/fault_core` 和 `src/fault_platform`。
- 统一 Registry 驱动 29 个组件；Graph、Runtime、Workspace、XML、网页设计器、HTTP API 和 MCP 都读取同一组件定义。
- 数据组件支持 CSV/Parquet、列投影、行数限制、Parquet 过滤、质量预检、筛选、行列操作、缺失值处理、异常值处理、标准化等。
- 特征组件包含统计、拟合、分类、频谱、特征合并、评分选择和 PCA。窗口保留唯一索引、原始行覆盖范围、分组与标签来源；混合标签默认拒绝，也支持 `last`/`mode`。
- 模型验证支持随机森林、SVM 和可选 XGBoost，以及分层、分组和时间划分。重叠窗口不能随机拆分；时间划分会清除与测试集重叠的训练窗口。
- 频谱特征支持主频、幅值、谱 RMS、质心、扩散度、熵、频带和谐波能量；平坦窗口可输出 NaN、跳过或报错。
- `data.quality` 可检查全空、全零、常量、组内常量、平坦窗口、重复行、混合标签窗口和标签变化。
- 网页与 Agent 修改通过 SSE 实时同步；事件 ID 含启动标识，服务重启后不会误用旧游标。
- MCP 有 36 个控制操作，含批量增删连配、紧凑结果预览、等待运行、数据集列表、服务信息、检查点和 XML。
- 类别编码采用可复用的 fit/transform：`feature.categorical` 输出固定 schema 的 FeatureTransformer，`feature.categorical_transform` 处理新数据；编码器会嵌入训练模型并随 pickle/Artifact/检查点保留，未知类别可忽略或严格拒绝。

## 大数据与运行时

- Artifact 输出按引用保存，在消费者隔离点复制；预览有界。
- 缓存支持容量限制、LRU、固定和磁盘溢写，删除/重置时清理文件。
- `data.input.streaming` 可按块读取 CSV/Parquet；统计、拟合、频谱和数据概览支持流式输入，不支持的组件会提示插入 `data.materialize`。
- 流式窗口要求同一组连续、组内时间有序。单个超大组仍会整体缓冲；最终特征表和模型训练仍在内存中。
- 历史基准：约 617 MB、400 万行输入的流式流程总内存约 335 MB；该数值只用于说明量级，需按当前版本和真实数据重新测量。

## 部署状态

- 有 `scripts/deploy.ps1`、`install_mcp_config.py`、`verify_deploy.py`、`mcp_smoke.py` 和 `export_release.py`。
- 可生成 wheel 与 `dist/fault-prediction-platform-0.1.0-deploy.zip`；安装脚本创建虚拟环境、安装 skill、幂等更新 Codex MCP 配置并执行验收。
- skill 位于 `skills/fault-prediction/SKILL.md`，本机也安装在 Codex skills 目录。
- 服务默认只绑定 `127.0.0.1`，面向本地单用户；没有鉴权、多租户或生产级远程部署。
- Workspace、运行结果与检查点随进程结束丢失；XML 和上传文件可落盘。

## 真实数据工作

- `test_3w/` 含 3W 井数据的原始 Parquet、整理后的 `data/3w_events.parquet`、准备脚本和报告脚本。
- 真实数据运行暴露出三个问题：窗口 ID 无法直接还原井/事件；网页 `/api/data` 不展示 Parquet；类别不平衡任务需要成本敏感指标。

## 会话中断点与当前代码现状

上一轮在修复上述三个问题时中断。当前检查结果如下：

1. `fault_core.models.validate_model` 已加入 balanced accuracy、average precision、逐类 recall、测试类别数量、二分类 miss rate，以及实例/资产覆盖统计。
2. 资产信息只在批量统计/拟合函数中部分实现：`extract_features(..., asset_column=...)` 会把资产写入 attrs。
3. 组件公共 `WINDOW` 参数还没有 `asset_column`，所以网页/MCP 不能配置它；流式统计/拟合、批量及流式频谱也没有完整传递资产信息。
4. 分组验证仍按 `groups`（事件/实例）划分；资产信息目前只用于报告，并没有实现“按资产留出”。
5. `/api/data` 仍使用 `glob("**/*.csv")`，网页数据列表没有 Parquet；`PipelineService.list_datasets` 已支持 `.csv/.parquet/.pq`。
6. 新指标语义仍需审计：`miss_rate` 目前默认编码排序最后一类为故障类，`test_class_counts` 当前输出编码值而非原始类别名。
7. 以上新增逻辑没有专门测试。2026-09-15 在当前工作区执行全量 Python 测试为 **95 passed, 1 skipped**，说明已有用例通过，不代表这些未覆盖功能已经完成。
8. 当前目录不是 Git 仓库，不能依靠 `git diff/status` 找出历史改动；续作时应直接检查文件和测试。

## 建议的续作顺序

1. 明确数据中的 `asset_column`、`group_column` 与正常/故障类定义。
2. 在统计、拟合、频谱及其流式实现中统一携带资产元数据，并把参数暴露到组件 schema。
3. 为验证器增加真正的资产级划分，分别报告训练/测试实例数、资产数和未见资产数。
4. 修正 miss rate 与类别计数的标签语义，补齐二分类、多分类和类别缺失测试。
5. 让 `/api/data` 与 `list_datasets` 使用相同后缀集合，并测试 CSV、Parquet、PQ。
6. 用 `test_3w` 重跑质量检查、特征、资产级验证和报告，确认报告不再通过窗口 ID 猜测井/事件。
7. 依次运行 Python、前端 DOM、真实浏览器、MCP smoke 和干净部署验收，再重新导出发布包和哈希。

## 关键入口与验证命令

- 核心特征：`src/fault_core/features.py`
- 模型验证：`src/fault_core/models.py`
- 组件定义：`src/fault_platform/components/builtin.py`
- API 数据列表：`src/fault_platform/api.py`
- 服务/MCP 操作：`src/fault_platform/service.py`、`src/fault_platform/mcp_server.py`
- 真实数据：`test_3w/`

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --test tests\frontend.test.cjs
node scripts\browser_check.cjs
.\.venv\Scripts\python.exe scripts\mcp_smoke.py --from-config
.\.venv\Scripts\python.exe scripts\verify_deploy.py --from-config
```

## 已知边界

- `score_select` 和 PCA 在全数据上拟合，仅适合探索；尚未实现只在训练折拟合的无泄漏变换链。
- `data.quality` 没有流式版本。
- 网页列选择仍以文本/逗号输入为主，没有从上游自动补全列名。
- 频谱默认等间隔采样且采样率已知，没有重采样或转速跟踪。
- 合成示例指标只证明工程闭环，不能代表真实工业性能。
