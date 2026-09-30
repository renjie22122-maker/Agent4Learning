import tempfile,unittest
from pathlib import Path
from agentplat.artifact_retention import audit_spills


class RetentionTests(unittest.TestCase):
    def test_reference_and_active_pins_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'.spill').mkdir();p=root/'.spill'/'sample.txt';p.write_text('evidence')
            refs=root/'refs';refs.mkdir();log=refs/'log.jsonl';log.write_text('sample.txt')
            for active,roots in [(True,[refs]),(False,[refs]),(False,[root/'missing'])]:
                result=audit_spills(root,roots,active=active,min_age_days=0)
                self.assertEqual(result['files'][0]['action'],'retain')
                self.assertEqual(p.read_text(),'evidence')
            log.write_text('no locator')
            result=audit_spills(root,[refs],active=False,min_age_days=0)
            self.assertEqual(result['files'][0]['action'],'review_candidate')
            self.assertTrue(p.exists());self.assertFalse(result['automatic_deletion'])


if __name__=='__main__':unittest.main()
