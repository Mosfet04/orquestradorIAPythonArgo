"""Implementações das portas ``IModelFactory``/``IEmbedderFactory`` para a suíte de contrato (F2-02).

O fake (``tests/fakes/models.py``) e o ``ProviderRegistry`` real com uma spec de
``tests/fakes/providers.py`` (sem SDK nem rede). Cada implementação entrega uma config válida e
uma que ela recusa.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from src.domain.entities.model_config import ModelConfig
from src.domain.ports import IEmbedderFactory, IModelFactory
from src.infrastructure.providers import ClassSpec, DestinationPolicy, ProviderRegistry, ProviderSpec
from tests.fakes.models import FakeEmbedderFactory, FakeModelFactory
from tests.fakes.providers import CHAT_PATH, EMBEDDER_PATH


@dataclass(frozen=True)
class ModelFactoryImplementation:
    models: IModelFactory
    embedders: IEmbedderFactory
    valid: ModelConfig
    refused: ModelConfig


def fake_model_factories() -> ModelFactoryImplementation:
    return ModelFactoryImplementation(
        models=FakeModelFactory(invalid_models={"recusado"}),
        embedders=FakeEmbedderFactory(invalid_models={"recusado"}),
        valid=ModelConfig("openai", "modelo-x"),
        refused=ModelConfig("openai", "recusado"),
    )


def registry_model_factories() -> ModelFactoryImplementation:
    spec = ProviderSpec(
        id="contrato",
        sdk_package="contrato-sdk",
        chat=ClassSpec(class_path=CHAT_PATH, base_url_kwarg="base_url"),
        embedder=ClassSpec(class_path=EMBEDDER_PATH, base_url_kwarg="base_url"),
    )
    registry = ProviderRegistry([spec], policy=DestinationPolicy(resolver=lambda host: ["10.0.0.1"]))
    return ModelFactoryImplementation(
        models=registry,
        embedders=registry,
        valid=ModelConfig("contrato", "modelo-x"),
        refused=ModelConfig("contrato", "modelo-x", base_url="https://fora.example"),
    )


MODEL_FACTORY_IMPLEMENTATIONS: dict[str, Callable[[], ModelFactoryImplementation]] = {
    "fake": fake_model_factories,
    "registry": registry_model_factories,
}
"""id do caso -> construtor: a mesma lista para todo teste de contrato dessas portas."""
