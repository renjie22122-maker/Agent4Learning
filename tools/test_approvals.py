from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentplat import approvals


class ApprovalTests(unittest.TestCase):
    def test_task_binding_and_single_use(self):
        with tempfile.TemporaryDirectory() as td, patch.object(approvals, 'DATABASE', Path(td)/'approvals.db'):
            key = approvals.request('session-A', td, 'python --version', '需要宿主版本')['request_id']
            with self.assertRaises(PermissionError): approvals.claim(key, 'session-A', td)
            approvals.decide(key, True)
            with self.assertRaises(PermissionError): approvals.claim(key, 'session-B', td)
            self.assertEqual(approvals.claim(key, 'session-A', td)['command'], 'python --version')
            with self.assertRaises(PermissionError): approvals.claim(key, 'session-A', td)


if __name__ == '__main__': unittest.main(verbosity=2)
