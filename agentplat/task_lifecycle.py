"""Declarative, replayable lifecycle contract, independent of agents and tools.

An event projection is not an execution engine. In particular, RESUMING never
authorizes replay, and COMPLETED requires host closure without pending work.
"""
from dataclasses import dataclass, field, asdict

TERMINAL = frozenset({'completed','failed','cancelled','interrupted'})
ACTIVE = frozenset({'created','planning','executing','waiting_user','waiting_approval',
                    'verifying','merging','final_review','resuming'})
ALLOWED = {
    'created': {'planning','executing','resuming','final_review','waiting_user','waiting_approval'},
    'planning': {'executing','waiting_user','waiting_approval','verifying','merging','final_review'},
    'executing': {'planning','waiting_user','waiting_approval','verifying','merging','final_review'},
    'waiting_user': {'planning','executing','verifying','waiting_approval'},
    'waiting_approval': {'planning','executing','verifying','waiting_user'},
    'verifying': {'planning','executing','waiting_user','waiting_approval','final_review'},
    'merging': {'planning','executing','verifying','final_review'},
    'final_review': {'planning','executing','verifying','waiting_user','waiting_approval','completed'},
    'resuming': {'planning','executing','verifying','waiting_user','waiting_approval'},
}
for phase in ACTIVE:
    ALLOWED[phase] |= {'failed','cancelled','interrupted'}


@dataclass
class Lifecycle:
    schema_version: int = 1
    state: str = 'created'
    last_seq: int = 0
    transition_count: int = 0
    violations: list = field(default_factory=list)
    questions: dict = field(default_factory=dict)
    tools: dict = field(default_factory=dict)
    unresolved_effects: dict = field(default_factory=dict)
    cancel_requested: bool = False

    def transition(self, target, seq, event):
        if target == self.state:
            return
        if target not in ALLOWED.get(self.state, set()):
            self.violations.append({'seq':seq,'event':event,'from':self.state,'to':target})
            return
        self.state = target
        self.transition_count += 1

    def apply(self, event):
        seq, kind, data = event.seq, event.kind, event.data
        if seq <= self.last_seq:
            self.violations.append({'seq':seq,'event':kind,'reason':'non-monotonic sequence'})
            return
        self.last_seq = seq
        if kind=='followup/user' and self.state not in TERMINAL:
            # Steering is not a new run and cannot resolve pending effects/questions.
            if not self.questions and not self.tools:self.transition('planning',seq,kind)
            return
        if kind in ('run/started','followup/user'):
            self.unresolved_effects.update({k:v for k,v in self.tools.items() if v['destructive']})
            self.tools.clear();self.cancel_requested=False
            self.state = 'resuming' if data.get('recovered') or self.unresolved_effects else 'planning'
            self.transition_count += 1
            return
        if kind == 'run/cancel_requested':
            self.cancel_requested=True
            self.transition('cancelled',seq,kind)
            return
        if kind == 'tool/call':
            self.tools[data.get('call_id',f'unknown:{seq}')]=dict(tool=data.get('tool'),destructive=data.get('destructive',False))
        elif kind == 'tool/result':
            self.tools.pop(data.get('call_id'),None)
            self.unresolved_effects.pop(data.get('call_id'),None)
        elif kind == 'human/requested':
            self.questions[data.get('question_id')] = data.get('request_type','question')
        elif kind == 'human/answered':
            self.questions.pop(data.get('question_id'),None)
        # Late results may settle bookkeeping, but cannot resurrect a terminal run.
        if self.state in TERMINAL:
            if kind in ('model/request','tool/call'):
                self.violations.append({'seq':seq,'event':kind,'reason':'work after terminal state'})
            return
        target = None
        if kind in ('session/closed','run/settled'):
            successful=data.get('finished') if kind=='session/closed' else data.get('status') in ('done','completed')
            pending={k:v for k,v in self.tools.items() if v.get('tool')!='finish'}
            if successful and (pending or self.questions or self.unresolved_effects):
                self.violations.append({'seq':seq,'event':kind,'reason':'completion with unresolved work'})
                target='interrupted'
            elif successful:
                self.transition('final_review',seq,kind)
                target='completed'
            else:target='failed' if data.get('status')=='failed' else 'interrupted'
        elif self.questions:
            target='waiting_approval' if 'approval' in self.questions.values() else 'waiting_user'
        elif kind in ('model/request','human/answered'):
            target='planning'
        elif kind == 'tool/call':
            name=data.get('tool')
            target='final_review' if name=='finish' else 'merging' if name in ('apply_agent_changes','merge_agent','merge_team_plan') else 'executing'
        elif kind == 'independent_review/started':target='verifying'
        elif kind == 'independent_review/result':target='final_review' if data.get('passed') else 'executing'
        elif kind == 'delivery/finalized':target='final_review'
        if target:self.transition(target,seq,kind)

    def snapshot(self):
        return asdict(self)


def project(events):
    state=Lifecycle()
    for event in events:state.apply(event)
    return state.snapshot()
