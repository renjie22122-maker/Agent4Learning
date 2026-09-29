"""Trusted runtime providers; host retains metering, workspace and cancellation."""
from dataclasses import dataclass
from typing import Callable

REQUIRED=frozenset({'host_tools','metered_client','cancellation','session_events','workspace_scope'})

@dataclass(frozen=True)
class Provider:
    name: str
    create: Callable
    capabilities: frozenset[str]

class ProviderRegistry:
    def __init__(self): self._providers={}
    def register(self, provider):
        if not provider.name or provider.name in self._providers: raise ValueError('Duplicate/empty provider')
        if not REQUIRED <= provider.capabilities: raise ValueError('Provider cannot enforce required host contracts')
        self._providers[provider.name]=provider
    def get(self,name):
        if name not in self._providers: raise ValueError(f'Unknown subagent provider: {name}')
        return self._providers[name]
    def names(self):return sorted(self._providers)

def default_registry():
    from .loop import CodingAgent
    registry=ProviderRegistry();registry.register(Provider('local',CodingAgent,REQUIRED))
    return registry
