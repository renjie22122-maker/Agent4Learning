"""Native wire codecs. Opaque provider blocks survive tool-result round trips.

Only text + client functions are supported. Unknown multimodal/server-tool
outputs are rejected rather than converted into fabricated local tool calls.
"""
import json
import uuid
from copy import deepcopy
from urllib.parse import quote
from agentlab.providers import Usage

NATIVE = ('openai_responses','anthropic','gemini')


def endpoint(cfg, model):
    base=cfg.base_url.rstrip('/')
    if cfg.transport=='openai_responses':return base if base.endswith('/responses') else base+'/responses'
    if cfg.transport=='anthropic':return base if base.endswith('/messages') else base+'/messages' if base.endswith('/v1') else base+'/v1/messages'
    return base+'/models/'+quote(model.removeprefix('models/'),safe='')+':generateContent'


def encode(cfg, model, messages, tools):
    from .llm_protocol import sanitize_messages
    messages,notes=sanitize_messages(messages)
    if any('重新编号' in note for note in notes):raise ValueError('Native call IDs ambiguous; refusing to rewrite opaque provider state')
    if any(not isinstance(m.content,str) for m in messages):raise ValueError('Native adapters currently support text only')
    functions=[t['function'] for t in tools]
    mode=cfg.transport
    if mode=='anthropic' and cfg.reasoning_effort:raise ValueError('Anthropic thinking configuration is not implemented; clear reasoning_effort')
    if mode=='anthropic' and cfg.json_mode and not tools:raise ValueError('Anthropic JSON-object mode is unsupported; disable json_mode or use an explicit tool schema')
    if mode=='gemini' and cfg.reasoning_effort:raise ValueError('Gemini thinking configuration is model-specific; clear reasoning_effort')
    system=[];items=[];names={}
    def append(role,parts):
        field='content' if mode=='anthropic' else 'parts'
        if items and items[-1].get('role')==role:items[-1][field].extend(parts)
        else:items.append({'role':role,field:parts})
    for m in messages:
        if m.role in ('system','developer'):
            if mode=='openai_responses':items.append({'role':m.role,'content':m.content})
            else:system.append(m.content)
            continue
        for c in m.tool_calls:names[c['id']]=c['function']['name']
        if m.role=='tool':
            if mode=='openai_responses':items.append({'type':'function_call_output','call_id':m.tool_call_id,'output':m.content})
            elif mode=='anthropic':append('user',[{'type':'tool_result','tool_use_id':m.tool_call_id,'content':m.content}])
            else:append('user',[{'functionResponse':{'id':m.tool_call_id,'name':names[m.tool_call_id],'response':{'result':m.content}}}])
            continue
        native=(m.tool_calls[0].get('_native') if m.tool_calls else None)
        if native:
            if native['transport']!=mode:raise ValueError('Cannot move native reasoning/signatures between providers')
            parts=deepcopy(native['items'])
            if mode=='openai_responses':items.extend(parts)
            else:append('assistant' if mode=='anthropic' else 'model',parts)
            continue
        if mode=='openai_responses':
            if m.content:items.append({'role':m.role,'content':m.content})
            items.extend({'type':'function_call','call_id':c['id'],'name':c['function']['name'],'arguments':c['function']['arguments']} for c in m.tool_calls)
        else:
            parts=([{'type':'text','text':m.content}] if mode=='anthropic' else [{'text':m.content}]) if m.content else []
            for c in m.tool_calls:
                args=json.loads(c['function']['arguments'])
                parts.append({'type':'tool_use','id':c['id'],'name':c['function']['name'],'input':args} if mode=='anthropic' else
                             {'functionCall':{'id':c['id'],'name':c['function']['name'],'args':args}})
            if parts:append(('assistant' if mode=='anthropic' else 'model') if m.role=='assistant' else 'user',parts)
    if mode=='openai_responses':
        body={'model':model,'input':items,'max_output_tokens':cfg.max_tokens,'store':False,'include':['reasoning.encrypted_content']}
        if functions:body['tools']=[{'type':'function','name':f['name'],'description':f.get('description',''),'parameters':f['parameters'],'strict':False} for f in functions]
        if cfg.reasoning_effort:body['reasoning']={'effort':cfg.reasoning_effort}
        else:body['temperature']=cfg.temperature
        if cfg.json_mode and not tools:body['text']={'format':{'type':'json_object'}}
    elif mode=='anthropic':
        body={'model':model,'messages':items,'system':'\n\n'.join(system),'max_tokens':cfg.max_tokens,'temperature':cfg.temperature}
        if functions:body['tools']=[{'name':f['name'],'description':f.get('description',''),'input_schema':f['parameters']} for f in functions]
    else:
        body={'contents':items,'systemInstruction':{'parts':[{'text':'\n\n'.join(system)}]},'generationConfig':{'maxOutputTokens':cfg.max_tokens,'temperature':cfg.temperature}}
        if functions:body['tools']=[{'functionDeclarations':[{'name':f['name'],'description':f.get('description',''),'parametersJsonSchema':f['parameters']} for f in functions]}]
        if cfg.json_mode and not tools:body['generationConfig']['responseMimeType']='application/json'
    return body


