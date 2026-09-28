"""Explicit, immediately reloadable public-web access preferences."""
import html
from . import ui, web_policy


def render(demo, qs):
    state = web_policy.policy()
    escape = html.escape
    token = f'<input type="hidden" name="token" value="{escape(demo.permissions_token, quote=True)}">'
    options = ''.join(
        f'<p><label><input type="radio" name="value" value="{key}" {"checked" if state["mode"] == key else ""}> <b>{label}</b> — {description}</label></p>'
        for key, label, description in (
            ('public', '允许公开网页（推荐）', '访问公开网站，无需逐个添加域名'),
            ('allowlist', '仅指定网站', '只访问下面列出的网站'),
            ('off', '关闭网页访问', '暂停网页工具联网；保留网站列表'),
        )
    )
    rows = ''.join(f'<form method="post" action="/permissions/web" style="display:flex;gap:16px;align-items:center;margin:8px 0">{token}<span style="flex:1;overflow-wrap:anywhere">{escape(host)}</span><input type="hidden" name="value" value="{escape(host)}"><button name="action" value="remove">移除</button></form>' for host in state['domains'])
    body = f'''<h1>网页访问设置</h1><p><a href="/agent">← 返回对话</a></p>
    <p role="status">{escape(qs.get('notice', ''))}</p>
    <section class="card"><h2>访问模式</h2><form method="post" action="/permissions/web">{token}{options}<button name="action" value="mode">保存访问模式</button></form>
    <p>影响此服务中所有会话的网页与浏览器工具，保存后即时生效。公开模式仍阻止本机、内网和云元数据地址；这里不改变 shell 命令的网络权限。</p></section>
    <section class="card"><h2>指定网站列表</h2><p>仅在“仅指定网站”模式下限制访问。可粘贴完整网址或域名，多个网站用换行或逗号分隔。</p>
    <form method="post" action="/permissions/web">{token}<textarea name="value" rows="3" required placeholder="https://www.example.com/article\nexample.org" style="width:100%;box-sizing:border-box"></textarea><button name="action" value="add">添加网站</button></form>
    <p>按完整域名匹配，例如 example.com 与 www.example.com 需要分别添加。</p>{rows or '<p>尚未添加网站。</p>'}</section>'''
    return ui.page('网页访问设置', 'settings', body)
