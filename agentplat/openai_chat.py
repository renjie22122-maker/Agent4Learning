"""OpenAI-compatible Chat Completions transport; no runtime orchestration."""
from __future__ import annotations
import json, threading, time, urllib.error, urllib.request
from typing import Sequence
from agentlab.metrics import METRICS
from agentlab.providers import ChatMessage, LLMError, Usage
from agentlab.tokens import count_messages, count_tokens
from .llmconfig import LLMConfig
from .llm_errors import LLMCallError, _http_status_to_llm_error
from .llm_protocol import PROTOCOL_REPAIRS, sanitize_messages, sanitize_in_place

class OpenAIChatClient:
    """极简 OpenAI 兼容客户端。只用标准库 `urllib`，不引入任何依赖。"""

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.calls = 0
        self.last_error = ""
        self.last_latency_ms = 0.0
        self.last_finish_reason = ""
        self.m_latency = METRICS.histogram("real_llm_latency_ms", "真实 LLM 端到端耗时")
        self.m_calls = METRICS.counter("real_llm_calls_total", "真实 LLM 调用次数")
        self.m_errors = METRICS.counter("real_llm_errors_total", "真实 LLM 调用失败")
        self.m_prompt_tokens = METRICS.counter("real_llm_prompt_tokens_total", "真实输入 token")
        self.m_completion_tokens = METRICS.counter(
            "real_llm_completion_tokens_total", "真实输出 token"
        )

    def _payload(self, model: str, messages: Sequence[ChatMessage],
                 tools: list[dict] | None = None) -> bytes:
        # ★ 发出去之前的最后一道结构校验。
        # 有些错误（重复的 tool_call_id、孤儿 tool 结果、没被回答的 tool_calls）
        # 在本地任何一层都看不出来 —— 工具成功、日志配对正确、不变量也不报，
        # 但 provider 会直接 400。必须在**这一层**拦住。
        #
        # 如果传进来的是 list，就**就地**清理，让对话本身回到合法状态；
        # 只读序列（tuple 等）退化成"只改发出去的那一份"。
        if isinstance(messages, list):
            notes = sanitize_in_place(messages)      # type: ignore[arg-type]
        else:
            messages, notes = sanitize_messages(messages)
        safe = messages
        for n in notes:
            key = n.split("：", 1)[-1].split("，")[0][:40]
            PROTOCOL_REPAIRS[key] = PROTOCOL_REPAIRS.get(key, 0) + 1
        body: dict = {
            "model": model,
            "messages": [m.to_api() for m in safe],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": False,
        }
        for message in body['messages']:
            if message.get('tool_calls'):
                if any(c.get('_native') for c in message['tool_calls']):
                    raise LLMCallError('400','历史含其他原生协议的工具状态，请保留原协议或开启新会话；不能静默丢弃签名或推理状态')
                message['tool_calls'] = [{k:v for k,v in c.items() if k in ('id','type','function')} for c in message['tool_calls']]
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        # 只在显式配置时才带 reasoning_effort：普通对话模型不认这个字段，
        # 无脑发送会换来一个 400，把"能跑"变成"跑不起来"。
        effort = (getattr(self.cfg, "reasoning_effort", "") or "").strip()
        if effort:
            body["reasoning_effort"] = effort
        # 强制结构化输出：靠 prompt 里写"请返回 JSON"不可靠，实测模型会把
        # 思考过程一起吐出来。用 API 参数约束才是工程做法。
        # 注意：带 tools 时**不能**加 response_format，两者都约束输出格式会冲突。
        if not tools and getattr(self.cfg, "json_mode", False):
            body["response_format"] = {"type": "json_object"}
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    # -- 函数调用（编码 agent 用）--------------------------------------
    def complete_with_tools(
        self, model: str, messages: Sequence[ChatMessage],
        tools: list[dict], timeout_s: float,
    ) -> tuple[str, list[dict], Usage]:
        """带工具的一次对话补全。

        返回 ``(文本, tool_calls, 用量)``。``tool_calls`` 非空时，调用方应当
        逐个执行工具、把结果以 ``role="tool"`` 回灌，然后**再次调用本方法** ——
        这个「调模型 → 执行工具 → 回灌结果 → 再调模型」的循环，就是 agent 的本体。
        单次调用不构成 agent，循环才构成。
        """
        url = self.cfg.chat_url()
        if not url:
            raise LLMCallError("400", "未配置 base_url", retryable=False)
        if not self.cfg.api_key:
            raise LLMCallError("401", "未配置 API Key", retryable=False)
        payload = json.loads(self._payload(model, messages, tools=tools))
        self.last_request_started_at = time.time()
        self.last_usage = None
        payload['stream'] = getattr(self.cfg, 'stream_tools', True)
        if payload['stream']:
            payload['stream_options'] = {'include_usage': True}
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.cfg.api_key}"},
            method="POST",
        )
        t0 = time.perf_counter()
        self.calls += 1
        self.m_calls.inc()
        try:
            with urllib.request.urlopen(req, timeout=max(1.0, timeout_s)) as resp:
                if 'text/event-stream' in resp.headers.get('Content-Type', ''):
                    from .streaming import assemble
                    done = threading.Event()
                    cancel = getattr(self, 'cancel_event', None)
                    def cancel_reader():
                        import socket
                        while not done.wait(.1):
                            if cancel is not None and cancel.is_set():
                                try: resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
                                except (AttributeError, OSError): pass
                                return
                    watcher = threading.Thread(target=cancel_reader, daemon=True)
                    watcher.start()
                    try:
                        raw = json.dumps(assemble(resp, getattr(self, 'on_text', None), cancel))
                    finally:
                        done.set(); watcher.join(1)
                else:
                    raw = resp.read(32_000_001).decode("utf-8", "replace")
                    if len(raw) > 32_000_000: raise ValueError('模型响应过大')
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            self.m_errors.inc()
            err = _http_status_to_llm_error(exc.code, body)
            self.last_error = str(err)
            raise err from None
        except urllib.error.URLError as exc:
            self.m_errors.inc()
            raise LLMCallError("TIMEOUT", f"网络不可达：{exc.reason}", retryable=True) from None
        except TimeoutError:
            self.m_errors.inc()
            raise LLMCallError("TIMEOUT", f"上游超过 {timeout_s:.1f}s 未响应",
                               retryable=True) from None
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.m_latency.observe(elapsed_ms)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self.m_errors.inc()
            raise LLMCallError("502", f"响应不是合法 JSON：{raw[:200]}", retryable=True) from None
        if data.get("error"):
            self.m_errors.inc()
            raise LLMCallError("502", f"上游 error：{str(data['error'])[:300]}", retryable=True)
        choices = data.get("choices") or []
        if not choices:
            self.m_errors.inc()
            raise LLMCallError("502", f"响应没有 choices：{raw[:200]}", retryable=True)
        msg = choices[0].get("message") or {}
        usage_raw = data.get("usage") or {}
        self.last_usage_estimated = not all(isinstance(usage_raw.get(k), int) for k in ('prompt_tokens','completion_tokens'))
        usage = Usage(
            in_tokens=int(usage_raw.get("prompt_tokens", count_messages(messages))),
            out_tokens=int(usage_raw.get("completion_tokens", count_tokens(json.dumps(msg, ensure_ascii=False)))),
            cached_tokens=int(usage_raw.get("prompt_cache_hit_tokens", (usage_raw.get('prompt_tokens_details') or {}).get('cached_tokens', 0))),
        )
        self.m_prompt_tokens.inc(usage.in_tokens)
        self.m_completion_tokens.inc(usage.out_tokens)
        # finish_reason 必须透出去：`length` 表示输出被 max_tokens 截断，
        # 此时工具参数（往往是一整个文件内容）必然是残的。
        # **系统要主动告诉模型"你被截断了"**，而不是让它自己从解析失败里猜 ——
        # 实测模型能猜到，但会浪费 2~3 轮才反应过来，那几轮都是白花的钱。
        self.last_finish_reason = str(choices[0].get("finish_reason") or "")
        return (msg.get("content") or ""), list(msg.get("tool_calls") or []), usage

    def complete(
        self, model: str, messages: Sequence[ChatMessage], timeout_s: float
    ) -> tuple[str, Usage]:
        """发一次对话补全。返回 (文本, token 用量)。"""
        self.last_request_started_at = time.time()
        self.last_usage = None
        url = self.cfg.chat_url()
        if not url:
            raise LLMCallError("400", "未配置 base_url", retryable=False)
        if not self.cfg.api_key:
            raise LLMCallError("401", "未配置 API Key", retryable=False)

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Accept": "application/json",
        }
        req = urllib.request.Request(url, data=self._payload(model, messages),
                                     headers=headers, method="POST")
        t0 = time.perf_counter()
        self.calls += 1
        self.m_calls.inc()
        try:
            with urllib.request.urlopen(req, timeout=max(1.0, timeout_s)) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            self.m_errors.inc()
            err = _http_status_to_llm_error(exc.code, body)
            self.last_error = str(err)
            raise err from None
        except urllib.error.URLError as exc:
            self.m_errors.inc()
            self.last_error = f"网络不可达: {exc.reason}"
            # 连不上是会恢复的（DNS 抖动/代理重启），所以可重试
            raise LLMCallError("TIMEOUT", f"网络不可达：{exc.reason}", retryable=True) from None
        except TimeoutError:
            self.m_errors.inc()
            self.last_error = "上游超时"
            raise LLMCallError("TIMEOUT", f"上游超过 {timeout_s:.1f}s 未响应", retryable=True) from None

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.last_latency_ms = elapsed_ms
        self.m_latency.observe(elapsed_ms)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self.m_errors.inc()
            raise LLMCallError("502", f"响应不是合法 JSON：{raw[:200]}", retryable=True) from None

        if "error" in data and data["error"]:
            self.m_errors.inc()
            msg = str(data["error"])[:300]
            self.last_error = msg
            raise LLMCallError("502", f"上游返回 error 字段：{msg}", retryable=True)

        choices = data.get("choices") or []
        if not choices:
            self.m_errors.inc()
            raise LLMCallError("502", f"响应没有 choices：{raw[:200]}", retryable=True)

        msg = choices[0].get("message") or {}
        text = msg.get("content") or ""
        # 推理模型（如 deepseek-reasoner）会把思考过程放在 reasoning_content，
        # 只取 content 作为答案；但若 content 为空则退回 reasoning_content，
        # 否则用户会看到"空回答"而不知道发生了什么。
        # Reasoning is not a completed answer. Reject empty/truncated output so
        # compaction keeps the original history rather than accepting thoughts.
        usage_raw = data.get("usage") or {}
        self.last_usage_estimated = not all(isinstance(usage_raw.get(k), int) for k in ('prompt_tokens','completion_tokens'))
        usage = Usage(
            in_tokens=int(usage_raw.get("prompt_tokens", count_messages(messages))),
            out_tokens=int(usage_raw.get("completion_tokens", count_tokens(text))),
            cached_tokens=int(usage_raw.get("prompt_cache_hit_tokens", (usage_raw.get('prompt_tokens_details') or {}).get('cached_tokens', 0))),
        )
        self.m_prompt_tokens.inc(usage.in_tokens)
        self.m_completion_tokens.inc(usage.out_tokens)
        self.last_usage = usage
        if not text.strip() or choices[0].get('finish_reason') == 'length':
            raise LLMCallError('502', '模型未返回完整正文（为空或达到输出上限），未采用推理文本代替答案', retryable=True)
        return text, usage

    def probe(self, model: str, timeout_s: float = 20.0) -> dict:
        """连通性自检：界面上"测试连接"按钮用它。

        返回结构化结果而不是抛异常 —— 界面需要展示**具体**是鉴权错、模型名错
        还是网络不通，而不是一句"失败了"。
        """
        t0 = time.perf_counter()
        msgs = [ChatMessage("user", '请返回一个 JSON 对象：{"reply":"好"}。')]
        try:
            text, usage = self.complete(model, msgs, timeout_s)
        except LLMError as exc:
            return {
                "ok": False,
                "code": str(getattr(exc, "code", "ERR")),
                "error": str(exc)[:400],
                "hint": _hint_for(str(getattr(exc, "code", ""))),
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                "url": self.cfg.chat_url(),
            }
        return {
            "ok": True,
            "model": model,
            "reply": text[:120],
            "in_tokens": usage.in_tokens,
            "out_tokens": usage.out_tokens,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            "url": self.cfg.chat_url(),
        }


def _hint_for(code: str) -> str:
    return {
        "401": "检查 API Key 是否正确、是否有多余空格；有些厂商要求 key 带前缀。",
        "403": "模型未开通或账户余额不足 —— 去厂商控制台确认。",
        "404": "base_url 或模型名不对。注意：有的要带 /v1，有的不带。",
        "400": "请求参数被拒，常见原因是上下文超长或 max_tokens 过大。",
        "429": "触发限流：降低并发或稍后重试（平台已内置退避重试）。",
        "TIMEOUT": "网络不通或超时。若在受限网络内，请配代理或换本地 Ollama。",
    }.get(code, "查看右侧错误详情；平台已把不可重试的错误与可重试的分开处理。")


