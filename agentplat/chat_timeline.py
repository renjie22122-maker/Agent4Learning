"""Recover display timing from existing logs without rewriting history."""
def enrich_turns(turns, events):
    saved = [i for i,e in enumerate(events) if e.kind == 'ui/turn']
    result=[]; previous=-1
    for index, source in enumerate(turns):
        turn=dict(source)
        if index < len(saved):
            end=saved[index]; segment=events[previous+1:end+1]; previous=end
            starts=[e.ts for e in segment if e.kind=='run/started']
            turn.setdefault('at',starts[-1] if starts else (segment[0].ts if segment else 0))
            turn.setdefault('ended_at',events[end].ts)
            if not turn.get('progress_times'):
                messages=[e for e in segment if e.kind=='assistant/message' and e.data.get('text')]
                times=[]; cursor=0
                for text in turn.get('progress_messages',[]):
                    match=next((j for j in range(cursor,len(messages)) if messages[j].data['text']==text),None)
                    times.append(messages[match].ts if match is not None else turn['at'])
                    if match is not None:cursor=match+1
                turn['progress_times']=times
            queued=[dict(e.data,at=e.data.get('at',e.ts)) for e in segment if e.kind=='steering/queued']
            if queued:
                known={x.get('id'):x for x in turn.get('steering_messages',[])}
                turn['steering_messages']=[{**q,**known.get(q.get('id'),{})} for q in queued]
        result.append(turn)
    return result
