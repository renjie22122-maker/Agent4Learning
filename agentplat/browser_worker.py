"""Playwright broker with domain-checked, pinned-IP GET transport."""
import json
from pathlib import Path
import sys
import uuid
import time
from urllib.parse import urlsplit, unquote


def main():
    dependencies = Path(__file__).resolve().parents[1] / '.agent-runtime' / 'dependencies'
    if dependencies.exists(): sys.path.insert(0, str(dependencies))
    from playwright.sync_api import sync_playwright
    from .workspace import Workspace
    from .sources import SourceStore
    from .web_policy import policy,allowed
    ws = Workspace(Path(sys.argv[1])); store = SourceStore(ws.root); store.policy_provider = policy
    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel='msedge', headless=True)
        context = browser.new_context(service_workers='block', accept_downloads=False)
        blocked = []
        preview = {}
        def route_request(route):
            try:
                if route.request.method != 'GET': raise PermissionError('当前浏览器只允许 GET；不提交外部写操作')
                parts = urlsplit(route.request.url)
                if parts.hostname == 'agent-preview.invalid' and preview:
                    import mimetypes
                    path = ws.resolve(unquote(parts.path).lstrip('/'))
                    route.fulfill(status=200, body=path.read_bytes(), content_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
                    return
                source = store.fetch(route.request.url, max_bytes=2_000_000, timeout_s=10)
                raw = (ws.root / source['path']).read_bytes()
                route.fulfill(status=200, body=raw, content_type=source['content_type'] or 'application/octet-stream')
            except Exception as exc:
                blocked.append(str(exc)); route.abort()
        context.route('**/*', route_request)
        context.route_web_socket('**/*', lambda socket: socket.close())
        page = context.new_page(); page.set_default_timeout(15000)
        for line in sys.stdin:
            try:
                args = json.loads(line); action = args['action']; blocked.clear()
                if action == 'open':
                    parts = urlsplit(args['url'])
                    if parts.scheme not in ('http','https') or not allowed(parts.hostname or ""):
                        raise PermissionError('浏览器 URL 必须为已授权的 HTTP(S) 域名')
                    page.goto(args['url'], wait_until='domcontentloaded')
                elif action == 'preview':
                    path = ws.resolve(args['path'])
                    if path.suffix.lower() not in ('.html','.htm'): raise ValueError('预览仅支持 HTML')
                    preview['enabled'] = True
                    page.goto('https://agent-preview.invalid/' + path.relative_to(ws.root).as_posix(), wait_until='domcontentloaded')
                elif action == 'click':
                    href = page.locator(args['selector']).evaluate('(e)=>e.closest("a")?.href || ""')
                    if href and urlsplit(href).scheme not in ('http','https'): raise PermissionError('禁止点击非 HTTP(S) 链接')
                    page.locator(args['selector']).click()
                elif action == 'check':
                    expected=args['expected_text']
                    if not expected or len(expected)>2000:raise ValueError('预期文本必须为 1 到 2000 字符')
                    target=page.locator(args['selector']);target.wait_for(state='visible')
                    deadline=time.monotonic()+10
                    while True:
                        actual=target.inner_text()
                        if (expected in actual if args.get('exact') is False else expected.strip()==actual.strip()):break
                        if time.monotonic()>=deadline:raise AssertionError('浏览器断言失败：'+actual[:1000])
                        page.wait_for_timeout(100)
                    print('BROWSER_RESULT '+json.dumps({'matched':True,'url':page.url,'selector':args['selector'],
                        'expected_text':expected,'actual':actual[:2000]}),flush=True);continue
                elif action == 'fill': page.locator(args['selector']).fill(args['text'])
                elif action == 'screenshot':
                    target = ws.root / '.browser' / (uuid.uuid4().hex + '.png')
                    target.parent.mkdir(exist_ok=True); page.screenshot(path=str(target), full_page=True)
                    print('BROWSER_RESULT ' + json.dumps({'path':str(target)}), flush=True); continue
                elif action != 'snapshot': raise ValueError('未知浏览器操作')
                result = {'url':page.url, 'title':page.title(), 'text':page.locator('body').inner_text()[:20000],
                          'links':page.locator('a').evaluate_all('(xs)=>xs.slice(0,100).map(x=>({text:x.innerText,url:x.href}))'),
                          'blocked_requests':blocked[:20], 'untrusted_reference':True}
                print('BROWSER_RESULT ' + json.dumps(result), flush=True)
            except Exception as exc:
                print('BROWSER_RESULT ' + json.dumps({'error':str(exc), 'blocked_requests':blocked[:20]}), flush=True)
        browser.close()


if __name__ == '__main__': main()
