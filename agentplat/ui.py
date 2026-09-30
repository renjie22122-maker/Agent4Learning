"""界面外壳：导航、样式、可复用组件。

把 HTML 生成集中在这里，页面模块只关心内容 —— 否则改一次配色要动十几个文件。

设计原则（都是为了"能看懂"）：
* **深色、低饱和**：长时间盯指标不刺眼。
* **等宽字体显示数字**：指标对齐才能横向比较。
* **状态用颜色 + 文字双编码**：只靠颜色的话，色弱用户读不出来。
* **每个数字旁边有"这说明什么"**：不能只有数字没有解释。
"""

from __future__ import annotations

import html
from typing import Sequence

#: 主色板。集中定义，页面里只用变量名，不写字面色值。
PALETTE = {
    "bg": "#0e1117",
    "panel": "#161a22",
    "panel2": "#1c212b",
    "border": "#262c38",
    "text": "#e3e8ef",
    "dim": "#8b97a8",
    "accent": "#4c8dff",
    "ok": "#3fb950",
    "warn": "#d29922",
    "bad": "#f85149",
    "mono": "ui-monospace,SFMono-Regular,Consolas,'Cascadia Mono',monospace",
}

CSS = f"""
*{{box-sizing:border-box}}
body{{margin:0;background:{PALETTE['bg']};color:{PALETTE['text']};
  font:14px/1.65 -apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;
  -webkit-font-smoothing:antialiased}}
a{{color:{PALETTE['accent']};text-decoration:none}}
a:hover{{text-decoration:underline}}
code,kbd,.mono{{font-family:{PALETTE['mono']}}}

/* ---- 顶部导航 ---- */
header{{background:{PALETTE['panel']};border-bottom:1px solid {PALETTE['border']};
  position:sticky;top:0;z-index:50}}
.hd{{max-width:1320px;margin:0 auto;padding:0 20px;display:flex;align-items:center;gap:22px;height:56px}}
.brand{{font-weight:600;font-size:15px;letter-spacing:.2px;white-space:nowrap}}
.brand span{{color:{PALETTE['dim']};font-weight:400;font-size:12.5px;margin-left:8px}}
nav{{display:flex;gap:2px;flex:1;overflow-x:auto}}
nav a{{padding:7px 13px;border-radius:7px;color:{PALETTE['dim']};font-size:13.5px;white-space:nowrap}}
nav a:hover{{background:{PALETTE['panel2']};color:{PALETTE['text']};text-decoration:none}}
nav a.on{{background:{PALETTE['accent']};color:#fff}}
.env{{font-size:12px;padding:4px 10px;border-radius:20px;white-space:nowrap;
  border:1px solid {PALETTE['border']};background:{PALETTE['panel2']}}}

.wrap{{max-width:1320px;margin:0 auto;padding:24px 20px 70px}}
h1{{font-size:20px;margin:0 0 6px;font-weight:600}}
h2{{font-size:15px;margin:26px 0 10px;color:{PALETTE['text']};font-weight:600}}
h3{{font-size:13.5px;margin:18px 0 8px;color:{PALETTE['dim']};font-weight:600;
  text-transform:uppercase;letter-spacing:.6px}}
.lead{{color:{PALETTE['dim']};font-size:13.5px;margin:0 0 20px;max-width:900px}}

/* ---- 卡片 ---- */
.card{{background:{PALETTE['panel']};border:1px solid {PALETTE['border']};
  border-radius:10px;padding:16px 18px;margin:14px 0}}
.card.tight{{padding:12px 14px}}
.grid{{display:grid;gap:14px}}
.g2{{grid-template-columns:repeat(auto-fit,minmax(330px,1fr))}}
.g3{{grid-template-columns:repeat(auto-fit,minmax(215px,1fr))}}
.g4{{grid-template-columns:repeat(auto-fit,minmax(168px,1fr))}}

/* ---- 指标块 ---- */
.metric{{background:{PALETTE['panel2']};border:1px solid {PALETTE['border']};
  border-radius:9px;padding:12px 14px}}
.metric .k{{font-size:11.5px;color:{PALETTE['dim']};letter-spacing:.3px;
  text-transform:uppercase;margin-bottom:4px}}
.metric .v{{font-family:{PALETTE['mono']};font-size:21px;font-weight:600;line-height:1.25}}
.metric .s{{font-size:11.5px;color:{PALETTE['dim']};margin-top:3px}}

/* ---- 表格 ---- */
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;padding:8px 10px;color:{PALETTE['dim']};font-weight:500;
  border-bottom:1px solid {PALETTE['border']};font-size:12px;
  text-transform:uppercase;letter-spacing:.4px;white-space:nowrap}}
td{{padding:8px 10px;border-bottom:1px solid {PALETTE['panel2']};vertical-align:top}}
tr:last-child td{{border-bottom:none}}
td.num,th.num{{text-align:right;font-family:{PALETTE['mono']}}}
tr:hover td{{background:{PALETTE['panel2']}}}

/* ---- 状态 ---- */
.ok{{color:{PALETTE['ok']}}}.warn{{color:{PALETTE['warn']}}}.bad{{color:{PALETTE['bad']}}}
.dim{{color:{PALETTE['dim']}}}
.pill{{display:inline-block;padding:1.5px 9px;border-radius:20px;font-size:11.5px;
  border:1px solid {PALETTE['border']};background:{PALETTE['panel2']};
  font-family:{PALETTE['mono']}}}
.pill.ok{{color:{PALETTE['ok']};border-color:#1d5130;background:#10251a}}
.pill.bad{{color:{PALETTE['bad']};border-color:#5c2226;background:#2a1417}}
.pill.warn{{color:{PALETTE['warn']};border-color:#5c4a15;background:#2a2311}}
.pill.info{{color:{PALETTE['accent']};border-color:#1e3f6d;background:#111d2e}}

/* ---- 表单 ---- */
form{{margin:0}}
.row{{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}}
label{{display:block;font-size:11.5px;color:{PALETTE['dim']};margin-bottom:4px;
  text-transform:uppercase;letter-spacing:.3px}}
input,select,textarea,button{{font:inherit;font-size:13.5px;color:{PALETTE['text']};
  background:{PALETTE['panel2']};border:1px solid {PALETTE['border']};
  border-radius:8px;padding:8px 11px;outline:none}}
input:focus,select:focus,textarea:focus{{border-color:{PALETTE['accent']}}}
input[name=q],input.wide{{flex:1;min-width:260px}}
button{{background:{PALETTE['accent']};border-color:{PALETTE['accent']};color:#fff;
  cursor:pointer;font-weight:500;white-space:nowrap}}
button:hover{{filter:brightness(1.12)}}
button.ghost{{background:{PALETTE['panel2']};border-color:{PALETTE['border']};
  color:{PALETTE['text']}}}
button.danger{{background:#7d2b2f;border-color:#7d2b2f}}
.field{{display:flex;flex-direction:column}}
.hint{{font-size:12px;color:{PALETTE['dim']};margin-top:5px}}

/* ---- 回答区 ---- */
.answer{{background:{PALETTE['panel2']};border-left:3px solid {PALETTE['ok']};
  border-radius:0 8px 8px 0;padding:13px 16px;white-space:pre-wrap;
  font-size:14px;margin:12px 0}}
.answer.err{{border-left-color:{PALETTE['bad']}}}
pre{{background:#0b0e13;border:1px solid {PALETTE['border']};border-radius:8px;
  padding:12px;overflow-x:auto;font-size:12.5px;margin:8px 0;
  font-family:{PALETTE['mono']}}}

/* ---- 进度条 ---- */
.bar{{height:7px;background:{PALETTE['panel2']};border-radius:4px;overflow:hidden;
  min-width:70px;flex:1}}
.bar > i{{display:block;height:100%;background:{PALETTE['accent']}}}
.barwrap{{display:flex;align-items:center;gap:9px;min-width:190px}}

/* ---- span 树 ---- */
.spans{{font-family:{PALETTE['mono']};font-size:12.5px}}
.spans .r{{display:grid;grid-template-columns:130px 1fr 92px;gap:10px;
  align-items:center;padding:3px 0}}
.spans .r > .n{{color:{PALETTE['dim']}}}

/* ---- 标签页（页面内二级） ---- */
.tabs{{display:flex;gap:4px;border-bottom:1px solid {PALETTE['border']};
  margin:20px 0 4px;overflow-x:auto}}
.tabs a{{padding:8px 14px;color:{PALETTE['dim']};font-size:13.5px;
  border-bottom:2px solid transparent;white-space:nowrap}}
.tabs a:hover{{color:{PALETTE['text']};text-decoration:none}}
.tabs a.on{{color:{PALETTE['text']};border-bottom-color:{PALETTE['accent']}}}

.empty{{color:{PALETTE['dim']};padding:26px 4px;text-align:center;font-size:13.5px}}
.kv{{display:grid;grid-template-columns:172px 1fr;gap:7px 14px;font-size:13.5px}}
.kv dt{{color:{PALETTE['dim']}}}
.kv dd{{margin:0}}
"""

