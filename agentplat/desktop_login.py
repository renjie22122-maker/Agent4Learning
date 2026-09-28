"""Reuse a host-only credential across restarts; never reveal it over an anonymous request."""
import json
import re


def existing_token(path, port, fallback):
    try:
        record=json.loads(path.read_text(encoding='utf-8'))
        token=record.get('token','')
        if record.get('port')==port and isinstance(token,str) and re.fullmatch('[a-f0-9]{32}',token):
            return token
    except (OSError,ValueError,TypeError):pass
    return fallback


def login_page():
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>登录 Agent4Learning</title><style>body{font:16px system-ui;background:#181818;color:#eee;max-width:560px;margin:12vh auto;padding:24px;line-height:1.8}main{padding:28px;border:1px solid #444;border-radius:16px}code{background:#303030;padding:4px}input{width:95%;padding:10px}button{margin-top:12px;padding:10px 22px}</style>
    <main><h1>打开你的 Agent</h1><p>这个浏览器尚未登录。请双击桌面的 Agent 启动入口，它会自动登录并打开页面。</p>
    <p>首次登录后，正常重启服务无需重新登录。</p>
    <details><summary>没有桌面入口？</summary><p>在项目目录运行：<br><code>python tools/start_desktop.py</code></p><p>也可粘贴本机登录链接：</p>
    <form id="login"><input id="link" type="password" autocomplete="off" required placeholder="专用登录链接"><button>登录</button><p id="error" role="alert"></p></form></details>
    <small>登录保护用于防止其他网页或沙箱任务控制你的服务。</small></main>
    <script>document.getElementById('login').onsubmit=e=>{e.preventDefault();try{const u=new URL(document.getElementById('link').value);if(u.origin!==location.origin||u.pathname!='/auth'||!u.searchParams.get('token'))throw Error();location.replace(u.href)}catch(e){document.getElementById('error').textContent='请输入当前本机服务的完整登录链接。'}};</script></html>'''.encode('utf-8')
