"""F2-11 (a): o embedder montado tem a dimensão real do modelo quando ``model_params.dimensions`` não vem.

Achado O2 (agno 2.5.8): o ``OllamaEmbedder`` assume ``dimensions=4096`` e transforma em ``[]`` o vetor
de outro tamanho. O ``rag_config`` legado (``ollama`` + ``nomic-embed-text:latest``, 768 posições)
não passa ``dimensions``. Aqui o ``OllamaEmbedder`` é o real do agno, com o cliente do SDK trocado por
``FakeOllamaClient`` (sem rede): a sonda descobre 768 uma vez por config e o embedder passa a devolver
o vetor inteiro. ``dimensions`` explícito continua valendo e dispensa a sonda.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest
from agno.knowledge.embedder.azure_openai import AzureOpenAIEmbedder
from agno.knowledge.embedder.base import Embedder
from agno.knowledge.embedder.ollama import OllamaEmbedder

from src.domain.entities.model_config import ModelConfig
from src.domain.entities.rag_config import RagConfig
from src.domain.ports.embedder_factory_port import IEmbedderFactory
from src.infrastructure.runtime.agno.embedder_dimensions import (
    DimensionResolvingEmbedderFactory,
    EmbedderProbeError,
)
from src.infrastructure.runtime.agno.rag_vector_db import EmbeddingDimensionError
from tests.fakes import RecordingLogger
from tests.fakes.embedders import FakeAzureOpenAIClient, FakeOllamaClient, NativeSizeEmbedder

LEGACY = RagConfig(active=True, doc_name="manual.md").embedder_model_config()  # ollama + nomic, sem params
SDK_ERROR_TEXT = "texto-da-excecao-do-sdk-com-segredo"


class _Factory(IEmbedderFactory):
    """Fábrica interna: um embedder novo por chamada, como o ``ProviderRegistry``."""

    def __init__(self, make: Callable[[ModelConfig], Embedder]) -> None:
        self._make = make
        self.calls = 0

    def create_embedder(self, config: ModelConfig) -> Embedder:
        self.calls += 1
        return self._make(config)


def _ollama_factory(client: FakeOllamaClient) -> _Factory:
    def make(config: ModelConfig) -> Embedder:
        kwargs = {"dimensions": config.params["dimensions"]} if "dimensions" in config.params else {}
        return OllamaEmbedder(id=config.model_id, ollama_client=client, **kwargs)  # type: ignore[arg-type]

    return _Factory(make)


def test_o2_reproduzido_sem_a_sonda_o_ollama_embedder_do_agno_devolve_vetor_vazio() -> None:
    client = FakeOllamaClient(native_size=768)

    embedder = _ollama_factory(client).create_embedder(LEGACY)

    assert embedder.dimensions == 4096
    assert embedder.get_embedding("Texto A") == []  # o defeito: 768 posições viram []


def test_config_legada_do_ollama_ganha_a_dimensao_real_do_modelo() -> None:
    client = FakeOllamaClient(native_size=768)
    logger = RecordingLogger()

    embedder = DimensionResolvingEmbedderFactory(_ollama_factory(client), logger=logger).create_embedder(LEGACY)

    assert embedder.dimensions == 768  # type: ignore[attr-defined]
    assert len(embedder.get_embedding("Texto A")) == 768
    probe = client.calls[0]
    assert probe["model"] == "nomic-embed-text:latest"
    assert "dimensions" not in probe  # a sonda não pede tamanho: quer o nativo
    [record] = [r for r in logger.records if r.level == "info"]
    assert record.context == {"provider": "ollama", "model_id": "nomic-embed-text:latest", "dimensions": 768}


def test_dimensions_explicito_tem_precedencia_e_dispensa_a_sonda() -> None:
    client = FakeOllamaClient(native_size=768)
    config = ModelConfig(provider="ollama", model_id="nomic-embed-text:latest", params={"dimensions": 768})

    embedder = DimensionResolvingEmbedderFactory(_ollama_factory(client), logger=RecordingLogger()).create_embedder(
        config
    )

    assert embedder.dimensions == 768  # type: ignore[attr-defined]
    assert client.calls == []  # sem sonda


def test_dimensions_explicito_errado_continua_valendo() -> None:
    """Quem escreveu ``dimensions`` decide: nada de sonda por cima (o chunk vazio é recusado depois)."""
    client = FakeOllamaClient(native_size=768)
    config = ModelConfig(provider="ollama", model_id="nomic-embed-text:latest", params={"dimensions": 1024})

    embedder = DimensionResolvingEmbedderFactory(_ollama_factory(client), logger=RecordingLogger()).create_embedder(
        config
    )

    assert embedder.dimensions == 1024  # type: ignore[attr-defined]
    assert client.calls == []


def test_sonda_roda_uma_vez_por_config() -> None:
    client = FakeOllamaClient(native_size=768)
    inner = _ollama_factory(client)
    factory = DimensionResolvingEmbedderFactory(inner, logger=RecordingLogger())
    other = ModelConfig(provider="ollama", model_id="mxbai-embed-large")

    first = factory.create_embedder(LEGACY)
    second = factory.create_embedder(LEGACY)
    third = factory.create_embedder(other)

    assert first is not second  # um embedder novo por chamada, como a fábrica interna
    assert [first.dimensions, second.dimensions, third.dimensions] == [768, 768, 768]  # type: ignore[attr-defined]
    assert [c["model"] for c in client.calls] == ["nomic-embed-text:latest", "mxbai-embed-large"]
    assert inner.calls == 3


def test_sondas_concorrentes_da_mesma_config_viram_uma() -> None:
    client = FakeOllamaClient(native_size=768)
    factory = DimensionResolvingEmbedderFactory(_ollama_factory(client), logger=RecordingLogger())
    results: list[int | None] = []

    def create() -> None:
        results.append(factory.create_embedder(LEGACY).dimensions)  # type: ignore[attr-defined]

    threads = [threading.Thread(target=create) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == [768] * 8
    assert len(client.calls) == 1


def test_embedder_generico_usa_o_tamanho_que_o_provider_devolve_sem_mexer_no_pedido() -> None:
    """Ex.: endpoint OpenAI-compatível que ignora ``dimensions`` e devolve 1024 (o agno assume 1536)."""
    created: list[NativeSizeEmbedder] = []

    def make(config: ModelConfig) -> Embedder:
        created.append(NativeSizeEmbedder(id=config.model_id, native_size=1024))
        return created[-1]

    config = ModelConfig(provider="openai_compatible", model_id="bge-m3", base_url="https://llm.example.com/v1")

    sized = DimensionResolvingEmbedderFactory(_Factory(make), logger=RecordingLogger()).create_sized(config)

    assert sized.embedder is created[0]
    assert sized.dimensions == 1024  # o tamanho do índice
    assert created[0].dimensions == 1536  # o pedido continua o configurado
    assert len(created[0].calls) == 1


# ── R1 (rodada 2): a dimensão do índice não muda o pedido ao provider ──


def _azure_factory(client: FakeAzureOpenAIClient) -> _Factory:
    return _Factory(
        lambda config: AzureOpenAIEmbedder(id=config.model_id, openai_client=client)  # type: ignore[arg-type]
    )


def test_azure_ada_002_sem_dimensions_continua_sem_dimensions_no_pedido() -> None:
    """``dimensions=None`` no ``AzureOpenAIEmbedder`` é "não pedir"; o ada-002 responde 400 se pedir."""
    client = FakeAzureOpenAIClient(native_size=1536)
    config = ModelConfig(provider="azure", model_id="text-embedding-ada-002")

    sized = DimensionResolvingEmbedderFactory(_azure_factory(client), logger=RecordingLogger()).create_sized(config)
    vector = sized.embedder.get_embedding("Texto A")

    assert sized.dimensions == 1536
    assert sized.embedder.dimensions is None  # type: ignore[attr-defined]
    assert len(vector) == 1536
    assert [("dimensions" in r) for r in client.requests] == [False, False]  # sonda e uso


def test_azure_text_embedding_3_mantem_o_dimensions_do_agno_no_pedido() -> None:
    client = FakeAzureOpenAIClient(native_size=1536)
    config = ModelConfig(provider="azure", model_id="text-embedding-3-small")

    sized = DimensionResolvingEmbedderFactory(_azure_factory(client), logger=RecordingLogger()).create_sized(config)

    assert sized.dimensions == 1536
    assert [r.get("dimensions") for r in client.requests] == [1536]


def test_ollama_continua_recebendo_a_dimensao_real_no_pedido() -> None:
    client = FakeOllamaClient(native_size=768)
    embedder = DimensionResolvingEmbedderFactory(_ollama_factory(client), logger=RecordingLogger()).create_embedder(
        LEGACY
    )

    assert len(embedder.get_embedding("Texto A")) == 768
    assert "dimensions" not in client.calls[0]  # a sonda pede o tamanho nativo
    assert client.calls[-1]["dimensions"] == 768  # o uso pede o tamanho real (não o 4096 do agno)


# ── BUG-F211-QA-1: dimensions explícito normalizado ────────────────


def test_dimensions_explicito_double_integral_vira_int() -> None:
    client = FakeOllamaClient(native_size=768)
    config = ModelConfig(provider="ollama", model_id="nomic-embed-text:latest", params={"dimensions": 768.0})

    sized = DimensionResolvingEmbedderFactory(_ollama_factory(client), logger=RecordingLogger()).create_sized(config)

    assert sized.dimensions == 768 and type(sized.dimensions) is int
    assert type(sized.embedder.dimensions) is int  # type: ignore[attr-defined]
    assert client.calls == []


@pytest.mark.parametrize("value", [0, -768, 768.5, True, float("nan")])
def test_dimensions_explicito_invalido_e_recusado_com_mensagem_clara(value: float) -> None:
    client = FakeOllamaClient(native_size=768)
    config = ModelConfig(provider="ollama", model_id="nomic-embed-text:latest", params={"dimensions": value})
    factory = DimensionResolvingEmbedderFactory(_ollama_factory(client), logger=RecordingLogger())

    with pytest.raises(EmbeddingDimensionError, match="inteiro positivo"):
        factory.create_sized(config)


# ── R2 (rodada 2): prazo da sonda e cache negativo curto ───────────


def test_sonda_sem_resposta_estoura_o_prazo_e_os_demais_agentes_nao_esperam_de_novo() -> None:
    hold = threading.Event()
    client = FakeOllamaClient(native_size=768, hold=hold)
    logger = RecordingLogger()
    factory = DimensionResolvingEmbedderFactory(_ollama_factory(client), logger=logger, probe_timeout_s=0.05)
    try:
        started = time.monotonic()
        results = [factory.create_sized(LEGACY) for _ in range(4)]
        elapsed = time.monotonic() - started
    finally:
        hold.set()

    assert [(r.dimensions, r.error_type) for r in results] == [(None, "TimeoutError")] * 4
    assert len(client.calls) == 1  # uma espera por config, não uma por agente
    assert elapsed < 2.0
    assert [r.context["error_type"] for r in logger.records if r.level == "error"] == ["TimeoutError"]


def test_prazo_da_sonda_respeita_model_params_timeout() -> None:
    hold = threading.Event()
    client = FakeOllamaClient(native_size=768, hold=hold)
    config = ModelConfig(provider="ollama", model_id="nomic-embed-text:latest", params={"timeout": 0.05})
    factory = DimensionResolvingEmbedderFactory(_ollama_factory(client), logger=RecordingLogger(), probe_timeout_s=60)
    try:
        started = time.monotonic()
        sized = factory.create_sized(config)
        elapsed = time.monotonic() - started
    finally:
        hold.set()

    assert (sized.dimensions, sized.error_type) == (None, "TimeoutError")
    assert elapsed < 2.0


def test_falha_em_cache_negativo_nao_chama_o_provider_de_novo_dentro_do_prazo() -> None:
    client = FakeOllamaClient(error=ConnectionError("fora"))
    factory = DimensionResolvingEmbedderFactory(_ollama_factory(client), logger=RecordingLogger())
    with pytest.raises(EmbedderProbeError):
        factory.create_embedder(LEGACY)
    client.error = None

    with pytest.raises(EmbedderProbeError):
        factory.create_embedder(LEGACY)

    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("client", "error_type"),
    [
        pytest.param(FakeOllamaClient(error=ConnectionError(SDK_ERROR_TEXT)), "ConnectionError", id="servidor-fora"),
        pytest.param(FakeOllamaClient(native_size=0), "EmptyEmbeddingError", id="vetor-vazio"),
    ],
)
def test_falha_da_sonda_levanta_erro_proprio_e_loga_so_o_tipo(client: FakeOllamaClient, error_type: str) -> None:
    logger = RecordingLogger()
    factory = DimensionResolvingEmbedderFactory(_ollama_factory(client), logger=logger)

    with pytest.raises(EmbedderProbeError) as raised:
        factory.create_embedder(LEGACY)

    assert SDK_ERROR_TEXT not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None  # sem a exceção do SDK
    [record] = [r for r in logger.records if r.level == "error"]
    assert record.context["provider"] == "ollama"
    assert record.context["model_id"] == "nomic-embed-text:latest"
    assert record.context["error_type"] == error_type
    assert SDK_ERROR_TEXT not in repr(logger.records)


def test_falha_da_sonda_expira_e_a_proxima_tentativa_sonda_de_novo() -> None:
    client = FakeOllamaClient(error=ConnectionError("fora"))
    factory = DimensionResolvingEmbedderFactory(_ollama_factory(client), logger=RecordingLogger(), negative_cache_s=0)
    with pytest.raises(EmbedderProbeError):
        factory.create_embedder(LEGACY)

    client.error = None  # modelo baixado / servidor de volta

    assert factory.create_embedder(LEGACY).dimensions == 768  # type: ignore[attr-defined]
    assert len(client.calls) == 2