#: 顶层标签页。id, 路径, 显示名
TABS: tuple[tuple[str, str, str], ...] = (
    ("agent", "/agent", "编码 Agent"),
    ("console", "/", "对话工作台"),
    ("models", "/models", "模型与路由"),
    ("cache", "/cache", "缓存"),
    ("resilience", "/resilience", "熔断与限流"),
    ("history", "/history", "请求历史"),
    ("sessions", "/sessions", "会话与隔离"),
    ("metrics", "/metrics-view", "指标与成本"),
    ("settings", "/settings", "LLM 设置"),
)


def esc(x) -> str:
    return html.escape(str(x))


def nav(active: str) -> str:
    """导航包含"环境指示器"——用户必须一眼看出当前是真实模型还是模拟器。

    真实/模拟混淆是很危险的：看着像真在答题，其实是本地模板。
    所以状态在**每一个页面**的右上角常驻。
    """
    from .ui_state import env_badge

    items = "".join(
        f'<a href="{path}" class="{"on" if key == active else ""}">{esc(label)}</a>'
        for key, path, label in TABS
    )
    return (
        f'<header><div class="hd">'
        f'<div class="brand">Agent4Learning<span>生产级 Agent 平台</span></div>'
        f"<nav>{items}</nav>{env_badge()}"
        f"</div></header>"
    )


