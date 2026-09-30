"""Transport contract and provider-specific construction, outside orchestration."""
from dataclasses import replace
from typing import Protocol, Sequence, runtime_checkable
from urllib.parse import urlsplit
from agentlab.providers import ChatMessage, Usage


@runtime_checkable
class ModelClient(Protocol):
    def complete(self, model: str, messages: Sequence[ChatMessage], timeout_s: float) -> tuple[str, Usage]: ...
    def complete_with_tools(self, model: str, messages: Sequence[ChatMessage], tools: list[dict], timeout_s: float) -> tuple[str, list[dict], Usage]: ...


_FACTORIES = {}


def register_transport(name, factory):
    """Trusted host extension only; configuration never imports Python modules."""
    if not name or name in ('openai_chat','openai_responses','anthropic','gemini') or name in _FACTORIES or not callable(factory):
        raise ValueError('Invalid or duplicate model transport')
    _FACTORIES[name] = factory


def create_client(cfg):
    transport = getattr(cfg, 'transport', 'openai_chat')
    if transport == 'openai_chat':
        from .llm import OpenAIChatClient
        return OpenAIChatClient(cfg)
    from .native_protocols import NATIVE
    if transport in NATIVE:
        from .native_client import NativeClient
        return NativeClient(cfg)
    if transport not in _FACTORIES:
        raise ValueError(f'Unsupported model transport: {transport}; no fallback')
    client = _FACTORIES[transport](cfg)
    if not isinstance(client, ModelClient):
        raise TypeError('Model transport must implement complete and complete_with_tools')
    return client


def review_config(cfg):
    options = {'memory_enabled':False}
    if urlsplit(cfg.base_url).hostname == 'api.deepseek.com':
        options['reasoning_effort'] = 'low'
    return replace(cfg, **options)


def summary_client(client, cfg):
    from .llm import OpenAIChatClient
    from .native_client import NativeClient
    if not isinstance(client, (OpenAIChatClient, NativeClient)) and getattr(cfg, 'transport', 'openai_chat') not in _FACTORIES:
        return client
    other = create_client(replace(cfg, json_mode=False))
    other.cancel_event = getattr(client, 'cancel_event', None)
    return other
