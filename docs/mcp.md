# MCP 接入

先运行网页服务，再配置 stdio bridge。Bridge 转发高层操作到同一 HTTP API。

~~~powershell
.\.venv\Scripts\python.exe -m fault_platform serve --port 8765
~~~

支持 mcpServers 配置的客户端（Codex 写入 `~/.codex/config.toml` 的 `[mcp_servers.fault-prediction]`）：

~~~json
{
  "mcpServers": {
    "fault-prediction": {
      "command": "D:/codespace/python/fault_pred/.venv/Scripts/python.exe",
      "args": ["-m", "fault_platform", "mcp", "--url", "http://127.0.0.1:8765"],
      "startupTimeoutSec": 60
    }
  }
}
~~~

工程移动后修改 command 绝对路径。配置位置由客户端决定，本工程不修改全局 MCP 配置。

## 验证

先启动服务，再用冒烟脚本按配置里的命令拉起 bridge，只用 MCP 工具完成一次“发现组件 → 建图 → 校验 → 执行 → 取结果 → 导出 XML → 检查点”的闭环：

~~~powershell
.\.venv\Scripts\python.exe scripts\mcp_smoke.py --from-config
~~~

`--from-config` 读取 `~/.codex/config.toml` 的 `[mcp_servers.fault-prediction]` 并原样启动，报告里会打印 `bridge_source`。不加该参数时使用 `sys.executable` 与 `--url`，便于其它客户端或 CI 复用；`--url` 可指向别的服务端口。

当前实测结果：39 个工具、13 个 feature 组件可检索、`feature.spectral` 的 sampling_rate 为必填、6 条连线成功、`Dataset → FeatureDataset` 被拒绝并返回 `Incompatible port types`、执行 SUCCESS、随机森林 accuracy=1.0（合成数据）、频域节点产出主频与谱 RMS 列、XML 6157 字符、检查点保存并恢复 5 个已完成节点。

客户端侧可以再确认一次注册情况：

~~~powershell
codex mcp list
~~~

Skill 位于 `skills/fault-prediction/`（`SKILL.md` + `references/` 三份参考资料），可整目录复制到 Agent 技能目录或直接读取；技能按阶段说明每个阶段怎么用、有哪些注意事项，并列出全部 39 个工具的用途。

Agent 的每次编辑与执行都会经 `GET /api/events`（SSE）推送给打开的网页：节点实时出现或消失，执行时节点状态、耗时与缓存命中逐条更新。用户此时若有未保存的本地改动，页面显示冲突横幅而不是静默覆盖。

HTTP 等价调用：

~~~http
POST /api/control/create_pipeline
Content-Type: application/json

{"name":"设备故障方案"}
~~~

execute_pipeline 返回 pipeline_id、workspace_id、RUNNING，之后轮询 get_pipeline_status。success=true 表示控制请求成功，运行成败看 status；查询 FAILED 运行仍是成功的查询。

失败 Observation 包含 success=false、error_code、summary、recommended_action。请求通过 Pydantic 严格校验；预览最多 100 行、50 列，不返回完整训练矩阵或模型权重。

工具支持组件编辑、连接、运行、重试、从节点执行、取消、检查点、历史和 XML。所有组件定义来自 Registry。

服务绑定本机 127.0.0.1，面向单用户开发。首版没有账号、访问令牌、分布式队列或远程多人部署。
