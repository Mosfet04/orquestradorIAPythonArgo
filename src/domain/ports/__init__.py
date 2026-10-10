"""
Ports do domínio — interfaces para adaptadores externos.

Seguindo a Arquitetura Hexagonal (Ports & Adapters), estas interfaces
definem contratos que a camada de infraestrutura deve implementar.
"""

from src.domain.ports.agent_runtime_port import AgentHandle, AgentRuntime, TeamHandle
from src.domain.ports.embedder_factory_port import IEmbedderFactory, TextEmbedder
from src.domain.ports.logger_port import ILogger
from src.domain.ports.model_factory_port import ChatModel, IModelFactory, InvalidModelConfigError
from src.domain.ports.tool_factory_port import IToolFactory

__all__ = [
    "AgentHandle",
    "AgentRuntime",
    "ChatModel",
    "IEmbedderFactory",
    "ILogger",
    "IModelFactory",
    "IToolFactory",
    "InvalidModelConfigError",
    "TeamHandle",
    "TextEmbedder",
]
