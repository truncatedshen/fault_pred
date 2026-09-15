# 故障预测组件化建模与执行平台 · 项目设计文档

**Fault Prediction Component Platform · Fault Studio v0.1**

| 项目 | 内容 |
| --- | --- |
| 文档性质 | 系统设计文档（架构、对象模型、执行语义、接口与约束） |
| 对应实现 | `src/fault_core`、`src/fault_platform`（版本 0.1.0） |
| 组件规模 | 29 个内置原子组件，5 个分类 |
| 主要读者 | 平台开发者、算法工程师、集成 Agent 的工程师、评审者 |
| 相关文档 | [总体架构摘要](architecture.md) · [组件参考](components.md) · [MCP 接入](mcp.md) · [验证记录](validation.md) · [README](../README.md) |

本文描述系统的**实际设计**，不是设想：所有对象字段、校验规则、状态机与接口都对应现有代码。文档中的数字（组件数量、预览上限、状态取值、错误结构）均可由源码和测试核对。

---

## 1. 设计目标与非目标

### 1.1 一句话定位

把故障预测方案拆成**可复用、可连接、可配置的原子组件**；人工拖拽与 AI Agent 自动搭建**编辑同一个 ComponentGraph**，由同一个 Runtime 执行，并可持久化为 XML。

### 1.2 设计目标

| 编号 | 目标 | 落地方式 |
| --- | --- | --- |
| G1 | 复杂方案由原子组件组合而成 | 29 个单一职责组件 + 类型化端口 |
| G2 | 人工与 Agent 使用同一底层系统 | 网页与 MCP 都调用同一 Pipeline Control API，操作同一 Graph/Workspace |
| G3 | 组件可插拔，扩展不触碰核心 | Registry 统一注册；Graph/Runtime/XML/UI/MCP 无组件分支 |
| G4 | 方案结构可持久化、可交换、可评审 | XML + XSD + Registry 语义校验，参数以 JSON 保类型 |
| G5 | 正确性由 Runtime 保证，而非 LLM | 端口、参数、依赖、数据源、泄漏检查全部在 Runtime 侧 |
| G6 | 数值能力可独立复用 | `fault_core` 纯 Python 库，不依赖任何平台对象 |
| G7 | 中间结果可见、可追踪、可复现 | Workspace + Artifact + 执行历史 + 检查点 + 指纹 |

### 1.3 非目标（首版明确不做）

- 不是"一个随机森林脚本"，也不是"故障预测大模型"；模型层只承担**特征有效性验证**。
- 不把每个原子组件暴露成独立 MCP Tool；MCP 只提供**高层图操作**。
- 不做 AutoML、深度网络、RUL/生存分析（首版为分类验证）。
- 不做多用户、鉴权、分布式队列；服务只监听 `127.0.0.1`，面向本机单用户。
- 不把大规模 DataFrame/模型权重放进 XML 或 LLM 上下文。
- 不自动把"当前故障识别"转成"未来故障预测"；预测标签与视界必须由使用者定义。

### 1.4 核心设计原则

1. **Component 是系统最核心的抽象**，一个组件对应一种原子能力。
2. Python 函数 ≠ Component；Component ≠ MCP Tool。
3. 组件之间只通过**带数据类型的 Port** 交换数据，不互相持有引用。
4. 参数必须 Schema 化，UI 表单与校验都由 Schema 推导。
5. Registry 是组件定义的**唯一来源**：UI、Runtime、XML、MCP 都读它。
6. ComponentGraph 表示方案结构，Workspace 保存运行数据，两者严格分离。
7. XML 存配置，不存数据。
8. LLM 只负责理解目标、规划方案、选择组件、解释结果；Runtime 负责正确性。
9. 新增组件不需要修改 Graph / Runtime / XML / Visualizer / MCP 核心。
10. 没有 LLM 时，系统仍应能被人工完整使用。

---

## 2. 总体架构

### 2.1 分层视图

```
                        ┌───────────────────────────┐
                        │           用户            │
                        └─────────────┬─────────────┘
             ┌────────────────────────┴────────────────────────┐
             ▼                                                 ▼
   Visual Designer（网页）                          AI Agent（Codex / Claude / …）
             │                                                 │
             │                                       Fault Prediction Skill
             │                                                 │
             │                                        MCP stdio bridge
             └────────────────────────┬────────────────────────┘
                                      ▼
                          Pipeline Control API（HTTP）
                                      │
                                      ▼
                             ComponentGraph（+版本号）
                                      │
             ┌────────────────────────┼────────────────────────┐
             ▼                        ▼                        ▼
    ComponentRegistry        XML 序列化 / 校验         ExecutionEngine
             │                        │                        │
             ▼                        ▼                        ▼
      组件定义与参数表            pipeline XML        FaultWorkspace / Artefact
                                                                  │
                                                                  ▼
                                                     fault_core（纯数值库）
```

### 2.2 两条入口，一个模型

| 入口 | 路径 | 最终产物 |
| --- | --- | --- |
| 人工 | 网页 → 拖拽/连线/配置 → `replace_pipeline` → Graph | ComponentGraph + XML |
| Agent | Skill → MCP → 控制 API → `add_component` / `connect_components` | 同一个 ComponentGraph + XML |

两个入口产生**结构完全相同**的 Graph，因此可以互相接力：Agent 搭好骨架，人工在网页微调参数；或人工搭好，Agent 批量替换组件。

### 2.3 依赖规则（分层不变量）

| 层 | 模块 | 允许依赖 | 禁止依赖 |
| --- | --- | --- | --- |
| L0 数值 | `fault_core/*` | numpy / pandas / scipy / sklearn | `fault_platform`、UI、MCP、Agent |
| L1 抽象 | `components/base.py`、`registry.py` | 标准库、L0 | `graph`、`runtime`、`service`、`web` |
| L2 图与执行 | `graph.py`、`runtime.py`、`workspace.py`、`xml_io/` | L0、L1 | `service`、`web`、`mcp_server` |
| L3 控制面 | `service.py`、`api.py`、`cli.py`、`mcp_server.py` | L0–L2 | 前端 JS |
| L4 前端 | `web/` | L3 的 HTTP 接口 | 直接访问 L2 对象 |

实现细节：`components/base.py` 只在 `TYPE_CHECKING` 下引用 `runtime.ExecutionContext`，因此 L1 对 L2 没有运行时依赖，避免了循环导入；`runtime.py` 通过 `workspace.py` 的数据结构工作，不反向依赖 `service.py`。

### 2.4 模块职责矩阵

| 模块 | 职责 | 不负责 |
| --- | --- | --- |
| `fault_core` | 数值计算、数据校验、特征窗口、模型训练与指标 | 图结构、状态、持久化 |
| `components/base.py` | 组件契约：端口、参数、元数据、执行接口 | 具体算法、IO、UI |
| `components/builtin.py` | 29 个内置组件的薄适配层 | 数学实现（在 fault_core） |
| `registry.py` | 组件注册、目录、检索、Schema 导出 | 组件实例状态 |
| `graph.py` | 节点/连线/DAG 校验/拓扑排序/克隆/序列化 | 运行数据 |
| `runtime.py` | 执行调度、输入解析、指纹、失败传播 | 组件内部算法 |
| `workspace.py` | 运行数据、状态、历史、检查点、Artifact | 方案结构 |
| `xml_io/` | Graph ↔ XML、XSD 与语义校验 | 运行数据 |
| `service.py` | 控制 API（编辑/执行/查询）、并发与取消 | UI 渲染 |
| `api.py` | HTTP 服务、静态资源、上传、本地来源限制 | 业务逻辑 |
| `mcp_server.py` | MCP 工具注册与 stdio 转发 | 独立实现另一套逻辑 |
| `web/` | 可视化编辑、参数表单、结果查看 | 任何数值算法 |
| `skills/` | 教 Agent 如何专业地搭方案 | 计算数据 |

---

## 3. 核心对象模型

### 3.1 BaseComponent

**职责**：声明一个可复用计算单元的契约，保存配置，不保存运行数据。

```python
class BaseComponent(ABC):
    metadata: ClassVar[ComponentMetadata]
    input_ports: ClassVar[tuple[InputPort, ...]] = ()
    output_ports: ClassVar[tuple[OutputPort, ...]] = ()
    parameter_schema: ClassVar[tuple[ParameterDefinition, ...]] = ()

    def __init__(self, component_id: str | None = None, parameters: dict[str, Any] | None = None)
    def validate(self, require_complete: bool = True) -> None
    def configure(self, parameters: dict[str, Any]) -> None      # 失败时回滚
    def reset(self) -> None                                       # 恢复参数默认值
    def preflight(self, context: ExecutionContext) -> None         # 外部资源预检
    def external_fingerprint(self, context: ExecutionContext) -> str  # 外部资源指纹
    def serialize(self) -> dict[str, Any]
    @classmethod
    def deserialize(cls, value: dict[str, Any]) -> BaseComponent
    @classmethod
    def schema(cls) -> dict[str, Any]                            # 供 UI / MCP 使用
    @abstractmethod
    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> ComponentResult
```

