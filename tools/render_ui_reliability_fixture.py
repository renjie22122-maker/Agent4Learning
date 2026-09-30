"""Generate the standards-mode fixture for test_ui_reliability_browser.cjs."""
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentplat.pages_agent import _thread, LIVE_JS
from agentplat.human_input_ui import HUMAN_JS

now=time.time()
fence=chr(96)*3
text=r'Inline $x^2$ and \(y_1\).'+'\n\n$$\n'+r'\frac{a}{b}'+'\n$$\n\n'+fence+'python\nprint(42)\n'+fence
active={'session_id':'fixture','status':'running','started_at':now,'task':'Render rich content',
        'progress_messages':[text],'progress_times':[now],'human_questions':[],
        'steps':[{'kind':'diff','title':'result.txt','detail':'-old\n+new\n','at':now}]}
thread=_thread(active)
page='''<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/ui-assets/katex.min.css">
<script defer src="/ui-assets/katex.min.js"></script>
<script defer src="/ui-assets/highlight.min.js"></script>
<script defer src="/ui-assets/enhancements.js"></script>
<style>#scroll{height:75vh;overflow:auto}body{background:#20232c;color:#ddd}button{padding:8px}</style>
</head><body><div class=top><span class=ttl></span></div><div id=scroll><div class=col>'''+thread+'''
</div><div id=human-history></div></div><div id=human-input data-session=FIXTURE_SESSION></div>
<form id=sendform data-running=true><input name=session value=FIXTURE_SESSION>
<textarea id=m></textarea><button class=send>Send</button></form>'''+LIVE_JS+HUMAN_JS+'</body></html>'
out=Path(__file__).resolve().parents[1]/'.diagnostics/ui-reliability'
out.mkdir(parents=True,exist_ok=True)
(out/'legacy-fixture.json').write_text(json.dumps({'page':page,'thread':thread}),encoding='utf-8')
print(out/'legacy-fixture.json')
