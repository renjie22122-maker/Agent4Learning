"""UI rendering and bounded filesystem observations, without a live model."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tempfile
import unittest
from agentplat.markdown_view import render
from agentplat.file_changes import capture, capture_tree, tree_diff
from agentplat.workspace import Workspace
from agentplat.pages_agent import _turn_timeline, _trace


class UiReliability(unittest.TestCase):
    def test_math_formats_and_code(self):
        for text in (r'$x_1^2$', r'\(x_1^2\)', '$$\n\\frac{a}{b}\n$$', '\\[\nx^2\n\\]'):
            self.assertIn('math-source', render(text))
        self.assertNotIn('math-source', render('```python\nprint("$x$")\n```'))
        self.assertNotIn('<script>', render('<script>alert(1)</script>'))

    def test_diff_escapes_html(self):
        markup=_trace([{'kind':'diff','title':'a.txt','detail':'+<img src=x onerror=alert(1)>\n-old\n'}])
        self.assertIn('diff-add',markup)
        self.assertNotIn('<img',markup)
        self.assertIn('&lt;img',markup)

    def test_elapsed_end_is_persisted(self):
        markup=_turn_timeline({'at':100,'ended_at':130},[],'finished','','turn-1')
        self.assertIn('data-end="130.0"',markup)
        self.assertIn('data-start="100.0"',markup)

    def test_snapshot_add_edit_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ws=Workspace(root)
            (root/'a.txt').write_text('before\n',encoding='utf-8')
            (root/'.env').write_text('secret',encoding='utf-8')
            before=capture_tree(ws)
            (root/'a.txt').write_text('after\n',encoding='utf-8')
            (root/'b.txt').write_text('new\n',encoding='utf-8')
            after=capture_tree(ws)
            self.assertEqual(len(list(tree_diff(before,after))),2)
            self.assertIsNone(capture(ws,'.env'))
            (root/'a.txt').unlink()
            self.assertIn('-after',dict(tree_diff(after,capture_tree(ws)))['a.txt'])


if __name__=='__main__':unittest.main()
