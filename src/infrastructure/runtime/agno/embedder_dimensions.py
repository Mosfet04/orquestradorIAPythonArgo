"""Dimensão real do embedder (F2-11, achado O2): sonda de uma embedding curta, uma vez por config.

A dimensão do **índice** (``numDimensions`` do ``vectorSearch`` e guarda contra vetor de outro
tamanho) é separada da dimensão do **pedido** ao provider. Semântica de ``dimensions`` nos embedders
que o registry monta (agno 2.5.8, ``agno/knowledge/embedder/``):

- ``OllamaEmbedder`` (``ollama.py:40,88-90,105-107``): padrão 4096, vai no pedido e o vetor de outro
  tamanho vira ``[]``. É o único que **precisa** do atributo igual ao tamanho real; aqui ele recebe a
  dimensão resolvida.
- ``OpenAIEmbedder`` (``openai.py:31-33,74-76``), ``OpenAILikeEmbedder`` (``openai_like.py:22-29``),
  ``AzureOpenAIEmbedder`` (``azure_openai.py:38-39,106-107``) e ``GeminiEmbedder`` (``google.py:21,71``):
  o atributo só pede um tamanho ao provider (``None`` no Azure = não pede; o ``text-embedding-ada-002``
  recusa o parâmetro) e nada é descartado. Não são tocados: a sonda só descobre o tamanho que eles
  devolvem, para o índice.

Sem ``model_params.dimensions``, a sonda pede uma embedding curta: ao Ollama pelo cliente do SDK sem
``dimensions`` (o ``get_embedding`` do agno descartaria o vetor), aos demais por ``get_embedding``. Com
``dimensions`` explícito não há sonda; o valor é normalizado (double integral do Mongo/YAML vira int;
não integral, booleano ou ≤ 0 é recusado com ``EmbeddingDimensionError``).

A sonda faz rede: roda com prazo (``model_params.timeout`` ou ``PROBE_TIMEOUT_S``) numa thread
daemon, então nem cliente sem timeout (o ``ollama.Client`` usa ``timeout=None``) trava o startup.
Falha fica em cache por ``NEGATIVE_CACHE_S``: agentes com a mesma config no mesmo startup pagam uma
espera, não uma cada. Chame fora do event loop.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from agno.knowledge.embedder.ollama import OllamaEmbedder

from src.domain.entities.model_config import ModelConfig, ParamValue
from src.domain.ports import IEmbedderFactory, ILogger
from src.domain.ports.embedder_factory_port import TextEmbedder
from src.infrastructure.runtime.agno.rag_vector_db import EmbeddingDimensionError

_PROBE_TEXT = "dimensão"
PROBE_TIMEOUT_S = 15.0
NEGATIVE_CACHE_S = 60.0


class EmbedderProbeError(RuntimeError):
    """A sonda não obteve um vetor do embedder: a dimensão ficou desconhecida."""


class EmptyEmbeddingError(ValueError):
    """O provider respondeu à sonda sem vetor (lista vazia ou formato inesperado)."""


@dataclass(frozen=True)
class SizedEmbedder:
    """Embedder e o tamanho dos vetores que ele devolve; ``dimensions=None`` = sonda falhou.

    ``error_type``: tipo da falha da sonda (para o log de quem decide o que fazer sem a dimensão).
    """

    embedder: TextEmbedder
    dimensions: int | None
    error_type: str | None = None


def explicit_dimensions(value: ParamValue) -> int:
    """``model_params.dimensions`` como int: ``768`` e ``768.0`` valem; o resto é recusado."""
    integral = isinstance(value, int) or (isinstance(value, float) and math.isfinite(value) and value.is_integer())
    if isinstance(value, bool) or not integral or int(cast(float, value)) <= 0:
        raise EmbeddingDimensionError("model_params.dimensions precisa ser um inteiro positivo")
    return int(cast(float, value))


def fit_request(embedder: TextEmbedder, dimensions: int) -> None:
    """Ajusta o pedido só no embedder que descarta vetor de outro tamanho (o ``OllamaEmbedder``)."""
    if isinstance(embedder, OllamaEmbedder):
        embedder.dimensions = dimensions


def _probe_vector(embedder: Any) -> Sequence[float]:
    """Vetor de uma embedding curta, no tamanho que o modelo devolve (sem mexer no pedido dos demais)."""
    if isinstance(embedder, OllamaEmbedder):
        kwargs: dict[str, Any] = {} if embedder.options is None else {"options": embedder.options}
        response = embedder.client.embed(model=embedder.id, input=_PROBE_TEXT, **kwargs)
        embeddings = response["embeddings"]
        vector = embeddings[0] if embeddings else []
    else:
        vector = embedder.get_embedding(_PROBE_TEXT)
    if not isinstance(vector, Sequence) or not vector or not all(isinstance(x, (int, float)) for x in vector):
        raise EmptyEmbeddingError("sonda sem vetor")
    return cast(Sequence[float], vector)


def _probe_timeout(config: ModelConfig, default: float) -> float:
    value = config.params.get("timeout")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return float(value)
    return default


def _probe_with_deadline(embedder: TextEmbedder, timeout: float) -> int:
    """``len`` do vetor da sonda; ``TimeoutError`` se o provider não responder no prazo.

    Thread daemon: se o cliente nunca volta, ela fica para trás sem segurar o startup nem a saída.
    """
    outcome: list[int | BaseException] = []
    done = threading.Event()

    def run() -> None:
        try:
            outcome.append(len(_probe_vector(embedder)))
        except BaseException as exc:  # noqa: BLE001 - repassada a quem espera, logada por lá
            outcome.append(exc)
        finally:
            done.set()

    threading.Thread(target=run, name="embedder-probe", daemon=True).start()
    if not done.wait(timeout):
        raise TimeoutError("sonda sem resposta no prazo")
    result = outcome[0]
    if isinstance(result, BaseException):
        raise result
    return result


class DimensionResolvingEmbedderFactory(IEmbedderFactory):
    """``IEmbedderFactory`` que resolve o tamanho real dos vetores do embedder da fábrica interna.

    Cache por ``ModelConfig`` (uma sonda por config no processo, mesmo com agentes criados em
    paralelo); falha em cache só por ``negative_cache_s``.
    """

    def __init__(
        self,
        inner: IEmbedderFactory,
        *,
        logger: ILogger,
        probe_timeout_s: float = PROBE_TIMEOUT_S,
        negative_cache_s: float = NEGATIVE_CACHE_S,
    ) -> None:
        """``inner``: a fábrica que monta o embedder (o ``ProviderRegistry`` em produção)."""
        self._inner = inner
        self._logger = logger
        self._probe_timeout_s = probe_timeout_s
        self._negative_cache_s = negative_cache_s
        self._dimensions: dict[ModelConfig, int] = {}
        self._failures: dict[ModelConfig, tuple[str, float]] = {}
        self._locks: dict[ModelConfig, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    @property
    def inner(self) -> IEmbedderFactory:
        """A fábrica embrulhada."""
        return self._inner

    def create_embedder(self, config: ModelConfig) -> TextEmbedder:
        """Embedder pronto para uso; dimensão desconhecida levanta ``EmbedderProbeError``.

        ``dimensions`` explícito inválido levanta ``EmbeddingDimensionError``.
        """
        sized = self.create_sized(config)
        if sized.dimensions is None:
            raise EmbedderProbeError(
                f"sonda de dimensão do embedder falhou ({sized.error_type}); confira o provider/modelo "
                "ou defina model_params.dimensions"
            )
        return sized.embedder

    def create_sized(self, config: ModelConfig) -> SizedEmbedder:
        """Embedder da fábrica interna e o tamanho dos vetores dele (``None`` se a sonda falhou)."""
        embedder = self._inner.create_embedder(config)
        explicit = config.params.get("dimensions")
        if explicit is not None:
            dimensions = explicit_dimensions(explicit)
            if hasattr(embedder, "dimensions"):
                setattr(embedder, "dimensions", dimensions)  # noqa: B010 - o mesmo valor pedido, como int
            return SizedEmbedder(embedder, dimensions)
        resolved, error_type = self._resolve(config, embedder)
        if resolved is not None:
            fit_request(embedder, resolved)
        return SizedEmbedder(embedder, resolved, error_type)

    def _resolve(self, config: ModelConfig, embedder: TextEmbedder) -> tuple[int | None, str | None]:
        with self._lock_for(config):
            cached = self._dimensions.get(config)
            if cached is not None:
                return cached, None
            failure = self._failures.get(config)
            if failure is not None and failure[1] > time.monotonic():
                return None, failure[0]
            error_type: str | None = None
            try:
                dimensions = _probe_with_deadline(embedder, _probe_timeout(config, self._probe_timeout_s))
            except Exception as exc:  # noqa: BLE001 - qualquer falha do SDK vira "dimensão desconhecida"
                error_type = type(exc).__name__  # o texto do SDK pode trazer segredo: só o tipo
            if error_type is not None:
                self._failures[config] = (error_type, time.monotonic() + self._negative_cache_s)
                self._logger.error(
                    "Sonda de dimensão do embedder falhou",
                    provider=config.provider,
                    model_id=config.model_id,
                    error_type=error_type,
                )
                return None, error_type
            self._failures.pop(config, None)
            self._dimensions[config] = dimensions
            self._logger.info(
                "Dimensão do embedder obtida pela sonda",
                provider=config.provider,
                model_id=config.model_id,
                dimensions=dimensions,
            )
            return dimensions, None

    def _lock_for(self, config: ModelConfig) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(config, threading.Lock())
