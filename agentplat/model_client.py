"""Transport contract and provider-specific construction, outside orchestration."""
from dataclasses import replace
from typing import Protocol, Sequence, runtime_checkable
from urllib.parse import urlsplit
from agentlab.providers import ChatMessage, Usage


@runtime_checkable
class ModelClient(Protocol):
    def complete(self, model: str, messages: Sequence[ChatMessage], timeout_s: float) -> tuple[str, Usage]: ...
    def complete_with_tools(self, model: str, messages: Sequence[ChatMessage], tools: list[dict], timeout_s: float) -> tuple[str, list[dict], Usage]: ...


def create_client(cfg):
    # Compatibility transport is explicit; no claim of native vendor adapters.
    from .llm import OpenAIChatClient
    return OpenAIChatClient(cfg)


def review_config(cfg):
    options = {'memory_enabled':False}
    if urlsplit(cfg.base_url).hostname == 'api.deepseek.com':
        options['reasoning_effort'] = 'low'
    return replace(cfg, **options)


def summary_client(client, cfg):
    from .llm import OpenAIChatClient
    if not isinstance(client, OpenAIChatClient): return client
    other = create_client(replace(cfg, json_mode=False))
    other.cancel_event = getattr(client, 'cancel_event', None)
    return other
