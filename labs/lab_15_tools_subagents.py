"""Lab: 工具与子 Agent 工程化 —— 工具怎么写、怎么调、子 agent 什么时候该开。
这个 lab 回答什么问题
--------------------
* 「如何开启子 agent，如何调用工具，如何编写工具？」
* 一次工具调用会以哪六种方式失败，每一种的工程解法是什么。
复现什么故障
-----------
v0 是"裸工具"：``fn(args)`` 直接调用 —— 没有 schema 校验、没有工具级超时、返回值原样进上下文、
没有幂等键、没有权限声明、没有迭代上限。于是：参数幻觉把脏数据写进表；挂死的工具让整条链路停住；
200KB 返回值挤爆上下文；重试 + 双线程写出重复行；描述含糊导致一半意图选错工具；脚本化模型反复调同一个工具，只能靠硬上限截断。
生产正确做法
-----------
``Tool``（params JSON Schema / timeout_s / retry / idempotent / required_perms / cost_usd /max_result_tokens / needs_confirm）+ ``ToolRegistry`` 强制这些能力：结构化 ToolArgError 回灌
prompt 让模型自我修复；工具自带 timeout_s（worker join + 协作取消）；结果截断 + 摘要 + ref 按需回查；同 key 写入串行化实现真幂等；权限在注册表里判定；max_iterations + 重复调用检测 + 环检测。
子 agent = 独立上下文 + 收窄工具集 + 独立预算/超时/模型档位 + 结构化信封回传。
工程结论
--------
工具的"能力"必须落在框架里，不能写在 prompt 里；子 agent 是用钱和延迟买上下文隔离、并行度、权限收窄和独立预算 —— 简单任务开子 agent 只会更慢更贵（本 lab 把倍数实测出来）。
"""
from __future__ import annotations
import hashlib, json, os, random, re, sys, threading, time  # noqa: E401
from dataclasses import dataclass, field
from typing import Any, Callable
from agentlab.metrics import METRICS
from agentplat.runtime import invoke_checked
from agentlab.orchestration import Deadline
from agentlab.providers import LLMServer, assistant, system, tool_msg, user
from agentlab.store import BM25Index, Query, build_corpus
from agentlab.tokens import MODELS, count_messages, count_tokens, price_of
from agentlab.util import (BROKEN, FIX, VERIFY, fmt_bytes, head, improvement, kv, lab, note, phase, run_concurrently, takeaway)
LAB_ID = "lab-15-tools-subagents"
CONFIRM_TICKET = "CONFIRM-7f21"  # human-in-the-loop 的确认票据（真实系统在网关侧签发）
PARENT_PERMS = frozenset({"kb:read", "net:read", "db:write", "shell:read"})
CTX_WINDOW, MAX_LOOP_CAP = 32_768, 20  # 模型上下文窗口 / 实验硬上限（绝不允许无界循环）
MAX_ITER, REPEAT_LIMIT = 6, 3  # 生产：工具迭代上限 / 同 name+args 重复 N 次判为重复调用
HANG_S, BIG_BODY_KB = 1.6, 200  # 对端挂死时长 / 超大返回值
DB: dict[str, list[dict]] = {"orders": [], "audit": []}  # 工具层的"业务库"，纯内存
DB_LOCK = threading.Lock()
def chg(before: float, after: float) -> str:  # 带符号变化率：正=上升、负=下降
    return f"{(after - before) / before * 100.0:+.1f}%" if before else "n/a"
_short = lambda v, n=40: (repr(v)[: n - 1] + "…") if len(repr(v)) > n else repr(v)  # noqa: E731
_TYPES = {"string": lambda v: isinstance(v, str), "boolean": lambda v: isinstance(v, bool), "integer": lambda v: isinstance(v, int) and not isinstance(v, bool), "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool), "object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list)}
_CHECKS = {"minimum": (lambda v, c: v < c, "小于"), "maximum": (lambda v, c: v > c, "大于"), "minLength": (lambda v, c: len(v) < c, "长度不足"), "maxLength": (lambda v, c: len(v) > c, "长度超限")}
T = lambda t, **k: {"type": t, **k}  # noqa: E731  —— 精简 JSON Schema 构造器
OBJ = lambda req, **p: {"type": "object", "additionalProperties": False, "required": req, "properties": p}  # noqa: E731
_toks = lambda s: set(re.findall(r"[a-z0-9_]{2,}", s.lower())) | {r[i: i + 2] for r in re.findall(r"[\u4e00-\u9fff]+", s.lower()) for i in range(max(1, len(r) - 1))}  # noqa: E731
CORRUPT = lambda r: not (isinstance(r.get("id"), int) and not isinstance(r.get("id"), bool) and r["id"] >= 1 and isinstance(r.get("amount"), (int, float)) and not isinstance(r.get("amount"), bool) and r["amount"] >= 0)  # noqa: E731

class ToolError(Exception):
    code = "TOOL_ERROR"
class ToolTimeout(ToolError):
    code = "TIMEOUT"
class ToolDenied(ToolError):
    code = "PERMISSION_DENIED"
class ToolNeedsConfirm(ToolError):
    code = "NEEDS_CONFIRM"
class ToolArgError(ToolError):
    """参数不合法。**结构化**错误会被渲染回 prompt，模型据此自我修复。"""
    code = "BAD_ARGS"
    def __init__(self, param: str, expected: str, got: Any, message: str):
        super().__init__(message)
        self.detail = {"param": param, "expected": expected, "got": _short(got), "message": message}
    def prompt_line(self, tool: str) -> str:
        d = self.detail
        return (f"tool_error({tool}): param={d['param']} expected={d['expected']} got={d['got']} :: {d['message']}" f" —— 请修正参数后重试")
def validate_args(value: Any, schema: dict, path: str = "$") -> None:
    """精简 JSON Schema 校验器：type/required/enum/minimum/maximum/pattern/maxLength/minLength/additionalProperties=false（递归）；错误信息带 param+expected+got 以便回灌 prompt。"""
    t = schema.get("type", "object")
    if not _TYPES.get(t, lambda v: isinstance(v, str))(value): raise ToolArgError(path, t, value, f"{path} 期望 {t}，收到 {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]: raise ToolArgError(path, f"enum {schema['enum']}", value, f"{path} 只能是 {schema['enum']}")
    for kw, (bad, name) in _CHECKS.items():
        if kw in schema and bad(value, schema[kw]): raise ToolArgError(path, f"{kw} {schema[kw]}", value, f"{path} {name} {kw}")
    if "pattern" in schema and not re.fullmatch(schema["pattern"], value): raise ToolArgError(path, f"pattern {schema['pattern']}", value, f"{path} 不匹配 pattern")
    if t == "array":
        for i, v in enumerate(value): validate_args(v, schema.get("items", {}), f"{path}[{i}]")
    elif t == "object":
        for req in schema.get("required", []):
            if req not in value: raise ToolArgError(f"{path}.{req}", "required", None, f"缺少必填参数 {req}")
        props = schema.get("properties", {}); extra = sorted(set(value) - set(props))
        if schema.get("additionalProperties") is False and extra:
            raise ToolArgError(f"{path}.{extra[0]}", f"one of {sorted(props)}", value[extra[0]], f"不存在的参数 {extra}；本工具只接受 {sorted(props)}")
        for k, v in value.items():
            if k in props: validate_args(v, props[k], f"{path}.{k}")

@dataclass
class ToolCtx:
    perms: frozenset[str] = frozenset(); tenant: str = "tenant-a"; confirm: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)
    refs: dict[str, str] = field(default_factory=dict)