| 约束 | 说明 |
| --- | --- |
| 组件不保存大型运行数据 | 输出通过 `ComponentResult` 返回，由 Workspace 接管 |
| 组件不知道 MCP / LLM / Skill / Visualizer | 只依赖 `fault_core` 与标准库 |
| 参数初始化即校验 | 构造时以 `require_complete=False` 校验，允许"尚未填完"的中间态 |
| 未知参数直接拒绝 | `validate` 对未在 Schema 中声明的参数抛错 |

`ComponentResult` = `{outputs: dict[str, Any], warnings: list[str]}`；`outputs` 的键必须落在 `output_ports` 内（Runtime 强制检查）。

### 3.2 InputPort / OutputPort

```python
@dataclass(frozen=True)
class InputPort:
    name: str
    data_type: DataType
    required: bool = True
    description: str = ""
```

`OutputPort` 同构。端口是轻量不可变值对象，用于：图连接校验（静态）、运行时值校验（动态）、UI 连线绘制与参数面板标注。

### 3.3 ParameterDefinition

```python
@dataclass(frozen=True)
class ParameterDefinition:
    name: str
    type: str = "string"
    default: Any = None
    required: bool = False
    display_name: str = ""
    description: str = ""
    min: float | None = None
    max: float | None = None
    options: tuple[Any, ...] = ()
    allow_multiple: bool = False

    def validate(self, value: Any) -> None
```

校验语义（**不做隐式类型转换**）：

| 类型 | 规则 |
| --- | --- |
| `integer` | 必须是 `int` 且非 `bool`；受 `min`/`max` 约束 |
| `float` | 必须是数字且非 `bool`；必须有限；受 `min`/`max` 约束 |
| `boolean` | 必须是 `bool` |
| `string` / `column` / `expression` / `enum` | 必须是 `str`；`enum` 还要求属于 `options` |
| `object` | 必须是 `dict` |
| `column_list` / `feature_list` / `list` / `allow_multiple` | 必须是 `list`；列名类要求元素为 `str`；`required` 时不能为空；有 `options` 时要求子集 |
| `any` | 不做类型约束（用于过滤条件值等场景） |
| 通用 | `required` 且为 `None` → 报错；必填字符串不能全空白；数值必须有限 |

### 3.4 ComponentMetadata

`component_type`（全局唯一主键，如 `feature.spectral`）、`display_name`、`category`、`description`、`version`、`subcategory`、`tags`。`category` 决定 UI 分组与图标；`tags` 参与检索；`version` 参与 XML 兼容校验。

### 3.5 ComponentRegistry

**职责**：组件定义的唯一来源，也是"新增组件不改核心"的支点。

```python
registry.register(ComponentClass)      # 校验类型唯一、端口名与参数名不重复
registry.unregister(type_name)
registry.get(type_name) -> type[BaseComponent]
registry.create(type_name, component_id=None, parameters=None) -> BaseComponent
registry.list(category=None, query="", tags=None, input_type=None,
              output_type=None, limit=100, include_schema=True) -> list[dict]
registry.search(query, **filters) / registry.filter(**filters)
```

检索维度对齐"组件数量会增长到上百个"的前提：`category`、关键词（类型名/显示名/描述/标签联合匹配）、`tags`（子集匹配）、`input_type`/`output_type`（端口类型过滤）、`limit`（钳制在 0–500）、`include_schema`（是否需要完整参数表）。因此 Agent 不必一次拉取全部 Schema。

### 3.6 ComponentGraph / ComponentNode / Connection

```python
@dataclass
class ComponentNode:
    component: BaseComponent
    position: dict[str, float]      # {"x": …, "y": …}
    ui: dict[str, Any]              # 前端展示信息（标签、折叠等）

@dataclass(frozen=True)
class Connection:
    source_node: str
    source_port: str
    target_node: str
    target_port: str
```

`ComponentGraph` 主要方法：

| 方法 | 语义 |
| --- | --- |
| `add_node` / `remove_node` / `get_node` | 节点增删查；删除节点会清理相关连线 |
| `configure(node_id, parameters)` | 参数更新（组件层失败回滚） |
| `connect` / `disconnect` | 连接管理；`connect` 校验端口存在、类型一致、目标输入未被占用、不产生环 |
| `validate_graph(require_complete=True)` | 汇总错误列表（不抛异常，供 UI/API 展示） |
| `detect_cycle()` / `topological_sort()` | Kahn 算法；有环抛 `Graph contains a cycle` |
| `descendants(node_id)` | 下游可达集合，用于"从节点执行"与失效传播 |
| `clone()` / `serialize()` / `deserialize()` | 深拷贝与结构化往返 |

图只保存配置（节点、参数、位置、连线、元数据、版本）；**不保存任何运行数据**。

### 3.7 FaultWorkspace / ExecutionHistory / Checkpoint / Artifact

```python
class FaultWorkspace:
    workspace_id: str          # ws_xxxxxxxxxxxx
    pipeline_id: str
    created_at / updated_at: str
    version: int               # 每次 touch() 递增
    status: PipelineStatus
    inputs: dict[str, Any]
    node_results: dict[str, dict[str, str]]     # node → port → artifact://…
    node_status: dict[str, NodeStatus]
    errors: dict[str, dict[str, Any]]
    history: list[ExecutionHistory]
    fingerprints: dict[str, str]
    metadata: dict[str, Any]   # 例如 graph_changed
    warnings / node_warnings: list[str] / dict[str, list[str]]
    artifacts: MemoryArtifactStore
    lock: RLock
```

`node_results` 存的是 **artifact 引用**而非对象本身；`MemoryArtifactStore` 在 `put`/`get` 时 `deepcopy`，保证分支之间不会因共享可变对象而互相污染（复制即隔离）。

更新（P0 内存优化后的实际语义）：

| 操作 | 行为 |
| --- | --- |
| `store_outputs` | 按引用保存组件返回的对象（组件约定：返回后不再修改自己的输出） |
| `get_output(copy=True)` | 默认在消费点复制一份，这是"分支互不影响"的保证（有测试覆盖） |
| `peek` / `get(copy=False)` | 只读访问，用于预览与检查；不复制 |
| `get_node_result` | 走 `peek`，且缺失率只在预览行上计算，不再为预览生成整表布尔副本 |
| `snapshot` / 检查点 | 只保存元数据与 artifact 引用，并对这些引用加 pin；恢复时重新指向同一批 payload |
| `--artifact-cache-mb` | 给每个 Workspace 的 artifact 缓存设字节预算；超限按 LRU 把未被 pin 的条目移出内存 |

移出内存的两种方式：配置了落盘目录（`--artifact-spill-dir`，服务默认 `.fault-platform/artifact-spill`，按会话分目录、退出清理）时**落盘**（pickle，保留 index/attrs，读回后自动加载，结果始终可用）；没有落盘目录时**淘汰**——清空该节点的输出、指纹与错误并回到 `PENDING`，管线状态由 `SUCCESS` 降级为 `READY`，并写入警告。详见 §8.4。

`ExecutionHistory` 字段：`timestamp / node_id / component_type / state_before / state_after / execution_time / input_summary / output_summary / success / error / cached`。

`Checkpoint` 字段：`checkpoint_id / pipeline_id / workspace_id / graph_version / timestamp / completed_nodes / failed_nodes / graph（深拷贝）/ snapshot（Workspace 深拷贝）`。

`WorkspaceManager` 负责工作区生命周期：`create_workspace / get_workspace / delete_workspace / reset_workspace / save_checkpoint / load_checkpoint`；运行中的工作区禁止删除、重置、保存检查点与恢复。

### 3.8 ExecutionContext 与 ExecutionEngine

```python
@dataclass
class ExecutionContext:
    workspace: FaultWorkspace
    data_root: Path
    cancel_event: Event = field(default_factory=Event)

    def resolve_data_path(self, value: str) -> Path   # 必须位于 data_root 内、存在、且为 .csv
```

`ExecutionContext` 是组件访问外部资源的**唯一通道**：数据输入组件只能读取 `data_root` 内的 CSV，路径穿越（`..`）被拒绝。

`ExecutionEngine` 提供三个能力：`validate`（图校验 + 每节点 `preflight`）、`fingerprints`（内容指纹）、`execute`（调度执行）。

### 3.9 PipelineService（控制面）