def page(title: str, active: str, body: str, refresh_s: int = 0) -> bytes:
    from .ui_i18n import assets
    meta = f'<meta http-equiv="refresh" content="{refresh_s}">' if refresh_s else ""
    return (
        f"<!doctype html><html lang=en><head><meta charset=utf-8>"
        f'<meta name=viewport content="width=device-width,initial-scale=1">'
        f"{meta}<title>{esc(title)} · Agent4Learning</title>"
        f"<style>{CSS}</style></head><body>"
        f"{nav(active)}<div class=wrap>{body}</div>{assets()}"
        '<script defer src="/ui-assets/enhancements.js"></script></body></html>'
    ).encode("utf-8")


# ==========================================================================
# 对话外壳（chat shell）
# ==========================================================================
# 为什么需要**第二套**外壳：
# 原来所有页面共用"顶部标签页 + 卡片 + 表格"这一套。它适合看指标，
# 但对话界面用它就很别扭 —— 每次提问都刷新整页、没有消息流、输入框
# 埋在表单里。用户的原话是"丑得要死"，问题不在配色，而在**布局范式错了**：
# 指标看板 ≠ 对话客户端。
#
# 所以：dashboard 页继续用 `page()`；编码 Agent 用 `page_chat()`。
# 两套共用同一个 CSS 变量与组件函数（card/table/pill），不重复实现。
#
# DSH 规模的三栏结构（侧栏 / 消息流 / 底部输入）在这里是对的：
# 左侧放会话与工作区（低频操作），中间放消息流（视觉主体），
# 底部固定输入框（高频，永远在同一个位置）。主流 Agent 客户端
# （Cline / Cursor / DSH）都是这个结构，不是审美偏好，是**操作频率决定的**。