@dataclass
class ToolResult:
    ok: bool
    text: str = ""; tokens: int = 0; full_tokens: int = 0; ref: str = ""; deduped: bool = False
@dataclass
class Tool:
    """生产级工具声明。缺任何一项都对应一类线上事故（见文件顶部）。"""
    name: str; description: str; params: dict; fn: Callable[[dict, ToolCtx], Any]
    timeout_s: float = 2.0; max_retries: int = 0; retry_on: tuple[str, ...] = ()
    idempotent: bool = True; required_perms: frozenset[str] = frozenset(); cost_usd: float = 0.0
    max_result_tokens: int = 600; needs_confirm: bool = False; when_to_use: str = ""; when_not_to_use: str = ""
    def wire(self) -> dict:  # 真正交给 LLM 的那段 JSON Schema 文本（含执行策略声明）
        pol = {"timeout_ms": int(self.timeout_s * 1000), "idempotent": self.idempotent, "cost_usd": self.cost_usd, "max_retries": self.max_retries, "retry_on": list(self.retry_on), "needs_confirm": self.needs_confirm, "required_perms": sorted(self.required_perms), "max_result_tokens": self.max_result_tokens}
        return {"name": self.name, "description": self.description, "parameters": self.params, "execution_policy": pol, "when_to_use": self.when_to_use, "when_not_to_use": self.when_not_to_use}

class ToolRegistry:
    """工具注册表：校验 / 权限 / 确认 / 幂等 / 超时 / 截断 全都在这一层强制。"""
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}; self._idem: dict[str, ToolResult] = {}
        self._key_locks: dict[str, threading.Lock] = {}; self._lock = threading.Lock()
        self.m_calls = METRICS.counter("tool_calls_total", "工具调用次数"); self.m_args = METRICS.counter("tool_arg_errors_total", "参数校验拦截次数")
        self.m_timeout = METRICS.counter("tool_timeouts_total", "工具超时次数"); self.m_tokens = METRICS.counter("tool_result_tokens_total", "工具返回值 token")
        self.m_lat = METRICS.histogram("tool_latency_ms", "工具耗时")
    def register(self, tool: Tool) -> Tool:
        self._tools[tool.name] = tool
        return tool
    def get(self, name: str) -> Tool: return self._tools[name]
    def list_tools(self) -> list[str]: return sorted(self._tools)
    def describe(self, print_it: bool = False) -> str:
        text = json.dumps({"tools": [t.wire() for t in self._tools.values()]}, ensure_ascii=False, indent=1)
        print(text) if print_it else None
        return text
    def invoke_v0(self, name: str, args: dict, ctx: ToolCtx) -> ToolResult:
        """v0：把模型给的参数直接塞进函数（生产事故的标准写法）。"""
        tool, t0 = self._tools[name], time.perf_counter()
        value = tool.fn(args, ctx)  # 不校验、不超时、不截断、不幂等、不查权限
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False); toks = count_tokens(text)
        self.m_tokens.inc(toks); self.m_lat.observe((time.perf_counter() - t0) * 1000.0)
        return ToolResult(True, text=text, tokens=toks, full_tokens=toks)
    def invoke(self, name: str, args: dict, ctx: ToolCtx) -> ToolResult:
        """1) 参数校验 2) 权限 3) 确认 4) 幂等 5) 超时 + 截断：全在注册表层强制。"""
        tool, t0 = self._tools.get(name), time.perf_counter(); self.m_calls.inc()
        try:
            if tool is None: raise ToolDenied(f"未注册的工具 {name}；可用 {self.list_tools()}")
            validate_args(args, tool.params)
            missing = tool.required_perms - ctx.perms  # 权限：模型说了不算，注册表说了算
            if missing: raise ToolDenied(f"{name} 需要权限 {sorted(missing)}，当前只有 {sorted(ctx.perms)}")
            if tool.needs_confirm and ctx.confirm != CONFIRM_TICKET: raise ToolNeedsConfirm(f"{name} 有副作用，缺少人工确认票据")
            key = str(args.get("idempotency_key", "")) if tool.idempotent else ""
            if not key: return self._fit(tool, self._execute(tool, args, ctx), ctx)
            ckey = f"{ctx.tenant}:{name}:{key}"
            with self._key_lock(ckey):  # 同 key 串行化：幂等必须在副作用**之前**判定
                hit = self._idem.get(ckey)
                if hit is not None: return ToolResult(True, text=hit.text, tokens=hit.tokens, ref=hit.ref, full_tokens=hit.full_tokens, deduped=True)
                res = self._fit(tool, self._execute(tool, args, ctx), ctx); self._idem[ckey] = res
                return res
        except ToolArgError:
            self.m_args.inc(); raise
        except ToolTimeout:
            self.m_timeout.inc(); raise
        finally:
            self.m_lat.observe((time.perf_counter() - t0) * 1000.0)
    def _key_lock(self, key: str) -> threading.Lock:
        with self._lock: return self._key_locks.setdefault(key, threading.Lock())
    def _execute(self, tool: Tool, args: dict, ctx: ToolCtx) -> Any:
        """工具级超时 + 受控重试：worker 线程 join 到点就走人，并打开协作取消标志。"""
        last: BaseException | None = None
        for attempt in range(tool.max_retries + 1):
            box: dict[str, Any] = {}; errs: list[BaseException] = []; done = threading.Event()
            def run() -> None:
                try: box["v"] = invoke_checked(tool.name, args, tool.params, lambda **kw: tool.fn(kw, ctx))
                except BaseException as exc: errs.append(exc)  # noqa: BLE001 - 收集后原样抛给调用方
                finally: done.set()
            threading.Thread(target=run, daemon=True).start()
            if not done.wait(tool.timeout_s):
                ctx.cancel.set()  # 协作取消：挂死的工具会在下一个检查点自己收尾
                last = ToolTimeout(f"{tool.name} 超过 timeout_s={tool.timeout_s}s（worker 仍在收尾）")
                if "TIMEOUT" not in tool.retry_on or attempt >= tool.max_retries: raise last
                continue
            if errs: raise errs[0]
            return box["v"]
        raise last  # type: ignore[misc]
    def _fit(self, tool: Tool, value: Any, ctx: ToolCtx) -> ToolResult:
        """返回值截断 + 抽取式摘要 + ref 按需回查：大结果永远不许直接进上下文。"""
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False); full = count_tokens(text)
        if full <= tool.max_result_tokens:
            self.m_tokens.inc(full); return ToolResult(True, text=text, tokens=full, full_tokens=full)
        ref = f"ref://{tool.name}/{hashlib.md5(text.encode('utf-8')).hexdigest()[:8]}"; ctx.refs[ref] = text
        picked: list[str] = []; used = 0
        for ln in text.splitlines():
            if not picked or "ERROR" in ln or "trace=" in ln:
                n = count_tokens(ln) + 1
                if used + n > max(40, tool.max_result_tokens - 60): break
                picked.append(ln); used += n
        body = (f"[工具返回值已截断] 原始 {fmt_bytes(len(text))} / {full} tokens → 抽取式摘要 {len(picked)} 行 / " f"{used} tokens；需要全文请按 ref 回查: {ref}\n" + "\n".join(picked))
        toks = count_tokens(body); self.m_tokens.inc(toks)
        return ToolResult(True, text=body, tokens=toks, full_tokens=full, ref=ref)