```python
class PipelineService:
    data_root: Path            # CSV 数据目录
    storage_root: Path         # XML 保存目录
    registry: ComponentRegistry
    graphs: dict[str, ComponentGraph]
    workspaces: WorkspaceManager
    latest: dict[str, str]     # pipeline → 最近使用的 workspace
    engine: ExecutionEngine
    lock: RLock
    pool: ThreadPoolExecutor(max_workers=2)
    jobs: dict[str, Future]
    events: dict[str, Event]
    bus: EventBus                      # 事件总线：图修订、执行状态、检查点
    operations: dict[str, Callable]   # 36 个控制操作，经 pydantic 严格校验
```

设计要点：

- **单一入口**：网页与 MCP 都只调用 `dispatch(operation, arguments)`，因此行为完全一致。
- **参数强校验**：每个操作用 `validate_call(config={"strict": True})` 包装，拒绝多余字段与错误类型。
- **有界返回**：`dispatch` 只返回摘要、结构化指标与有界预览，永不返回原始 Artifact。
- **并发模型**：线程池 2 个 worker，允许两个独立方案同时执行；同一方案在运行期间禁止编辑。
- **执行快照**：`execute_pipeline` 提交的是 `graph.clone()`，因此运行期的外部修改不会污染正在执行的调度。

### 3.10 对象关系总览

```
BaseComponent ──┬── InputPort / OutputPort
                ├── ParameterDefinition
                └── ComponentMetadata
        ▲
        │ register()                       ┌──────────────┐
ComponentRegistry ──────────────────────► │ 组件目录/检索 │
        │ create()                        └──────────────┘
        ▼
 ComponentNode ─┐
                ├──► ComponentGraph ──► XMLSerializer / XMLParser ──► pipeline.xml
 Connection ────┘        │
                         │ execute(ExecutionContext)
                         ▼
                  ExecutionEngine ──► FaultWorkspace ──► Artifact / History / Checkpoint
                         ▲                    │
                         │                    ▼
                  PipelineService ◄── 摘要、指标、有界预览
                         ▲
              ┌──────────┴──────────┐
        HTTP (api.py)          MCP (mcp_server.py)
              ▲                     ▲
          Visual Designer        AI Agent
```

---

## 4. 数据类型系统

### 4.1 类型清单

| `DataType` | 运行时对象 | 运行时校验 | 当前产生者 | 主要消费者 |
| --- | --- | --- | --- | --- |
| `Dataset` | DataFrame | `isinstance(df, pd.DataFrame)` | 数据输入、过滤、行/列操作、规范化、标准化、转换 | 特征组件、探索、可视化、窗口组件 |
| `TimeSeries` | DataFrame | 同上 | （保留） | （保留） |
| `LabelVector` | Series | `isinstance(s, pd.Series)` | 窗口组件的 `labels`、标签向量组件 | 模型验证、标签编码 |
| `FeatureDataset` | DataFrame | 同上 | 特征组件、特征列选择、合并、评分选择、PCA | 模型验证、特征合并、评分选择、PCA |
| `StatisticsResult` | dict | `isinstance(value, dict)` | 集中趋势、离散度量、模型对比、PCA 方差 | 结果面板 |
| `CorrelationMatrix` | DataFrame | DataFrame | 相关性度量 | 结果面板（矩阵着色） |
| `Model` | 带 `predict` 的对象 | `callable(value.predict)` | 随机森林、SVM、XGBoost | 结果面板、后续预测 |
| `Prediction` | DataFrame | DataFrame | 同上 | 结果面板 |
| `Metrics` | dict | dict | 同上 | 模型对比、结果面板 |
| `FeatureImportance` | DataFrame | DataFrame | 随机森林、XGBoost、特征评分选择 | 结果面板 |
| `FeatureTransformer` | 带 `transform` 的对象 | `callable(value.transform)` | 分类特征 | 分类特征变换、模型内嵌预处理 |
| `Visualization` | dict | dict | 数据概览 | 结果面板 |
| `PlotArtifact` | dict（图形规格） | dict | 散点图、折线图 | 结果面板（SVG 渲染） |
| `GenericArtifact` | dict | dict | （保留） | — |

### 4.2 两阶段类型检查

1. **静态（建图时）**：`ComponentGraph.connect` 比较 `OutputPort.data_type` 与 `InputPort.data_type`，不相等即拒绝，例如 `Dataset → FeatureDataset` 会得到 `Incompatible port types: Dataset -> FeatureDataset`。
2. **动态（执行时）**：Runtime 在把上游输出注入组件前，用 `validate_value(value, port.data_type)` 校验真实对象；组件返回后再次校验其输出端口。

静态检查保证图"结构合法"，动态检查保证"数据确实符合声明"，两者缺一不可（例如组件实现写错返回类型时会被立刻拦截）。

### 4.3 为什么不做隐式转换

故障预测流程里，`Dataset`（原始行）与 `FeatureDataset`（窗口特征行）的语义完全不同：前者有逐行标签，后者有窗口来源。若允许隐式转换，最容易出现的错误正是"把逐行标签接到窗口特征上"，而这在工业场景里会直接产出错误的模型评估。因此平台选择让类型不匹配**在建模阶段就失败**，并强制显式使用 `feature.select` / 窗口组件完成转换。

---

## 5. 组件库设计与清单

### 5.1 清单（29 个）

| 分类 | 组件 |
| --- | --- |
| 数据处理 `data` | `data.input`、`data.filter`、`data.row_operation`、`data.column_operation`、`data.normalization`、`data.standardization`、`data.transformation`、`data.labels` |
| 数据探索 `explore` | `explore.central_tendency`、`explore.dispersion`、`explore.correlation` |
| 数据可视化 `visual` | `visual.overview`、`visual.scatter`、`visual.line` |
| 特征提取 `feature` | `feature.statistical`、`feature.fitting`、`feature.spectral`、`feature.categorical`、`feature.categorical_transform`、`feature.select`、`feature.merge`、`feature.score_select`、`feature.pca` |
| 算法验证 `validation` | `validation.random_forest`、`validation.svm`、`validation.xgboost`、`validation.compare` |

完整端口与参数表由 `scripts/export_catalog.py` 从 Registry 生成到 [components.md](components.md)。

### 5.2 组件实现约定

- 组件是**薄适配层**：`execute` 只做参数整理 + 调用 `fault_core`，把返回值包装成 `ComponentResult`。
- 需要外部资源的组件（数据输入）覆盖 `preflight`（提前发现缺文件）与 `external_fingerprint`（文件内容 sha256，用于失效判断）。
- 窗口类组件统一复用 `fault_core.features.windows`，因此窗口索引（`g{i}_w{start}`）、来源覆盖（`source_rows`）、分组信息（`groups`）在所有特征分支中语义一致，这是 `feature.merge` 能安全合并的前提。
- 组件返回值中的 `warnings` 会写入 Workspace 警告与节点警告；数据属性里的 `evaluation_warnings` 会随 DataFrame 传播到模型指标。

### 5.3 注册期校验

`registry.register` 会拒绝：重复 `component_type`、端口名重复、参数名重复。因此"组件定义错误"在**注册阶段**即暴露，而不是等到运行。

---

## 6. 图模型与校验

### 6.1 连接规则

1. 源端口必须是源组件的输出端口，目标端口必须是目标组件的输入端口。
2. 两端 `data_type` 必须完全相等。
3. 一个输入端口最多接一条连线（`Multiple connections to one input`）。
4. 连接后图中不能出现环（`Graph contains a cycle`），违规连接会被回滚。

### 6.2 校验项汇总

`validate_graph(require_complete=True)` 返回错误字符串列表：

| 检查 | 触发条件 |
| --- | --- |
| 组件存在 | 节点类型不在 Registry |
| 参数合法 | 参数缺失/类型错/越界/未知参数 |
| 必填输入已连接 | 存在 `required=True` 且未连线的输入端口 |
| 端口类型兼容 | 任一边两端类型不一致 |
| 输入唯一 | 同一输入端口被多次连接 |
| 无环 | 拓扑排序失败 |
| 非空图 | 完整校验时节点数为 0 |

此外执行前还有：数据源存在（`preflight`）、标签存在、模型参数合法等运行时检查。

### 6.3 版本与乐观并发

- 每次结构编辑（增删节点、改参数、连线）都会 `version += 1`。
- `replace_pipeline(expected_version=…)` 在版本不一致时拒绝整图替换（`Graph changed in another client; reload before editing`），用于避免覆盖并发编辑。
- 检查点恢复后 `graph.version = 当前版本 + 1`，把恢复视作一次新的编辑修订。

---

## 7. 执行语义

### 7.1 状态机

**Pipeline 状态**

