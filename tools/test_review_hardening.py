"""Failure-oriented regressions for the source-review remediation."""
import os
import sys
import tempfile
import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentlab.providers import ChatMessage, Usage
from agentplat.compaction import Compactor, SUMMARY_SECTIONS
from agentplat.context import RequestContext
from agentplat.execution_environment import task_environment
from agentplat.llmconfig import LLMConfig
from agentplat.operation_ledger import OperationLedger
from agentplat.runtime import CapabilityPolicy, PermissionDenied, effective_policy
from agentplat.tools import Tool, ToolRegistry, ToolError, ToolResult


class Summarizer:
    def __init__(self, fail=0):self.prompts=[];self.fail=fail
    def complete(self, model, messages, timeout):
        self.prompts.append(messages[0].content)
        if len(self.prompts)==self.fail:raise RuntimeError('interrupted')
        return '\n'.join(s+'：摘要' for s in SUMMARY_SECTIONS), Usage(100,20,0)


class HardeningTests(unittest.TestCase):
    def history(self):
        return [ChatMessage('system','system'),ChatMessage('user','task'),
                ChatMessage('assistant','x'*26000+'LATE_SENTINEL'),
                ChatMessage('user','KEEP_CONSTRAINT'),ChatMessage('assistant','y'*12000),
                ChatMessage('assistant','recent')]

    def test_summary_covers_entire_input_and_keeps_constraints(self):
        model=Summarizer();messages=self.history()
        c=Compactor(model,LLMConfig(provider='mock',model='fake'))
        result=c.summarize(messages,1)
        self.assertGreater(result[0],0)
        self.assertEqual(result[1],2)
        self.assertIn('LATE_SENTINEL',''.join(model.prompts))
        self.assertIn('KEEP_CONSTRAINT',[m.content for m in messages])
        self.assertEqual(c.last_billing['in_tokens'],200)

    def test_failed_second_summary_retains_original(self):
        model=Summarizer(fail=2);messages=self.history();before=list(messages)
        result=Compactor(model,LLMConfig(provider='mock',model='fake')).summarize(messages,1)
        self.assertEqual(result[0],0);self.assertEqual(messages,before)
        self.assertTrue(result[3])

    def test_grandchild_observes_live_parent_revocation(self):
        root=SimpleNamespace(capabilities=CapabilityPolicy(allow_network=True))
        child=SimpleNamespace(capabilities=CapabilityPolicy(allow_network=True),authority_provider=lambda:effective_policy(root))
        grandchild=SimpleNamespace(capabilities=CapabilityPolicy(allow_network=True),authority_provider=lambda:effective_policy(child))
        effective_policy(grandchild).check('write',writes=True)
        root.capabilities=CapabilityPolicy(frozenset({'read'}),False,False,False)
        for kwargs in ({'writes':True},{'shell':True},{'network':True}):
            with self.assertRaises(PermissionDenied):effective_policy(grandchild).check('read',**kwargs)
        with self.assertRaises(PermissionDenied):effective_policy(grandchild).check('write')

    def test_environment_excludes_credentials_and_python_hooks(self):
        env=task_environment({'PATH':'safe','SystemRoot':'windows','DEEPSEEK_API_KEY':'secret',
                              'AWS_SECRET_ACCESS_KEY':'secret','PYTHONPATH':'hook','USERPROFILE':'private'})
        self.assertEqual(env['PATH'],'safe')
        for key in ('DEEPSEEK_API_KEY','AWS_SECRET_ACCESS_KEY','PYTHONPATH','USERPROFILE'):self.assertNotIn(key,env)
        self.assertEqual(env['PYTHONUTF8'],'1')

    def test_real_subprocess_receives_filtered_environment(self):
        env=task_environment({**os.environ,'PRIVATE_TEST_CREDENTIAL':'sentinel'})
        result=subprocess.run([sys.executable,'-c',"import os; assert 'PRIVATE_TEST_CREDENTIAL' not in os.environ; print('clean')"],env=env,capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.strip(),'clean')

    def test_memory_dedup_retains_provenance_and_valid_json(self):
        from agentplat.memory import MemoryStore,inject,PREFIX
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=MemoryStore(root/'db')
            for n in range(3):
                source=root/f'{n}.jsonl'
                source.write_text(json.dumps({'seq':1,'kind':'session/created','data':{'task':'prefer pytest'}}),encoding='utf-8')
                store.select_source(source,root)
            for row in store.list():store.update(row['id'],row['content'],'preference','project','active',None,1)
            hits=store.search(root,'pytest');self.assertEqual(len(hits),1);self.assertEqual(len(hits[0]['sources']),3)
            # Injection must skip whole records, not slice a JSON string.
            entries=[{**hits[0],'id':str(n),'content':'长'*6000} for n in range(3)]
            events=[];agent=SimpleNamespace(cfg=SimpleNamespace(memory_enabled=True),ws=SimpleNamespace(root=root),session=SimpleNamespace(append=lambda *a,**kw:events.append(kw)))
            messages=[ChatMessage('system','s')]
            with patch.object(MemoryStore,'search',return_value=entries):inject(agent,'query',messages)
            data=json.loads(messages[1].content.split('\n',2)[2])
            self.assertEqual(len(data),1);self.assertEqual(events[0]['ids'],['0'])

    def test_model_contract_accepts_scripted_adapter(self):
        from agentplat.model_client import ModelClient,review_config
        from agentplat.experiments import ScriptedModel
        # The coding-only scripted adapter intentionally does not pretend to
        # support plain completions; both methods belong to the full contract.
        class Adapter(ScriptedModel):
            def complete(self,*args):return '',Usage(0,0,0)
        self.assertIsInstance(Adapter(),ModelClient)
        cfg=LLMConfig(base_url='https://api.deepseek.com',reasoning_effort='high')
        self.assertEqual(review_config(cfg).reasoning_effort,'low')
        self.assertEqual(cfg.reasoning_effort,'high')

    def test_ledger_reopen_and_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ops.db';calls=[]
            def op():calls.append(1);return ToolResult(True,'done')
            OperationLedger(path).execute(['tenant','user','id'],{'a':1},op)
            result=OperationLedger(path).execute(['tenant','user','id'],{'a':1},op)
            self.assertEqual(result.value,'done');self.assertEqual(len(calls),1)
            with self.assertRaises(ToolError) as caught:
                OperationLedger(path).execute(['tenant','user','id'],{'a':2},op)
            self.assertEqual(caught.exception.code,'OPERATION_CONFLICT')
            OperationLedger(path).execute(['tenant','other','id'],{'a':1},op)
            self.assertEqual(len(calls),2)

    def test_ledger_unknown_never_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ops.db'
            def crash():raise RuntimeError('after external side effect')
            with self.assertRaises(RuntimeError):OperationLedger(path).execute(['id'],{},crash)
            with self.assertRaises(ToolError) as caught:OperationLedger(path).execute(['id'],{},lambda: self.fail('replayed'))
            self.assertEqual(caught.exception.code,'OUTCOME_UNKNOWN')

    def test_capstone_limit_config_and_scoped_reset(self):
        registry=ToolRegistry(max_calls_per_request=1)
        registry.register(Tool('read','',{'type':'object'},lambda:'ok',max_calls_per_request=100))
        a=RequestContext('t','a','s');b=RequestContext('t','b','s')
        registry.call('read',{},a,'same');registry.call('read',{},b,'same')
        registry.new_request('same',a);registry.call('read',{},a,'same')
        with self.assertRaises(ToolError):registry.call('read',{},b,'same')

    def test_side_effect_failure_is_not_retried(self):
        registry=ToolRegistry();calls=[]
        def uncertain():calls.append(1);raise RuntimeError('connection lost after write')
        registry.register(Tool('write','',{'type':'object'},uncertain,has_side_effects=True,max_retries=3))
        with self.assertRaises(RuntimeError):registry.call('write',{},RequestContext('t','u','s'))
        self.assertEqual(len(calls),1)

    def test_durable_cache_still_checks_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=ToolRegistry(ledger_path=Path(tmp)/'ops.db')
            r.register(Tool('write','',{'type':'object'},lambda:'ok',requires_roles=frozenset({'admin'})))
            r.call('write',{},RequestContext('t','u','s',roles=frozenset({'admin'})),logical_operation_id='business-id')
            with self.assertRaises(ToolError) as caught:r.call('write',{},RequestContext('t','u','s'),logical_operation_id='business-id')
            self.assertEqual(caught.exception.code,'FORBIDDEN')

    def test_durable_truncated_result_can_be_read_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ops.db';ctx=RequestContext('t','u','s')
            first=ToolRegistry(ledger_path=path)
            first.register(Tool('big','',{'type':'object'},lambda:'x'*4000,max_output_tokens=20))
            result=first.call('big',{},ctx,logical_operation_id='id')
            second=ToolRegistry(ledger_path=path)
            second.register(Tool('big','',{'type':'object'},lambda:self.fail('replayed'),max_output_tokens=20))
            replayed=second.call('big',{},ctx,logical_operation_id='id')
            self.assertEqual(result.ref,replayed.ref)
            self.assertEqual(second.read_result(replayed.ref),'x'*4000)


if __name__=='__main__':unittest.main()
