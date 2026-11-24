"""Embedders OpenAI e OpenAI-compatível com cliente httpx guardado nos dois caminhos (F4-01).

O ``OpenAIEmbedder`` do agno 3.1.12 (``agno/knowledge/embedder/openai.py``, ``client``/``aclient``) passa o
mesmo ``client_params`` ao ``OpenAI`` síncrono e ao ``AsyncOpenAI``, e o SDK ``openai`` 2.x exige
``httpx.Client`` num e ``httpx.AsyncClient`` no outro (``openai/_base_client.py``, ``isinstance``): um
``http_client`` só não serve aos dois, e o RAG usa os dois (indexação e busca hierárquica síncronas,
``async_search`` do agno assíncrono). Estas subclasses recebem um cliente para cada caminho; sem eles,
comportam-se como a classe do agno. Os parâmetros do SDK são os mesmos que o agno monta.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from agno.knowledge.embedder.openai import OpenAIEmbedder
from agno.knowledge.embedder.openai_like import OpenAILikeEmbedder
from openai import AsyncOpenAI, OpenAI


def _sdk_params(embedder: OpenAIEmbedder) -> dict[str, Any]:
    params = {"api_key": embedder.api_key, "organization": embedder.organization, "base_url": embedder.base_url}
    return {**{k: v for k, v in params.items() if v is not None}, **(embedder.client_params or {})}


def _sync_sdk(embedder: OpenAIEmbedder, http_client: httpx.Client) -> OpenAI:
    return OpenAI(**{**_sdk_params(embedder), "http_client": http_client})


def _async_sdk(embedder: OpenAIEmbedder, http_client: httpx.AsyncClient) -> AsyncOpenAI:
    return AsyncOpenAI(**{**_sdk_params(embedder), "http_client": http_client})


@dataclass
class GuardedOpenAIEmbedder(OpenAIEmbedder):
    """``OpenAIEmbedder`` com ``sync_http_client``/``async_http_client`` (o guardado, com ``base_url`` da config)."""

    sync_http_client: httpx.Client | None = None
    async_http_client: httpx.AsyncClient | None = None

    @property
    def client(self) -> OpenAI:
        """Cliente síncrono do SDK, sobre ``sync_http_client`` se houver."""
        if self.openai_client is None and self.sync_http_client is not None:
            self.openai_client = _sync_sdk(self, self.sync_http_client)
        return super().client

    @property
    def aclient(self) -> AsyncOpenAI:
        """Cliente assíncrono do SDK, sobre ``async_http_client`` se houver."""
        if self.async_client is None and self.async_http_client is not None:
            self.async_client = _async_sdk(self, self.async_http_client)
        return super().aclient


@dataclass
class GuardedOpenAILikeEmbedder(OpenAILikeEmbedder, GuardedOpenAIEmbedder):
    """``OpenAILikeEmbedder`` com ``sync_http_client``/``async_http_client`` (ver ``GuardedOpenAIEmbedder``).

    MRO: os defaults e o ``__post_init__`` são os do ``OpenAILikeEmbedder``; ``client``/``aclient``, os do
    ``GuardedOpenAIEmbedder`` (o ``OpenAILikeEmbedder`` do agno 3.1.12 não os redefine).
    """
