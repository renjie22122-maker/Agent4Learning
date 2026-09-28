from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentplat import git_workflow as flow


class GitWorkflowTests(unittest.TestCase):
    def test_worktree_excludes_uncommitted_changes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)/'repo'; root.mkdir()
            flow.git(root, 'init')
            (root/'a.txt').write_text('committed')
            flow.git(root, 'add', 'a.txt')
            flow.git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture')
            (root/'a.txt').write_text('uncommitted')
            with patch.object(flow, 'MANAGED', Path(td)/'worktrees'):
                result = flow.create_worktree(root)
                try:
                    self.assertEqual((Path(result['workspace'])/'a.txt').read_text(), 'committed')
                    self.assertIn('uncommitted', flow.review(root)['unstaged_diff'])
                    self.assertEqual((root/'a.txt').read_text(), 'uncommitted')
                finally:
                    flow.git(root, 'worktree', 'remove', result['workspace'])


if __name__ == '__main__': unittest.main(verbosity=2)
