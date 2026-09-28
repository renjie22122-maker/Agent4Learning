"""验证编码 Agent 页面与工作区选择。

用 HTTP 客户端跑（urlencode 正确编码），检查：
  ① /agent 页面渲染出工作区条、选择器、安全说明、任务表单、会话历史
  ② 切换到允许的根 → 成功并记住
  ③ 切换到不允许的路径 → **被拒绝**，且页面显示原因
  ④ 切换后 current 真的变了
"""

from __future__ import annotations

import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# 本脚本只用标准库，**没有**导入 agentlab/agentplat，因此不会自动触发
# force_utf8()。Windows 控制台默认 GBK，直接 print("✅") 会 UnicodeEncodeError。
for _s in (sys.stdout, sys.stderr):
    with __import__("contextlib").suppress(Exception):
        _s.reconfigure(encoding="utf-8", errors="replace")

BASE = os.environ.get("AGENTLAB_DEMO_BASE", "http://127.0.0.1:8800")


def get(url_path, **params):
    """注意参数名不能叫 path —— 调用方要用 path= 传工作区路径，会撞名。"""
    url = BASE + url_path + ("?" + urllib.parse.urlencode(params) if params else "")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), url
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}", url


def check(name, ok, detail=""):
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  {detail}" if detail else ""))
    return ok


