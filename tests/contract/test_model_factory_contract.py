"""Contrato das portas ``IModelFactory``/``IEmbedderFactory`` (F2-02): mesma suíte para o fake e
para o ``ProviderRegistry`` (com spec de ``tests/fakes/providers.py``, sem SDK nem rede).

As implementações vêm de ``tests/fakes/model_factories.py``: cada uma entrega uma config válida e
uma que ela recusa.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from src.domain.ports import InvalidModelConfigError
from tests.fakes.model_factories import MODEL_FACTORY_IMPLEMENTATIONS, ModelFactoryImplementation


@pytest.fixture(params=list(MODEL_FACTORY_IMPLEMENTATIONS.values()), ids=list(MODEL_FACTORY_IMPLEMENTATIONS))
def impl(request: pytest.FixtureRequest) -> ModelFactoryImplementation:
    factory: Callable[[], ModelFactoryImplementation] = request.param
    return factory()


def test_modelo_criado_tem_o_id_pedido(impl: ModelFactoryImplementation):
    assert impl.models.create_model(impl.valid).id == impl.valid.model_id


def test_embedder_devolve_lista_de_floats(impl: ModelFactoryImplementation):
    vector = impl.embedders.create_embedder(impl.valid).get_embedding("x")

    assert isinstance(vector, list) and vector and all(isinstance(v, float) for v in vector)


@pytest.mark.parametrize("kind", ["model", "embedder"])
def test_config_recusada_levanta_invalid_model_config_error_sem_causa(impl: ModelFactoryImplementation, kind: str):
    create = impl.models.create_model if kind == "model" else impl.embedders.create_embedder

    with pytest.raises(InvalidModelConfigError) as caught:
        create(impl.refused)

    assert caught.value.__cause__ is None
    assert str(caught.value)
