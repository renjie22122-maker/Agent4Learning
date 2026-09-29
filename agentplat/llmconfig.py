"""LLM 后端配置：支持真实 API 与内置模拟器，运行时可切换。

安全约定（很重要）
------------------
* **API Key 只从这两处读取**，顺序为：环境变量 → 本地配置文件。
* 本地配置文件是 ``.agentlab_llm.json``，已写进 ``.gitignore``，**不会被提交**。
* 界面上**永远不回显完整 key**，只显示 ``sk-****abcd`` 形式的前后缀。
* 日志/错误信息里做脱敏处理，避免 key 泄漏到 traceback 里。

支持任意 **OpenAI 兼容** 端点 —— 一套代码覆盖绝大部分厂商：

    DeepSeek    https://api.deepseek.com            deepseek-chat
    OpenAI      https://api.openai.com/v1           gpt-4o-mini
    通义千问     https://dashscope.aliyuncs.com/compatible-mode/v1   qwen-plus
    智谱 GLM    https://open.bigmodel.cn/api/paas/v4   glm-4-flash
    Moonshot    https://api.moonshot.cn/v1          moonshot-v1-8k
    本地 Ollama http://127.0.0.1:11434/v1           qwen2.5:7b
    本地 vLLM   http://127.0.0.1:8000/v1            任意已加载模型
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: 本地配置文件（已 gitignore）。放在仓库根目录。
CONFIG_PATH = Path(__file__).resolve().parent.parent / ".agentlab_llm.json"


#: 常见厂商预设：界面上一点就填好 base_url，降低配置门槛
PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "small": "deepseek-chat",
        "mid": "deepseek-chat",
        "large": "deepseek-reasoner",
        "note": "国内可直连，价格便宜；deepseek-reasoner 是推理模型（更准更慢）",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "small": "gpt-4o-mini",
        "mid": "gpt-4o",
        "large": "gpt-4o",
        "note": "需要能访问 api.openai.com（本机探测不通时请用代理或其他厂商）",
    },
    "dashscope": {
        "label": "通义千问（阿里云）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "small": "qwen-turbo",
        "mid": "qwen-plus",
        "large": "qwen-max",
        "note": "Compatible Mode，三档模型齐全，适合演示大小模型路由",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "small": "glm-4-flash",
        "mid": "glm-4-air",
        "large": "glm-4-plus",
        "note": "glm-4-flash 有免费额度，适合先跑通",
    },
    "moonshot": {
        "label": "Moonshot（Kimi）",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "small": "moonshot-v1-8k",
        "mid": "moonshot-v1-32k",
        "large": "moonshot-v1-128k",
        "note": "长上下文见长",
    },
    "ollama": {
        "label": "本地 Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "small": "qwen2.5:3b",
        "mid": "qwen2.5:7b",
        "large": "qwen2.5:14b",
        "note": "完全本地、不需要 key；需要先 `ollama serve` 并 pull 对应模型",
    },
    "vllm": {
        "label": "本地 vLLM / 自建",
        "base_url": "http://127.0.0.1:8000/v1",
        "model": "Qwen2.5-7B-Instruct",
        "small": "Qwen2.5-7B-Instruct",
        "mid": "Qwen2.5-7B-Instruct",
        "large": "Qwen2.5-7B-Instruct",
        "note": "自建推理服务，OpenAI 兼容",
    },
    "custom": {
        "label": "自定义（任意 OpenAI 兼容端点）",
        "base_url": "",
        "model": "",
        "small": "",
        "mid": "",
        "large": "",
        "note": "填 base_url + 模型名即可，路径会自动补 /chat/completions",
    },
}


@dataclass
class ModelRouting:
    """把平台的三个档位（small/mid/large）映射到真实模型名。

    为什么要映射而不是直接用真实模型名：**路由、降级链、预算闸门这些逻辑
    依赖"档位"这个抽象**。真实模型名会随厂商变，档位不会。所以模型调度层
    永远只认档位，具体用哪个模型由这张表决定 —— 换厂商只改这张表。
    """

    small: str = ""
    mid: str = ""
    large: str = ""


@dataclass
class LLMConfig:
    """一次 LLM 后端的完整配置。"""

    #: 配置结构版本。**改默认值时必须一起想清楚迁移** ——
    #: 实测踩过：把 `max_tokens` 默认值从 512 提到 8192 后，
    #: 已经落盘的 `.agentlab_llm.json` 里仍写着 `512`，
    #: 于是"改好的默认值"被旧配置整个吃掉，agent 继续反复撞输出上限，
    #: 而代码里看默认值一切正常 —— 这种问题极难从代码上发现。
    #:
    #: 规则：**会随版本演进的技术参数**（max_tokens/json_mode/reasoning_effort 等）
    #: 在版本变化时重置为新默认；**用户的连接与偏好**（key/base_url/模型映射/
    #: temperature）永远保留。
    CONFIG_VERSION = 2

    #: 版本升级时需要重置为新默认的字段（技术参数，不是用户偏好）
    MIGRATED_FIELDS = ("max_tokens", "json_mode", "reasoning_effort")

    _version: int = 0  # 落盘文件里的版本；0/缺失 = 老配置

    provider: str = "mock"  # mock | real
    preset: str = "deepseek"
    base_url: str = ""
    api_key: str = ""
    model: str = ""  # 单模型模式下的模型名
    routing: ModelRouting = field(default_factory=ModelRouting)
    temperature: float = 0.3
    stream_tools: bool = True
    context_window: int = 0  # 0 resolves known official models, otherwise 64K fallback.

    def resolved_context_window(self, model=None):
        from .model_capacity import resolve
        return resolve(self, model)[0]
    #: 输出上限。**编码 agent 需要给足**：
    #: 实测 2048 时，模型用 write_file 写一个稍大的文件，JSON 参数会在
    #: 字符串中间被截断（`{"path":"x.py","content":"...`），解析必然失败，
    #: agent 就会陷入"重试 → 再截断"的循环。4096 仍会偶发，8192 基本够用；
    #: 配合 append_file 与"截断抢救"，长文件也能稳定写完。
    max_tokens: int = 8192
    verification_token_budget: int = 0  # 0: no per-review cap; shared budget still applies.
    review_profile: str = 'strict'  # balanced relaxes document-only work, never code/security.
    delegation_policy: str = 'manual'  # adaptive requires measured evidence; see delegation.py.
    delegation_evidence_path: str = ''  # trusted host report, never supplied by model tool args.
    subagent_max_depth: int = 2
    subagent_max_parallel: int = 3
    subagent_max_tasks: int = 24
    subagent_total_tokens: int = 0
    subagent_default_tokens: int = 0
    memory_enabled: bool = True
    timeout_s: float = 30.0
    #: 推理等级（OpenAI 兼容端点的 reasoning_effort 参数）。
    #: 空字符串 = 不发送该字段（普通对话模型不认这个参数，发了可能报 400）。
    #: 取值一般是 "low" / "medium" / "high"（不同厂商支持度不同，
    #: DeepSeek 用 deepseek-reasoner 模型本身表达"高推理"，不一定吃这个字段）。
    reasoning_effort: str = ""
    #: 是否用 OpenAI 兼容的 ``response_format={"type":"json_object"}`` 强制结构化输出。
    #:
    #: 为什么需要它：实测某模型会把**思考过程**当正文吐出来
    #: （"We need answer in JSON. User asks: ... Need produce JSON output..."），
    #: 之后再接一段 JSON。靠 system prompt 里写"输出必须是 JSON"是不够可靠的，
    #: 用 API 参数约束才是工程做法。
    #: 若某个厂商不支持这个字段（会返回 400），关掉即可。
    json_mode: bool = True

    # ---- 容量参数（决定并发闸门与限流，不再用模拟器的假数值）----
    max_parallel: int = 4
    price_in_per_m: float = 1.0  # $/1M input tokens
    price_out_per_m: float = 2.0  # $/1M output tokens
    offline_mock_fallback: bool = True  # 真实调用失败时是否退回模拟器

    # ------------------------------------------------------------------
    @property
    def is_real(self) -> bool:
        return self.provider == "real" and bool(self.base_url) and bool(self.model_or("mid"))

    def model_or(self, tier: str) -> str:
        """取某档位的真实模型名。

        回退链：本档位 → mid → 单模型字段。
        **为什么要有回退**：用户往往只填一个模型（比如只买得起 mid）。
        如果 small 档留空就直接返回空字符串，实验会以 "model name is empty"
        的 400 报错失败 —— 而用户的意图明明是"三档都用同一个模型"。
        回退让"只配一个模型"成为可用配置，而不是一个陷阱。
        """
        spec = getattr(self.routing, tier, "") or ""
        if spec:
            return spec
        if tier != "mid":
            mid = getattr(self.routing, "mid", "") or ""
            if mid:
                return mid
        return self.model

    def tier_map(self) -> dict[str, str]:
        """内部档位 → 真实模型名。三档都走同一套回退逻辑。"""
        return {
            "small-8b": self.model_or("small"),
            "mid-32b": self.model_or("mid"),
            "large-400b": self.model_or("large"),
        }

    def masked_key(self) -> str:
        k = self.api_key
        if not k:
            return "（未设置）"
        if len(k) <= 8:
            return "****"
        return f"{k[:4]}****{k[-4:]}（长度 {len(k)}）"

    def chat_url(self) -> str:
        """拼出 /chat/completions 的完整 URL。

        用户经常只填 ``https://api.deepseek.com`` 或带上 ``/v1`` —— 两种都要能跑，
        所以这里做归一化，而不是要求用户记住各家路径差异。
        """
        base = (self.base_url or "").strip().rstrip("/")
        if not base:
            return ""
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def apply_preset(self, name: str) -> None:
        p = PRESETS.get(name)
        if not p:
            return
        self.preset = name
        self.base_url = p["base_url"]
        self.model = p["model"]
        self.routing = ModelRouting(p["small"], p["mid"], p["large"])

    # ---- 持久化 -------------------------------------------------------
    def save(self) -> None:
        data = asdict(self)
        data.pop("_version", None)
        data["_version"] = self.CONFIG_VERSION
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls) -> "LLMConfig":
        cfg = cls()
        # ① 环境变量优先（CI/容器场景不落盘）
        env_map = {
            "AGENTLAB_LLM_KEY": "api_key",
            "AGENTLAB_LLM_BASE": "base_url",
            "AGENTLAB_LLM_MODEL": "model",
        }
        for env, attr in env_map.items():
            v = os.environ.get(env)
            if v:
                setattr(cfg, attr, v)
        if cfg.api_key and cfg.base_url:
            cfg.provider = "real"
            if not cfg.model:
                for probe in ("OPENAI_MODEL", "DEEPSEEK_MODEL"):
                    if os.environ.get(probe):
                        cfg.model = os.environ[probe]
                        break

        # 常见厂商的标准环境变量也要认，降低上手成本
        for key_env, base_env, model in (
            ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "deepseek-chat"),
            ("OPENAI_API_KEY", "OPENAI_BASE_URL", "gpt-4o-mini"),
            ("MOONSHOT_API_KEY", "MOONSHOT_BASE_URL", "moonshot-v1-8k"),
            ("DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL", "qwen-plus"),
            ("ZHIPU_API_KEY", "ZHIPU_BASE_URL", "glm-4-flash"),
        ):
            k = os.environ.get(key_env)
            if k and not cfg.api_key:
                cfg.api_key = k
                cfg.provider = "real"
                cfg.preset = key_env.split("_")[0].lower()
                cfg.base_url = os.environ.get(base_env) or cfg.base_url
                if not cfg.model:
                    cfg.model = model
                if not cfg.routing.mid:
                    cfg.apply_preset(cfg.preset)
                break

        # ② 本地配置文件（界面保存的）
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                file_ver = int(data.pop("_version", 0) or 0)
                cfg._version = file_ver
                # 版本迁移：老配置里那些"当年的默认值"不能压住新默认值。
                # 只重置技术参数，用户填的连接信息与偏好一律保留。
                if file_ver < cls.CONFIG_VERSION:
                    dropped = [f for f in cls.MIGRATED_FIELDS if f in data]
                    for f in dropped:
                        data.pop(f, None)
                    cfg._migrated_fields = dropped
                for k, v in data.items():
                    if k == "routing" and isinstance(v, dict):
                        cfg.routing = ModelRouting(**v)
                    elif hasattr(cfg, k):
                        setattr(cfg, k, v)
            except Exception:  # noqa: BLE001 - 配置坏了不能让服务起不来
                pass

        if not cfg.routing.mid and cfg.model:
            cfg.routing = ModelRouting(cfg.model, cfg.model, cfg.model)
        return cfg

    def redacted(self) -> dict:
        """给界面用的安全视图：**绝不包含明文 key**。"""
        d = asdict(self)
        d["api_key"] = self.masked_key()
        d["has_key"] = bool(self.api_key)
        d["chat_url"] = self.chat_url()
        d["is_real"] = self.is_real
        return d
