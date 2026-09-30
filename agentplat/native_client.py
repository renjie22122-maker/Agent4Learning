"""Bounded native JSON transport. Streaming is explicitly unsupported for now."""
import json
import socket
import threading
import time
import urllib.request
import urllib.error
from .llm_errors import LLMCallError, _http_status_to_llm_error
from .native_protocols import encode, decode, endpoint


class NativeClient:
    capabilities={'text':True,'tools':True,'streaming':False,'vision':False,'server_tools':False}

    def __init__(self,cfg):
        self.cfg=cfg;self.calls=0;self.last_error='';self.last_finish_reason=''
        self.cancel_event=None;self.last_usage=None;self.last_usage_estimated=True

    def complete_with_tools(self,model,messages,tools,timeout_s):
        if self.cfg.stream_tools:raise LLMCallError('400','此原生适配器暂不支持流式，请显式关闭 stream_tools；不会静默降级')
        if not self.cfg.base_url or not self.cfg.api_key:raise LLMCallError('401','原生 API 缺少端点或密钥')
        if self.cancel_event and self.cancel_event.is_set():raise InterruptedError('模型请求已取消')
        try:body=encode(self.cfg,model,messages,tools)
        except (ValueError,KeyError) as exc:raise LLMCallError('400',str(exc)) from None
        headers={'Content-Type':'application/json'}
        if self.cfg.transport=='anthropic':headers.update({'x-api-key':self.cfg.api_key,'anthropic-version':'2023-06-01'})
        elif self.cfg.transport=='gemini':headers['x-goog-api-key']=self.cfg.api_key
        else:headers['Authorization']='Bearer '+self.cfg.api_key
        request=urllib.request.Request(endpoint(self.cfg,model),data=json.dumps(body,ensure_ascii=False).encode(),headers=headers,method='POST')
        self.calls+=1;self.last_request_started_at=time.time();start=time.monotonic();self.last_usage=None
        try:
            with urllib.request.urlopen(request,timeout=max(1,timeout_s)) as response:
                done=threading.Event()
                def interrupt():
                    while not done.wait(.1):
                        if (self.cancel_event and self.cancel_event.is_set()) or time.monotonic()-start>timeout_s:
                            try:response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
                            except (AttributeError,OSError):pass
                            return
                watcher=threading.Thread(target=interrupt,daemon=True);watcher.start()
                try:raw=response.read(32_000_001)
                finally:done.set();watcher.join(1)
            if self.cancel_event and self.cancel_event.is_set():raise InterruptedError('模型请求已取消')
            if time.monotonic()-start>timeout_s:raise TimeoutError()
            if len(raw)>32_000_000:raise ValueError('模型响应超过 32 MB')
            text,calls,usage,measured=decode(self.cfg.transport,json.loads(raw))
        except urllib.error.HTTPError as exc:
            # Do not echo untrusted upstream bodies that could contain credentials.
            raise _http_status_to_llm_error(exc.code,'native provider request failed') from None
        except (urllib.error.URLError,TimeoutError):raise LLMCallError('TIMEOUT','原生 API 网络连接或响应超时',True) from None
        except (ValueError,KeyError,TypeError) as exc:raise LLMCallError('502',str(exc)[:300],False) from None
        if not measured:
            from agentlab.tokens import count_messages,count_tokens
            usage.in_tokens=count_messages(messages);usage.out_tokens=count_tokens(text+json.dumps(calls))
        self.last_latency_ms=(time.monotonic()-start)*1000
        self.last_usage=usage;self.last_usage_estimated=not measured;self.last_finish_reason='tool_calls' if calls else 'stop'
        return text,calls,usage

    def complete(self,model,messages,timeout_s):
        text,calls,usage=self.complete_with_tools(model,messages,[],timeout_s)
        if calls or not text.strip():raise LLMCallError('502','未得到完整正文')
        return text,usage

    def probe(self,model,timeout_s=20):
        from agentlab.providers import ChatMessage,LLMError
        try:
            text,usage=self.complete(model,[ChatMessage('user','Reply with a JSON object {"reply":"ok"}.')],timeout_s)
            return dict(ok=True,model=model,reply=text[:120],in_tokens=usage.in_tokens,out_tokens=usage.out_tokens,url=endpoint(self.cfg,model))
        except LLMError as exc:return dict(ok=False,code=exc.code,error=str(exc),url=endpoint(self.cfg,model))
