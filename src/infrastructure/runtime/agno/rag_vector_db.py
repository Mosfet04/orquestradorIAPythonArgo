"""Vector db do RAG semântico que nunca grava embedding inválido e corrige a coleção legada (F2-11).

Subclasse do ``MongoDb`` do agno 2.5.8 (``agno/vectordb/mongodb/mongodb.py``):

- a dimensão do índice é a de ``dimensions`` (resolvida fora: ``model_params``, sonda ou o índice que
  já existe), não o atributo ``dimensions`` do embedder, que em vários embedders é só o que se pede
  ao provider (``None`` no Azure). Por isso a criação do índice é daqui (``_create_search_index``, que
  o agno chama em ``_get_or_create_collection``, ``:218``, substituído; o dele lê o atributo do
  embedder, ``:318-333``, e dorme 10 s fixos depois de derrubar);
- (achado O1) o agno só recusa ``embedding is None`` (``:541-545``) e grava ``[]``; aqui todo
  documento passa por ``prepare_doc`` (o ponto comum de ``insert``/``upsert`` e das versões
  assíncronas, ``:987``) e o de tamanho diferente de ``dimensions`` é recusado com
  ``EmbeddingDimensionError`` (um ``ValueError``: o agno pula o documento, ``:548``/``:576``/
  ``:1076``). As recusas ficam contadas por ``content_hash`` para quem indexou descartar o resto do
  documento (com ``skip_if_exists`` um documento parcial nunca seria completado).
- ``repair`` corrige no startup a coleção gravada antes: índice ``vectorSearch`` ausente ou com
  ``numDimensions`` diferente é recriado (espera o antigo sumir: no Atlas a remoção é assíncrona) e
  o chunk com embedding vazio ou de outro tamanho é reindexado no lugar, a partir do ``content``
  gravado (nada é apagado).

Tudo síncrono e com I/O: chame fora do event loop.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from agno.knowledge.document import Document
from agno.vectordb.mongodb import MongoDb as MongoVectorDb
from pymongo.operations import SearchIndexModel

_EMBEDDING_FIELD = "embedding"
_INDEX_POLL_SECONDS = 1.0
_INDEX_DROP_TIMEOUT_SECONDS = 120.0


class EmbeddingDimensionError(ValueError):
    """Embedding vazio ou de tamanho diferente do índice: o chunk não é gravado."""


@dataclass(frozen=True)
class RepairReport:
    """O que ``repair`` encontrou e corrigiu na coleção.

    ``previous_dimensions``: ``numDimensions`` do índice antes (``None`` = não havia índice);
    ``index_rebuilt``: o índice foi criado ou recriado; ``reembedded``/``failed``: chunks inválidos
    reindexados / que continuaram inválidos (ficam como estavam; o próximo startup tenta de novo).
    """

    previous_dimensions: int | None
    index_rebuilt: bool
    reembedded: int
    failed: int


def _valid(embedding: object, dimensions: int) -> bool:
    return isinstance(embedding, list) and len(embedding) == dimensions


def _invalid_embedding_filter(dimensions: int) -> dict[str, Any]:
    """Documentos sem a posição ``dimensions - 1`` (vazio, curto, ausente) ou com a ``dimensions``."""
    return {
        "$or": [
            {f"{_EMBEDDING_FIELD}.{dimensions - 1}": {"$exists": False}},
            {f"{_EMBEDDING_FIELD}.{dimensions}": {"$exists": True}},
        ]
    }


def _vector_dimensions(index: Mapping[str, Any]) -> int | None:
    """``numDimensions`` do campo vetorial ``embedding`` de um índice de ``list_search_indexes``."""
    definition = index.get("latestDefinition") or index.get("definition") or {}
    for field in definition.get("fields", []):
        if field.get("type") == "vector" and field.get("path") == _EMBEDDING_FIELD:
            value = field.get("numDimensions")
            return value if isinstance(value, int) else None
    return None


class GuardedMongoVectorDb(MongoVectorDb):
    """``MongoDb`` do agno com a guarda de dimensão do embedding (F2-11)."""

    def __init__(self, *, dimensions: int | None = None, **kwargs: Any) -> None:
        """``dimensions``: tamanho do índice e dos vetores aceitos; demais kwargs, os do ``MongoDb``."""
        super().__init__(**kwargs)
        self._dimensions = dimensions
        self._rejected: dict[str, int] = {}

    @property
    def dimensions(self) -> int:
        """Dimensão do índice; sem ela nada é gravado (``EmbeddingDimensionError``)."""
        value = self._dimensions
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise EmbeddingDimensionError("dimensão do índice desconhecida")
        return value

    @dimensions.setter
    def dimensions(self, value: int) -> None:
        self._dimensions = value

    def existing_index_dimensions(self) -> int | None:
        """``numDimensions`` do índice vetorial que a coleção já tem (``None``: sem coleção ou índice)."""
        if not self.collection_exists():
            return None
        index = self._find_index(self._get_collection())
        value = _vector_dimensions(index) if index is not None else None
        return value if value is not None and value > 0 else None

    @property
    def rejected_chunks(self) -> int:
        """Chunks recusados desde o último ``discard_rejected``."""
        return sum(self._rejected.values())

    def prepare_doc(
        self, content_hash: str, document: Document, filters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Documento a gravar; embedding vazio ou de outro tamanho levanta ``EmbeddingDimensionError``.

        A mensagem não leva o texto do chunk (o agno a loga com o nome do documento).
        """
        if not _valid(document.embedding, self.dimensions):
            self._rejected[content_hash] = self._rejected.get(content_hash, 0) + 1
            raise EmbeddingDimensionError(
                f"embedding vazio ou de tamanho diferente do índice (esperado {self.dimensions}, "
                f"recebido {len(document.embedding or [])}); chunk não gravado"
            )
        prepared: dict[str, Any] = super().prepare_doc(content_hash, document, filters)
        return prepared

    def discard_rejected(self) -> int:
        """Apaga os chunks já gravados dos documentos com recusa; devolve quantos apagou.

        Só os ``content_hash`` que tiveram recusa nesta indexação, na coleção deste agente.
        """
        hashes = list(self._rejected)
        self._rejected.clear()
        if not hashes:
            return 0
        result = self._get_collection().delete_many({"content_hash": {"$in": hashes}})
        return int(result.deleted_count)

    def repair(self) -> RepairReport:
        """Recria o índice de dimensão errada/ausente e reindexa os chunks inválidos. Idempotente."""
        dimensions = self.dimensions
        collection = self._get_collection()
        previous = self._index_dimensions(collection)
        rebuilt = previous != dimensions
        if rebuilt:
            self._create_search_index(overwrite=True)
        reembedded = failed = 0
        for doc in collection.find(_invalid_embedding_filter(dimensions), {"content": 1}):
            content = doc.get("content")
            embedding = self.embedder.get_embedding(content) if isinstance(content, str) and content else None
            if not _valid(embedding, dimensions):
                failed += 1
                continue
            collection.update_one({"_id": doc["_id"]}, {"$set": {_EMBEDDING_FIELD: embedding}})
            reembedded += 1
        return RepairReport(previous_dimensions=previous, index_rebuilt=rebuilt, reembedded=reembedded, failed=failed)

    @property
    def _index_name(self) -> str:
        return self.search_index_name or "vector_index_1"  # o mesmo padrão do agno

    def _find_index(self, collection: Any) -> Mapping[str, Any] | None:
        for index in collection.list_search_indexes(self._index_name):
            if index.get("name") == self._index_name:
                return cast(Mapping[str, Any], index)
        return None

    def _index_dimensions(self, collection: Any) -> int | None:
        index = self._find_index(collection)
        return _vector_dimensions(index) if index is not None else None

    def _create_search_index(self, overwrite: bool = True) -> None:
        """Cria o índice ``vectorSearch`` com ``numDimensions = dimensions`` e espera ficar listado.

        Substitui o do agno (chamado por ele ao criar a coleção). ``overwrite``: derruba antes o
        índice de mesmo nome e espera sumir (no Atlas a remoção é assíncrona).
        """
        collection = self._get_collection()
        if overwrite and self._find_index(collection) is not None:
            collection.drop_search_index(self._index_name)
            self._wait_until(lambda: self._find_index(collection) is None, "índice vetorial antigo não foi removido")
        definition = {
            "fields": [
                {
                    "type": "vector",
                    "numDimensions": self.dimensions,
                    "path": _EMBEDDING_FIELD,
                    "similarity": self.distance_metric,
                }
            ]
        }
        collection.create_search_index(
            model=SearchIndexModel(definition=definition, name=self._index_name, type="vectorSearch")
        )
        self._wait_until(lambda: self._find_index(collection) is not None, "índice vetorial não apareceu")

    @staticmethod
    def _wait_until(done: Callable[[], bool], timeout_message: str) -> None:
        deadline = time.monotonic() + _INDEX_DROP_TIMEOUT_SECONDS
        while not done():
            if time.monotonic() > deadline:
                raise TimeoutError(f"{timeout_message} a tempo")
            time.sleep(_INDEX_POLL_SECONDS)