# ==========================================================================
# DS 设计令牌 —— 从 DSH 的设计系统里直接提取
# ==========================================================================
# 这些值不是我调出来的，是**从 DSH 自己的主题包里读出来的**：
#   D:\DSH\node_modules\.pnpm\node_modules\@deepseek-ai\dsh-client-ui-theme\lib\client.js
#   → `design_platform_css_default`（--dsw-static-*）与别名层（--dsw-alias-*）
#
# 只用 alias 层（语义名）而不直接写色值，是为了**换主题时只改一处**：
# DSH 的 dark theme 就是通过覆写 alias 层实现的（同一套 static 调色板，
# 两套 alias 映射）。这个分层值得照搬 —— 否则每一页都会散落硬编码色值。
#
# 对齐关系（浅色 → 深色）：
#   bg-base / bg-layer-1/2/3   #fff → #151517 / #1b1b1c / #232324
#   label-primary/secondary/tertiary  #0f1115 / #61666b / #81858c
#                              → #f9fafb / #cfd3d6 / #adb2b8
#   border-l1/l2/l3/l4         黑 4% → 白 8%（所以边框用 alpha，不用固定灰）
DS_DARK = {
    # static 调色板（只取用到的）
    "bluish-00": "#fff", "bluish-50": "#f9fafb", "bluish-60": "#f5f6f7",
    "bluish-75": "#f1f3f5", "bluish-100": "#ebeef2", "bluish-150": "#e9ecf2",
    "bluish-200": "#e1e5ee", "bluish-300": "#cfd3d6", "bluish-400": "#adb2b8",
    "bluish-500": "#979da6", "bluish-600": "#81858c", "bluish-700": "#61666b",
    "bluish-750": "#43454a", "bluish-800": "#353638", "bluish-850": "#2c2c2e",
    "bluish-875": "#232324", "bluish-900": "#1b1b1c", "bluish-950": "#151517",
    "bluish-1000": "#0f1115",
    "deepseek-400": "#679efe", "deepseek-500": "#4176e6",
    "green-400": "#4ed17e", "green-500": "#22c55e",
    "amber-400": "#f7ad31", "amber-500": "#f59e0b",
    "neutral-400": "#a2a4a6",
}

#: 深色主题下的 **alias 层**（页面只引用这一层）
DS_ALIAS_DARK = {
    "bg-base": DS_DARK["bluish-950"],
    "bg-layer-1": DS_DARK["bluish-900"],
    "bg-layer-2": DS_DARK["bluish-875"],
    "bg-layer-3": DS_DARK["bluish-850"],
    "bg-module": DS_DARK["bluish-900"],
    "label-primary": DS_DARK["bluish-50"],
    "label-secondary": DS_DARK["bluish-300"],
    "label-tertiary": DS_DARK["bluish-400"],
    "label-caption": DS_DARK["bluish-500"],
    "border-l1": "rgb(255 255 255 / 6%)",
    "border-l2": "rgb(255 255 255 / 8%)",
    "border-l3": "rgb(255 255 255 / 12%)",
    "border-l4": "rgb(255 255 255 / 16%)",
    "interactive-hover": "rgb(255 255 255 / 6%)",
    "interactive-active": "rgb(255 255 255 / 10%)",
    "brand": DS_DARK["deepseek-500"],
    "link": DS_DARK["deepseek-400"],
    "ok": DS_DARK["green-400"],
    "warn": DS_DARK["amber-400"],
    "bad": "#f85149",
    "mono": ("ui-monospace,SFMono-Regular,'Cascadia Mono',Consolas,"
             "'Liberation Mono',monospace"),
    "sans": ("-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',"
             "'Hiragino Sans GB','Microsoft YaHei',sans-serif"),
}


def _ds_vars() -> str:
    return "".join(f"--ds-{k}:{v};" for k, v in DS_ALIAS_DARK.items())