| 状态 | 含义 | 进入条件 |
| --- | --- | --- |
| `CREATED` | 已创建，尚未执行 | 新建/编辑后（编辑会把工作区标记 `graph_changed`） |
| `VALIDATING` | 正在做图与数据源校验 | `execute` 开始 |
| `READY` | 校验通过，可执行 | 校验无错误 |
| `RUNNING` | 执行中 | 调度开始 |
| `SUCCESS` | 所有节点成功 | 全部节点 `SUCCESS` |
| `FAILED` | 存在失败或被跳过的节点 | 任一节点 `FAILED`/`SKIPPED` |
| `CANCELLED` | 用户请求停止并生效 | 组件边界检测到取消信号 |

**Node 状态**：`PENDING → READY → RUNNING → SUCCESS`，异常分支为 `FAILED`，上游不可用为 `SKIPPED`。节点状态独立维护，因此支持任意 DAG（含并行分支）。

### 7.2 执行流程

```
ComponentGraph(clone)
   │
   ├─ 1. validate_graph() + 每节点 preflight        → 失败：pipeline=FAILED，记录 _validation 错误
   ├─ 2. fingerprints() 计算每节点内容指纹
   ├─ 3. 清理：移除已删除节点；指纹变化的节点清空输出
   ├─ 4. 选择执行集合（all / node / from）
   ├─ 5. 拓扑序遍历：
   │      ├─ 取消信号？ → CANCELLED
   │      ├─ 上游未成功？ → SKIPPED（记 history，继续其它分支）
   │      ├─ 指纹未变且增量开启？ → 复用缓存（cached=True）
   │      ├─ 解析输入（从 Workspace 取上游 artifact 值，深拷贝）
   │      ├─ 注入校验 → component.execute(inputs, context)
   │      ├─ 输出端口校验 → store_outputs（写入 artifact 引用）
   │      └─ 异常 → 记录 error（类型/消息/堆栈/参数/输入摘要），节点 FAILED
   └─ 6. 汇总 pipeline 状态与去重后的 warnings
```

### 7.3 数据传递语义

- 连线只描述"逻辑来源"：`node_A.features → node_B.features`。
- 运行时通过 `workspace.get_output(node_id, port)` 取值，再以关键字参数注入 `execute(features=value)`。
- 组件之间**不持有彼此引用**；`ArtifactStore` 在写入与读取时都做深拷贝，因此一个分支修改自己的 DataFrame 不会影响另一个分支。

### 7.4 内容指纹与增量执行

每个节点的指纹 = `sha256` over：

```json
{
  "component": {组件类型 + 全部参数},
  "version": "组件版本",
  "inputs": [[目标端口, 上游节点, 上游端口, 上游指纹], …],
  "external": "外部资源指纹（数据输入组件为 CSV 文件 sha256）"
}
```

由此得到三类失效：**改参数**失效该节点及其下游；**改连线**失效目标节点及其下游；**改数据文件内容**失效数据输入及其下游。未失效的节点直接复用缓存输出，实现"依赖感知的增量执行"。

### 7.5 执行模式

| 模式 | 入口 | 语义 | 增量 |
| --- | --- | --- | --- |
| `all` | `execute_pipeline` | 执行全部节点 | 默认开启 |
| `node` | `execute_node` | 只执行该节点，要求上游已有有效输出 | 关闭 |
| `from` | `execute_from_node` / `retry_node` | 执行该节点及其全部下游 | 关闭 |

### 7.6 失败传播、重试与取消

- 节点失败：记录 `error_type / error_message / stack / parameters / input_summary`；节点 `FAILED`。
- 下游节点：因上游非 `SUCCESS` 而标记 `SKIPPED`，并写入历史，**不会导致 Workspace 整体丢失**。
- 独立分支：其它不依赖失败节点的分支继续执行。
- 重试：修改参数后调用 `retry_node`（等价于 `execute_from_node`），只重算必要节点。
- 取消：`cancel_pipeline` 置位 `Event`，Runtime 在**组件边界**检查，因此正在训练的模型会先结束；状态转为 `CANCELLED`。
- 编辑保护：方案运行期间，任何编辑类操作都会因 `Pipeline is running; wait or cancel before editing` 被拒绝。

### 7.7 时间与并发

- 单机线程池 2 worker：允许两个独立方案并行执行，同一个 DAG 内串行。
- 每次执行都基于 `graph.clone()`，执行期间外部编辑不影响本次调度。
- `FaultWorkspace.lock`（RLock）保护状态与历史写入，UI 轮询读取不会读到半更新结构。

---

## 8. Workspace、预览与数据生命周期

### 8.1 存储布局

```
FaultWorkspace
├── inputs            外部输入引用（保留）
├── node_results      node → port → artifact://uuid
├── node_status       node → PENDING/READY/RUNNING/SUCCESS/FAILED/SKIPPED
├── errors            node → 错误对象
├── history           ExecutionHistory 列表
├── fingerprints      node → 内容指纹
├── artifacts         MemoryArtifactStore（深拷贝存取）
├── metadata          例如 graph_changed
└── warnings          全局警告 + 每节点警告
```

### 8.2 有界预览规则

Agent 与 UI 永远拿不到完整数据集，返回内容受三层限制：

| 函数 | 限制 |
| --- | --- |
| `summarize(DataFrame)` | 默认前 20 行 × 前 50 列；返回 `shape / columns / dtypes / missing_rate / missing_rate_rows / index / preview / truncated`；缺失率基于预览行，避免为渲染生成整表布尔副本 |
| `summarize(Series / ndarray / Model)` | 前 `limit` 个值；模型返回类名与特征名列表 |
| `json_safe` | 递归深度 ≤ 8；容器元素 ≤ 100；字符串 ≤ 4000 字符；非有限浮点转 `null` |
| 节点结果 API | `get_node_result(limit=20)`；`get_pipeline_result(limit=10)` 每节点 5 行 |
| 历史 API | `get_history(limit=100)`，上限 200 |

### 8.3 检查点

- 保存条件：工作区非运行态，且方案自上次执行后未被修改（否则提示先执行）。
- 内容：Graph 深拷贝 + Workspace 元数据（状态、节点输出引用、历史、指纹、错误），artifact payload 由引用共享并加 pin（`pinned` 条目不参与淘汰）。
- 恢复：同时恢复 Graph 与 Workspace，且 Graph 版本递增为"新修订"。
- 定位：长时间方案的中途快照、失败恢复、调试、对比实验。

因此检查点的成本从"每个 artifact 再复制一份"降到"一份元数据 + 引用计数"；代价是检查点与运行结果共享同一份不可变 payload——这在执行路径上是安全的，因为组件拿到的是输入副本、产出的是新对象。`delete_checkpoint` 会解 pin 并立即按预算回收。若引用的 payload 已因工作区重置而消失，恢复会降级：受影响的节点回到 `PENDING` 并给出警告，而不是抛错。

### 8.4 缓存溢出：落盘优先，淘汰兜底

配置了落盘目录（`--artifact-spill-dir`，服务默认 `.fault-platform/artifact-spill`，按会话分目录、退出清理）时，超预算的未 pin 条目**落盘**：pickle 序列化（保留 index 与 attrs），读回时自动加载，因此结果始终可读——`evictions` 保持 0。没有落盘目录时才退化为**淘汰**（清空该节点的输出、指纹与错误、状态回 `PENDING`、管线降为 `READY`、写警告）。

两条边界规则：DAG 执行期间缓存暂停溢出（运行中的每个上游输出都还被下游需要），预算只在两次运行之间生效；`peek` 载回某条目时会把该条目排除在本轮溢出之外，避免"加载—溢出"抖动。删除引用、重置或删除工作区、服务退出都会清理对应文件。

### 8.5 输入侧的有界读取

真正的大文件从读入那一刻就要受限，`data.input` 因此提供：

| 参数 | 作用 |
| --- | --- |
| `columns` | 列裁剪：CSV 走 `usecols`，Parquet 走投影（只读需要的列） |
| `max_rows` | 行数上限（0 = 全读）；CSV 走 `nrows`，Parquet 无谓词时逐批流式读取，峰值由批大小而非文件大小决定 |
| `format` | `csv` / `parquet`（也按扩展名推断）；Parquet 需要 `.[parquet]`（pyarrow） |
| `filters` | Parquet 谓词下推，例如 `[["equipment", ">", 10]]`，由 row-group 裁剪减少读入量 |

任何裁剪都会写一条 `evaluation_warnings`（"结果只描述该子集"）并随 DataFrame 传到模型指标，避免把子集指标误当成全量结论。`source_projection` 记录本次实际读入的列、行上限与过滤条件；`source_id` 仍是文件内容指纹，用于缓存失效判断。

裁剪能显著压低读入成本，但不改变"一次运行的工作集随输入规模增长"这一事实——把单次运行也压到常数内存需要分块流式特征提取（见 §18.2 的 P2 项）。

### 8.7 分块流式执行（`streaming`）

