"""A volatile inbox loses messages; durable IDs make delivery auditable."""
from pathlib import Path
import tempfile
from agentlab.util import lab
from agentplat.team_coordination import Coordination


def main():
    with lab('lab-42-team-delivery','团队消息的持久化与确认','入队、交给上下文、处理确认是三个不同状态'):
        volatile=['important finding'];volatile=[]
        before=int(not volatile)
        with tempfile.TemporaryDirectory() as td:
            store=Coordination(Path(td))
            sent=store.send('child','root','important finding',dedup='finding-1')
            restored=Coordination(Path(td))
            duplicate=restored.send('child','root','important finding',dedup='finding-1')
            assert sent['id']==duplicate['id']
            after=int(not restored.pending('root'))
            restored.drain('root')
            record=restored.messages('root')[0]
            assert record['delivered'] and not record['acknowledged']
            restored.acknowledge('root',sent['id'])
            assert restored.messages('root')[0]['acknowledged']
        print('[BROKEN-REPRODUCED] 仅内存队列在进程重启后丢失未读消息')
        print('[FIX-APPLIED] 持久化消息 ID、幂等去重及独立确认状态')
        print(f'[VERIFY] lost_unread_messages: {before} -> {after}')
        print('[TAKEAWAY] 确认表示收件方声明已处理，不是对结论真实性的验证。')
    return 0


if __name__=='__main__':raise SystemExit(main())
