"""Pure event projection. Facts never execute tools or replay side effects."""
from dataclasses import dataclass, field, asdict


@dataclass
class RunState:
    schema_version: int = 1
    last_seq: int = 0
    run_seq: int = 0
    phase: str = 'idle'
    model_calls: int = 0
    tool_calls: int = 0
    pending_tools: dict = field(default_factory=dict)
    unresolved_effects: dict = field(default_factory=dict)
    pending_questions: list = field(default_factory=list)
    review: dict = field(default_factory=dict)
    acceptance: dict = field(default_factory=dict)
    author_summary: str = ''
    input_tokens: int = 0
    output_tokens: int = 0
    errors: list = field(default_factory=list)


def project(events):
    state=RunState()
    for event in events:
        seq,kind,data=event.seq,event.kind,event.data
        if seq <= state.last_seq:
            state.errors.append(f'non-monotonic seq {seq}')
            continue
        if kind == 'run/started':
            unresolved={**state.unresolved_effects,**{k:v for k,v in state.pending_tools.items() if v.get('destructive')}}
            state=RunState(run_seq=seq,phase='running',unresolved_effects=unresolved)
        state.last_seq=seq
        if kind == 'model/request':state.model_calls+=1;state.phase='model'
        elif kind == 'assistant/message':
            state.input_tokens+=data.get('in_tokens',0);state.output_tokens+=data.get('out_tokens',0)
        elif kind == 'tool/call':
            state.tool_calls+=1;state.phase='tool'
            state.pending_tools[data.get('call_id',f'unknown:{seq}')]=dict(tool=data.get('tool'),destructive=data.get('destructive',False),seq=seq)
        elif kind == 'tool/result':
            state.pending_tools.pop(data.get('call_id'),None)
            state.unresolved_effects.pop(data.get('call_id'),None)
        elif kind == 'human/requested':
            state.pending_questions.append(data.get('question_id'));state.phase='waiting_user'
        elif kind == 'human/answered':
            state.pending_questions=[q for q in state.pending_questions if q!=data.get('question_id')]
            state.phase='running' if not state.pending_questions else 'waiting_user'
        elif kind == 'independent_review/started':state.review=dict(data,status='running');state.phase='review'
        elif kind == 'independent_review/result':state.review=dict(data,status='passed' if data.get('passed') else 'failed')
        elif kind == 'independent_review/superseded':state.review=dict(status='superseded');state.acceptance={}
        elif kind == 'delivery/finalized':
            state.acceptance=data.get('acceptance',{});state.author_summary=data.get('author_summary','')
        elif kind == 'followup/user':state.acceptance={}
        elif kind == 'session/closed':state.phase='completed' if data.get('finished') else 'stopped'
        elif kind == 'run/settled':state.phase=data.get('status','unknown')
    return asdict(state)
