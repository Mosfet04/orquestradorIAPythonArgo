"""Testes para MongoAgentConfigRepository (motor async)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.infrastructure.repositories.mongo_agent_config_repository import (
    MongoAgentConfigRepository,
)


class _AsyncCursorMock:
    """Mock de cursor motor que suporta async for."""

    def __init__(self, docs):
        self._docs = docs
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._docs):
            raise StopAsyncIteration
        doc = self._docs[self._index]
        self._index += 1
        return doc


@pytest.fixture
def repo(mock_logger):
    with patch(
        "src.infrastructure.repositories.mongo_base.MongoClientFactory.get_client"
    ) as mock_factory:
        mock_collection = MagicMock()
        mock_db = MagicMock()
        mock_db.__getitem__ = MagicMock(return_value=mock_collection)
        mock_client = MagicMock()
        mock_client.__getitem__ = MagicMock(return_value=mock_db)
        mock_factory.return_value = mock_client

        repository = MongoAgentConfigRepository(
            connection_string="mongodb://localhost:27017",
            database_name="testdb",
            logger=mock_logger,
        )
        # Override _collection com nosso mock
        repository._collection = mock_collection
        yield repository, mock_collection


class TestMongoAgentConfigRepository:
    async def test_get_active_agents_success(self, repo):
        repository, mock_collection = repo
        docs = [
            {
                "id": "a1",
                "nome": "Agent 1",
                "factory_ia_model": "ollama",
                "model": "llama3.2:latest",
                "descricao": "desc",
                "prompt": "prompt",
                "active": True,
            }
        ]
        mock_collection.find.return_value = _AsyncCursorMock(docs)

        configs = await repository.get_active_agents()
        assert len(configs) == 1
        assert configs[0].id == "a1"

    async def test_get_active_agents_empty(self, repo):
        repository, mock_collection = repo
        mock_collection.find.return_value = _AsyncCursorMock([])

        configs = await repository.get_active_agents()
        assert configs == []

    async def test_get_agent_by_id_success(self, repo):
        repository, mock_collection = repo
        mock_collection.find_one = AsyncMock(
            return_value={
                "id": "a1",
                "nome": "Agent 1",
                "factory_ia_model": "ollama",
                "model": "llama3.2:latest",
                "descricao": "desc",
                "prompt": "prompt",
                "active": True,
            }
        )

        config = await repository.get_agent_by_id("a1")
        assert config.id == "a1"

    async def test_get_agent_by_id_not_found_raises(self, repo):
        repository, mock_collection = repo
        mock_collection.find_one = AsyncMock(return_value=None)

        with pytest.raises(ValueError, match="não encontrado"):
            await repository.get_agent_by_id("missing")

    async def test_backward_compat_factoryIaModel(self, repo):
        """Testa compatibilidade com campo legado factoryIaModel."""
        repository, mock_collection = repo
        docs = [
            {
                "id": "a2",
                "nome": "Agent 2",
                "factoryIaModel": "openai",
                "model": "gpt-4",
                "descricao": "desc",
                "prompt": "prompt",
                "active": True,
            }
        ]
        mock_collection.find.return_value = _AsyncCursorMock(docs)

        configs = await repository.get_active_agents()
        assert len(configs) == 1
        assert configs[0].factory_ia_model == "openai"