打开 `data.input.streaming` 后，输入节点不再读数据，而是返回一个 `StreamedDataset`（路径、格式、投影、`chunk_rows`、谓词）。它仍是 `Dataset` 类型，因此图结构与端口校验不变；差别在消费端：

| 角色 | 行为 |
| --- | --- |
| 支持流式的组件 | `feature.statistical` / `feature.fitting` / `feature.spectral`（窗口族）与 `visual.overview`（单遍聚合）声明 `accepts_streaming = True`，按块消费并产出与批量路径**逐位一致**的结果 |
| 不支持的组件 | 运行时在注入输入时拒绝，并提示插入 `data.materialize`（例如排序/去重/相关性/散点图需要全表） |
| `data.materialize` | 把流式输入读成一张表，附带"内存随输入增长"的警告，用于需要全局操作的场景 |

前置条件（不满足即报错，不静默出错）：每个分组在文件里连续；存在 `time_column` 时组内按时间有序；窗口长度满足组件下限。窗口键与来源信息在批量/流式两条路径上完全一致（`g{组号}_w{起始行}`）。

内存效果来自两处设计：

1. **按组缓冲**：分组模式下只保留当前组的行（外加一个 chunk），组结束即算窗口并释放；无分组时用滚动窗口缓冲（上限为窗口长度）。
2. **来源用区间表示**：批量路径的 `attrs["source_rows"]` 是"每窗口一行行号"的列表（约 36 字节/源行）；流式路径改存 `attrs["source_rows_ranges"]`（`[start, stop)` 区间，约 16 字节/窗口），校验与合并通过 `expand_coverage / windows_share_rows / rows_without_overlap / provenance_matches` 使用区间逻辑，需要具体行号时才展开。因此流式的内存由**输出特征表 + 窗口数**决定，而不是输入行数。

模型训练仍然需要一次性拿到特征表与标签——这是输出规模，不是输入规模：例如 400 万行输入、窗口 64，特征表是 6.25 万行。

### 8.6 生命周期边界

| 数据 | 存储位置 | 服务重启后 |
| --- | --- | --- |
| 方案结构 | 内存 + XML（`.fault-platform/pipelines/*.xml`） | XML 保留，可重新导入 |
| 上传的 CSV | 磁盘（`data_root/uploads/`） | 保留 |
| 节点输出 / 模型 / 预测 | 内存 Artifact | **丢失** |
| 检查点 | 内存 | **丢失** |
| 执行历史 | 内存 | **丢失** |

这是首版刻意的取舍：先保证 Graph/Runtime/XML 的独立性，持久化留给 ArtifactStore 的后续实现（见 §13）。

---

## 9. XML 持久化格式

### 9.1 为什么这样设计

XML 是**方案结构**的交换格式：可读、可 diff、可评审、可版本控制。它不承载运行数据——因此不存在"XML 里塞进 200 MB 特征矩阵"的可能，文件体积有 2 MB 硬上限。

### 9.2 结构

```
faultPredictionPipeline
├── @id            方案标识（identifier 规则）
├── @name          方案名称
├── @version       格式版本（固定 1.0）
├── @graphVersion  编辑修订号（正整数）
├── metadata       JSON 文本（描述、随机种子等自由元数据）
├── nodes
│   └── node @id @type @componentVersion
│       ├── position @x @y                     画布坐标
│       ├── parameters
│       │   └── parameter @name @encoding=json  参数值（JSON 保类型）
│       ├── ports
│       │   ├── inputPort  @name @dataType @required
│       │   └── outputPort @name @dataType @required
│       └── ui         JSON 文本（节点展示信息）
├── connections
│   └── connection @sourceNode @sourcePort @targetNode @targetPort
└── ui               JSON 文本（画布视口等）
```

参数使用 `encoding="json"` 的文本节点，因此 `int`/`float`/`bool`/`null`/`list`/`dict`/字符串都能无损往返，避免"字符串化后类型丢失"这一常见缺陷。

### 9.3 真实片段（来自 `examples/example_pipeline.xml`）

```xml
<?xml version='1.0' encoding='utf-8'?>
<faultPredictionPipeline id="pipeline_4e67f8581f6e" name="设备故障 · 特征与模型验证"
                         version="1.0" graphVersion="30">
  <metadata>{"description": "合成设备数据演示；模型指标不代表真实工业效果", "seed": 42}</metadata>
  <nodes>
    <node id="source" type="data.input" componentVersion="1.0">
      <position x="60" y="190"/>
      <parameters>
        <parameter name="path" encoding="json">"synthetic_equipment.csv"</parameter>
        <parameter name="encoding" encoding="json">"utf-8-sig"</parameter>
        <parameter name="separator" encoding="json">","</parameter>
      </parameters>
      <ports>
        <outputPort name="dataset" dataType="Dataset" required="true"/>
      </ports>
      <ui>{}</ui>
    </node>
    <node id="filter" type="data.filter" componentVersion="1.0">
      <position x="330" y="190"/>
      <parameters>
        <parameter name="column" encoding="json">"temperature"</parameter>
        <parameter name="operator" encoding="json">"gt"</parameter>
        <parameter name="value" encoding="json">0</parameter>
        <parameter name="conditions" encoding="json">[]</parameter>
        <parameter name="logical_operator" encoding="json">"and"</parameter>
      </parameters>
      <ports>
        <inputPort name="dataset" dataType="Dataset" required="true"/>
        <outputPort name="dataset" dataType="Dataset" required="true"/>
      </ports>
      <ui>{}</ui>
    </node>
  </nodes>
  <connections>
    <connection sourceNode="source" sourcePort="dataset" targetNode="filter" targetPort="dataset"/>
  </connections>
  <ui>{}</ui>
</faultPredictionPipeline>
```

### 9.4 解析与校验链

`XMLParser.loads` 依次执行（任一步失败即拒绝，错误信息包含具体原因）：

1. **体积检查**：超过 2 MB 直接拒绝（提示"graphs must not embed runtime datasets"）。
2. **安全解析**：禁用实体解析、禁用网络、禁用 DTD；出现 DOCTYPE 即拒绝（防 XXE）。
3. **XSD 结构校验**：元素顺序、必需属性、`identifier` 规则、`xs:unique`（节点 id 唯一）与 `xs:keyref`（连线的节点必须存在）。
4. **组件版本校验**：`componentVersion` 必须与 Registry 中该组件当前版本一致。
5. **端口一致性校验**：XML 中声明的端口必须与 Registry 定义**逐项相等**（名字、类型、必填）。
6. **参数解析**：JSON 解码，重复参数拒绝。
7. **图重建**：`add_node` + `connect`，因此连线规则、类型兼容、无环都在导入时再次生效。
8. **完整性校验**：`load(..., require_complete=True)` 时会检查必填输入是否连接、图是否为空。

序列化侧同样自检：`XMLSerializer.dumps` 先做图校验，写出后再用 `_parse` 回读校验，保证"能保存的必定能加载"。

`Graph → XML → Graph` 的一致性由测试覆盖（节点、参数类型、位置、连线、UI 元数据、版本号）。

---

## 10. 可视化设计器

### 10.1 前端架构

| 项 | 选择 | 理由 |
| --- | --- | --- |
| 技术栈 | 原生 HTML + CSS + ES Module JS | 无构建步骤、无 Node 依赖，`python -m fault_platform serve` 即可用 |
| 渲染 | DOM 节点 + SVG 连线 | 节点数量有限，DOM 便于表单与可访问性；连线用贝塞尔路径 |
| 状态 | 单一 `state` 对象 | 便于撤销/重做与一致性维护 |
| 通信 | 只调用 `/api/control/*` 与 `/api/data*` | 与 MCP 共用同一条控制通道 |

前端状态核心字段：

```js
state = {
  catalog, graph, selected:Set, edge, pending,        // 结构编辑
  zoom, pan, drag,                                    // 视口与拖拽
  undo:[], redo:[],                                   // 历史栈（快照式）
  statuses:{}, running, saving, dirty, poll,          // 运行与轮询
  tab, resultRequest,                                 // 结果面板
  onlyFavorites, favorites                            // 组件库筛选（localStorage）
}
```

### 10.2 关键交互实现