#: 对话外壳样式。**照着 DSH 的设计语言写**：小字号（12~14px）、
#: 克制的圆角（6~12px）、用 alpha 边框而不是实色灰、留白靠间距而不是分割线。
CHAT_CSS = f"""
:root{{{_ds_vars()}}}
*{{box-sizing:border-box}}
html,body{{height:100%}}
body.chat{{margin:0;background:var(--ds-bg-base);color:var(--ds-label-primary);
  font:14px/22px var(--ds-sans);-webkit-font-smoothing:antialiased;
  height:100vh;overflow:hidden;display:flex;flex-direction:column}}
body.chat a{{color:var(--ds-link);text-decoration:none}}
body.chat a:hover{{text-decoration:underline}}
.mono{{font-family:var(--ds-mono);font-variant-numeric:tabular-nums}}
.shell{{flex:1;display:flex;min-height:0}}

/* ---------- 左侧栏（DSH 是 240px 级别的窄栏） ---------- */
.side{{width:248px;flex-shrink:0;display:flex;flex-direction:column;min-height:0;
  background:var(--ds-bg-base);border-right:.5px solid var(--ds-border-l2)}}
.side-h{{padding:16px 14px 10px;display:flex;align-items:center;gap:8px}}
.side-h .mark{{width:22px;height:22px;border-radius:6px;flex-shrink:0;
  background:var(--ds-brand);display:grid;place-items:center;
  color:#fff;font-size:12px;font-weight:700}}
.side-h .nm{{font-size:14px;font-weight:600;letter-spacing:.01em}}
.side-b{{flex:1;overflow-y:auto;padding:2px 8px 12px}}
.side-f{{padding:8px;border-top:.5px solid var(--ds-border-l2)}}
.grp{{display:flex;align-items:center;gap:6px;color:var(--ds-label-caption);
  font-size:11px;font-weight:500;padding:14px 6px 6px;letter-spacing:.02em}}
.grp .n{{margin-left:auto;font-variant-numeric:tabular-nums}}
.item{{display:block;padding:7px 9px;border-radius:8px;font-size:13px;
  line-height:18px;color:var(--ds-label-secondary);margin-bottom:1px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.item:hover{{background:var(--ds-interactive-hover);text-decoration:none;
  color:var(--ds-label-primary)}}
.item.on{{background:var(--ds-interactive-active);color:var(--ds-label-primary)}}
.item .sub{{color:var(--ds-label-caption);font-size:11px;margin-top:2px;
  display:flex;align-items:center;gap:5px}}
.dot{{width:6px;height:6px;border-radius:50%;flex-shrink:0;display:inline-block}}
.dot.run{{background:var(--ds-warn)}} .dot.ok{{background:var(--ds-ok)}}
.dot.bad{{background:var(--ds-bad)}} .dot.idle{{background:var(--ds-label-caption)}}

/* ---------- 主区 ---------- */
.main{{flex:1;display:flex;flex-direction:column;min-width:0;min-height:0}}
.top{{height:48px;flex-shrink:0;display:flex;align-items:center;gap:10px;
  padding:0 16px;border-bottom:.5px solid var(--ds-border-l2)}}
.top .ttl{{font-size:14px;font-weight:600}}
.top .spacer{{flex:1}}
.iconbtn{{width:30px;height:30px;border-radius:8px;border:none;cursor:pointer;
  background:transparent;color:var(--ds-label-tertiary);font-size:14px;
  display:grid;place-items:center;text-decoration:none}}
.iconbtn:hover{{background:var(--ds-interactive-hover);
  color:var(--ds-label-primary);text-decoration:none}}
.iconbtn.on{{background:var(--ds-interactive-active);color:var(--ds-label-primary)}}
.wschip{{display:flex;align-items:center;gap:6px;max-width:46%;
  padding:4px 9px;border-radius:8px;background:var(--ds-bg-layer-1);
  border:.5px solid var(--ds-border-l2);color:var(--ds-label-secondary);
  font-size:12px;overflow:hidden}}
.wschip:hover{{background:var(--ds-interactive-hover);
  border-color:var(--ds-border-l4);color:var(--ds-label-primary);
  text-decoration:none}}
.wschip .p{{font-family:var(--ds-mono);font-size:11.5px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;direction:rtl;text-align:left}}
.item.add{{color:var(--ds-link);border:.5px dashed var(--ds-border-l3);
  text-align:center;margin-top:6px}}
.item.add:hover{{border-color:var(--ds-link);background:transparent}}

.banner{{flex-shrink:0;padding:8px 16px;font-size:12.5px;display:flex;
  gap:8px;align-items:center;border-bottom:.5px solid var(--ds-border-l2)}}
.banner.bad{{background:color-mix(in srgb,var(--ds-bad) 12%,transparent);
  color:var(--ds-bad)}}
.banner.ok{{background:color-mix(in srgb,var(--ds-ok) 12%,transparent);
  color:var(--ds-ok)}}
.banner.warn{{background:color-mix(in srgb,var(--ds-warn) 12%,transparent);
  color:var(--ds-warn)}}

/* ---------- 会话流 ---------- */
.scroll{{flex:1;overflow-y:auto;min-height:0}}
.col{{max-width:760px;margin:0 auto;padding:20px 20px 8px}}
.turn{{margin-bottom:22px}}
.who{{display:flex;align-items:center;gap:8px;font-size:12px;
  color:var(--ds-label-caption);margin-bottom:7px}}
.who .av{{width:20px;height:20px;border-radius:5px;display:grid;
  place-items:center;font-size:11px;font-weight:700;flex-shrink:0}}
.who .av.u{{background:var(--ds-bg-layer-3);color:var(--ds-label-primary)}}
.who .av.a{{background:var(--ds-brand);color:#fff}}
.who .t{{font-family:var(--ds-mono);font-size:11px}}
.say{{font-size:14px;line-height:23px;white-space:pre-wrap;
  overflow-wrap:break-word;color:var(--ds-label-primary)}}
.turn.u .say{{background:var(--ds-bg-layer-1);border:.5px solid var(--ds-border-l2);
  border-radius:10px;padding:9px 13px}}

/* 轨迹：默认只露最后几条，点「展开」看全部 —— 这就是 DSH 那种"对话轨迹" */
.trace{{margin-top:10px;border-left:1.5px solid var(--ds-border-l3);
  padding-left:14px}}
.trace .ln{{display:flex;gap:9px;align-items:baseline;padding:3px 0;
  font-size:12.5px;line-height:19px;color:var(--ds-label-tertiary)}}
.trace .ln .k{{flex-shrink:0;color:var(--ds-label-secondary)}}
.trace .ln .v{{font-family:var(--ds-mono);font-size:11.5px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}}
.trace .ln.tool .k{{color:var(--ds-link)}}
.trace .ln.finish .k{{color:var(--ds-ok)}}
.trace .ln.error .k{{color:var(--ds-bad)}}
.trace .ln.guard .k{{color:var(--ds-warn)}}
details.more{{margin-top:8px}}
details.more>summary{{list-style:none;cursor:pointer;color:var(--ds-label-caption);
  font-size:12px;display:inline-flex;align-items:center;gap:5px;
  padding:3px 8px;border-radius:6px}}
details.more>summary::-webkit-details-marker{{display:none}}
details.more>summary:hover{{background:var(--ds-interactive-hover);
  color:var(--ds-label-secondary)}}
details.more>summary:before{{content:"▸"}}
details.more[open]>summary:before{{content:"▾"}}
.stats{{display:flex;gap:10px;flex-wrap:wrap;margin-top:8px;font-size:11.5px;
  color:var(--ds-label-caption);font-family:var(--ds-mono)}}
.stats b{{font-weight:500;color:var(--ds-label-tertiary)}}

.hero{{padding:56px 20px 30px;text-align:center}}
.hero h1{{font-size:24px;line-height:34px;font-weight:600;margin:0 0 8px;
  letter-spacing:-.01em}}
.hero p{{color:var(--ds-label-tertiary);font-size:13.5px;margin:0 auto;
  max-width:520px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));
  gap:10px;margin-top:26px;text-align:left;max-width:700px;margin-inline:auto}}
.pcard{{border:.5px solid var(--ds-border-l2);border-radius:12px;padding:13px 15px;
  background:var(--ds-bg-layer-1);cursor:pointer;display:block}}
.pcard:hover{{border-color:var(--ds-border-l4);text-decoration:none}}
.pcard .t{{font-size:13px;font-weight:500;color:var(--ds-label-primary);
  margin-bottom:3px}}
.pcard .d{{font-size:12px;line-height:18px;color:var(--ds-label-tertiary)}}

/* ---------- 底部输入（DSH 是一个圆角盒子，不是表单堆） ---------- */
.dock{{flex-shrink:0;padding:6px 0 14px}}
.dockin{{max-width:760px;margin:0 auto;padding:0 20px}}
.box{{background:var(--ds-bg-layer-1);border:.5px solid var(--ds-border-l3);
  border-radius:14px;padding:10px 10px 8px 14px;display:flex;
  flex-direction:column;gap:8px}}
.box:focus-within{{border-color:var(--ds-border-l4)}}
.box textarea{{background:transparent;border:none;outline:none;resize:none;
  color:var(--ds-label-primary);font:14px/22px var(--ds-sans);
  min-height:22px;max-height:168px;padding:0}}
.box textarea::placeholder{{color:var(--ds-label-caption)}}
.boxrow{{display:flex;align-items:center;gap:8px}}
.boxrow .sp{{flex:1}}
.ctl{{display:flex;align-items:center;gap:5px;color:var(--ds-label-caption);
  font-size:11.5px}}
.ctl input,.ctl select{{background:var(--ds-bg-layer-2);color:var(--ds-label-secondary);
  border:.5px solid var(--ds-border-l2);border-radius:6px;padding:3px 6px;
  font:11.5px var(--ds-mono);width:58px;outline:none}}
.ctl input:focus{{border-color:var(--ds-border-l4)}}
.send{{border:none;border-radius:9px;background:var(--ds-brand);color:#fff;
  font:13px/1 var(--ds-sans);font-weight:500;padding:8px 16px;cursor:pointer}}
.send:hover{{filter:brightness(1.08)}}
.send:disabled{{background:var(--ds-bg-layer-3);color:var(--ds-label-caption);
  cursor:not-allowed}}
.foot{{text-align:center;color:var(--ds-label-caption);font-size:11.5px;
  margin-top:8px}}

/* ---------- 右侧面板（会话/工作区/说明） ---------- */
.panel{{width:0;flex-shrink:0;overflow:hidden;background:var(--ds-bg-base);
  border-left:.5px solid var(--ds-border-l2);transition:width .16s ease}}
.panel.open{{width:392px;overflow-y:auto}}
.panel .pin{{padding:16px 18px 40px;min-width:392px}}
.panel h2{{font-size:13px;font-weight:600;margin:22px 0 10px;
  color:var(--ds-label-primary)}}
.panel h2:first-child{{margin-top:0}}
.panel .mut{{color:var(--ds-label-tertiary);font-size:12px;line-height:18px}}
.panel .card{{background:var(--ds-bg-layer-1);border:.5px solid var(--ds-border-l2);
  border-radius:10px;padding:12px 14px}}
.panel table{{width:100%;border-collapse:collapse;font-size:12.5px}}
.panel th{{text-align:left;color:var(--ds-label-caption);font-weight:500;
  font-size:11px;padding:6px 8px;border-bottom:.5px solid var(--ds-border-l2)}}
.panel td{{padding:7px 8px;border-bottom:.5px solid var(--ds-border-l1);
  color:var(--ds-label-secondary);vertical-align:top}}
.panel td.num{{text-align:right;font-family:var(--ds-mono);
  font-variant-numeric:tabular-nums}}
.panel input[type=text],.panel input:not([type]){{width:100%;background:var(--ds-bg-layer-2);
  color:var(--ds-label-primary);border:.5px solid var(--ds-border-l2);
  border-radius:8px;padding:7px 10px;font:12.5px var(--ds-mono);outline:none}}
.panel input:focus{{border-color:var(--ds-border-l4)}}
.panel button{{border:none;border-radius:8px;background:var(--ds-bg-layer-3);
  color:var(--ds-label-primary);font:12.5px var(--ds-sans);padding:7px 13px;
  cursor:pointer}}
.panel button:hover{{background:var(--ds-interactive-active)}}
.panel button.pri{{background:var(--ds-brand);color:#fff}}
.panel .pill{{display:inline-block;padding:1px 7px;border-radius:20px;
  font-size:11px;background:var(--ds-bg-layer-3);color:var(--ds-label-secondary)}}
.panel .kv{{display:flex;justify-content:space-between;gap:10px;padding:5px 0;
  font-size:12.5px;border-bottom:.5px solid var(--ds-border-l1)}}
.panel .kv .k{{color:var(--ds-label-caption)}}
.panel .kv .v{{font-family:var(--ds-mono);color:var(--ds-label-secondary)}}

@media (max-width:1100px){{.panel.open{{width:0}}}}
@media (max-width:840px){{.side{{display:none}}}}
"""


