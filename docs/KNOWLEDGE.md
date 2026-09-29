# 本地文档知识库

真实 `/agent` 已接入持久化知识库；首页用于性能实验的合成语料仍是独立模块。

启动服务后，从具体对话输入框旁的“知识库”进入。在“本会话资料”“项目知识库”“公共知识库”中选择导入目标，再填写本机文件或目录的完整路径。后台完成导入后可检索验证、查看原件或撤销检索。项目库供同一工作区会话共享，公共库需各会话主动启用。完整步骤和范围边界见 [知识库范围](KNOWLEDGE-SCOPES.md)。

知识库管理页采用路径导入；聊天输入框的拖拽附件是另一条会话附件流程，不会自动加入公共库。

也可以使用命令行管理项目库（`--workspace` 必须与项目主目录一致；此 CLI 尚无 session/scope 选择器，会话库与公共库请用页面）：

```powershell
python -m agentplat.knowledge_cli --workspace D:\资料\工作区 import D:\资料\文档
python -m agentplat.knowledge_cli --workspace D:\资料\工作区 search "退款政策"
python -m agentplat.knowledge_cli --workspace D:\资料\工作区 list
```

| 格式 | 提取内容与引用位置 |
|---|---|
| TXT、Markdown、代码、JSON、HTML 等文本 | 文本与行范围；HTML 不执行脚本 |
| CSV、TSV | 列名与值、记录行号 |
| DOCX | 正文及表格段落；段落编号 |
| XLSX | 单元格值、已有公式与行号；不计算公式或运行宏 |
| PPTX | 幻灯片文字与 slide 编号 |
| PDF | 文本层与页码；加密文件需先解密 |
| PNG、JPEG、BMP、TIFF、WebP | 本机 OCR 文字；原件可查看 |

旧版 DOC/XLS 不支持。扫描 PDF 须先导出图片再 OCR；Office 内嵌图片、图片场景理解、图表理解尚未实现。无文字图片可保留原件，分块数为零，不会伪造检索内容。OCR 失败会明确报告错误。PDF 需要 `pypdf`；Windows OCR 需要可用语言包，其他系统可配置本机 Tesseract。解析在有超时的宿主子进程执行，不等同于对不可信解析器漏洞的完整 OS 隔离。

Windows 签名策略阻止 OCR 时，可明确授权仅固定 `ocr_windows.ps1` 子进程使用 `-ExecutionPolicy Bypass`。配置位于 `.agent-runtime/ocr-policy.json` 的 `allow_unsigned_local_ocr_script`，不修改系统执行策略；公开仓库不附带本机授权配置。导入解析和本地 OCR 不上传文档。

提取后按来源位置分块，每块最多 1000 字符，长段重叠 150 字符。基础索引采用 SQLite FTS5 BM25 加词项覆盖率，支持中文字符/双字词切分。配置本地 embedding 模型后融合向量召回；大库可使用 HNSW。模型或索引不可用时返回降级信息，不把关键词检索冒充语义检索。安装与评测边界见 [混合检索](RAG_HYBRID.md)。

Agent 工具为 `search_knowledge(query, top_k)`、`read_knowledge_chunk(chunk_id)`、`list_knowledge()`；返回文件名、位置、SHA-256、scope 和 `kb:范围:分块ID`，例如 `kb:project:…`。`expanded_search_knowledge` 可用已配置模型扩展查询词，会消耗 API 额度。模型不能通过这些工具导入文件、撤销文档或授予权限。子 Agent 继承父任务选择，独立验收为启用的库建立快照。正文标记为不可信参考内容，标记本身不等于已解决所有提示注入问题。

知识库按工作区路径隔离，存放在项目 `.agent-runtime/knowledge/`。相同文件重复导入不重复索引；更新成功后才切换可见版本，更新失败保留旧版本；撤销后检索和分块回取均不可见。原件及历史索引保留用于审计，撤销不等于物理删除。单文件 25 MB、单次目录 1000 文件、提取文本 400 万字符、每文件 20000 分块；解析进程超时 90 秒。

验证：`python tools/test_knowledge.py`，`python verify.py lab-36`。测试使用真实格式夹具和脚本模型，不消费模型额度。
