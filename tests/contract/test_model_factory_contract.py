"""Contrato das portas ``IModelFactory``/``IEmbedderFactory`` (F2-02): mesma suíte para o fake e
para o ``ProviderRegistry`` (com spec de ``tests/fakes/providers.py``, sem SDK nem rede).

Cada implementação entrega, pela fixture, uma config válida e uma que ela recusa.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from src.domain.entities.model_config import ModelConfig
from src.domain.ports import IEmbedderFactory, IModelFactory, InvalidModelConfigError
from src.infrastructure.providers import ClassSpec, DestinationPolicy, ProviderRegistry, ProviderSpec
from tests.fakes import FakeEmbedderFactory, FakeModelFactory
from tests.fakes.providers import CHAT_PATH, EMBEDDER_PATH


@dataclass(frozen=True)
class Implementation:
    models: IModelFactory
    embedders: IEmbedderFactory
    valid: ModelConfig
    refused: ModelConfig


def _fake() -> Implementation:
    return Implementation(
        models=FakeModelFactory(invalid_models={"recusado"}),
        embedders=FakeEmbedderFactory(invalid_models={"recusado"}),
        valid=ModelConfig("openai", "modelo-x"),
        refused=ModelConfig("openai", "recusado"),
    )


def _registry() -> Implementation:
    spec = ProviderSpec(
        id="contrato",
        sdk_package="contrato-sdk",
        chat=ClassSpec(class_path=CHAT_PATH, base_url_kwarg="base_url"),
        embedder=ClassSpec(class_path=EMBEDDER_PATH, base_url_kwarg="base_url"),
    )
    registry = ProviderRegistry([spec], policy=DestinationPolicy(resolver=lambda host: ["10.0.0.1"]))
    return Implementation(
        models=registry,
        embedders=registry,
        valid=ModelConfig("contrato", "modelo-x"),
        refused=ModelConfig("contrato", "modelo-x", base_url="https://fora.example"),
    )


@pytest.fixture(params=[_fake, _registry], ids=["fake", "registry"])
def impl(request: pytest.FixtureRequest) -> Implementation:
    factory: Callable[[], Implementation] = request.param
    return factory()


def test_modelo_criado_tem_o_id_pedido(impl: Implementation):
    assert impl.models.create_model(impl.valid).id == impl.valid.model_id


def test_embedder_devolve_lista_de_floats(impl: Implementation):
    vector = impl.embedders.create_embedder(impl.valid).get_embedding("x")

    assert isinstance(vector, list) and vector and all(isinstance(v, float) for v in vector)


@pytest.mark.parametrize("kind", ["model", "embedder"])
def test_config_recusada_levanta_invalid_model_config_error_sem_causa(impl: Implementation, kind: str):
    create = impl.models.create_model if kind == "model" else impl.embedders.create_embedder

    with pytest.raises(InvalidModelConfigError) as caught:
        create(impl.refused)

    assert caught.value.__cause__ is None
    assert str(caught.value)
