"""Embedders do F2-11: reproduzem o tamanho real do vetor de um modelo, sem rede.

- ``FakeOllamaClient``: o ``ollama.Client`` (0.6.3) que o ``OllamaEmbedder`` real do agno 2.5.8 usa;
  injetado em ``OllamaEmbedder(ollama_client=...)``. Devolve sempre o tamanho nativo do modelo (ex.:
  768 do ``nomic-embed-text``), com ou sem ``dimensions`` no pedido: o embedder do agno, com o
  padrão ``dimensions=4096``, transforma esse vetor em ``[]`` (achado O2).
- ``NativeSizeEmbedder``: embedder genérico do agno que ignora ``dimensions`` e devolve o tamanho
  nativo (como um endpoint OpenAI-compatível que não trunca).
- ``ScriptedEmbedder``: devolve vetores de tamanho escolhido por texto (vazio, curto) para chunks
  específicos; o resto, o tamanho de ``dimensions``.
- ``FakeAzureOpenAIClient``: o ``openai.AzureOpenAI`` (só ``embeddings.create``) para o
  ``AzureOpenAIEmbedder`` real; como o ``text-embedding-ada-002`` do Azure, recusa ``dimensions`` no
  pedido (só ``text-embedding-3*`` aceita) e devolve o tipo real do SDK ``openai``.
"""

from __future__ import annotations

import hashlib
import math
import struct
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from agno.knowledge.embedder.base import Embedder
from openai.types import CreateEmbeddingResponse, Embedding
from openai.types.create_embedding_response import Usage


def unit_vector(text: str, size: int) -> list[float]:
    """Vetor unitário determinístico de ``size`` posições para ``text``."""
    raw: list[float] = []
    counter = 0
    while len(raw) < size:
        digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
        raw.extend(v / 2**31 for (v,) in struct.iter_unpack(">i", digest))
        counter += 1
    vec = raw[:size]
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


@dataclass
class FakeOllamaClient:
    """``ollama.Client.embed`` com o tamanho nativo do modelo.

    ``error`` simula servidor fora do ar; ``hold`` (se definido) segura a resposta até ser liberado,
    como um host que aceita a conexão e não responde.
    """

    native_size: int = 768
    error: Exception | None = None
    hold: threading.Event | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def embed(self, model: str = "", input: str = "", **kwargs: Any) -> Mapping[str, Any]:  # pylint: disable=redefined-builtin
        """``input``: o nome do kwarg no SDK."""
        self.calls.append({"model": model, "input": input, **kwargs})
        if self.hold is not None:
            self.hold.wait()
        if self.error is not None:
            raise self.error
        return {"embeddings": [unit_vector(str(input), self.native_size)]}


class AzureBadRequest(Exception):
    """O 400 do Azure para ``dimensions`` num modelo que não aceita o parâmetro."""


@dataclass
class _AzureEmbeddings:
    native_size: int
    requests: list[dict[str, Any]]

    def create(self, **params: Any) -> CreateEmbeddingResponse:
        self.requests.append(params)
        if "dimensions" in params and not str(params["model"]).startswith("text-embedding-3"):
            raise AzureBadRequest("This model does not support specifying dimensions.")
        vectors = [unit_vector(str(text), self.native_size) for text in params["input"]]
        return CreateEmbeddingResponse(
            data=[Embedding(embedding=v, index=i, object="embedding") for i, v in enumerate(vectors)],
            model=str(params["model"]),
            object="list",
            usage=Usage(prompt_tokens=1, total_tokens=1),
        )


@dataclass
class FakeAzureOpenAIClient:
    """``AzureOpenAI`` com ``embeddings.create``; ``requests`` guarda cada pedido."""

    native_size: int = 1536
    requests: list[dict[str, Any]] = field(default_factory=list)

    @property
    def embeddings(self) -> _AzureEmbeddings:
        return _AzureEmbeddings(self.native_size, self.requests)


@dataclass
class NativeSizeEmbedder(Embedder):
    """Embedder que ignora ``dimensions`` e devolve sempre ``native_size`` posições."""

    id: str = "native"
    native_size: int = 1024
    calls: list[str] = field(default_factory=list)

    def get_embedding(self, text: str) -> list[float]:
        self.calls.append(text)
        return unit_vector(text, self.native_size)

    def get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
        return self.get_embedding(text), None


@dataclass
class ScriptedEmbedder(Embedder):
    """Tamanho do vetor por texto: ``size_for(text)`` devolve o tamanho (0 = ``[]``) ou ``None`` (padrão)."""

    id: str = "scripted"
    dimensions: int | None = 8
    size_for: Callable[[str], int | None] = lambda _text: None
    calls: list[str] = field(default_factory=list)

    def get_embedding(self, text: str) -> list[float]:
        self.calls.append(text)
        size = self.size_for(text)
        return unit_vector(text, (self.dimensions or 8) if size is None else size)

    def get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
        return self.get_embedding(text), None
