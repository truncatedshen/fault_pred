# 部署到别人的电脑

目标机器**只需要 Python 3.11+**。其余（依赖、skill、MCP 注册）由一个安装包和一条命令完成。

## 1. 在开发机上打包

```powershell
.\.venv\Scripts\python.exe scripts\export_release.py --build
```

产物：`dist/fault-prediction-platform-0.1.0-deploy.zip`，内含

| 内容 | 作用 |
| --- | --- |
| `dist/*.whl` | 平台本体（`fault_core` + `fault_platform`） |
| `skills/fault-prediction/SKILL.md` | 给 Agent 的领域技能 |
| `scripts/deploy.ps1` | 目标机一键安装（建 venv、装包、装 skill、写 MCP 配置、跑验收） |
| `scripts/verify_deploy.py` | 目标机验收脚本（14 项检查） |
| `scripts/mcp_smoke.py` | MCP 端到端冒烟（只用 MCP 工具建图并执行） |
| `scripts/install_mcp_config.py` | 幂等写入 `config.toml`（自动备份，先校验再写） |
| `scripts/browser_check.cjs` | 可选的真实浏览器验收（需本机 Chrome） |
| `docs/*`、`README.md`、`DEPLOY.md` | 说明文档与快速开始 |

把 zip 通过任意方式（U 盘 / 内网共享 / IM）传给对方即可，不需要联网安装依赖之外的东西。

## 2. 对方机器上安装

```powershell
Expand-Archive fault-prediction-platform-0.1.0-deploy.zip -DestinationPath .
cd fault-prediction-platform-0.1.0-deploy
powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1
```

可调参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `-InstallDir` | `%LOCALAPPDATA%\fault-prediction-platform` | 安装目录（venv、脚本、默认数据目录、`start.ps1`） |
| `-CodexHome` | `%USERPROFILE%\.codex` | skill 与 `config.toml` 的所在目录 |
| `-Port` | 8765 | 写进 MCP 配置与启动脚本的端口 |
| `-SkipSkill` / `-SkipMcp` / `-SkipVerify` | 关 | 只做其中一部分（例如只装服务、先不碰 Codex 配置） |

安装脚本是幂等的：重复执行会复用已有 venv、覆盖同名 skill、并把 MCP 配置更新为最新路径（写入前自动备份 `config.toml.bak-<时间戳>`）。

## 3. 怎么测试（四层，从快到慢）

### 第 1 层：环境与安装

`deploy.ps1` 最后会自动跑，也可以随时手动跑：

```powershell
%LOCALAPPDATA%\fault-prediction-platform\.venv\Scripts\python.exe `
  %LOCALAPPDATA%\fault-prediction-platform\scripts\verify_deploy.py --from-config
```

14 项检查，逐条打印 PASS/FAIL：

| 组 | 检查 |
| --- | --- |
| 环境 | Python ≥ 3.11；`fault_platform` 可导入且注册表组件数 ≥ 27；`mcp` / `pyarrow` / `xgboost` 三个可选依赖的存在情况（缺了会标注哪些功能不可用，不算失败） |
| Skill | `$CODEX_HOME/skills/fault-prediction/SKILL.md` 存在、front matter 完整、`name` 与目录名一致、正文包含 MCP 用法 |
| MCP 配置 | `config.toml` 里有 `[mcp_servers.fault-prediction]`，且 `command` 指向的解释器真实存在 |
| 服务 | 用临时数据目录启动服务，`/api/health`、首页、静态资源、SSE 事件流（`/api/events`）都正常 |
| 端到端 | 用**配置里的那条命令**拉起 MCP stdio 桥，走完「建图 → 校验 → 执行 → 取指标 → 导出 XML → 检查点」，并打印工具数、状态与准确率 |

加 `--json-report report.json` 可以把结果落盘，便于交付留痕；`--work-dir` 指定临时工作目录；`--skill-dir` 覆盖 skill 位置。

### 第 2 层：只测 MCP 协议

```powershell
%LOCALAPPDATA%\fault-prediction-platform\.venv\Scripts\python.exe `
  %LOCALAPPDATA%\fault-prediction-platform\scripts\mcp_smoke.py --from-config
```

`--from-config` 读取 `config.toml` 的条目并原样启动（报告里会打印 `bridge_source`）。它会先调用 `create_example` 生成合成数据，因此在全新机器上也能自给自足。需要服务在运行；不加 `--url` 时用配置里的地址。

### 第 3 层：人工看网页

```powershell
%LOCALAPPDATA%\fault-prediction-platform\start.ps1
```

打开 http://127.0.0.1:8765 → 加载示例 → 运行方案 → 点节点看结果；再用 Agent 改图，页面应当**实时**出现变化（SSE）。

### 第 4 层（可选）：真实浏览器验收

目标机装了 Chrome 与 Node.js 时：

```powershell
node scripts\browser_check.cjs
```

它会拉起临时服务与 Chrome headless，验证渲染、拖拽、缩放、Agent 改动实时同步、执行进度与结果面板，并把截图写到 `.fault-platform/screenshots`。

## 4. 交给 Agent 使用

1. 保持服务运行（`start.ps1`）。
2. **重启 Codex 会话**——MCP 服务器与 skill 在会话启动时加载，当前会话不会热加载。
3. 让 Agent 干活，例如："用统计特征和随机森林搭一个故障预测方案并跑出指标"。
4. 想确认 Agent 真的加载了 skill：让它复述"建方案前先做什么"——按 skill 它会先看数据概览、再决定清洗与特征，而不是直接堆模型。

## 5. 常见问题

| 现象 | 处理 |
| --- | --- |
| `python was not found on PATH` | 安装 Python 3.11+，安装时勾选 "Add python.exe to PATH" |
| MCP 报 `CONTROL_API_UNAVAILABLE` | 服务没在运行：先跑 `start.ps1`；桥只是转发，服务必须先起 |
| 端口被占用 | `deploy.ps1 -Port 8766`，或启动时加 `--port 8766`（两边要一致） |
| Agent 看不到工具 | 重启会话；再跑 `verify_deploy.py --from-config` 确认配置与解释器路径有效 |
| Agent 没用 skill | 确认 `$CODEX_HOME/skills/fault-prediction/SKILL.md` 存在（验收脚本会检查），然后重启会话 |
| Parquet/XGBoost 不可用 | 可选依赖未装：`pip install "fault-prediction-platform[parquet,xgboost]"` |
| 想卸载 | 删除 `InstallDir`，删除 `$CODEX_HOME/skills/fault-prediction`，并从 `config.toml` 移除 `[mcp_servers.fault-prediction]`（用备份 `.bak-*` 恢复最省事） |

## 6. 平台侧的边界（交付时要说明）

- 服务只监听 `127.0.0.1`，是**单机单用户**工具，没有鉴权与多租户。
- 运行数据、模型、检查点在内存中，重启即丢失；XML 与上传的 CSV 留在磁盘上。
- 示例数据是合成的，指标只证明工程闭环，不代表工业性能；真实任务需要对方定义预测标签与视界。
- 大文件：> 1 GB 时打开 `data.input.streaming`（按分组连续、组内按时间有序），或先用 `columns` / `max_rows` / Parquet 谓词缩小范围。
