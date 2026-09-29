# Windows 原生执行隔离

提供 `local`、`native`、`docker`、`disabled` 四种执行模式。没有 Docker 也可以使用 Windows AppContainer。下面以 **native 文件隔离 + host 网络** 为配置示例；公开仓库不包含本机策略，不声称等同于 Codex 的完整沙箱。

配置保存在 `.agent-runtime/execution-policy.json`：

```json
{"mode":"native","native_network":"host"}
```

环境变量 `AGENTLAB_EXECUTION_MODE`、`AGENTLAB_NATIVE_NETWORK` 可覆盖配置。无配置时兼容默认 local；native 的未指定网络策略默认 deny。

每条命令创建独立 AppContainer SID，只向任务工作区授予写权限，向项目私有 Python 运行时授予读取执行权限。清洗子进程环境，使用显式继承句柄和 Windows Job 回收进程树。正常退出清理授权与 profile；异常宿主崩溃的残留清理尚没有恢复日志。私有 Python 提供标准库，不复制整个 Conda site-packages。

宿主命令是单独的授权路径：`request_execution(command, reason, timeout_s=60)` 获批后以宿主环境执行一次，保留用户目录等正常运行信息，但仍过滤凭据。它不会给后续原生沙箱命令开放宿主包，也不会修改沙箱 ACL。完整语义和结果字段见 [权限恢复](PERMISSION-RECOVERY.md)。沙箱缺包不能作为宿主软件损坏的证据。

实测已验证：工作区写入、工作区外测试文件读写被拒、子进程继承限制、环境凭据不继承、超时回收、AppContainer token。不是整台系统不可见：Windows 自身授权的系统资源及容器 profile 仍可能可用。

这台机器零网络 capability 的 AppContainer 仍能连接回环端口，因此 `deny` 模式先执行可信预检；未观察到系统拒绝时，拒绝启动用户命令。通过该门控测试不代表网络隔离已经可用。用户选择的 `host` 模式明确允许宿主网络，不安装防火墙规则，不修改 UAC。域名授权仅约束 `fetch_url` 宿主工具，不是 host 模式下 shell 的网络防火墙。

网页控制端需要本机浏览器登录凭据；原生沙箱不可读项目 `.agent-runtime`，仅有回环连接权限无法直接调用控制 API。使用 `python -m agentplat.knowledge_cli open` 打开登录入口，启动日志不打印凭据。服务重启需要重新登录。

`/permissions` 可配置网页工具的 `public`、`allowlist`、`off` 模式。白名单模式使用精确域名，不自动包含子域名或重定向目标；公共网页模式仍执行 SSRF 校验。网页工具的策略不是 shell 网络防火墙。

```powershell
python tools/test_native_sandbox.py
python verify.py --native lab-30 lab-31 lab-32 --jobs 1
python tools/test_sandbox_contract.py
```

原生验证应从正常宿主终端运行；嵌套在另一个沙箱内可能因权限不足无法创建 AppContainer，不能静默退回 local。

实验 30–32 检查原生文件/子进程边界和网络预检；33–35 检查环境清洗、域名撤销与轮数误判；36 检查知识库版本与撤销。Agent 默认不再以“40 次模型调用但没有 shell 验证”判定无效循环。界面区分用户交互轮次与模型调用次数；显式上限仍精确生效，连续重复失败和连续工具超额另行检测。

机制参考：[Microsoft AppContainer isolation](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation)、[Implementing an AppContainer](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)。
