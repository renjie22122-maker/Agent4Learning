from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentplat.markdown_view import render


class MarkdownTests(unittest.TestCase):
    def test_structure(self):
        result = render('# 标题\n\n**重点**\n\n- 第一项\n- 第二项\n\n|列|值|\n|---|---|\n|A|1|\n\n```python\nprint(1)\n```')
        for tag in ('<h1>', '<strong>', '<ul>', '<table>', '<pre>'):
            self.assertIn(tag, result)

    def test_no_active_content(self):
        result = render('<script>alert(1)</script>\n\n[x](javascript:alert(1))\n\n![image](https://example.org/pixel)')
        self.assertNotIn('<script>', result)
        self.assertNotIn('href="javascript:', result)
        self.assertNotIn('<img', result)


if __name__ == '__main__': unittest.main(verbosity=2)
