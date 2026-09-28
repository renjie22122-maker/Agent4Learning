# Runtime 11：聊天附件与直接可见的设置入口

输入框支持拖入文件、点击「＋」选择多个文件、粘贴剪贴板图片。
发送前显示名称、图片缩略图、解析进度和错误，可逐个移除。
每条消息最多 10 个文件，单文件最大 25 MB。只发附件时默认请求概述内容。
解析未完成或失败时禁止发送，失败的消息保留文本和附件。

沿用本地文档解析器：文本/代码、CSV/TSV、PDF、DOCX、XLSX、PPTX、PNG/JPG 等图片。
图片当前是本地 OCR，不代表模型拥有视觉能力；扫描文档、内嵌图片和特殊格式的解析限制以附件警告为准。
模型可通过 `list_attachments`、`read_attachment`、`search_attachment` 读取分页文本和检索结果。
大文档不会整份塞进提示词；附件中内容按不可信参考资料处理。

附件缓存放在宿主 `.agent-runtime/attachments`，不会写入项目工作区或自动加入项目知识库。
发送后授权绑定会话，恢复日志和委派子 Agent 时保留；其他会话不能读取未授权附件。
从输入框移除只取消此次发送，本地上传缓存不立即清除；会话回收站也不会永久清除附件。
不带附件的任务不加载附件工具，避免增加无关工具描述的 token 开销。

「工具与设置」在左上角两列平铺，无二级展开菜单。窄屏提供独立工具页入口。

验证：`tools/test_attachments.py`、`tools/test_chat_browser.cjs`、
`tools/check_attachments_live.py`，以及本地 PNG OCR 集成结果 `.diagnostics/attachment-ocr.json`。
