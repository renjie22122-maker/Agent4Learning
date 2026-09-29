# 技能导入

服务提供 `/skills` 页面，也可从 Agent 侧栏点击“技能管理”。输入本机目录、`SKILL.md` 或 ZIP 的完整路径，点击“导入并启用”。支持安装多个技能；目前是本机路径导入，不是浏览器上传或远程市场安装。

最小目录：

```text
review-code/
  SKILL.md
  references/checklist.md
```

`SKILL.md` 示例：

```markdown
---
name: review-code
description: 审查代码变更并收集验证证据
---
先阅读相关代码，再检查边界条件。
通过 read_skill_file 读取 references/checklist.md。
最终列出发现、证据和未验证事项。
```

对 Agent 说“使用 review-code 技能审查当前修改”即可。模型通过 `list_skills` 发现技能，使用返回的完整名称调用 `read_skill`，并用 `read_skill_file` 按需读取包内文本。实际是否正确遵循技能仍取决于模型，导入成功不等于任务执行成功。

`list_skills(query, offset, limit)` 支持搜索和分页，默认每页 8 个、最多 20 个，返回 `next_offset`；长目录不必先写脚本解析 spill。目录显示完整标识、技能名和导入来源。同名技能可共存，读取时使用完整标识；含糊的短名称会报错并列出候选项。

加载器支持 name/description 的常用带引号标量及 `description: >`、`description: |` 多行文本，不要求把上游技能改成单行。这是受限元数据读取器，不是完整 YAML 实现。技能未声明依赖不代表不需要依赖；正文中的 MCP、账号、工具链和软件包要求仍须检查。

启用和停用在下一模型步骤或新任务生效。正在进行的模型调用不会被打断，已读取进入上下文的内容也不会自动抹除。技能不能扩大工具、网络或沙箱权限；导入脚本不会执行，依赖不会自动安装。暂不承诺所有第三方 Skill 的特定工具和运行时都兼容。

限制：每包最多 2000 个文件、展开后 25 MB、50 个技能；主文件和单次参考文本最多 100 KB。拒绝路径越界和符号链接。安装副本存放在受保护的 `.agent-runtime/skills`，原始文件不会被修改。

验证：

```powershell
python -X utf8 tools/test_plugins.py
python -X utf8 verify.py lab-37
```

实验 37 对比宿主停用后仍使用旧目录的错误实现与步骤边界刷新，观察过期技能计数从 1 降至 0。
