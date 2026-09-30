"""UI language boundary and delivery layout regression tests."""
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat import ui
from agentplat.pages_agent import _turn_agent, _turn_timeline


class LanguageTests(unittest.TestCase):
    def test_default_shell_and_switch(self):
        for body in (ui.page('Settings','settings','<h1>设置</h1>'),
                     ui.page_chat('Agent','agent','<div class="side-h"></div>','')):
            text=body.decode()
            self.assertIn('lang=en',text)
            self.assertIn('agent-ui-language',text)
            self.assertIn('data-user-content',text)

    def test_no_fixed_summary_heading_or_rewritten_prose(self):
        text=_turn_agent('','','直接给出答案。')
        self.assertNotIn('本轮结论',text)
        self.assertIn('直接给出答案。',text)
        text=_turn_timeline({'acceptance':{'status':'passed'}},[],'','直接给出答案。','turn-1')
        self.assertIn('acceptance-status',text)
        self.assertLess(text.index('直接给出答案。'),text.index('独立验收通过'))
        self.assertNotIn('acceptance-status',_turn_timeline({},[],'','答案','turn-2'))

    def test_catalog_is_plain_data(self):
        path=Path(__file__).resolve().parents[1]/'agentplat'/'ui_catalog.json'
        catalog=json.loads(path.read_text(encoding='utf-8'))
        self.assertGreater(len(catalog),100)
        self.assertTrue(all(isinstance(k,str) and isinstance(v,str) for k,v in catalog.items()))
        self.assertTrue(all('<' not in v and '>' not in v for v in catalog.values()))


if __name__=='__main__':unittest.main()
