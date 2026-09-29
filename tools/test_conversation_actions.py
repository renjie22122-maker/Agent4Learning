import sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agentplat.conversation_actions import branch, feedback, ratings
from agentplat.conversation_versions import capture, version_roots
from agentplat.session import SessionLog, replay
from unittest.mock import patch


class ActionsTests(unittest.TestCase):
    def fixture(self, td):
        base=Path(td);root=base/'files';root.mkdir();(root/'version.txt').write_text('one')
        manager=SimpleNamespace(state_path=base/'workspaces.json')
        log=SessionLog(base/'.sessions'/'parent.jsonl',session_id='parent')
        log.append('session/created',workspace=str(root),workspace_roots={'main':str(root)},conversation_kind='general')
        log.append('conversation/message',message={'role':'user','content':'first'})
        log.append('ui/turn',text='first',summary='answer one',file_version=capture(manager,{'main':root}))
        (root/'version.txt').write_text('two')
        log.append('conversation/message',message={'role':'user','content':'FUTURE_SECRET'})
        log.append('ui/turn',text='second',summary='answer two');log.flush('test')
        def sessions(n):
            return [dict(session_id=p.stem,log_path=str(p),title=p.stem) for p in (base/'.sessions').glob('*.jsonl')]
        return SimpleNamespace(ws_mgr=manager,live_sessions={},list_agent_sessions=sessions),root,log

    def test_snapshot_fork_excludes_future_and_isolates_files(self):
        with tempfile.TemporaryDirectory() as td:
            demo,root,source=self.fixture(td)
            before=source.path.read_bytes()
            result=branch(demo,dict(session='parent',turn='1',files='snapshot'))
            log,_=SessionLog.load(Path(td)/'.sessions'/(result['session']+'.jsonl'))
            self.assertNotIn('FUTURE_SECRET',str(replay(log).messages))
            dest=Path(log.of_kind('session/created')[0].data['workspace'])
            self.assertEqual((dest/'version.txt').read_text(),'one')
            (dest/'version.txt').write_text('branch')
            self.assertEqual((root/'version.txt').read_text(),'two')
            self.assertEqual(source.path.read_bytes(),before)
            self.assertFalse(log.of_kind('tool/call'))

    def test_refork_earlier_turn_cannot_inherit_future(self):
        with tempfile.TemporaryDirectory() as td:
            demo,root,source=self.fixture(td)
            first=branch(demo,dict(session='parent',turn='2',files='current'))
            second=branch(demo,dict(session=first['session'],turn='1',files='snapshot'))
            log,_=SessionLog.load(Path(td)/'.sessions'/(second['session']+'.jsonl'))
            self.assertNotIn('FUTURE_SECRET',str(replay(log).messages))

    def test_feedback_persist_change_withdraw(self):
        with tempfile.TemporaryDirectory() as td:
            demo,_,_=self.fixture(td)
            feedback(demo,dict(session='parent',turn='1',vote='down',reason='incorrect'))
            self.assertEqual(ratings(demo,'parent')['1']['reason'],'incorrect')
            feedback(demo,dict(session='parent',turn='1',vote='up'))
            self.assertEqual(ratings(demo,'parent')['1']['vote'],'up')
            feedback(demo,dict(session='parent',turn='1',vote=''))
            self.assertEqual(ratings(demo,'parent'),{})

    def test_invalid_branch_modes_and_running_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            demo,_,log=self.fixture(td)
            for mode in ('shared','snapshot'):
                with self.assertRaises(ValueError):branch(demo,dict(session='parent',turn='2',files=mode))
            demo.live_sessions['parent']=({'status':'running'},SimpleNamespace(session=log),None)
            with self.assertRaises(ValueError):branch(demo,dict(session='parent',turn='1',files='snapshot'))

    def test_snapshot_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            demo,_,log=self.fixture(td)
            version=log.of_kind('ui/turn')[0].data['file_version']
            roots=version_roots(demo.ws_mgr,version)
            (roots['main']/'version.txt').write_text('tampered')
            with self.assertRaises(ValueError):version_roots(demo.ws_mgr,version)

    def test_multiple_project_folders_have_independent_copies(self):
        with tempfile.TemporaryDirectory() as td:
            demo,root,log=self.fixture(td)
            other=Path(td)/'other';other.mkdir();(other/'b.txt').write_text('B')
            info=log.events[0].data
            info.update(conversation_kind='project',workspace_roots={'main':str(root),'extra':str(other)})
            # Use a new fixture log, because existing events are append-only.
            project=SessionLog(Path(td)/'.sessions'/'project.jsonl',session_id='project')
            project.append('session/created',**info)
            project.append('conversation/message',message={'role':'user','content':'project'})
            project.append('ui/turn',text='project',summary='done');project.flush('test')
            sid=branch(demo,dict(session='project',turn='1',files='current'))['session']
            fork,_=SessionLog.load(Path(td)/'.sessions'/(sid+'.jsonl'))
            roots=fork.of_kind('session/created')[0].data['workspace_roots']
            self.assertEqual((Path(roots['extra'])/'b.txt').read_text(),'B')
            self.assertNotEqual(Path(roots['main']),root)
            self.assertNotEqual(Path(roots['extra']),other)

    def test_snapshot_limit_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            demo,root,_=self.fixture(td)
            with patch('agentplat.conversation_versions.MAX_BYTES',1):
                result=capture(demo.ws_mgr,{'main':root})
            self.assertEqual(result['status'],'unavailable')
            self.assertIn('超过',result['reason'])

if __name__=='__main__':unittest.main()
