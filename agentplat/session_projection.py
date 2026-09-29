"""Single pure projection for durable session recovery, independent of the agent loop.

No command, provider, workspace, or model import is allowed here. This reconstructs
recorded facts; checking current files and resuming side effects remain host decisions.
"""
from dataclasses import dataclass,field,asdict

@dataclass
class SessionState:
 session_id: str = ''
 iterations_done: int = 0
 tool_calls_done: int = 0
 tokens_in: int = 0
 tokens_out: int = 0
 usd: float = 0.0
 files_written: list = field(default_factory=list)
 commands_run: list = field(default_factory=list)
 finished: bool = False
 last_step: int = 0
 skipped_lines: int = 0
 unknown_calls: list = field(default_factory=list)
 failed_calls: list = field(default_factory=list)
 messages: list = field(default_factory=list)
 requirements: list = field(default_factory=list)
 acceptance: dict = field(default_factory=dict)
 permission_mode: str | None = None
 source_reads: list = field(default_factory=list)
 last_checkpoint: dict = field(default_factory=dict)
 anomalies: list = field(default_factory=list)

def project_session(events,session_id='',skipped=0):
 state=SessionState(session_id=session_id,skipped_lines=skipped)
 pending={};last_seq=0
 for event in events:
  if event.seq<=last_seq:
   state.anomalies.append({'seq':event.seq,'error':'non_monotonic_sequence'})
   continue
  last_seq=event.seq;k,d=event.kind,event.data
  if k=='session/created':
   state.session_id=d.get('session_id',state.session_id)
   if d.get('task'):state.requirements.append(d['task'])
  elif k=='run/started':
   state.finished=False;state.acceptance={}
   if d.get('text'):state.requirements.append(d['text'])
  elif k=='step/start':
   state.iterations_done=max(state.iterations_done,int(d.get('iteration',0)))
  elif k=='assistant/message':
   state.tokens_in+=int(d.get('in_tokens',0) or 0)
   state.tokens_out+=int(d.get('out_tokens',0) or 0)
   state.usd+=float(d.get('usd',0) or 0)
  elif k=='tool/call':
   identity=d.get('call_id',f'legacy-{event.seq}')
   if identity in pending:
    state.anomalies.append({'seq':event.seq,'error':'duplicate_call_id'})
    # Preserve both uncertain intents rather than silently overwrite the first.
    identity=f'duplicate:{event.seq}:{identity}'
   pending[identity]=dict(d)
  elif k=='tool/result':
   intent=pending.pop(d.get('call_id'),None)
   if intent is None:continue
   state.tool_calls_done+=1
   if not d.get('ok'):state.failed_calls.append(intent);continue
   if intent.get('destructive'):
    name=intent.get('tool')
    if name in ('write_file','edit_file','append_file','delete_file'):
     path=intent.get('path')
     if path and path not in state.files_written:state.files_written.append(path)
    elif name=='run_shell':state.commands_run.append(intent.get('command') or intent.get('brief',''))
  elif k=='conversation/message':state.messages.append(d['message'])
  elif k=='conversation/snapshot':state.messages=list(d.get('messages',[]))
  elif k=='followup/user':
   state.finished=False;state.acceptance={}
   if d.get('text'):state.requirements.append(d['text'])
  elif k=='delivery/finalized':state.acceptance=dict(d.get('acceptance',{}))
  elif k=='independent_review/superseded':state.acceptance={}
  elif k=='permission/applied':state.permission_mode=d.get('mode')
  elif k=='knowledge/source_read':state.source_reads.append(dict(d))
  elif k=='recovery/checkpoint':state.last_checkpoint=dict(d,seq=event.seq)
  elif k=='session/closed':state.finished=bool(d.get('finished',False))
  elif k=='run/settled' and d.get('status') not in ('done','completed'):
   state.finished=False
 state.last_step=state.iterations_done
 state.unknown_calls=list(pending.values())
 return asdict(state)