def build_registry(idx: BM25Index) -> ToolRegistry:
    r = random.Random(7)
    def body(kb: int, url: str) -> str:  # 造一个 ~kb 大小的"网页响应体"（真实字符）
        n = max(1, (max(1, min(int(kb), 512)) * 1024) // 170)  # 工具侧的物理上限
        return "\n".join(f"{i:06d} {'ERROR upstream timeout' if i % 97 == 0 else 'INFO ok'} GET {url} trace={i:016x} payload={'x' * 60}" for i in range(n))
    def search(a: dict, c: ToolCtx) -> Any:
        res = idx.search(Query(a["query"], a.get("top_k", 5), c.tenant, frozenset({"ga", "public", "secret"}), rerank=a.get("rerank", False)))
        return {"hits": [{"doc_id": h.doc.doc_id, "score": round(h.score, 3), "text": h.text} for h in res.hits], "scanned": res.scanned, "latency_ms": round(res.latency_ms, 2)}
    def calc(a: dict, c: ToolCtx) -> Any:
        x, y, op = a["a"], a["b"], a["op"]
        if op == "div" and y == 0: return {"error": "DIV_ZERO", "message": "除数不能为 0"}  # 业务错误：schema 校验替代不了它
        out = {"add": x + y, "sub": x - y, "mul": x * y, "div": x / y, "pow": x ** y}[op]
        return {"error": "OVERFLOW", "message": "结果溢出双精度范围"} if isinstance(out, float) and (out != out or abs(out) == float("inf")) else {"result": out, "op": op}
    def fetch(a: dict, c: ToolCtx) -> Any:
        time.sleep(0.02 + r.random() * 0.03)  # 网络抖动
        if a.get("mode") == "hang":  # 对端挂死：连上了但永不返回
            end = time.perf_counter() + HANG_S
            while time.perf_counter() < end and not c.cancel.is_set(): time.sleep(0.02)  # 协作取消：20ms 内收尾
        return body(int(a.get("max_kb", BIG_BODY_KB)), a["url"])
    def write(a: dict, c: ToolCtx) -> Any:
        time.sleep(0.05)  # 真实写库延迟：把并发窗口撑开，好暴露重复写
        with DB_LOCK:
            DB[a["table"]].append({**a["row"], "_key": a.get("idempotency_key", "")}); n = len(DB[a["table"]])
        return {"table": a["table"], "written": 1, "rows": n, "row": a["row"]}
    def shell(a: dict, c: ToolCtx) -> Any:
        if a.get("dry_run", False): return {"dry_run": True, "would_run": a["command"]}
        return {"exit_code": 0, "stdout": f"[sandbox 白名单] {a['command'].split()[0]}: 3 行只读输出"}
    specs = [  # (name, fn, params, exec_kwargs, description, when_to_use, when_not_to_use)
        ("search_kb", search, OBJ(["query"], query=T("string", minLength=2, maxLength=120), top_k=T("integer", minimum=1, maximum=20), rerank=T("boolean")),
         dict(timeout_s=0.6, max_retries=1, retry_on=("TIMEOUT",), required_perms=frozenset({"kb:read"}), cost_usd=0.00002, max_result_tokens=300),
         "在**内部知识库**里检索文档片段，返回命中的文档与打分。", "用户问公司内部规范/wiki/历史工单/技术方案，需要引用内部资料时。",
         "需要实时网页内容、需要数值计算、需要写库、需要执行命令时都不要用本工具。"),
        ("calculator", calc, OBJ(["a", "b", "op"], a=T("number"), b=T("number"), op=T("string", enum=["add", "sub", "mul", "div", "pow"])),
         dict(timeout_s=0.3, max_result_tokens=120), "做**精确的四则运算与乘方**（避免模型自己算错）。",
         "加减乘除、乘方、金额与折扣等纯数值计算。", "需要查资料、需要写库、需要抓网页时都不要用本工具。"),
        ("http_fetch", fetch, OBJ(["url"], url=T("string", pattern="https?://[^ ]{3,120}"), max_kb=T("integer", minimum=1, maximum=512), mode=T("string", enum=["ok", "hang"])),
         dict(timeout_s=0.4, max_retries=0, max_result_tokens=400, required_perms=frozenset({"net:read"})),  # 挂死类故障绝不重试
         "对外部 URL 发起 HTTP GET 并返回**响应体**。", "需要实时网页/外部接口数据，且用户给了 http(s) 链接时。",
         "内部资料检索、数值计算、写数据库、执行本地命令都不要用本工具。"),
        ("write_db", write, OBJ(["table", "row"], table=T("string", enum=["orders", "audit"]),
                                row=OBJ(["id", "amount"], id=T("integer", minimum=1), amount=T("number", minimum=0), currency=T("string", enum=["CNY", "USD"])),
                                idempotency_key=T("string", pattern="[a-z0-9-]{4,32}")),
         dict(timeout_s=0.8, max_retries=2, retry_on=("TIMEOUT",), needs_confirm=True, max_result_tokens=200, required_perms=frozenset({"db:write"}), cost_usd=0.00001),
         "往**业务库的 orders / audit 表写入一行**（有副作用，需要人工确认）。", "落库、持久化、保存订单或审计记录，尤其是需要幂等重试的写入。",
         "只是查询、数值计算、抓网页、执行命令时都不要用本工具。"), ("dangerous_shell", shell, OBJ(["command"], command=T("string", pattern="[a-z][a-z0-9 _./-]{0,60}"), dry_run=T("boolean")), dict(timeout_s=1.0, idempotent=False, needs_confirm=True, max_result_tokens=200, required_perms=frozenset({"shell:read"})), "在**受控沙箱里执行单条只读 shell 命令**（高权限，必须人工确认）。", "查看目录、进程、磁盘、日志等运维排查命令。", "写数据库、查资料、抓网页时都不要用本工具。")]
    reg = ToolRegistry()
    for name, fn, params, kw, desc, wtu, wntu in specs:
        reg.register(Tool(name, desc, params, fn, when_to_use=wtu, when_not_to_use=wntu, **kw))
    return reg
DIRTY_CALLS: list[dict] = [  # 同一批非法调用**同序同量**地喂给 v0 与 v1，保证分母一致
    {"why": "参数名幻觉：topk 不是 top_k（被静默忽略）", "tool": "search_kb", "args": {"query": "缓存穿透 治理", "topk": 5}, "fix": {"query": "缓存穿透 治理", "top_k": 5}},
    {"why": "类型幻觉：top_k 写成字符串", "tool": "search_kb", "args": {"query": "限流算法", "top_k": "5"}, "fix": {"query": "限流算法", "top_k": 5}},
    {"why": "类型+枚举幻觉：a 是字符串、op 写成 multiply", "tool": "calculator", "args": {"a": "128", "b": 37, "op": "multiply"}, "fix": {"a": 128, "b": 37, "op": "mul"}},
    {"why": "漏必填 + 负值：row 缺 id、amount 为负 → 脏数据落库", "tool": "write_db", "args": {"table": "orders", "row": {"amount": -5, "currency": "USD"}, "idempotency_key": "lab15-alpha"}, "fix": {"table": "orders", "row": {"id": 1001, "amount": 25.5, "currency": "USD"}, "idempotency_key": "lab15-alpha"}},
    {"why": "嵌套类型幻觉：id 是字符串、amount 是中文 → 脏数据落库", "tool": "write_db", "args": {"table": "orders", "row": {"id": "abc", "amount": "一百"}, "idempotency_key": "lab15-beta"}, "fix": {"table": "orders", "row": {"id": 1002, "amount": 88, "currency": "CNY"}, "idempotency_key": "lab15-beta"}},
    {"why": "pattern + maximum 幻觉：url 不是 http、max_kb 要 4096", "tool": "http_fetch", "args": {"url": "corp-wiki://runbook", "max_kb": 4096}, "fix": {"url": "https://example.com/runbook", "max_kb": 64}}]
def call_tool(reg: ToolRegistry, name: str, args: dict, ctx: ToolCtx) -> tuple[ToolResult | None, str]:
    try: return reg.invoke(name, args, ctx), ""
    except ToolArgError as exc: return None, exc.prompt_line(name)
    except ToolError as exc: return None, f"tool_error({name}): [{exc.code}] {exc}"
def run_tool_failures(reg: ToolRegistry, ctx: ToolCtx) -> dict:
    DB["orders"].clear(); v0 = {"caught": 0, "errors": [], "silent": 0}
    for c in DIRTY_CALLS:
        try: reg.invoke_v0(c["tool"], c["args"], ctx); v0["silent"] += 1
        except BaseException as exc: v0["caught"] += 1; v0["errors"].append(type(exc).__name__)  # 工具体内裸异常：挡下了但无结构化信息
    v0["corrupt"] = sum(1 for x in DB["orders"] if CORRUPT(x)); DB["orders"].clear()
    v1 = {"caught": 0, "repaired": 0, "transcript": ""}
    for c in DIRTY_CALLS:
        res, err = call_tool(reg, c["tool"], c["args"], ctx)
        if res is not None: continue
        v1["caught"] += 1; v1["transcript"] = v1["transcript"] or err
        again, _ = call_tool(reg, c["tool"], c["fix"], ctx)  # 第二次尝试（模型已按结构化错误修正参数）
        v1["repaired"] += 1 if again is not None else 0
    v1["corrupt"] = sum(1 for x in DB["orders"] if CORRUPT(x))
    hang = {"url": "https://slow.example.com/hang", "mode": "hang", "max_kb": 8}
    t0 = time.perf_counter(); reg.invoke_v0("http_fetch", dict(hang), ToolCtx(perms=PARENT_PERMS))
    to = {"v0_ms": (time.perf_counter() - t0) * 1000.0}
    hctx = ToolCtx(perms=PARENT_PERMS); t0 = time.perf_counter()
    try: reg.invoke("http_fetch", dict(hang), hctx)
    except ToolTimeout: pass
    to["v1_ms"] = (time.perf_counter() - t0) * 1000.0; time.sleep(0.03)  # 等被协作取消的 worker 收尾
    big = {"url": "https://big.example.com/report", "max_kb": BIG_BODY_KB}
    r0 = reg.invoke_v0("http_fetch", dict(big), ToolCtx(perms=PARENT_PERMS))
    bctx = ToolCtx(perms=PARENT_PERMS); r1 = reg.invoke("http_fetch", dict(big), bctx); full = bctx.refs[r1.ref]
    ov = {"bytes": len(full), "v0_tokens": r0.tokens, "v1_tokens": r1.tokens, "ref": r1.ref,
          "lookup_tokens": count_tokens("\n".join(x for x in full.splitlines() if "ERROR" in x)[:600])}  # ref 按需回查局部
    wargs = {"table": "orders", "row": {"id": 2001, "amount": 19.9, "currency": "CNY"}, "idempotency_key": "lab15-idem"}
    DB["orders"].clear(); run_concurrently(lambda i: reg.invoke_v0("write_db", dict(wargs), ToolCtx(perms=PARENT_PERMS)), 2, 2)
    dup = {"v0_rows": len(DB["orders"])}; DB["orders"].clear()
    run_concurrently(lambda i: reg.invoke("write_db", dict(wargs), ToolCtx(perms=PARENT_PERMS)), 2, 2)
    dup["v1_rows"] = len(DB["orders"])
    return {"v0": v0, "v1": v1, "timeout": to, "oversize": ov, "dup": dup}
INTENTS: list[tuple[str, str]] = [
    ("帮我查一下公司知识库里关于缓存穿透的治理方案", "search_kb"), ("检索内部文档：多租户会话隔离是怎么做的", "search_kb"), ("从 wiki 里找找限流算法的对比资料", "search_kb"), ("知识库搜索：索引重建的标准流程", "search_kb"), ("我想看内部的灰度发布 checklist", "search_kb"),
    ("找一下历史工单里有没有类似的熔断降级问题", "search_kb"), ("查内部规范里 SLO 是怎么定义的", "search_kb"), ("把公司内部关于背压控制的方案找出来", "search_kb"), ("帮我算一下 128 乘 37 等于多少", "calculator"), ("计算 1440 除以 12 的结果", "calculator"),
    ("199 打八折之后是多少钱，帮我算算", "calculator"), ("算一下 2 的 10 次方", "calculator"), ("帮我把 3200 减去 875", "calculator"), ("求一下 15 加 27 的和", "calculator"), ("四则运算：96 除以 8", "calculator"),
    ("算个折扣：原价 880 打七折后多少", "calculator"), ("帮我抓取这个页面的内容 https://example.com/a", "http_fetch"), ("访问一下 https://api.example.com/health 看看返回什么", "http_fetch"), ("把这个链接的正文下载下来 https://news.example.com/x", "http_fetch"), ("用 GET 请求拉一下 https://example.com/status", "http_fetch"),
    ("看看这个网址现在能不能打开 https://example.com", "http_fetch"), ("抓一下外部接口的数据 https://api.example.com/v1/items", "http_fetch"), ("下载 https://example.com/report.html 的内容", "http_fetch"), ("请求网页 https://example.com/help 并返回响应体", "http_fetch"), ("把这条订单写入 orders 表", "write_db"),
    ("新增一条审计记录到数据库", "write_db"), ("把刚才的结果落库保存", "write_db"), ("在 orders 表里创建一条订单，金额 25.5", "write_db"), ("写入订单的时候要保证重试不会重复", "write_db"), ("把这条记录持久化到 audit 表", "write_db"),
    ("插入一条订单数据，id 是 1001", "write_db"), ("保存这条订单，需要幂等", "write_db"), ("在服务器上执行 ls 看看目录", "dangerous_shell"), ("跑一下 df -h 看磁盘占用", "dangerous_shell"), ("执行命令查看当前进程", "dangerous_shell"), ("用 shell 执行 docker ps", "dangerous_shell"), ("帮我执行这条只读命令 cat /var/log/app.log", "dangerous_shell"), ("命令行执行一下 grep ERROR app.log", "dangerous_shell"), ("执行运维排查命令看看服务状态", "dangerous_shell"), ("用终端执行一条查看日志的命令", "dangerous_shell")]
VAGUE = {"search_kb": "搜索知识库资料。参数: query, top_k", "calculator": "做数学计算。参数: a, b, op", "http_fetch": "获取网页或接口内容。参数: url, max_kb", "write_db": "写入数据到数据库。参数: table, row, idempotency_key", "dangerous_shell": "执行系统命令。参数: command, dry_run"}
def route(intent: str, catalog: dict[str, str], order: list[str]) -> str:
    # 确定性词项路由模拟器：此准确率不代表真实 LLM 工具选择准确率。
    it = _toks(intent); return max(order, key=lambda n: len(it & _toks(catalog[n])))
def run_selection(reg: ToolRegistry) -> dict:
    order = reg.list_tools()
    precise = {n: f"{reg.get(n).description} 适用：{reg.get(n).when_to_use} 不适用：{reg.get(n).when_not_to_use} 参数：{json.dumps(reg.get(n).params, ensure_ascii=False)}" for n in order}
    out = {}
    for tag, catalog in (("v0", VAGUE), ("v1", precise)):
        wrong = [(q, e, g) for q, e in INTENTS if (g := route(q, catalog, order)) != e]
        zero = sum(1 for q, _ in INTENTS if max(len(_toks(q) & _toks(catalog[n])) for n in order) == 0)
        out[tag] = {"acc": (len(INTENTS) - len(wrong)) / len(INTENTS), "wrong": wrong, "zero": zero}
    return out

class LoopGuard:
    def __init__(self, max_iter: int = MAX_ITER, repeat_limit: int = REPEAT_LIMIT, window: int = 4):
        self.max_iter, self.repeat_limit, self.window = max_iter, repeat_limit, window; self.seen: dict[str, int] = {}; self.seq: list[str] = []
    def check(self, tool: str, args: dict, i: int) -> str:
        if i > self.max_iter: return f"max_iterations({self.max_iter})"
        key = f"{tool}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"; self.seen[key] = self.seen.get(key, 0) + 1
        if self.seen[key] >= self.repeat_limit: return f"repeated_call({self.repeat_limit}x)"
        self.seq.append(tool); half = self.window // 2
        if len(self.seq) >= self.window and self.seq[-half:] == self.seq[-self.window: -half]: return f"loop_detected({'->'.join(self.seq[-self.window:])})"
        return ""
LOOP_PLAN = [{"tool": "search_kb", "args": {"query": "缓存穿透 治理", "top_k": 3}}, {"tool": "calculator", "args": {"a": 2, "b": 3, "op": "mul"}}]
def run_loop(reg: ToolRegistry, guard_on: bool, plan: list[dict] | None = None) -> dict:
    ctx, g, calls, tokens, reason = ToolCtx(perms=PARENT_PERMS), LoopGuard(), 0, 0, ""
    plan = plan or LOOP_PLAN
    for i in range(1, MAX_LOOP_CAP + 1):
        step = plan[(i - 1) % len(plan)]
        if guard_on and (stop := g.check(step["tool"], step["args"], i)): reason = stop; break
        calls += 1; tokens += reg.invoke(step["tool"], step["args"], ctx).tokens + 12  # 往返 prompt 开销
    else:
        reason = f"experiment_cap({MAX_LOOP_CAP})"
    return {"calls": calls, "tokens": tokens, "reason": reason}
PARENT_SYS = "你是生产级 agent，负责把用户问题拆解、调用工具、给出结论。"
CHILD_SYS = ("你是「检索子 agent」，在隔离的上下文里只做一件窄任务：从内部知识库找出与任务最相关的 3 条文档，给出结论 + 关键事实。" "硬性约束：1) 只能调用被授予的工具（search_kb / calculator），不能写数据库、不能抓外网、不能执行命令；" "2) 工具返回结构化错误时按 param/expected/got 修正参数，最多重试 1 次；3) token 预算由父 agent 下发，" "超预算立即返回 budget_exceeded；4) 只回传 {summary, key_facts}，不复述检索原文，不输出思考过程。")

@dataclass
class SubAgentSpec:
    """子 agent 的**全部**独立配置：上下文、工具面、预算、超时、模型档位。"""
    name: str; system_prompt: str; tools: frozenset[str]; model: str = "small-8b"
    token_budget: int = 4000; timeout_ms: float = 3000.0; max_facts: int = 3
@dataclass
class SubAgentResult:
    """结构化信封 —— 回传给父级的**只有这个**，不是完整轨迹。"""
    status: str  # ok | failed | cancelled | budget_exceeded
    summary: str = ""; key_facts: list[str] = field(default_factory=list)
    tokens: int = 0; usd: float = 0.0; latency_ms: float = 0.0; tool_calls: int = 0; error: str = ""
    trajectory_tokens: int = 0  # 仅供本 lab 对比，不进信封
    def wire(self) -> str:
        return json.dumps({"status": self.status, "summary": self.summary, "key_facts": self.key_facts, "tokens": self.tokens, "usd": round(self.usd, 6), "latency_ms": round(self.latency_ms, 1), "error": self.error}, ensure_ascii=False)
def child_proxy(reg: ToolRegistry, spec: SubAgentSpec, parent_perms: frozenset[str], inherit: bool = False):
    allowed = frozenset(reg.list_tools()) if inherit else frozenset(spec.tools)  # v0 继承父的全部工具
    perms = parent_perms if inherit else frozenset().union(*[reg.get(n).required_perms for n in allowed]) & parent_perms
    ctx = ToolCtx(perms=perms, confirm=CONFIRM_TICKET)
    def invoke(name: str, args: dict) -> ToolResult:
        if name not in allowed: raise ToolDenied(f"子 agent 无权调用 {name}（允许: {sorted(allowed)}）")
        return reg.invoke(name, args, ctx)
    return invoke
def _child_run(task: str, spec: SubAgentSpec, srv: LLMServer, proxy, cancel: threading.Event, dl: Deadline, long_task: bool) -> SubAgentResult:
    t0 = time.perf_counter(); msgs = [system(spec.system_prompt), user(task)]; pre = count_messages(msgs)
    if pre > spec.token_budget:  # 预算在花钱之前就判，绝不"先花再发现超了"
        return SubAgentResult("budget_exceeded", error=f"预估 {pre} tokens > 预算 {spec.token_budget}")
    if long_task:  # 长任务：每 20ms 检查一次取消标志（协作式取消的唯一正确姿势）
        while time.perf_counter() - t0 < spec.timeout_ms / 1000.0:
            if cancel.is_set() or dl.expired():
                return SubAgentResult("cancelled", error="父 deadline 到期 → 协作取消", latency_ms=(time.perf_counter() - t0) * 1000.0)
            time.sleep(0.02)
        return SubAgentResult("ok", summary="长任务完成", latency_ms=(time.perf_counter() - t0) * 1000.0)
    tok, usd, calls = pre, 0.0, 0
    try:
        with dl.stage("llm") as st:  # 子预算：子 agent 的每次调用都挂在自己的 deadline 上
            reply = srv.call(msgs, model=spec.model, timeout=st.timeout_s(floor_s=0.05), tenant="sub", tag=spec.name)
        tok += reply.usage.in_tokens + reply.usage.out_tokens
        usd += price_of(MODELS[spec.model], reply.usage.in_tokens, reply.usage.out_tokens, reply.usage.cached_tokens)
        msgs.append(assistant(reply.text)); res = proxy("search_kb", {"query": task, "top_k": 3}); calls += 1; tok += res.tokens
        msgs.append(tool_msg(res.text, "search_kb")); hits = json.loads(res.text).get("hits", []) if res.text.startswith("{") else []
        return SubAgentResult("ok", summary=reply.text[:48], tokens=tok, usd=usd, tool_calls=calls, key_facts=[f"{h['doc_id']}:{h['text'][:18]}" for h in hits[: spec.max_facts]], latency_ms=(time.perf_counter() - t0) * 1000.0, trajectory_tokens=count_messages(msgs))
    except BaseException as exc:  # noqa: BLE001 - 子 agent 的失败必须被信封吃掉，不能拖垮父级
        return SubAgentResult("failed", error=f"{type(exc).__name__}: {exc}", tokens=tok, usd=usd, tool_calls=calls, latency_ms=(time.perf_counter() - t0) * 1000.0, trajectory_tokens=count_messages(msgs))
def spawn_subagent(task: str, spec: SubAgentSpec, parent_dl: Deadline, srv: LLMServer, reg: ToolRegistry, parent_perms: frozenset[str], *, inherit_perms: bool = False, long_task: bool = False) -> SubAgentResult:
    """真开线程 + 真给子预算。父级只做一件事：盯自己的 deadline，到期就取消孩子。"""
    t0, cancel = time.perf_counter(), threading.Event(); proxy = child_proxy(reg, spec, parent_perms, inherit_perms)
    budget_ms = spec.timeout_ms if inherit_perms else min(spec.timeout_ms, max(50.0, parent_dl.remaining_ms()))
    dl = Deadline.root(budget_ms, {"llm": budget_ms * 0.6, "tools": budget_ms * 0.4}, name=f"sub:{spec.name}")
    box: dict[str, SubAgentResult] = {}
    th = threading.Thread(target=lambda: box.setdefault("env", _child_run(task, spec, srv, proxy, cancel, dl, long_task)), daemon=True)
    th.start()
    if inherit_perms:
        th.join()  # v0：父只 join，不传播取消 → 被孩子拖到它自己的超时
    else:
        while th.is_alive() and not parent_dl.expired(): th.join(0.01)
        if th.is_alive(): cancel.set(); th.join(0.25)  # 取消传播 + 有界 join：绝不为子 agent 无限等待
    env = box.get("env") or SubAgentResult("cancelled", error="父预算耗尽，子 agent 已取消（有界 join）")
    env.latency_ms = (time.perf_counter() - t0) * 1000.0
    if env.status != "ok": METRICS.counter("subagent_failed_total", "子 agent 取消/失败/超预算").inc()
    return env
def run_subagents(srv: LLMServer, reg: ToolRegistry) -> dict:
    """Task A（3 个独立子任务：串行 vs 并行）+ Task B（简单任务：直接做 vs 开子 agent）+ 隔离/权限/预算/取消。"""
    tasks = ["缓存穿透的治理方案", "限流算法怎么选型", "多租户会话隔离怎么做"]
    t0 = time.perf_counter(); ser_tok = 0
    for q in tasks:
        msgs = [system(PARENT_SYS), user(q)]; reply = srv.call(msgs, model="small-8b", timeout=2.0, tenant="parent", tag="serial")
        ser_tok += count_messages(msgs) + reply.usage.out_tokens + reg.invoke("search_kb", {"query": q, "top_k": 3}, ToolCtx(perms=PARENT_PERMS)).tokens
    a = {"serial_ms": (time.perf_counter() - t0) * 1000.0, "serial_tokens": ser_tok}
    spec = SubAgentSpec("researcher", CHILD_SYS, frozenset({"search_kb"}), timeout_ms=2000.0)
    t0 = time.perf_counter()
    envs = run_concurrently(lambda i: spawn_subagent(tasks[i], spec, Deadline.root(2000.0, {}, name=f"parentA{i}"), srv, reg, PARENT_PERMS), 3, 3)
    a["parallel_ms"] = (time.perf_counter() - t0) * 1000.0
    a["parallel_tokens"] = sum(e.tokens + count_tokens(e.wire()) + 4 for e in envs if isinstance(e, SubAgentResult)); a["status"] = [e.status for e in envs]
    task = "把这条工单分类：登录失败，只影响一个用户，优先级是？"; msgs = [system(PARENT_SYS), user(task)]
    t0 = time.perf_counter(); r0 = srv.call(msgs, model="small-8b", timeout=2.0, tenant="parent", tag="directB")
    b = {"direct_ms": (time.perf_counter() - t0) * 1000.0, "direct_tokens": count_messages(msgs) + r0.usage.out_tokens, "direct_usd": price_of(MODELS["small-8b"], r0.usage.in_tokens, r0.usage.out_tokens, 0)}
    t0 = time.perf_counter()
    env = spawn_subagent(task, SubAgentSpec("classifier", CHILD_SYS, frozenset({"search_kb"}), timeout_ms=1500.0), Deadline.root(1500.0, {}, name="parentB"), srv, reg, PARENT_PERMS)
    pmsgs = [system(PARENT_SYS), user(task), tool_msg(env.wire(), "subagent")]; r1 = srv.call(pmsgs, model="small-8b", timeout=2.0, tenant="parent", tag="integrateB")
    b.update({"sub_ms": (time.perf_counter() - t0) * 1000.0, "status": env.status, "trajectory_tokens": env.trajectory_tokens, "envelope_tokens": count_tokens(env.wire()) + 4, "sub_tokens": env.tokens + count_tokens(env.wire()) + 4 + count_messages(pmsgs) + r1.usage.out_tokens, "sub_usd": env.usd + price_of(MODELS["small-8b"], r1.usage.in_tokens, r1.usage.out_tokens, 0)})
    METRICS.counter("subagent_spawned_total", "子 agent 启动次数").inc(4)
    iso = spawn_subagent("查一下缓存穿透的治理方案", SubAgentSpec("researcher", CHILD_SYS, frozenset({"search_kb", "calculator"}), timeout_ms=1500.0), Deadline.root(1500.0, {}, name="parentIso"), srv, reg, PARENT_PERMS)
    forbidden = [("write_db", {"table": "audit", "row": {"id": 9, "amount": 1}}), ("http_fetch", {"url": "https://evil.example.com/x", "max_kb": 8}), ("dangerous_shell", {"command": "rm -rf /tmp/x"})]
    viol = {}
    for tag, inherit in (("v0", True), ("v1", False)):  # v0 继承父权限，v1 收窄到 spec.tools
        proxy, viol[tag] = child_proxy(reg, spec, PARENT_PERMS, inherit), 0
        for name, args in forbidden:
            try: proxy(name, dict(args)); viol[tag] += 1
            except ToolError: pass
    tight = spawn_subagent("这个子任务不该被允许开跑", SubAgentSpec("tight", CHILD_SYS, frozenset({"search_kb"}), token_budget=80), Deadline.root(800.0, {}, name="parentTight"), srv, reg, PARENT_PERMS)
    ghost = spawn_subagent("模型不存在时的失败隔离", SubAgentSpec("ghost", CHILD_SYS, frozenset({"search_kb"}), model="ghost-model"), Deadline.root(800.0, {}, name="parentGhost"), srv, reg, PARENT_PERMS)
    cancel, cspec = {}, SubAgentSpec("crawler", CHILD_SYS, frozenset({"http_fetch"}), timeout_ms=600.0)
    for tag, inherit in (("v0", True), ("v1", False)):
        t0 = time.perf_counter()
        spawn_subagent("慢慢爬一批页面", cspec, Deadline.root(300.0, {}, name=f"parentC{tag}"), srv, reg, PARENT_PERMS, inherit_perms=inherit, long_task=True)
        cancel[tag] = (time.perf_counter() - t0) * 1000.0
    return {"a": a, "b": b, "env": iso, "env_tokens": count_tokens(iso.wire()) + 4, "violations": viol, "tight": tight, "ghost": ghost, "cancel": cancel}
def main() -> int:
    with lab(LAB_ID, "工具与子 Agent 工程化：如何写工具、如何调用、如何开子 Agent", "如何开启子 agent，如何调用工具，如何编写工具？"):
        os.makedirs(".lab_state", exist_ok=True)
        idx = BM25Index(build_corpus(20_000, seed=7)); reg = build_registry(idx); srv = LLMServer(max_queue=64, max_wait_s=5.0, seed=7)
        phase("1. 复现故障", "(v0 裸工具：无校验/无超时/无截断/无幂等/无权限/无迭代上限)")
        head("交给模型的工具清单（describe() 真实生成的 JSON Schema）")
        sketch = reg.describe(); note(f"5 个工具、{len(sketch)} 字符的 schema 文本会整段进 system prompt："); reg.describe(print_it=True)
        f = run_tool_failures(reg, ToolCtx(perms=PARENT_PERMS, confirm=CONFIRM_TICKET))
        a0, a1, to, ov, dup = f["v0"], f["v1"], f["timeout"], f["oversize"], f["dup"]; n_inj = len(DIRTY_CALLS)
        sel = run_selection(reg); loop0 = run_loop(reg, False); loop1 = run_loop(reg, True)
        loop2 = run_loop(reg, True, [{"tool": "search_kb", "args": {"query": "同一个问题", "top_k": 3}}] * 8)
        print(f"\n{BROKEN} 参数幻觉：{n_inj} 次非法调用 v0 只挡下 {a0['caught']} 次（{n_inj - a0['caught']} 次静默通过、{a0['corrupt']} 行脏数据入库）")
        print(f"{BROKEN} 无超时：工具对端挂死，没有工具级超时时整个 agent 实等 {to['v0_ms']:.0f}ms")
        print(f"{BROKEN} 返回值过大：单次工具返回 {fmt_bytes(ov['bytes'])} = {ov['v0_tokens']} tokens，是上下文窗口 {CTX_WINDOW} 的 {ov['v0_tokens'] / CTX_WINDOW:.1f} 倍")
        print(f"{BROKEN} 重复副作用：同一笔写被两个线程各执行一次 → orders 表 {dup['v0_rows']} 行重复")
        print(f"{BROKEN} 工具选择错误：含糊描述下 40 条意图只选对 {sel['v0']['acc'] * len(INTENTS):.0f} 条（准确率 {sel['v0']['acc']:.3f}）")
        print(f"{BROKEN} 无限循环：脚本化模型反复调同一工具，跑到实验硬上限 {loop0['calls']} 次（{loop0['reason']}），白烧 {loop0['tokens']} tokens")
        phase("2. 观测 / 归因", "(指标 + 对账：故障的根因在哪一层)")
        METRICS.render("工具与子 Agent 指标（v0 阶段真实累计）", include=["tool_", "subagent_"])
        kv("挂死工具实等 / 超大返回值", f"{to['v0_ms']:.0f}ms", f" / {ov['v0_tokens']} tokens（窗口 {CTX_WINDOW}）"); kv("脏行 / 重复写", f"{a0['corrupt']} / {dup['v0_rows']}", " 行")
        if sel["v0"]["wrong"]:
            wq, wexp, wgot = sel["v0"]["wrong"][0]; kv("选错工具", f"{len(sel['v0']['wrong'])}/{len(INTENTS)}", f" 条；例：{wq[:12]}… 应为 {wexp} 实为 {wgot}")
        kv("含糊描述下 0 分意图", f"{sel['v0']['zero']}/{len(INTENTS)}", " 条（所有工具都匹配不上 → 按注册顺序兜底，等于随机）")
        note("归因：六类故障全在**框架层**，不在 prompt 层 —— v0 把模型给的 dict 直接当 kwargs（多余键静默丢弃）、")
        note("工具自己不会返回、原始响应体被当字符串返回、重试没绑幂等、描述只写「做什么」、循环控制权在模型手里。")
        phase("3. 修复", "(v1 五件套：schema 校验 / 工具超时 / 截断+ref / 幂等键 / 权限+确认)")
        kv("①②③④⑤ 五件套", "validate_args+ToolArgError / timeout_s+worker join+协作取消 / 截断+摘要+ref / 同 key 串行化 / required_perms+确认票据")
        note("模型自我修复回路（真实渲染回 prompt 的工具错误消息）："); note(f"  {a1['transcript']}")
        note(f"  → 同一批 {n_inj} 次非法调用：v1 全部拦回，其中 {a1['repaired']} 次带修正参数重试成功；单条错误消息约 " f"{count_messages([system('你是生产级 agent'), user('x'), tool_msg(a1['transcript'], 't')])} tokens")
        for tag, ticket in (("无票据", ""), ("有人工确认", CONFIRM_TICKET)):
            r, err = call_tool(reg, "write_db", {"table": "audit", "row": {"id": 7, "amount": 1.0}, "idempotency_key": "lab15-conf"}, ToolCtx(perms=PARENT_PERMS, confirm=ticket))
            kv(f"  write_db @ {tag}", "OK" if r else err[:52])
        for perms in (frozenset({"shell:read", "db:write"}), frozenset({"db:write"})):
            try:
                reg.invoke("dangerous_shell", {"command": "ls /tmp", "dry_run": True}, ToolCtx(perms=perms, confirm=CONFIRM_TICKET)); kv(f"  权限 perms={sorted(perms)}", "执行了")
            except ToolError as exc: kv(f"  权限 perms={sorted(perms)}", f"[{exc.code}] {exc}"[:60])
        kv("ref 按需回查片段", ov["lookup_tokens"], f" tokens（vs 全量 {ov['v0_tokens']}）")
        note(f"循环三道闸门：无护栏 {loop0['reason']} / 环检测 {loop1['reason']} / 重复调用 {loop2['reason']}")
        print(f"\n{FIX} 五件套上线后：同一批 {n_inj} 次非法调用 {a1['caught']}/{n_inj} 被结构化拦回并修复 {a1['repaired']} 次，脏行 {a1['corrupt']}，" f"工具超时 {to['v1_ms']:.0f}ms，返回值 {ov['v1_tokens']} tokens，重复写 {dup['v1_rows']} 行，选对工具 {sel['v1']['acc'] * len(INTENTS):.0f}/40，循环 {loop1['calls']} 次即停")
        phase("4. 验证", "(同一批脚本化输入：v0 → v1)")
        s = run_subagents(srv, reg); ta, tb, env, viol, cancel = s["a"], s["b"], s["env"], s["violations"], s["cancel"]
        note(f"注意：v0 的 {a0['caught']} 不是「更干净」，是它压根没有校验层 —— 同样 {n_inj} 次注入里它漏过 {n_inj - a0['caught']} 次" f"（静默接受 / 脏数据入库），v1 是 {a1['caught']}/{n_inj} 全拦。两侧分母相同，都是 {n_inj} 次注入。")
        note(f"Task A 3 个独立子任务：串行 {ta['serial_tokens']} tokens / 并行 3 子 agent {ta['parallel_tokens']} tokens，状态={ta['status']}")
        note(f"Task B 简单任务（单位=tokens）：直接做 {tb['direct_tokens']}/{tb['direct_ms']:.0f}ms/${tb['direct_usd']:.6f}；" f"开子 agent {tb['sub_tokens']}/{tb['sub_ms']:.0f}ms/${tb['sub_usd']:.6f}")
        note(f"信封：status={env.status} tool_calls={env.tool_calls} 轨迹 {env.trajectory_tokens} tokens → 父级只增 {s['env_tokens']} tokens；" f"越权成功 v0 {viol['v0']} 次 / v1 {viol['v1']} 次；token_budget=80 → {s['tight'].status}；ghost-model → {s['ghost'].status}（父级没崩）")
        note(f"取消传播：父 deadline=300ms、子 timeout=600ms → v0 实等 {cancel['v0']:.0f}ms；v1 {cancel['v1']:.0f}ms")
        print(f"\n{VERIFY} invalid_tool_args_caught: {a0['caught']} -> {a1['caught']} ({chg(a0['caught'], a1['caught'])})  # 注入 {n_inj} 次 vs {n_inj} 次，分母相同")
        print(f"{VERIFY} invalid_tool_args_caught_rate: {a0['caught'] / n_inj:.3f} -> {a1['caught'] / n_inj:.3f} ({chg(a0['caught'] / n_inj, a1['caught'] / n_inj)})")
        print(f"{VERIFY} corrupted_rows_written: {a0['corrupt']} -> {a1['corrupt']} ({improvement(a0['corrupt'], a1['corrupt'])})")
        print(f"{VERIFY} tool_timeout_ms: {to['v0_ms']:.0f} -> {to['v1_ms']:.0f} ({improvement(to['v0_ms'], to['v1_ms'])})")
        print(f"{VERIFY} context_tokens_from_tools: {ov['v0_tokens']} -> {ov['v1_tokens']} ({improvement(ov['v0_tokens'], ov['v1_tokens'])})")
        print(f"{VERIFY} duplicate_rows_written: {dup['v0_rows']} -> {dup['v1_rows']} ({improvement(dup['v0_rows'], dup['v1_rows'])})")
        print(f"{VERIFY} tool_selection_accuracy: {sel['v0']['acc']:.3f} -> {sel['v1']['acc']:.3f} ({chg(sel['v0']['acc'], sel['v1']['acc'])})")
        print(f"{VERIFY} infinite_loop_tool_calls: {loop0['calls']} -> {loop1['calls']} ({improvement(loop0['calls'], loop1['calls'])})")
        print(f"{VERIFY} loop_context_tokens: {loop0['tokens']} -> {loop1['tokens']} ({improvement(loop0['tokens'], loop1['tokens'])})")
        print(f"{VERIFY} parallel_subagent_latency_ms: {ta['serial_ms']:.0f} -> {ta['parallel_ms']:.0f} ({improvement(ta['serial_ms'], ta['parallel_ms'])})")
        print(f"{VERIFY} subagent_cost_overhead: {tb['direct_tokens']} -> {tb['sub_tokens']} ({chg(tb['direct_tokens'], tb['sub_tokens'])})" f"  # direction: increase-expected （简单任务开子 agent 更贵，这是结论本身，不是缺陷）")
        print(f"{VERIFY} parent_context_delta_tokens: {env.trajectory_tokens} -> {s['env_tokens']} ({improvement(env.trajectory_tokens, s['env_tokens'])})")
        print(f"{VERIFY} child_permission_violations: {viol['v0']} -> {viol['v1']} ({improvement(viol['v0'], viol['v1'])})")
        print(f"{VERIFY} subagent_cancel_latency_ms: {cancel['v0']:.0f} -> {cancel['v1']:.0f} ({improvement(cancel['v0'], cancel['v1'])})")
        head("工程结论：什么时候该开子 agent / 什么时候不该开")
        note("该开（四个理由，缺一条就该重新评估）：")
        note(f"1) 上下文隔离：检索原文与试错轨迹留在子上下文，父级只收信封 —— 实测轨迹 {env.trajectory_tokens} tokens，父级只增 {s['env_tokens']} tokens；")
        note(f"2) 并行探索：3 个独立子任务墙钟 {ta['serial_ms']:.0f}ms → {ta['parallel_ms']:.0f}ms，代价是 tokens {ta['serial_tokens']} → {ta['parallel_tokens']}；")
        note(f"3) 权限收窄：子 agent 只拿到子任务需要的工具面（越权成功 {viol['v0']} → {viol['v1']} 次）；")
        note("4) 独立预算与模型档位：子预算 = min(spec.timeout_ms, 父剩余预算)，超预算/失败都只体现为一个信封。")
        note("不该开（本 lab 的诚实数字）：")
        note(f"1) 简单任务：直接回答 {tb['direct_tokens']} tokens / {tb['direct_ms']:.0f}ms，开子 agent {tb['sub_tokens']} tokens / " f"{tb['sub_ms']:.0f}ms —— 贵 {tb['sub_tokens'] / max(1, tb['direct_tokens']):.1f} 倍、慢 {tb['sub_ms'] / max(1.0, tb['direct_ms']):.1f} 倍；")
        note("2) 强依赖父上下文：把父上下文塞给子 agent，等于把隔离成本又付一遍；")
        note("3) 放大成本与延迟：子 agent 的独立 system prompt + 信封 + 父级整合调用是纯固定开销；")
        note(f"4) 无界等待：父 deadline 到期后必须在有界时间内收手（实测 {cancel['v0']:.0f}ms → {cancel['v1']:.0f}ms）。")
        takeaway("工具的校验/超时/截断/幂等/权限必须由框架强制（模型只产生意图）；子 agent 是用钱和延迟买上下文隔离、并行度和权限收窄 —— 简单任务别开。")
        METRICS.reset()
    return 0
QUESTIONS = [
    "如何调用工具？ -> ToolRegistry.invoke：先 JSON Schema 校验参数，再查权限/确认，超时与截断由框架兜底，失败返回结构化 ToolArgError 让模型修正后重试",
    "如何编写工具？ -> Tool 声明 params(JSON Schema)/timeout_s/retry/idempotent/required_perms/cost_usd/max_result_tokens/needs_confirm + when_to_use / when_not_to_use",
    "如何开启子 agent？ -> SubAgentSpec(独立 system prompt / 收窄工具集 / 独立预算 / 独立超时 / 独立模型档位) + threading.Thread + Deadline 子预算，回传结构化信封", "什么时候不该开子 agent？ -> 简单任务、强依赖父上下文、只需一次检索的场景：实测贵约 5 倍、慢约 2 倍", "六种典型工具故障？ -> 参数幻觉 / 无超时 / 返回值过大 / 重复副作用 / 工具选错 / 无限循环，都由框架层解决而不是 prompt 层", "子 agent 权限为什么必须收窄？ -> 继承父权限时越权写入/抓外网/执行命令全部成功，收窄后一律被注册表拒绝", ]

if __name__ == "__main__":
    sys.exit(main())