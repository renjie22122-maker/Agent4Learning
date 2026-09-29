"""Skip only recognized browser-host limitations, never application assertions."""
from pathlib import Path
import tempfile
import unittest


def require_browser():
    from agentplat.workspace import Workspace
    from agentplat.browser_tools import BrowserSession
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(Path(td)/'ws'); browser = BrowserSession(ws)
        (ws.root/'index.html').write_text('<p id="ready">ready</p>',encoding='utf-8')
        try:
            browser.call('preview',path='index.html')
            result = browser.call('check',selector='#ready',expected_text='ready',exact=True)
            if not result.get('matched'):
                raise AssertionError('Browser preflight loaded but assertion failed')
        except RuntimeError as exc:
            text = str(exc).lower()
            if any(marker in text for marker in ('mojo', "executable doesn't exist", 'browser executable not found', 'browsertype.launch: spawn eperm')):
                raise unittest.SkipTest('Browser host unavailable; skipped, not passed: '+str(exc)[:300]) from exc
            raise
        finally:
            browser.close(); ws.processes.close()