def page_chat(title: str, active: str, sidebar: str, main: str,
              refresh_s: int = 0, extra_js: str = "", panel: str = "") -> bytes:
    """对话式外壳：左侧栏 + 会话流 + 可展开的右侧面板。

    与 `page()` 的区别不只是样式，而是**布局范式**：
      · `page()`      —— 顶部标签页 + 居中卡片，适合扫读指标；
      · `page_chat()` —— 全屏三栏 + 底部固定输入，适合连续对话。
    两者共用同一套 CSS 变量与组件，不重复实现。

    右侧面板默认宽度为 0（收起）。为什么用 CSS 宽度过渡而不是 `display:none`：
    展开/收起要有"抽屉"的手感；`display` 切换没有过渡，会突然跳一下。
    """
    from .ui_polish import ASSETS
    from .ui_i18n import assets
    from .reply_actions_ui import ASSETS as REPLY_ASSETS
    meta = f'<meta http-equiv="refresh" content="{refresh_s}">' if refresh_s else ""
    return (
        f"<!doctype html><html lang=en><head><meta charset=utf-8>"
        f'<meta name=viewport content="width=device-width,initial-scale=1">'
        f"{meta}<title>{esc(title)} · Agent4Learning</title>"
        f"<style>{CSS}{CHAT_CSS}</style></head>"
        f'<body class=chat><div class=shell>{sidebar}{main}{panel}</div>'
        f"{extra_js}{ASSETS}{REPLY_ASSETS}{assets()}"
        f"</body></html>"
    ).encode("utf-8")


