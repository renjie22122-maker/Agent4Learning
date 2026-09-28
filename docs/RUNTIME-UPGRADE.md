# 运行时升级与能力边界

runtime-4：增加 [Windows 原生沙箱](NATIVE-SANDBOX.md) 与 [真实文档知识库](KNOWLEDGE.md)。默认模型调用总数不设限，取消以“40 轮未运行验证命令”判无效循环的规则；精确执行用户设置的上限。

网页现支持运行中追加提示（步骤边界生效）、独立选择历史会话、保存每轮完整回答与工具轨迹。进度通过局部刷新更新，保留输入框、滚动位置及展开状态。同一服务生命周期内保留旧对话实例，可以继续追问；服务重启后的历史会话可查看已保存记录，旧日志可能存在截断。当前仍串行运行主任务：另一个主任务运行时，可以浏览历史，但不能启动第二个主任务；子 Agent 并行不受此 UI 限制。消息队列写入日志；若进程在接收消息后崩溃，需人工核对日志中的待处理消息，不宣称自动恢复队列。

本机控制端增加浏览器登录凭据，避免允许宿主网络的沙箱命令直接调用宿主控制接口。运行 `python -m agentplat.knowledge_cli open` 登录；重启后旧 cookie 失效。

本机默认模式修订（runtime-3）：统一执行后端配置，默认 local，Docker/MCP 均为可选能力。写入子任务在独立副本中继承父任务的执行后端及 shell 权限；readonly 仍无命令权限，需要运行测试时应选择 isolated。新增默认模式与子任务实际执行/合并回归，运行时测试共 16 项通过，11 套离线测试通过。没有安装 Docker 或浏览器 MCP，也没有调用付费模型。

2026-09-28。项目包含教学模拟、压测平台和真实 CodingAgent；教学指标不等于真实模型成功率，也不表示已经达到 Codex 的全部能力。

## 已实现

| 问题 | 修改 |
| --- | --- |
| 命令超时后子进程占用管道 | ProcessSupervisor 管理进程句柄、文件输出、输出上限和取消。Windows 使用 Job Object，启动时先挂起、加入 Job 再恢复；POSIX 使用进程组。 |
| 工具协议不一致 | 教学工具、平台工具、CodingAgent 共用 schema 与权限执行入口。截断 JSON 不执行；重复调用 ID 返回错误，不重复副作用。 |
| 虚假完成 | 达到反射拒绝上限返回未验证。证据来自结构化退出码并绑定工作区摘要；后续编辑使证据失效。跳过工具的批次不能直接 finish。 |
| 恢复时重放副作用 | 调用意图和结果配对，未完成记录标为未知；恢复保留对话、迭代编号和同一日志，不自动重放未知调用。 |
| 大文件和正则阻塞 | 分页读取及字节游标 read_chunk；grep 在可取消进程中执行；spill 按 UTF-8 字节计量。 |
| 上下文丢约束 | 保留后续用户要求；摘要作为 assistant 历史，工具文本不升级为系统指令。 |
| 线程超时后仍在后台运行 | 模拟平台回调有界，超时记为结果未知且不自动重试。真实命令由进程监督器回收。任意 Python 回调不能强杀线程。 |
| 子 Agent 只有教学模拟 | 独立模型上下文，spawn/get/wait/send/cancel，状态与用量持久化，原子预算预留，默认只读，最大并发 3、排队总量 12、深度 1。 |
| 多 Agent 覆盖文件 | isolated 模式复制当前工作区（包含未提交文件），显式合并前比较基线，冲突拒绝覆盖；合并后父任务重新验证。这是独立副本，不是 Git worktree。 |
| 无公开信息来源 | 配置域名后可 fetch_url，记录 URL、获取时间、内容哈希与完整性；check_sources 检查数量、重复引用、引文和哈希，不声称证明语义真实性。 |
| 外部工具不可扩展 | 可配置 MCP HTTP 工具发现和调用、工具 allowlist、参数校验、会话初始化。浏览器等能力由实际配置的 MCP 服务提供。 |
| 隐藏能力状态 | /api/runtime 显示执行模式及依赖状态；/api/agent-tasks 显示子任务；页面安全边界栏显示当前执行模式。 |

墙钟上限和成本护栏可选；取消、单工具超时、输出上限和并发边界仍保留。成本不限不意味着允许失控进程。

## 配置与启动

默认 `AGENTLAB_EXECUTION_MODE=local`，适用于本机可信教学任务，无需安装 Docker。可设置 `disabled` 禁用命令执行。

