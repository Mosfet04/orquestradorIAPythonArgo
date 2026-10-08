"""
Ports do domínio — interfaces para adaptadores externos.

Seguindo a Arquitetura Hexagonal (Ports & Adapters), estas interfaces
definem contratos que a camada de infraestrutura deve implementar.
"""

from typing import Any

from src.domain.ports.agent_builder_port import IAgentBuilder
from src.domain.ports.embedder_factory_port import IEmbedderFactory
from src.domain.ports.logger_port import ILogger
from src.domain.ports.model_factory_port import IModelFactory
from src.domain.ports.tool_factory_port import IToolFactory

# Type alias para desacoplar o domínio do framework agno
AgentInstance = Any

__all__ = [
    "AgentInstance",
    "IAgentBuilder",
    "IEmbedderFactory",
    "ILogger",
    "IModelFactory",
    "IToolFactory",
]
