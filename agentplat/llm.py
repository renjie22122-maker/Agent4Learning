"""真实 LLM 后端：走任意 **OpenAI 兼容** 端点，零第三方依赖。

设计要点
--------
**它是 `LLMServer` 的子类，只覆写 `_serve()`。** 这一个决定带来三件好处：

1. 并发闸门、排队与 429、客户端超时语义、前缀缓存、成本账本、指标打点
   —— 全部复用父类已经写好且被 17 个 lab 验证过的实现；换成真实后端后，
   我们观察到的仍然是**同一套可靠性行为**，而不是"真实模式下另一套逻辑"。
2. 17 个 lab 用的是内置模拟器（`LLMServer`），**完全不受影响** ——
   真实后端是扩展开关，不是替换。
3. 想接新厂商只需要改 base_url / 模型名，不用碰引擎和 UI。

只覆盖 `_serve()` 而不是重写 `call()`，是因为父类的 `call()` 里有真正重要的
逻辑：并发槽的获取与释放、排队深度与 429、客户端超时后"服务端仍在跑"的
隔离语义。那些才是生产行为，必须原样保留。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Sequence

from agentlab.metrics import METRICS
from agentlab.providers import ChatMessage, LLMError, LLMReply, LLMServer, Usage
from agentlab.tokens import MID, ModelSpec, count_messages, count_tokens

from .llmconfig import LLMConfig


class LLMCallError(LLMError):
    """真实调用失败。继承 LLMError，所以重试/熔断策略会自动生效。"""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(code, message, 0.0, retryable)


#: 已修好的协议异常计数（按类型）。用于在页面上把"曾经坏过"讲清楚，
#: 而不是悄悄修掉 —— 静默修复会让同一个 bug 在别处继续长出来。
PROTOCOL_REPAIRS: dict[str, int] = {}


def sanitize_messages(messages: Sequence[ChatMessage]) -> tuple[list[ChatMessage], list[str]]:
    """修掉会让 **provider 直接 400** 的消息序列问题，返回 (修好的消息, 说明列表)。

    ## 为什么必须有这一层

    真实 provider 对消息序列的约束比大多数人以为的严格得多。实测踩到的报错：

        400 Duplicate value for 'tool_call_id' of call_00_ET_... in message[4]

    这类错误**在本地任何一层都看不出来**：工具执行成功、会话日志配对正确、
    不变量也没报（因为日志本身没错）—— 错的是**发出去的那个数组**。
    所以必须在**发出去之前的最后一刻**做一次结构性校验。

    ## 修哪三类（都是"必然被拒"的）

    1. **重复的 tool 结果**：同一个 `tool_call_id` 出现两次。
       provider 直接 400。修法：保留第一条（先到的才是真正的结果），
       丢掉后面的。
    2. **孤儿 tool 消息**：`tool_call_id` 在历史里找不到对应的 assistant
       `tool_calls`。provider 同样拒绝。修法：丢掉。
    3. **没被回答的 tool_calls**：assistant 声明了 N 个调用，但后面只有
       M < N 个结果。OpenAI 兼容端普遍要求**全部**有结果；缺的会让请求被拒。
       修法：给缺的那些补一条"已丢弃/未执行"的结果，而不是删掉 assistant
       消息 —— 删掉会让模型以为自己没调用过，然后原样再调一次。

    ## 它**不**做什么

    它不修"内容不对"（那是模型的问题），也不修"顺序不对"（那是循环的问题）。
    它只保证发出去的数组**结构合法**。任何被修掉的地方都会记进返回的说明里，
    调用方应当把它们暴露出来而不是吞掉 —— 一次静默修复意味着一个还在的 bug。
    """
    fixed: list[ChatMessage] = []
    notes: list[str] = []
    # ⚠ 这里必须**按 id 收集结果**，不能只用集合去重。
    #
    # 早期版本只维护 `seen_result_ids`（见过哪些结果 id），然后用
    # `declared - seen_result_ids` 求"哪些调用没有结果"。看起来对，
    # 但漏了一种情况：**同一个调用被回答了两次**（第一次是真的结果、
    # 第二次是上一轮补的占位）—— 集合里它只算"出现过"，而多出来的那条
    # 又不在"缺结果"名单里，于是**每一轮都会再补一条**，
    # 消息数组越滚越长。实测就是靠这个把循环搞坏的：
    # 一次被拒的 finish 调用，每轮都多一条假 tool 结果，
    # 模型的轮次计数全乱，反射看起来"完全没生效"。
    result_ids: list[str] = []
    declared_order: list[str] = []
    for i, m in enumerate(messages):
        role = getattr(m, "role", "")
        if role == "assistant":
            for tc in (getattr(m, "tool_calls", None) or []):
                tid = tc.get("id") if isinstance(tc, dict) else None
                if tid and str(tid) not in declared_order:
                    declared_order.append(str(tid))
        elif role == "tool":
            tid = str(getattr(m, "tool_call_id", "") or "")
            if tid and tid in result_ids:
                notes.append(f"message[{i}]：tool_call_id={tid} 的结果重复，已丢弃后一条")
                continue
            if tid and tid not in declared_order:
                # 注意：declared_order 是按顺序累加的，这里可能遇到
                # "结果出现在它对应的 assistant 之前"的坏序 —— 那也丢掉。
                notes.append(f"message[{i}]：tool_call_id={tid} 没有对应的调用，"
                             f"已丢弃这条孤儿结果")
                continue
            if tid:
                result_ids.append(tid)
        fixed.append(m)

    # 补齐"声明了但没结果"的调用。统一追加到末尾 —— 顺序上它们是
    # "最后发生的"，语义也说得通；插到紧邻位置会打乱已有顺序。
    missing = [t for t in declared_order if t not in result_ids]
    for tid in missing:
        fixed.append(ChatMessage(
            "tool",
            "[结果缺失] 这条工具调用没有产生结果（循环被中断或结果被压缩移除）。"
            "需要它的输出请重新调用一次。",
            tool_call_id=tid,
        ))
        notes.append(f"tool_call_id={tid} 的调用没有结果，已补一条占位结果")
    return fixed, notes


def sanitize_in_place(messages: list[ChatMessage]) -> list[str]:
    """就地清理（**会改写传入的列表**），返回说明列表。

    ## 为什么要就地改，而不是只改发出去的那一份

    只在 `_payload()` 里改的话，**坏消息会一直留在历史里**：
    它每轮都参与压缩压力计算、每轮都要重新清一遍，而且下一次拼接时
    还可能再长出一条重复的 —— 同一个 bug 会被"修"无数次，而源头一直在。

    ⚠ 这确实是对调用方数据的副作用。之所以接受：清理后的序列在语义上
    等价于清理前（丢的是重复/孤儿，补的是占位），继续带着坏数据往下走
    只会更糟。但调用方必须知道 —— 所以函数名里就写了 `in_place`，
    而不是悄悄塞进 `_payload()` 里。
    """
    fixed, notes = sanitize_messages(messages)
    if notes:
        messages[:] = fixed
    return notes


def _http_status_to_llm_error(status: int, body: str) -> LLMCallError:
    """把 HTTP 状态映射成平台认识的错误码。

    关键点：**retryable 的判定决定上层要不要重试**，判错会放大故障。
    429/5xx/超时是可重试的（上游暂时不行）；401/400/404 不可重试
    （key 错了、模型名拼错了 —— 重试一万次也没用，只会浪费时间）。
    """
    detail = body[:400].replace("\n", " ")
    if status == 429:
        return LLMCallError("429", f"上游限流：{detail}", retryable=True)
    if status in (500, 502, 503, 504):
        return LLMCallError("503", f"上游 {status}：{detail}", retryable=True)
    if status == 401:
        return LLMCallError("401", f"鉴权失败（API Key 不对或没权限）：{detail}", retryable=False)
    if status == 403:
        return LLMCallError("403", f"无权限（可能是模型未开通/余额不足）：{detail}", retryable=False)
    if status == 404:
        return LLMCallError("404", f"端点或模型不存在（检查 base_url 与模型名）：{detail}", retryable=False)
    if status == 400:
        return LLMCallError("400", f"请求被拒（参数/上下文超限）：{detail}", retryable=False)
    if status >= 500:
        return LLMCallError("503", f"上游 {status}：{detail}", retryable=True)
    return LLMCallError(str(status), f"上游返回 {status}：{detail}", retryable=False)


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
            notes = sanitize_messages(messages)[1]
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


class RealLLMServer(LLMServer):
    """把真实 OpenAI 兼容端点接进平台，但**保留全部可靠性机制**。"""

    def __init__(self, cfg: LLMConfig, guard=None):
        self.llm_cfg = cfg
        self.guard = guard
        self.dry_run = bool(getattr(guard, "dry_run", False))
        self.coalesced = 0  # 被合并掉的重复调用数（省下的真实请求）
        # ⚠ 命名警告：**不要叫 `_inflight`**。
        # 父类 `LLMServer` 已经用 `self._inflight` 存"每个模型的并发计数"
        # （`dict[str, int]`）。子类若用同名属性去存合并表（`dict[key, _PendingCall]`），
        # 就会把父类那个字典整个覆盖掉，于是父类里的
        #     sum(self._inflight.values())
        # 变成 int + _PendingCall，直接抛
        #     TypeError: unsupported operand type(s) for +: 'int' and '_PendingCall'
        #
        # 这个 bug 真发生过。教训：**继承时新增状态前，先确认父类没有同名属性**；
        # 子类私有状态统一加业务前缀（这里是 _coalesce_*），不要图省事沿用通用名。
        self._coalesce: dict[tuple, "_PendingCall"] = {}
        self._coalesce_lock = threading.Lock()
        tier = cfg.tier_map()
        specs = [
            ModelSpec(
                name="small-8b", tier="small",
                latency_p50_ms=800, quality=0.7,
                in_price=cfg.price_in_per_m, out_price=cfg.price_out_per_m,
                max_parallel=cfg.max_parallel,
            ),
            ModelSpec(
                name="mid-32b", tier="mid",
                latency_p50_ms=1200, quality=0.85,
                in_price=cfg.price_in_per_m, out_price=cfg.price_out_per_m,
                max_parallel=cfg.max_parallel,
            ),
            ModelSpec(
                name="large-400b", tier="large",
                latency_p50_ms=2000, quality=0.95,
                in_price=cfg.price_in_per_m, out_price=cfg.price_out_per_m,
                max_parallel=max(1, cfg.max_parallel // 2),
            ),
        ]
        super().__init__(specs, max_queue=64, max_wait_s=cfg.timeout_s, seed=7)
        self.client = OpenAIChatClient(cfg)
        self.real_calls = 0
        self.real_errors = 0
        self.fallbacks = 0
        self._tier_models = tier
        self.m_real = METRICS.counter("real_llm_served_total", "真实 LLM 成功应答")
        self.m_fallback = METRICS.counter("real_llm_fallback_total", "退回模拟器次数")
        self.m_coalesced = METRICS.counter(
            "real_llm_coalesced_total", "并发重复请求被合并（省下的真实调用）"
        )

    # -- 覆写这一处即可 ------------------------------------------------
    #: 只有**暂时性**故障才退回模拟器。401/403/404/400 是配置错误，
    #: 退回模拟器会把"key 错了"伪装成"模型答得怪"，用户永远查不出问题。
    #: 宁可让请求失败并显示真实原因，也不要给一个看起来正常的假答案。
    TRANSIENT_CODES = ("429", "503", "TIMEOUT", "502")

    def _serve(
        self,
        spec: ModelSpec,
        messages: Sequence[ChatMessage],
        queued_ms: float,
        tenant: str,
        tag: str,
    ) -> LLMReply:
        model_name = self._tier_models.get(spec.name, self.llm_cfg.model)
        # 变量名必须区分开：下面退回分支会把"加了说明的副本"传给父类，
        # 若直接覆盖 messages，父类再读 messages[-1].content 就会炸
        # （实测踩过：AttributeError: 'list' object has no attribute 'content'）。
        req_messages = list(messages)
        in_tokens = count_messages(req_messages)
        cached = self._classify(req_messages, spec.name)[2]

        with self._lock:
            self.stats["requests"] += 1
            self.m_calls.inc()
            self.m_by_model.inc()
            self.m_tokens_in.inc(in_tokens)

        # ---- 成本护栏：必须在**出网之前**检查，事后统计没有意义 ----
        if self.guard is not None:
            self.guard.preflight(in_tokens, tag=tag or spec.name)
            if self.dry_run:
                # 干跑模式：不发出真实请求，返回一个明确标注的占位结果。
                # 这样批量实验可以先跑一遍看调用量与估算花费，再决定是否真跑。
                return LLMReply(
                    text='{"answer": "[dry-run] 未发起真实请求", "confidence": 0.0}',
                    model=f"{model_name}(dry-run)",
                    usage=Usage(in_tokens, 8, cached),
                    latency_ms=queued_ms,
                    cached=False,
                    queued_ms=queued_ms,
                    raw={"dry_run": True, "tier": spec.name},
                )

        # ---- 并发请求合并（singleflight）----
        # 10 个用户同时问同一句话时，只发 1 次真实请求，其余等结果。
        # 这是真实后端下**最直接的省钱手段**（缓存只能挡住"先后到达"的重复，
        # 挡不住"同时到达"的重复）。
        key = (model_name, self.llm_cfg.temperature, hash(tuple(
            (m.role, m.content) for m in req_messages
        )))
        with self._coalesce_lock:
            pending = self._coalesce.get(key)
            if pending is None:
                pending = _PendingCall()
                self._coalesce[key] = pending
                leader = True
            else:
                leader = False

        if not leader:
            # wait() 在领头失败时会原样抛出同一个异常 —— 等待者不会各自重发，
            # 所以上游出问题时并发请求数不会从 1 被放大成 N。
            text, usage = pending.wait(self.llm_cfg.timeout_s + 5)
            with self._lock:
                self.coalesced += 1
                self.stats["ok"] += 1
            self.m_coalesced.inc()
            self.ledger.add(spec, usage.in_tokens, usage.out_tokens, cached,
                            tenant=tenant, tag=f"{tag}:coalesced")
            self.m_usd.inc(
                (max(0, usage.in_tokens - cached) + cached * 0.1) * spec.in_price / 1e6
                + usage.out_tokens * spec.out_price / 1e6
            )
            return LLMReply(
                text=text, model=model_name, usage=usage,
                latency_ms=queued_ms + pending.elapsed_ms, cached=False,
                queued_ms=queued_ms,
                raw={"coalesced": True, "tier": spec.name},
            )

        t0 = time.perf_counter()
        try:
            text, usage = self.client.complete(
                model_name, req_messages, self.llm_cfg.timeout_s
            )
        except LLMError as exc:
            with self._coalesce_lock:
                self._coalesce.pop(key, None)
            pending.fail(exc)
            code = str(getattr(exc, "code", "ERR"))
            transient = code in self.TRANSIENT_CODES
            with self._lock:
                self.real_errors += 1
                self.stats["server_errors"] += 1
                self.m_5xx.inc()
                if transient and self.llm_cfg.offline_mock_fallback:
                    self.fallbacks += 1
            # 失败也记账：这些调用同样消耗了并发窗口与上行流量
            self.ledger.add(spec, in_tokens, 0, cached, tenant=tenant,
                            tag=f"{tag}:failed")
            if not (transient and self.llm_cfg.offline_mock_fallback):
                # 配置类错误，或用户关掉了兜底 → 如实抛出，让上层熔断/降级处理
                raise
            self.m_fallback.inc()
            note = (
                f"[真实 LLM 暂时不可用，本条由内置模拟器代答] {code}: {str(exc)[:160]}"
            )
            fallback = super()._serve(
                spec, _prepend_note(req_messages, note), queued_ms, tenant, tag
            )
            fallback.raw["fallback_reason"] = str(exc)[:300]
            return fallback

        total_ms = queued_ms + (time.perf_counter() - t0) * 1000.0
        # 让等待中的并发请求拿到同一份结果（singleflight 收尾）
        with self._coalesce_lock:
            self._coalesce.pop(key, None)
        pending.resolve(text, usage)
        if self.guard is not None:
            self.guard.record(
                usage.in_tokens, usage.out_tokens,
                self.llm_cfg.price_in_per_m, self.llm_cfg.price_out_per_m,
                tag=tag or spec.name,
            )
        with self._lock:
            self.real_calls += 1
            self.stats["ok"] += 1
            self.stats["total_queue_ms"] += queued_ms
            self.m_tokens_out.inc(usage.out_tokens)
            if cached:
                self.stats["cache_hits"] += 1
                self.m_cache.inc()
        self.m_real.inc()
        self.m_latency.observe(total_ms)
        self.m_queue.observe(queued_ms)
        self.ledger.add(
            spec, usage.in_tokens, usage.out_tokens, cached, tenant=tenant, tag=tag
        )
        self.m_usd.inc(
            (max(0, usage.in_tokens - cached) + cached * 0.1) * spec.in_price / 1_000_000
            + usage.out_tokens * spec.out_price / 1_000_000
        )
        return LLMReply(
            text=text,
            model=f"{model_name}",  # 显示真实模型名，而不是内部档位名
            usage=usage,
            latency_ms=total_ms,
            cached=cached > 0,
            queued_ms=queued_ms,
            raw={"real": True, "tier": spec.name},
        )

    # -- 界面需要的诊断 ------------------------------------------------
    def summary_lines(self) -> list[str]:
        base = super().summary_lines()
        return [
            f"后端=真实 LLM  成功={self.real_calls}  失败={self.real_errors}  "
            f"退回模拟器={self.fallbacks}",
            f"端点={self.llm_cfg.chat_url()}",
            *base,
        ]

    def probe(self, tier: str = "mid") -> dict:
        model = self._tier_models.get(f"{tier}", self.llm_cfg.model)
        return self.client.probe(model)


def _prepend_note(messages: Sequence[ChatMessage], note: str) -> list[ChatMessage]:
    """给退回模拟器的调用加一条说明，让用户明确知道"这条不是真实模型答的"。"""
    if not messages:
        return [ChatMessage("system", note)]
    out = list(messages)
    first = out[0]
    if first.role == "system":
        out[0] = ChatMessage("system", f"{note}\n{first.content}")
    else:
        out.insert(0, ChatMessage("system", note))
    return out


class _PendingCall:
    """一次在飞的真实调用，供并发请求共享结果（singleflight）。

    为什么要它：缓存只挡得住"**先后**到达"的重复请求，挡不住"**同时**到达"的。
    10 个用户同时问同一句话时，没有合并就会打出 10 次真实 API 调用 ——
    这是真实 key 下最直接、最容易忽略的浪费。
    """

    __slots__ = ("_ev", "_result", "_error", "started_at")

    def __init__(self) -> None:
        self._ev = threading.Event()
        self._result: tuple[str, Usage] | None = None
        self._error: BaseException | None = None
        self.started_at = time.perf_counter()

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started_at) * 1000.0

    def resolve(self, text: str, usage: Usage) -> None:
        self._result = (text, usage)
        self._ev.set()

    def fail(self, exc: BaseException) -> None:
        self._error = exc
        self._ev.set()

    def wait(self, timeout_s: float) -> tuple[str, Usage]:
        """等领头请求完成。

        * 成功 → 返回 ``(text, usage)``，**每个等待者拿到同一份结果**。
        * 领头失败 → **原样抛出同一个异常**，而不是返回 None 让等待者各自重发。
          为什么这点重要：等待者失败后若各自重发，N 个并发请求就变成
          1 + N 次上游调用 —— 合并白做了；而且上游正出问题时被我们 N 倍放大，
          恰好是最该收敛的时刻。
        * 超时 → 抛 ``LLMError.timeout``，交给上层正常超时路径处理。
        """
        if not self._ev.wait(max(1.0, timeout_s)):
            raise LLMError.timeout(timeout_s)
        if self._error is not None:
            raise self._error
        if self._result is None:
            raise LLMError("EMPTY", "合并调用没有返回结果", 0.0, retryable=True)
        return self._result


def build_server(cfg: LLMConfig, guard=None) -> LLMServer:
    """按配置返回后端：真实 LLM 或内置模拟器。

    ``guard`` 只对真实后端生效 —— 模拟器不花钱，不需要护栏。
    """
    if cfg.is_real:
        return RealLLMServer(cfg, guard=guard)
    return LLMServer(max_queue=64, max_wait_s=30.0, seed=7)


__all__ = [
    "LLMConfig",
    "OpenAIChatClient",
    "RealLLMServer",
    "build_server",
    "MID",
    "LLMCallError",
]
