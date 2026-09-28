# 本地文档知识库

真实 `/agent` 已接入持久化知识库；首页用于性能实验的合成语料仍是独立模块。

启动服务后运行 `python -m agentplat.knowledge_cli open` 登录面板，再点“知识库”。填写本机文件或目录的完整路径，后台完成导入后可在页面检索验证、查看原件或撤销检索。当前采用路径导入，没有浏览器上传控件。

也可以使用命令行（`--workspace` 必须与 Agent 当前工作区一致）：

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

当前机器已由用户授权：仅固定 `ocr_windows.ps1` 的 OCR 子进程使用 `-ExecutionPolicy Bypass`。配置位于 `.agent-runtime/ocr-policy.json` 的 `allow_unsigned_local_ocr_script`，没有修改系统执行策略。文档与图片不会上传外部服务。

提取后按来源位置分块，每块最多 1000 字符，长段重叠 150 字符。索引采用 SQLite FTS5 BM25 加词项覆盖率，支持中文字符/双字词切分。目前没有 embedding 或向量数据库，因此不能承诺语义同义召回。

Agent 工具为 `search_knowledge(query, top_k)`、`read_knowledge_chunk(chunk_id)`、`list_knowledge()`；返回文件名、位置、SHA-256 与 `kb:分块ID`。模型可检索，不能通过这些工具导入文件、撤销文档或授予权限。子 Agent 继承父任务的知识库范围。正文标记为不可信参考内容，标记本身不等于已解决所有提示注入问题。

知识库按工作区路径隔离，存放在项目 `.agent-runtime/knowledge/`。相同文件重复导入不重复索引；更新成功后才切换可见版本，更新失败保留旧版本；撤销后检索和分块回取均不可见。原件及历史索引保留用于审计，撤销不等于物理删除。单文件 25 MB、单次目录 1000 文件、提取文本 400 万字符、每文件 20000 分块；解析进程超时 90 秒。

验证：`python tools/test_knowledge.py`，`python verify.py lab-36`。测试使用真实格式夹具和脚本模型，不消费模型额度。