| 交互 | 实现要点 |
| --- | --- |
| 实时同步 | `EventSource('/api/events')` 订阅；`graph_changed` 且版本不同 → 无本地改动自动重载，有改动弹冲突横幅；`node_status`/`history` 直接更新节点徽章与耗时；断线自动重连 |
| 组件库 | 由 `list_components` 动态生成；按 `category` 分组，搜索匹配类型/显示名/描述/标签；收藏存 `localStorage` |
| 拖入节点 | 调色板 `dragstart` 写入 `component` MIME，画布 `drop` 计算画布坐标后 `addNode` |
| 节点移动 | 画布 `pointerdown` 命中节点 → `pointermove` 更新位置 → `pointerup` 提交（提交前保留 `before` 快照用于撤销） |
| 连线 | 点击输出端口进入 pending 状态（跟随鼠标绘制虚线），再点击输入端口完成；Esc 取消 |
| 多选/复制/删除 | Shift 点击多选；Ctrl+C / Delete；节点与连线都可删除 |
| 撤销/重做 | 快照式 undo/redo 栈，Ctrl+Z / Ctrl+Y（Ctrl+Shift+Z） |
| 视口 | 空白拖动平移；滚轮/按钮缩放（0.2–2.0）；「适应画布」按节点包围盒计算缩放与居中 |
| 自动布局 | 按拓扑层次重新排布节点坐标 |
| 参数面板 | 完全由 `parameter_schema` 生成控件：`boolean→checkbox`、`enum→select`、`integer/float→number`、`list/object/any→JSON textarea`、`column_list/feature_list→逗号分隔输入` |
| 结果面板 | 按输出类型渲染：表格（截断提示）、指标卡片 + 混淆矩阵、特征重要性表、相关性热力矩阵、`PlotArtifact` → SVG 折线/散点、概览表格、错误块 |
| 执行日志 | `get_history` 渲染节点状态、耗时、缓存命中、错误 |
| XML 页签 | `get_pipeline_xml` 直接展示当前方案 XML |

### 10.3 前端安全

- 所有插入 DOM 的文本走 `esc()` 转义（节点名、列名、错误信息、XML 预览）；浏览器验收脚本包含"恶意节点名按文本渲染"的用例。
- 服务端下发 CSP：`default-src 'self'`、`script-src 'self'`、`frame-ancestors 'none'`；`X-Content-Type-Options: nosniff`。
- 变更类请求校验 `Origin` 与 `Host` 一致，拒绝跨源写入；`TrustedHostMiddleware` 限制 Host 白名单。
- 上传限制 25 MB，只接受 CSV；`data.input` 只能读取 `data_root` 内文件，路径穿越被拒绝。
- XML 导入限制 2 MB 且禁用 DTD/实体。

### 10.4 实时同步（SSE）

人工端与 Agent 端操作同一个 Graph，因此页面必须能感知"别人改了"。实现是一条单向事件流：

```
ExecutionEngine ──on_event──┐
                            ├──► EventBus（进程内、线程安全、有界）──► GET /api/events（SSE）──► 浏览器 EventSource
PipelineService（编辑操作）─┘
```

| 组件 | 设计 |
| --- | --- |
| 事件源 | 引擎在节点/流程状态迁移处发事件（`RUNNING/SUCCESS/FAILED/SKIPPED`、缓存命中、每次 history）；控制面在所有编辑操作、执行启动、检查点保存处发事件 |
| 事件类型 | `graph_changed`（含 `version` 与 `operation`）、`run_started`、`pipeline_status`、`node_status`、`history`、`checkpoint` |
| 解耦 | `ExecutionContext.on_event` 是可选回调，默认 `None`；引擎与 `fault_platform.events` 没有硬依赖，单独使用 Runtime 时不产生任何事件开销 |
| 可靠性 | 每个订阅者一个有界队列（200 帧，满了丢最旧），**慢客户端永不阻塞执行线程**；总线保留最近 200 条事件用于 `Last-Event-ID` 断线补发；连接空闲 15 s 发送心跳注释帧 |
| 前端处理 | 无本地未保存改动 → 自动重载新版本并提示；有本地改动 → 黄色横幅提供「重新加载 / 保留我的修改」；执行事件直接驱动状态芯片、节点徽章与耗时/复用标注 |
| 冲突兜底 | 即使横幅被忽略，写入仍走 `expected_version` 乐观并发，冲突时服务端拒绝，前端再次弹出横幅 |

Server-Sent Events 而不是 WebSocket，是因为这里只有服务端→浏览器单向推送、需要自动重连与断线补发，SSE 在浏览器里内置这些语义且不需要额外协议层。

### 10.5 前端已知限制

- 参数表单中的列名是文本/逗号输入，尚未与上游列元数据联动做自动补全。
- 画布坐标是相对坐标，尚无分组、注释、子图等高级编辑能力。
- 结果面板只展示有界预览，不提供完整数据导出（设计如此）。
- 事件总线是进程内的：多进程或远程部署时需要换成外部 broker（首版是单进程本机服务）。

---

## 11. 控制 API 与 MCP 设计

