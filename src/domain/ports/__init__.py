"""
Ports do domínio — interfaces para adaptadores externos.

Seguindo a Arquitetura Hexagonal (Ports & Adapters), estas interfaces
definem contratos que a camada de infraestrutura deve implementar.
"""
from src.domain.ports.logger_port import ILogger
from src.domain.ports.model_factory_port import IModelFactory
from src.domain.ports.embedder_factory_port import IEmbedderFactory
from src.domain.ports.tool_factory_port import IToolFactory
from src.domain.ports.agent_builder_port import IAgentBuilder

# Type alias para desacoplar o domínio do framework agno
from typing import Any
AgentInstance = Any

__all__ = [
    "ILogger",
    "IModelFactory",
    "IEmbedderFactory",
    "IToolFactory",
    "IAgentBuilder",
    "AgentInstance",
]