def card(inner: str, cls: str = "") -> str:
    return f'<div class="card {cls}">{inner}</div>'


def metric(key: str, value: str, sub: str = "", tone: str = "") -> str:
    sub_html = f'<div class=s>{esc(sub)}</div>' if sub else ""
    return (
        f'<div class=metric><div class=k>{esc(key)}</div>'
        f'<div class="v {tone}">{value}</div>{sub_html}</div>'
    )


def pill(text: str, tone: str = "") -> str:
    return f'<span class="pill {tone}">{esc(text)}</span>'


def table(headers: Sequence[str], rows: Sequence[Sequence[str]],
          numeric: Sequence[int] = (), empty: str = "暂无数据") -> str:
    if not rows:
        return f'<div class=empty>{esc(empty)}</div>'
    th = "".join(
        f'<th class="{"num" if i in numeric else ""}">{esc(h)}</th>'
        for i, h in enumerate(headers)
    )
    body = []
    for r in rows:
        tds = "".join(
            f'<td class="{"num" if i in numeric else ""}">{c}</td>'
            for i, c in enumerate(r)
        )
        body.append(f"<tr>{tds}</tr>")
    return f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def bars(rows: Sequence[tuple[str, float, str]], total: float | None = None) -> str:
    """横向耗时条：用于 span 归因。total 为 None 时取最大值。"""
    if not rows:
        return '<div class=empty>没有可展示的阶段</div>'
    peak = total or max((v for _, v, _ in rows), default=1.0) or 1.0
    out = []
    for name, value, extra in rows:
        pct = max(0.0, min(100.0, value / peak * 100.0))
        out.append(
            f'<div class=r><span class=n>{esc(name)}</span>'
            f'<span class=barwrap><span class=bar><i style="width:{pct:.1f}%"></i></span>'
            f'<span class="mono">{value:,.1f}ms</span></span>'
            f'<span class=dim>{esc(extra)}</span></div>'
        )
    return f'<div class=spans>{"".join(out)}</div>'