### 11.1 HTTP 端点

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/` | 可视化设计器页面 |
| `GET` | `/static/*` | 前端资源（app.js / style.css） |
| `GET` | `/api/health` | 状态与组件数量 |
| `GET` | `/api/data` | 列出 `data_root` 下的 CSV |
| `POST` | `/api/data/upload` | 上传 CSV（25 MB，返回列名与预览） |
| `POST` | `/api/control/{operation}` | 36 个控制操作统一入口 |
| `GET` | `/api/events` | Server-Sent Events：图修订、节点状态、执行状态、检查点（可按 `pipeline_id` 过滤，支持 `Last-Event-ID` 补发） |

### 11.2 操作分组（36 个）

| 分组 | 操作 |
| --- | --- |
| 方案 | `create_pipeline`、`list_pipelines`、`get_pipeline`、`replace_pipeline`、`load_pipeline`、`save_pipeline`、`create_example` |
| 组件发现 | `list_components`、`search_components`、`get_component_schema` |
| 图编辑 | `add_component`、`remove_component`、`configure_component`、`connect_components`、`disconnect_components`、`validate_pipeline` |
| 执行 | `execute_pipeline`、`execute_node`、`execute_from_node`、`retry_node`、`cancel_pipeline`、`get_pipeline_status`、`get_node_result`、`get_pipeline_result`、`get_history` |
| 检查点与导出 | `save_checkpoint`、`load_checkpoint`、`list_checkpoints`、`get_pipeline_xml` |

### 11.3 Observation 约定

### 11.3a Agent 可用性（第三轮真实使用后的修订）

第一轮真实使用暴露了三个"能跑但不好用"的点，设计上做了对应修订：

| 现象 | 修订 |
| --- | --- |
| 每次编辑都回吐整张图，搭一张 11 节点图要回传数万 token | 编辑类操作新增 `include_graph`（单个操作默认 `true` 以兼容 UI，批量操作默认 `false`），只回 `version / node_count / edge_count / added` |
| 9 节点图需要 26 次往返 | 新增 `add_components` / `connect_many` / `configure_components` 批量操作 |
| `get_node_result` 把 `train_indices` / `test_indices` 原样返回（数千条），反而埋没了指标 | `summarize` 对长数组折叠为 `*_count` 与 5 条预览，`include_indices=true` 才展开；缺失率同理只看预览行 |

另外补了运行期可观测与生命周期：`get_server_info`（data_root / storage_root / 缓存预算与用量 / 数据文件）、`list_datasets`、`wait_for_pipeline`（阻塞到终态，带 `timed_out`；它是唯一不在全局锁内执行的阻塞操作）、`delete_pipeline`（连工作区、落盘文件与检查点一起回收），以及"读到非最新 workspace 时返回警告"。

结果摘要的变化不改动核心契约：模型指标的结构化字段（accuracy/confusion_matrix/各类指标）原样保留，只是把大数组降级为计数——前端 `metricsView` 与 `validation.compare` 都不受影响。

成功（`dispatch` 自动补齐 `success/summary/warnings`）：

```json
{
  "success": true,
  "summary": "execute pipeline",
  "pipeline_id": "pipeline_001",
  "workspace_id": "ws_001",
  "status": "RUNNING",
  "warnings": []
}
```

失败：

```json
{
  "success": false,
  "error_code": "ValueError",
  "summary": "Incompatible port types: Dataset -> FeatureDataset",
  "recommended_action": "Check parameters, ports and current run status."
}
```

约定要点：

- `error_code` 取异常类名（`ValueError` / `KeyError` / `TypeError` / `OSError` / pydantic `ValidationError`），`summary` 截断到 3000 字符。
- `validate_pipeline` 属于"失败即业务结论"的操作：校验不通过时 `success=false` 且带 `errors` 列表。
- **两层成功**必须区分：HTTP 200 表示控制请求被受理；方案运行成败看 `status`（`SUCCESS/FAILED/CANCELLED`）。查询一个失败运行的 `get_pipeline_status` 本身仍然是成功请求。
- 返回内容永不包含完整数据集、模型权重或原始 Artifact。

### 11.4 MCP bridge

```
Agent ──stdio──► mcp_server.py ──HTTP──► /api/control/{operation} ──► PipelineService
```

| 设计点 | 说明 |
| --- | --- |
| 工具集 | 与 `CONTROL_OPERATIONS` 一一对应，共 36 个工具；**不**把 29 个原子组件暴露成工具 |
| 签名来源 | 用 `inspect.signature` + `get_type_hints` 从 `PipelineService` 方法自动生成工具入参，避免"两套定义漂移" |
| 描述 | 每个工具带一句面向 Agent 的说明（见 `DESCRIPTIONS`） |
| 传输 | `FastMCP` + stdio；`--url` 指定后端服务地址（默认 `http://127.0.0.1:8765`） |
| 依赖前提 | 必须先启动 `fault_platform serve`；后端不可用时返回 `error_code=CONTROL_API_UNAVAILABLE` 与启动提示 |
| 大对象边界 | Agent 只用 `pipeline_id / workspace_id / node_id` 操作，读回有界预览 |

### 11.5 Skill 的定位

`skills/fault-prediction/SKILL.md` **不计算数据**，只提供领域工作流知识：如何先探索数据、如何选择预处理、如何做窗口与标签、如何并行验证多个算法、如何解读警告与指标、什么时候修改方案。它使 Agent 的操作序列符合工程习惯，而不是把组件按字母顺序堆起来。

---

## 12. 故障预测领域设计

这一层是平台与"通用数据流工具"的区别所在。

### 12.1 推荐工作流

```
data.input
   ├─► visual.overview            先看清列、类型、缺失率
   ├─► data.filter / row / column  按业务规则清洗
   ├─► 探索分支：标准化 → 折线图 / 散点图 / 相关性
   └─► 特征分支（同一窗口配置）
          ├─ feature.statistical
          ├─ feature.fitting
          ├─ feature.spectral       需要真实采样率
          └─ feature.categorical    有离散属性时
                 │
                 ▼
          feature.merge → feature.score_select（可选）→ feature.pca（可选）
                 │
                 ├─► validation.random_forest ─┐
                 ├─► validation.svm            ├─► validation.compare
                 └─► validation.xgboost ───────┘
                         ▲
                    labels（来自窗口组件）
```

### 12.2 窗口化与标签对齐

- `windows()` 要求源行索引唯一；按 `group_column` 分组，存在 `time_column` 时先排序，再按 `window_size`/`step` 切窗；窗口键为 `g{组号}_w{起始行}`。
- 每个特征行携带 `source_rows`（覆盖的原始行）、`groups`、`window_size`、`step`、`overlapping` 等属性。
- 标签按**同一窗口**生成：`label_policy=strict` 拒绝混标签窗口，`last` 取窗口末行，`mode` 取众数。
- `feature.merge` 要求两分支索引与来源属性（`source_rows/groups/source_path/source_id`）完全一致，且特征名不重叠。

### 12.3 泄漏防护（本平台的核心正确性保证）

| 机制 | 实现 |
| --- | --- |
| 索引对齐 | 模型要求特征与标签索引唯一且完全一致 |
| 划分方法 | `stratified` 分层随机、`group` 按设备分组（`GroupShuffleSplit`）、`temporal` 按特征行顺序 |
| 重叠窗口限制 | 标记 `overlapping` 的特征集禁止随机划分，必须用 `group` 或 `temporal` |
| 重复设备限制 | 组内多窗口时禁止分层随机划分 |
| 窗口级交叉检查 | 训练与测试窗口若共享原始数据行，直接报错 `Train/test windows share source rows` |
| 时间划分净化 | `temporal` 会剔除与测试窗口重叠的训练窗口 |
| 训练内拟合 | SVM 的 `StandardScaler` 与概率校准（`CalibratedClassifierCV`）只在训练分区拟合 |
| 全量拟合标记 | 缩放/编码/特征选择/PCA 等在全量数据上拟合的操作会写入 `evaluation_warnings`，随 DataFrame 传播到指标 `warnings` |
| 目标编码 | 使用 K 折 OOF 编码，避免直接用本行标签 |

### 12.4 频域特征的设计约定

| 约定 | 说明 |
| --- | --- |
| 加窗 | Hann 窗，抑制频谱泄漏 |
| 幅值归一化 | 相干增益归一化（`2|X|/Σw`），DC 与 Nyquist 不做双侧加倍 |
| 能量 | 谱 RMS 用 Parseval 一致公式，单音信号可还原理论值 |
| 频带 | `band_edges` 以 Nyquist 比例表示，`band_energy_ratio_i` 之和为 1 |
| 谐波 | `harmonic_ratio` 统计 2–5 倍主频 ±`harmonic_tolerance` 内的能量占比 |
| 前置条件 | `sampling_rate` 必填（Hz）；窗口至少 8 个样本；假设样本等间隔、窗口内连续 |
| 明确不做 | 不重采样、不推断转速、不做阶次跟踪 |

平窗口与量化通道（真实数据的主要失败点）：默认 `flat_policy=nan` —— 该窗口的频域特征记为 NaN 并保持行索引对齐（`skip` 会破坏与其它特征分支的 merge，`error` 才硬失败），同时回传每个通道的平窗口计数警告。`data.quality` 组件把"逐组常数列、全 NaN 列、全零列、平窗口比例、重复行、混标签窗口与标签变化次数"一次性报告出来，作为特征工程前的强制预检——这些问题是 missing_rate 看不出来的。

### 12.5 指标语义与诚实性

- 指标集合：`accuracy / precision / recall / f1 / roc_auc` + 混淆矩阵、训练/测试样本数、划分方法、随机种子、训练与测试索引、预测分布、警告列表。
- `validation.compare` 要求多个模型使用**完全相同的测试索引**，否则拒绝比较。
- 文档与示例明确：合成数据上的 100% 准确率只证明工程闭环，不代表工业性能；平台不会把"当前故障识别"包装成"未来故障预测"。

---

## 13. 错误处理与可观测性

### 13.1 三层错误结构

| 层 | 结构 | 用途 |
| --- | --- | --- |
| 控制面 | `{success:false, error_code, summary, recommended_action}` | Agent/UI 决策 |
| 运行面 | `{error_type, error_message, stack, parameters, input_summary}` | 定位组件内失败 |
| 校验面 | `errors: [字符串]`（`validate_graph` / `validate_pipeline`） | 编辑期即时反馈 |

运行时若调度本身崩溃（非组件异常），会写入 `workspace.errors["_runtime"]` 并把状态置为 `FAILED`，避免"看起来在工作其实已死"。

### 13.2 执行历史

每个节点每次执行都追加一条历史，含前后状态、耗时、输入/输出摘要、成功标记、错误对象与是否命中缓存。历史既服务于人工调试，也服务于 Agent 推理与审计。

### 13.3 警告传播链

```
组件 warnings ──► ComponentResult.warnings ──► workspace.node_warnings[node] ──► workspace.warnings（去重）
DataFrame.attrs["evaluation_warnings"] ──► 特征合并合并属性 ──► metrics.warnings ──► 结果面板/指标结构
```

---

## 14. 扩展机制

### 14.1 新增一个原子组件（不修改任何核心）

1. 在 `fault_core` 中实现数值函数（保持无平台依赖）。
2. 在 `components/builtin.py` 中继承 `BaseComponent`，声明 `metadata`、`input_ports`、`output_ports`、`parameter_schema`，实现 `execute`。
3. 加入 `BUILTIN_COMPONENTS` 元组。
4. 运行 `scripts/export_catalog.py` 重新生成组件参考。

自动获得：网页组件库与参数表单、端口与参数校验、XML 读写与校验、MCP `list_components`/`get_component_schema`、拓扑调度与增量缓存。

必须遵守的不变量：

- 输出键必须与 `output_ports` 一致，且返回值类型匹配端口类型；
- 不持有跨执行状态（组件实例只存配置）；
- 需要外部资源时实现 `preflight` 与 `external_fingerprint`；
- 全量拟合类操作必须调用 `mark_fitted` 打标记。

### 14.2 新增数据类型

在 `DataType` 中增加枚举 → 在 `runtime.validate_value` 增加运行时判定 → 在 `summarize` 增加预览策略 → 前端 `objectView` 增加渲染分支。图/XML/MCP 无需改动。

### 14.3 新增数据源（当前仅 CSV）

实现新的输入组件，覆盖 `preflight`（校验连接串/文件）与 `external_fingerprint`（把连接串或文件摘要纳入失效判断）。Workspace、Graph、XML 不受影响。

### 14.4 视觉设计器扩展

组件分类、图标、颜色由前端 `categories` 表决定；新增分类只需在该表登记。参数控件由 Schema 推导，无需为每个组件写 GUI。

---

## 15. 目录与模块映射

```
src/fault_core/                 纯数值库（L0）
  data.py                       过滤、行/列操作、安全表达式求值
  preprocessing.py              缩放、转换、mark_fitted
  exploration.py                集中趋势、离散度、相关性
  visualization.py              概览与图形规格
  features.py                   窗口、统计/拟合/频域/分类特征、合并
  selection.py                  方差/相关性/互信息/模型重要性
  reduction.py                  PCA
  models.py                     训练、划分、指标、TrainedClassifier

src/fault_platform/             平台层（L1–L3）
  components/base.py            组件契约
  components/builtin.py         26 个内置组件
  registry.py                   注册与目录
  graph.py                      图模型与校验
  runtime.py                    ExecutionContext / ExecutionEngine
  workspace.py                  Workspace / Manager / 历史 / 检查点 / Artifact
  xml_io/__init__.py            XML 读写与校验链
  xml_io/schema.xsd             XSD
  service.py                    控制面（29 操作）
  api.py                        FastAPI 应用与静态资源
  cli.py                        命令行入口
  mcp_server.py                 MCP bridge
  examples.py                   合成数据与示例方案
  web/                          可视化设计器（index.html / app.js / style.css）

skills/fault-prediction/        Agent 领域技能
scripts/                        export_catalog.py / browser_check.cjs / mcp_smoke.py
tests/                          pytest + DOM 集成测试
examples/                       数据、示例 XML、Python API 示例
docs/                           设计、架构、组件、MCP、验证
```

---

## 16. 测试策略与验证证据

### 16.1 分层

| 层 | 文件 | 覆盖 |
| --- | --- | --- |
| 组件与契约 | `tests/test_components.py`、`tests/test_feature_extension.py` | 每个组件执行、参数严格性、端口类型、窗口与标签对齐、频域与选择/降维语义 |
| 图与运行时 | `tests/test_graph_runtime.py`、`tests/test_runtime_integrity.py` | 环检测、拓扑序、缓存失效、部分执行、失败传播、重试、检查点深拷贝、文件变更失效、时间窗口去重叠 |
| 控制面 | `tests/test_api_mcp.py` | API 校验、并发编辑拒绝、上传、真实 MCP stdio 握手与 HTTP 图共享 |
| 实时同步 | `tests/test_events.py` | 总线过滤/断线补发/慢读者不阻塞；编辑与执行事件序列；SSE 端点的真实流式帧与未知方案 404 |
| 前端 DOM | `tests/frontend.test.cjs` | 组件库、参数提交、连线、复制、删除、撤销重做、示例运行、指标与曲线渲染、转义 |
| 真实浏览器 | `scripts/browser_check.cjs` | 渲染尺寸、节点与连线绘制、指针拖拽持久化、缩放、组件放置与参数表单、**Agent 改动实时出现在页面**、**Agent 触发执行的页面进度**、执行与结果、日志与 XML 页签 |
| MCP 闭环 | `scripts/mcp_smoke.py` | 按配置启动 bridge，只用 MCP 工具完成建图→校验→执行→结果→XML→检查点 |

### 16.2 当前验证数字

| 项目 | 结果 |
| --- | --- |
| pytest | 53 项通过 |
| DOM 集成测试 | 4 项通过 |
| 浏览器验收 | 9 项检查通过（Chrome headless + DevTools 协议），含实时同步两项 |
| MCP 闭环 | 29 个工具、5 节点 6 连线、status=SUCCESS、检查点恢复成功 |
| 静态检查 | `ruff check` 通过、`ruff format --check` 通过、`node --check` 通过 |
| 依赖 | `pip check` 无冲突；wheel 构建成功 |

详见 [validation.md](validation.md)。

---

## 17. 设计决策记录（ADR 摘要）

| 编号 | 决策 | 背景 | 取舍与代价 |
| --- | --- | --- | --- |
| ADR-1 | 用**类型化端口**替代自由连接 | 特征行与原始行的语义混淆是故障预测最常见错误 | 更严格的建模体验；需要显式转换组件 |
| ADR-2 | Component 与 MCP Tool 分离 | 组件数量会增长，逐个暴露会让 Agent 上下文爆炸 | Agent 需要多次调用编辑工具 |
| ADR-3 | Registry 作为唯一组件来源 | 避免 UI/Runtime/MCP 三份定义漂移 | 组件元数据必须写全 |
| ADR-4 | XML 只存结构 | 大矩阵塞进 XML 会破坏可读性与可评审性 | 运行结果不可通过 XML 迁移 |
| ADR-5 | Workspace 首版内存实现 | 先保证 Graph/Runtime 独立性，ArtifactStore 接口先定型 | 重启丢失运行数据与检查点 |
| ADR-6 | 执行基于内容指纹 | 需要"改一个参数只重算下游" | 指纹计算引入少量开销 |
| ADR-7 | Runtime 负责正确性，LLM 不参与 | 校验不应依赖概率性判断 | Agent 必须处理错误 Observation |
| ADR-8 | 顶层 API 面向图操作，而非数据搬运 | MCP 传输大对象不可行 | 只能拿到有界预览 |
| ADR-9 | 前端零构建（原生 JS） | 降低使用门槛，`serve` 即用 | 复杂交互需自行实现，无框架生态 |
| ADR-10 | 全量拟合操作打探索性标记 | 平台无法自动知道数据划分意图 | 需要使用者理解并处理警告 |

---

## 18. 已知限制与演进路线

### 18.1 首版限制

1. 运行数据、模型与检查点为内存实现，服务退出即丢失。
2. 数据源仅 CSV；无流式/增量输入、无数据库或时序库接入。
3. 单机串行 DAG 调度（2 个方案可并行），无分布式执行。
4. 只做分类验证；无回归、RUL、生存分析、异常检测组件。
5. 参数面板列名需手输，未与上游列元数据联动。
6. 特征选择与 PCA 在全量数据上拟合，携带探索性警告；未内置"训练折内选择"。
7. 前端无窄屏移动端优化、无像素级回归基线。
8. 无鉴权与多用户隔离（服务仅绑定本机）。

### 18.2 演进路线

| 优先级 | 方向 | 设计影响 |
| --- | --- | --- |
| P0 | ArtifactStore 落盘（DataFrame/模型/图形） | 只替换存储实现，Workspace 接口不变 |
| P0 | 组件扩展：小波、STFT、变点、健康指标、FFT 阶次 | 只新增组件并注册 |
| P1 | 交叉验证、超参数搜索、模型比较增强 | 新增组件；`Metrics` 结构扩展 |
| P1 | 训练折内预处理/特征选择（管道化拟合） | 组件新增 `fit/transform` 语义与作用域标记 |
| P1 | 列元数据联动（上游输出的列自动补全） | 前端参数面板 + 轻量元数据接口 |
| P2 | 多数据源（Parquet/MDF/数据库） | 新增输入组件，`resolve_data_path` 泛化 |
| P2 | 多用户与远程部署 | 控制面鉴权、作业队列、Artifact 后端替换 |
| P2 | 组件检索升级（向量检索、端口兼容推荐） | Registry 增加索引层，接口兼容 |

---

## 附录 A · 术语表

| 术语 | 含义 |
| --- | --- |
| Atomic Component | 单一职责的原子组件，平台的最小建模单元 |
| ComponentGraph | 方案结构（节点 + 连线 + 元数据 + 版本） |
| Port | 组件的数据接口，带数据类型 |
| Parameter Schema | 组件参数的声明式定义，驱动校验与 UI |
| Registry | 组件定义注册表，UI/Runtime/XML/MCP 的统一来源 |
| Workspace | 一次执行产生的数据环境（输出、状态、历史） |
| Artifact | Workspace 中的大对象存储，以 `artifact://` 引用 |
| Checkpoint | Graph + Workspace 的联合快照 |
| Fingerprint | 节点内容指纹，用于增量执行与失效判断 |
| Observation | 控制操作的统一返回结构（成功/失败） |
| Skill | 教 Agent 如何专业建模的领域知识文件 |

## 附录 B · 状态与错误速查

| 类别 | 取值 |
| --- | --- |
| Pipeline 状态 | `CREATED` `VALIDATING` `READY` `RUNNING` `SUCCESS` `FAILED` `CANCELLED` |
| Node 状态 | `PENDING` `READY` `RUNNING` `SUCCESS` `FAILED` `SKIPPED` |
| 执行模式 | `all` `node` `from` |
| 划分方法 | `stratified` `group` `temporal` |
| 标签策略 | `strict` `last` `mode` |
| 错误码 | 异常类名（`ValueError` / `KeyError` / `TypeError` / `OSError` / `ValidationError`）；MCP 侧额外有 `CONTROL_API_UNAVAILABLE` |
| 关键错误信息 | `Incompatible port types: A -> B`、`Multiple connections to one input`、`Graph contains a cycle`、`required input not connected`、`Train/test windows share source rows`、`Pipeline is running; wait or cancel before editing` |
