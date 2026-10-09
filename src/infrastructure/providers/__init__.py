"""Providers de modelo e embedder: registry, specs e built-ins (F2-02)."""

from src.infrastructure.providers.registry import (
    ClassSpec,
    DestinationPolicy,
    OperatorEnv,
    ProviderRegistry,
    ProviderSpec,
)

__all__ = ["ClassSpec", "DestinationPolicy", "OperatorEnv", "ProviderRegistry", "ProviderSpec"]
