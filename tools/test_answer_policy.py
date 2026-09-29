import sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.reflection import extract_requirements


class AnswerPolicyTests(unittest.TestCase):
    def test_negative_bound_is_not_list_requirement(self):
        self.assertEqual(extract_requirements('-104 <= Node.val <= 104'), [])
        self.assertEqual(extract_requirements('-10^4 <= Node.val <= 10^4'), [])

    def test_real_bullets_remain_requirements(self):
        self.assertTrue(extract_requirements('- 必须运行 pytest'))
        self.assertTrue(extract_requirements('1. 修改 solution.py'))


if __name__=='__main__':unittest.main()