def decode(mode,data):
    calls=[];texts=[]
    if data.get('error'):raise ValueError('Provider returned an error object')
    if mode=='openai_responses':
        if data.get('status')!='completed':raise ValueError('Responses result incomplete or failed; no tools released')
        parts=data.get('output',[]);u=data.get('usage',{})
        usage=Usage(u.get('input_tokens',0),u.get('output_tokens',0),(u.get('input_tokens_details') or {}).get('cached_tokens',0))
        measured='input_tokens' in u and 'output_tokens' in u
        for p in parts:
            if p['type']=='message':
                for c in p.get('content',[]):
                    if c['type']!='output_text':raise ValueError('Unsupported/refused Responses content')
                    texts.append(c['text'])
            elif p['type']=='function_call':calls.append((p['call_id'],p['name'],p['arguments']))
            elif p['type']!='reasoning':raise ValueError('Unsupported Responses server tool')
    elif mode=='anthropic':
        if data.get('stop_reason') not in ('end_turn','tool_use','stop_sequence'):raise ValueError('Anthropic response not complete; no tools released')
        parts=data.get('content',[]);u=data.get('usage',{})
        usage=Usage(u.get('input_tokens',0)+u.get('cache_creation_input_tokens',0)+u.get('cache_read_input_tokens',0),u.get('output_tokens',0),u.get('cache_read_input_tokens',0))
        measured='input_tokens' in u and 'output_tokens' in u
        for p in parts:
            if p['type']=='text':texts.append(p['text'])
            elif p['type']=='tool_use':calls.append((p['id'],p['name'],json.dumps(p['input'],ensure_ascii=False)))
            elif p['type'] not in ('thinking','redacted_thinking'):raise ValueError('Unsupported Anthropic server tool')
    else:
        candidate=(data.get('candidates') or [{}])[0]
        if candidate.get('finishReason')!='STOP':raise ValueError('Gemini response incomplete or blocked; no tools released')
        parts=candidate.get('content',{}).get('parts',[]);u=data.get('usageMetadata',{})
        usage=Usage(u.get('promptTokenCount',0),u.get('candidatesTokenCount',0)+u.get('thoughtsTokenCount',0),u.get('cachedContentTokenCount',0))
        measured='promptTokenCount' in u and 'candidatesTokenCount' in u
        for p in parts:
            if 'functionCall' in p:
                c=p['functionCall'];identifier=c.get('id') or 'call_'+uuid.uuid4().hex
                c['id']=identifier
                calls.append((identifier,c['name'],json.dumps(c.get('args',{}),ensure_ascii=False)))
            elif 'text' in p:
                if not p.get('thought'):texts.append(p['text'])
            else:raise ValueError('Unsupported Gemini content')
    normalized=[{'id':i,'type':'function','function':{'name':n,'arguments':a}} for i,n,a in calls]
    if normalized:normalized[0]['_native']={'transport':mode,'items':deepcopy(parts)}
    if not texts and not normalized:raise ValueError('Provider returned no usable text or function calls')
    return ''.join(texts),normalized,usage,measured