如需容器隔离，显式设置 `AGENTLAB_EXECUTION_MODE=docker`。此模式需要可用的 Docker 服务和预先拉取的 `python:3.11-slim` 镜像，可通过 `AGENTLAB_SANDBOX_IMAGE` 指定其他本地镜像。启动命令不自动拉取镜像、不挂载 Docker socket；容器限制网络、CPU、内存、进程数与根文件系统写入，仅工作区和临时目录可写。缺少依赖时明确拒绝，不回退宿主。

本机启动示例（PowerShell，可省略默认的 local 设置）：

```powershell
$env:AGENTLAB_EXECUTION_MODE = 'local'
python -X utf8 -m agentplat.demo --host 127.0.0.1 --port 8800 --unlimited
```

本机模式没有 OS 沙箱；命令白名单无法隔离恶意 Python。isolated 子任务继承父任务的执行后端和 shell 权限；副本用于隔离修改冲突，不是 OS 沙箱。只读子任务不需要 Docker，因为没有 shell 和写工具。

联网域名由宿主授权，例如 `$env:AGENTLAB_WEB_DOMAINS = 'www.example.org'`。默认没有授权域名。禁止私网、回环与保留 IP；重定向重新检查域名，连接固定到已验证 IP。网页获取不等于搜索引擎，也无法绕过付费墙。来源检查不能证明榜单年份、排名和实体都正确，仍需要任务专属验收。

`AGENTLAB_MCP_CONFIG` 指向宿主 JSON 配置文件：

```json
{
  "servers": {
    "browser": {
      "url": "http://127.0.0.1:8931/mcp",
      "allowed_tools": ["browser_navigate", "browser_snapshot"],
      "token_env": "BROWSER_MCP_TOKEN"
    }
  }
}
```

以上端点与工具名是配置示例，需要替换为实际服务；无认证时省略 token_env。客户端只支持 [MCP 2025-03-26 HTTP 传输](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports) 的 JSON/SSE 响应子集，不支持 stdio、OAuth、服务端采样和全部 JSON Schema 特性。不支持的 schema 组合会拒绝调用。服务端返回的 instructions 不授予权限。

可用 `AGENTLAB_SUBAGENT_TOKENS` 设置子任务共享额度；每个子任务还必须预留自己的 token_budget。父子用量分别记录，模型供应商实际超额计入账目。取消在轮次边界检查；正在等待的模型 HTTP 请求仍受客户端超时约束，并不保证瞬时停止。

## 新增教学实验

20 进程所有权；21 完成证据；22 崩溃恢复；23 实际子任务调度；24 编辑冲突；25 并发预算；26 工具协议；27 不可信文本与能力边界；28 压缩保留用户约束；29 来源完整性。

这些实验调用共享运行时并使用脚本模型，不消耗 API 费用。注入实验验证权限边界，不代表对任意自然语言注入都有效。并行实验展示可并行负载的时延，不承诺任意任务都提速。

```powershell
python -X utf8 verify.py
python -X utf8 tools/test_offline.py
python -X utf8 tools/test_hang_regression.py
python -X utf8 tools/evaluate_runtime.py
```

最后一个命令只打印评估计划，不调用模型。显式加 `--real --runs 3 --variant single` 或 `--variant multi` 才调用配置的模型、产生费用。评估在临时工作区运行固定任务，以评估器自己的断言验收，记录成本、耗时和父子 token。此轮没有执行真实付费模型评估。

本轮本机验收：`verify.py` 28/28、135 条断言；`test_offline.py` 11 套通过（其中新增运行时回归 14 项）；挂起回归通过（进程树超时、部分输出、2,000 个解析输入、spill、压缩）；`compileall` 通过。上一轮服务加载 runtime-2，三个页面/API 冒烟检查返回 HTTP 200。教学压测平台另一次运行的 SLO 为 6/7，不属于“全部生产 SLO 达标”；以上离线通过数字不能替代容量评估。

## 验证范围与尚未具备的能力

此次验证包括离线教学实验、真实本机进程、临时文件、脚本模型和本机 MCP HTTP 夹具。当前开发机未安装 Docker，也未配置浏览器 MCP 服务，因此容器和真实浏览器集成没有实测；不能将接口实现说成已连接真实浏览器。

运行时仍不是 Codex 的等价实现：没有通用审批平台、远程执行基础设施、完整浏览器适配、连接器生态、跨设备任务和全套模型评估。容器是 OS 隔离的一层，不是多租户生产认证。工作区摘要证明“验证后文件没变”，不能证明测试覆盖充分；合并能检测冲突和在异常时回滚，但不是跨进程崩溃事务。网络读有大小和 socket 超时边界，DNS 与服务端慢速响应仍依赖系统网络栈；不要把这理解为网络调用的严格墙钟保证。
