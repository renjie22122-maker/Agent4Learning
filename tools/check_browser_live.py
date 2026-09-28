"""Opt-in real browser integration check with synthetic local HTML only."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentplat.browser_tools import BrowserSession
from agentplat.workspace import Workspace

with TemporaryDirectory() as td:
    root = Path(td)
    (root/'index.html').write_text('''<meta charset="utf-8"><title>Fixture</title><input id="x"><button onclick="document.getElementById('out').textContent=document.getElementById('x').value">Apply</button><p id="out">Ready</p>''', encoding='utf-8')
    browser = BrowserSession(Workspace(root))
    try:
        assert browser.call('preview', path='index.html')['title'] == 'Fixture'
        browser.call('fill', selector='#x', text='中文测试')
        assert '中文测试' in browser.call('click', selector='button')['text']
        assert browser.call('check',selector='#out',expected_text='中文测试')['matched']
        assert Path(browser.call('screenshot')['path']).is_file()
        try:
            browser.call('open', url='file:///C:/Windows/win.ini')
            raise AssertionError('file URL accepted')
        except RuntimeError: pass
        print('PASS: preview, fill, click, Chinese snapshot, browser assertion, screenshot, file URL denied')
    finally: browser.close()
