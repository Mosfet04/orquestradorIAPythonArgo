"""QA F2-02: propriedades extras do contrato das portas de modelo/embedder, para o fake e o registry.

Complementa ``test_model_factory_contract.py`` com as mesmas implementações
(``tests/fakes/model_factories.py``): instâncias independentes, ``ModelConfig`` intocado, embedder
determinístico e recusa que não ecoa o destino nem a referência de chave (o fake e o real têm de se
comportar igual).
"""

from __future__ import annotations

import pytest

from src.domain.entities.model_config import ModelConfig
from src.domain.ports import InvalidModelConfigError
from tests.fakes.model_factories import MODEL_FACTORY_IMPLEMENTATIONS, ModelFactoryImplementation

EVIL_HOST = "coletor.evil.invalid"
LEAK_REF = "env:QA_F202_LEAK_API_KEY"


@pytest.fixture(params=list(MODEL_FACTORY_IMPLEMENTATIONS.values()), ids=list(MODEL_FACTORY_IMPLEMENTATIONS))
def impl(request: pytest.FixtureRequest) -> ModelFactoryImplementation:
    factory = request.param
    return factory()


def test_duas_criacoes_dao_instancias_independentes(impl: ModelFactoryImplementation):
    first, second = impl.models.create_model(impl.valid), impl.models.create_model(impl.valid)

    assert first is not second and first.id == second.id == impl.valid.model_id
    assert impl.embedders.create_embedder(impl.valid) is not impl.embedders.create_embedder(impl.valid)


def test_criar_nao_altera_o_model_config(impl: ModelFactoryImplementation):
    before = (impl.valid.provider, impl.valid.model_id, dict(impl.valid.params), impl.valid.base_url)

    impl.models.create_model(impl.valid)
    impl.embedders.create_embedder(impl.valid)

    assert before == (impl.valid.provider, impl.valid.model_id, dict(impl.valid.params), impl.valid.base_url)


def test_embedder_e_deterministico_para_o_mesmo_texto(impl: ModelFactoryImplementation):
    embedder = impl.embedders.create_embedder(impl.valid)

    assert embedder.get_embedding("mesmo texto") == embedder.get_embedding("mesmo texto")


@pytest.mark.parametrize("kind", ["model", "embedder"])
def test_recusa_nao_ecoa_destino_nem_referencia_de_chave(impl: ModelFactoryImplementation, kind: str):
    refused = ModelConfig(
        impl.refused.provider, impl.refused.model_id, base_url=f"https://{EVIL_HOST}/v1", api_key_ref=LEAK_REF
    )
    create = impl.models.create_model if kind == "model" else impl.embedders.create_embedder

    with pytest.raises(InvalidModelConfigError) as caught:
        create(refused)

    assert EVIL_HOST not in str(caught.value) and "QA_F202_LEAK" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
