"""Adversarial host-file and wire-protocol tests; no real user targets."""
import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentlab.providers import ChatMessage
from agentplat.llm import OpenAIChatClient, sanitize_messages
from agentplat.llmconfig import LLMConfig
from agentplat.isolation import IsolatedChanges, inventory
from agentplat.spill import SpillPolicy
from agentplat import model_client

def call(identifier):
    return ChatMessage('assistant','',tool_calls=[{'id':identifier,'type':'function','function':{'name':'write_file','arguments':'{}'}}])


class Boundaries(unittest.TestCase):
    def test_tuple_repaired_before_transmission(self):
        history=(call('a'),ChatMessage('user','continue'))
        body=json.loads(OpenAIChatClient(LLMConfig())._payload('fixture',history))
        self.assertEqual([m['role'] for m in body['messages']],['assistant','tool','user'])
        self.assertIn('不得盲目重放',body['messages'][1]['content'])
        self.assertEqual(len(history),2)

    def test_reused_ids_preserve_distinct_results_and_are_idempotent(self):
        history=[call('a'),ChatMessage('tool','one',tool_call_id='a'),call('a'),ChatMessage('tool','two',tool_call_id='a')]
        fixed,notes=sanitize_messages(history)
        ids=[m.tool_call_id for m in fixed if m.role=='tool']
        self.assertEqual(len(set(ids)),2)
        self.assertEqual([m.content for m in fixed if m.role=='tool'],['one','two'])
        self.assertEqual(history[2].tool_calls[0]['id'],'a')
        self.assertTrue(notes);self.assertEqual(sanitize_messages(fixed)[1],[])

    def test_ambiguous_batch_fails_without_inventing_results(self):
        message=call('a');message.tool_calls*=2
        with self.assertRaises(ValueError):sanitize_messages([message])

    def test_hardlink_never_copied_or_merged(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);parent=root/'parent';parent.mkdir();outside=root/'private';outside.write_text('KEEP')
            (parent/'normal').write_text('ok')
            branch=IsolatedChanges(parent,root/'child')
            os.link(outside,branch.root/'linked')
            with self.assertRaises(ValueError):branch.apply()
            os.link(outside,parent/'linked')
            with self.assertRaises(ValueError):inventory(parent)
            self.assertEqual(outside.read_text(),'KEEP')
            self.assertEqual((parent/'normal').read_text(),'ok')

    def test_linked_directory_cannot_enter_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);parent=root/'parent';parent.mkdir();outside=root/'outside';outside.mkdir()
            try:os.symlink(outside,parent/'linked',target_is_directory=True)
            except OSError as exc:self.skipTest('symlink creation unavailable: '+str(exc))
            with self.assertRaises(ValueError):IsolatedChanges(parent,root/'child')

    def test_spill_cache_poison_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            spill=SpillPolicy(Path(td),max_inline_bytes=10)
            spill.apply('read_file','evidence'*100)
            target=next(spill.dir.glob('*.txt'));target.write_text('forged')
            with self.assertRaises(ValueError):spill.apply('read_file','evidence'*100)

    def test_spill_hardlink_cannot_read_external_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);ws=root/'ws';ws.mkdir()
            spill=SpillPolicy(ws,max_inline_bytes=10);spill.apply('read_file','evidence'*100)
            target=next(spill.dir.glob('*.txt'));target.unlink()
            outside=root/'outside';outside.write_text('evidence'*100);os.link(outside,target)
            with self.assertRaises(ValueError):spill.apply('read_file','evidence'*100)

    def test_transport_registry_has_no_silent_fallback(self):
        with patch.dict(model_client._FACTORIES,clear=True):
            cfg=LLMConfig(transport='fixture')
            with self.assertRaises(ValueError):model_client.create_client(cfg)
            class Client:
                def complete(self,*a):pass
                def complete_with_tools(self,*a):pass
            model_client.register_transport('fixture',lambda cfg:Client())
            self.assertIsInstance(model_client.create_client(cfg),Client)
            with self.assertRaises(ValueError):model_client.register_transport('fixture',Client)

    def test_security_contract_reports_configuration_not_proof(self):
        from agentplat.execution import execution_status
        local=execution_status('local');native=execution_status('native','host')
        self.assertFalse(local['local_is_sandboxed'])
        self.assertEqual(native['network_policy'],'host')
        self.assertFalse(native['security_contract']['preflight_is_proof_of_complete_isolation'])
        self.assertFalse(native['security_contract']['workspace_copy_is_sandbox'])

if __name__=='__main__':unittest.main()