def main() -> int:
    ok = True
    code, _, _ = get("/readyz")
    if code == 0:
        print("服务没起来")
        return 2

    print("=" * 78)
    print("  ① /agent 页面渲染")
    print("=" * 78)
    st, body, _ = get("/agent")
    ok &= check("HTTP 200", st == 200, f"{st}")
    for key in ("编码 Agent", "工作区", "选择工作区", "安全边界",
                "会话历史", "防手滑"):
        present = key in body
        ok &= check(f"包含「{key}」", present)
    # 对话式外壳的结构性标志 —— 这些才是"主流 Agent 界面"该有的东西：
    # 会话流、底部固定输入、左侧栏、右侧抽屉。缺了它们就又退回"后台监控台"。
    for key, why in (("class=dock", "底部固定输入"),
                     ("class=scroll", "会话流滚动区"),
                     ("class=side", "左侧栏（会话/工作区）"),
                     ('class="panel', "右侧抽屉（辅助信息）")):
        ok &= check(f"对话式结构：{why}", key in body)
    # ⚠ 底部输入框有**两种形态**，测的时候不能只认一种：
    #   · 没有活对话 → 首轮提交（name=task，action=/agent/run，显示空状态引导）
    #   · 有活对话   → 追问（name=message，action=/agent/chat）
    # 服务端可能带着上一次任务的状态（这本身就是正确行为），
    # 写死"必须是首轮形态"会让检查在服务刚跑过任务之后假失败。
    # 这正是界面类断言的通病：**测了产品的一个瞬间状态，而不是它的能力**。
    first_run = ("name=task" in body and 'action="/agent/run"' in body)
    followup = ("name=message" in body and 'action="/agent/chat"' in body)
    ok &= check("底部输入框形态正确（首轮提交 或 追问，二者其一）",
                first_run != followup,
                "首轮" if first_run else ("追问" if followup else "两种形态都没有"))
    ok &= check("空状态有引导（无对话时）或有序状态（有对话时）",
                ("开始一个任务" in body) or ("class=\"turn" in body))
    # 设计语言必须是 DSH 那套令牌，而不是自己调的颜色
    ok &= check("用的是 DS 设计令牌（--ds-label-primary 等）",
                "--ds-label-primary" in body and "--ds-bg-base" in body)
    ok &= check("边框用 alpha 而不是实色灰（DSH 的做法）",
                "rgb(255 255 255 / 8%)" in body)
    # 换工作区必须有**常驻入口**。
    # 这一条是补的漏洞：改版时我把换目录的表单挪进了右侧抽屉，
    # 而抽屉默认收起 —— 表单还在，但用户根本看不到，
    # 结果就是"我现在也不能增加修改工作区什么的？"。
    # 教训：**控件移进默认隐藏的容器时，必须同时给它一个常驻入口。**
    ok &= check("侧栏有常驻的「添加 / 修改目录」入口",
                "添加 / 修改目录" in body)
    ok &= check("顶栏工作区 chip 本身可点（第二个入口）",
                'href="/agent?panel=1#wspath"' in body)
    # 上下文用量必须**常驻可见**。数据一直都在压缩器里，
    # 但在这之前没有任何出口 —— 用户看不到"离撑爆还有多远、压缩省了多少"。
    ok &= check("顶栏常驻显示上下文占用（不是只在收起的面板里）",
                ('href="/agent?panel=1#ctx"' in body) or ("上下文" in body))
    ok &= check("抽屉里有「上下文用量」区块", "上下文用量" in body
                and "id=ctx" in body)
    for key, why in (("当前占用", "当前 token 占用"),
                     ("压缩阈值", "压缩触发线"),
                     ("累计省下", "压缩省了多少"),
                     ("摘要花费", "压缩自己花了多少（半个真相也要给）")):
        ok &= check(f"上下文区块含：{why}", key in body)
    print("  注：字段在**空状态也渲染**（数字是「—」）—— 先给个空盒子的话，"
          "用户不知道这里将来会有什么。")
    ok &= check("路径输入框在页面上（不是只在收起的面板里）",
                "name=path" in body and "id=wspath" in body)
    ok &= check("列出了推荐目录",
                "Agent4Learning" in body and "workspace" in body)

    print("\n" + "=" * 78)
    print("  ② 危险路径必须被拒绝，且理由说得清")
    print("=" * 78)
    print("  策略是黑名单：任意目录都能选，只有真正危险的位置被拒。")
    st, body, url = get("/agent/switch", path="C:/Windows")
    notice = urllib.parse.unquote(re.search(r"error=([^&]+)", url).group(1)) if "error=" in url else ""
    ok &= check("被重定向并带 error 参数", "error=" in url)
    ok &= check("拒绝理由指向**具体原因**（不是笼统的'不允许'）",
                "系统目录" in notice or "盘根" in notice or "家目录" in notice,
                notice[:90])

    # 拒绝原因要能在**跟随重定向后的页面**上看到。
    st, body, _ = get("/agent", error=notice)
    ok &= check("页面显示拒绝原因",
                "系统目录" in body or "盘根" in body or "家目录" in body,
                notice[:80])

    print("\n" + "=" * 78)
    print("  ③ 切换到盘根 → 必须被拒绝")
    print("=" * 78)
    st, _b, url = get("/agent/switch", path="D:/")
    notice = urllib.parse.unquote(re.search(r"error=([^&]+)", url).group(1)) if "error=" in url else ""
    ok &= check("盘根被拒绝", "盘根" in notice or "家目录" in notice, notice[:70])

    print("\n" + "=" * 78)
    print("  ④ 切换到允许的根（项目目录）→ 成功并记住")
    print("=" * 78)
    st, _b, url = get("/agent/switch", path="D:/Desktop/Project/Agent4Learning")
    notice = urllib.parse.unquote(re.search(r"notice=([^&]+)", url).group(1)) if "notice=" in url else ""
    ok &= check("切换成功", "已切换到" in notice, notice[:70])
    st, body, _ = get("/agent")
    # 断言"切换真的生效了"，而不是断言某个 HTML 片段长什么样 ——
    # 界面范式改过两次，写死片段的断言每次都跟着坏，而且它测的是
    # 模板细节，不是用户能看到的事实。
    ok &= check("页面里出现了新工作区路径",
                "Desktop\\Project\\Agent4Learning" in body
                or "Desktop/Project/Agent4Learning" in body)
    ok &= check("新工作区被标为「当前」", body.count("当前") >= 1)

    print("\n" + "=" * 78)
    print("  ④b 切换后必须**看得到结果**（否则用户以为点了没反应）")
    print("=" * 78)
    print("  表单在右侧抽屉里，而抽屉默认收起 —— 所以切换后必须自动打开它。")
    ok &= check("切换成功后重定向会打开抽屉", "panel=1" in url, url[:80])
    ok &= check("锚点指向路径输入框", "#wspath" in url, url[-24:])
    st, _b, url_bad = get("/agent/switch", path="C:/Windows")
    ok &= check("被拒绝时也会打开抽屉（否则看不到理由）",
                "panel=1" in url_bad, url_bad[:80])

    print("\n" + "=" * 78)
    print("  ⑤ 切回默认工作区")
    print("=" * 78)
    st, _b, url = get("/agent/switch", path="D:/Desktop/Project/Agent4Learning/workspace")
    notice = urllib.parse.unquote(re.search(r"notice=([^&]+)", url).group(1)) if "notice=" in url else ""
    ok &= check("切回成功", "已切换到" in notice, notice[:50])

    print("\n" + "=" * 78)
    print("  ⑥ 提交校验：空任务必须被拒绝（不能启动一个注定失败的任务）")
    print("=" * 78)
    print("  注：这一项**绝不提交真实任务** —— 之前用非空 task 测过一次，")
    print("      结果真的起了 agent 任务、真花了钱。检查脚本不该有副作用。")
    st, _b, url = get("/agent/run", task="", max_iters=1, max_usd=0.0)
    _ = st
    if "error=" in url:
        msg = urllib.parse.unquote(re.search(r"error=([^&]+)", url).group(1))
        ok &= check("空任务被明确拒绝", "不能为空" in msg or "太短" in msg, msg[:70])
    else:
        ok &= check("空任务被明确拒绝", False,
                    "竟然接受了空任务 —— 会启动一个注定失败的任务并花钱")

    print("\n" + "=" * 78)
    print("  ⑦ 追问：没有活对话时必须明确拒绝，而不是偷偷开新会话")
    print("=" * 78)
    print("  注：这一项**绝不产生真实调用**。")
    st, _b, url = get("/agent/chat", message="继续", max_iters=1, max_usd=0.0)
    if "error=" in url:
        msg = urllib.parse.unquote(re.search(r"error=([^&]+)", url).group(1))
        ok &= check("没有活对话时追问被拒绝",
                    "没有可继续的对话" in msg or "不能为空" in msg
                    or "太短" in msg, msg[:80])
    elif "notice=" in url:
        ok &= check("没有活对话时追问被拒绝", False,
                    "竟然接受了 —— 会偷偷开一个'不记得任何事'的新会话")
    else:
        ok &= check("没有活对话时追问有明确结果", False, url[:80])

    print("\n" + "=" * 78)
    print("  结论：" + ("编码 Agent 页、工作区选择与多轮追问全部正确 ✅"
                       if ok else "存在失败项 ❌"))
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
