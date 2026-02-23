"""Testes estendidos para MongoTeamConfigRepository — async operations."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.entities.team_config import TeamConfig
from src.infrastructure.repositories.mongo_team_config_repository import (
    MongoTeamConfigRepository,
)


class _AsyncCursorStub:
    """Cursor async fake que itera sobre uma lista de documentos."""

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
def team_repo(mock_logger):
    """Cria MongoTeamConfigRepository com collection mockada."""
    with patch(
        "src.infrastructure.repositories.mongo_base.AsyncIOMotorClient"
    ):
        repo = MongoTeamConfigRepository(
            connection_string="mongodb://test:27017",
            database_name="agno",
            collection_name="teams_config",
            logger=mock_logger,
        )
    repo._collection = MagicMock()
    return repo


class TestGetActiveTeams:
    async def test_returns_team_configs(self, team_repo):
        docs = [
            {
                "id": "team-1",
                "nome": "Router",
                "model": "qwen3",
                "factory_ia_model": "ollama",
                "mode": "route",
                "member_ids": ["a1"],
                "active": True,
            },
            {
                "id": "team-2",
                "nome": "Coordinator",
                "model": "gpt-4o",
                "factory_ia_model": "openai",
                "mode": "coordinate",
                "member_ids": ["a2", "a3"],
                "active": True,
            },
        ]
        team_repo._collection.find.return_value = _AsyncCursorStub(docs)

        result = await team_repo.get_active_teams()
        assert len(result) == 2
        assert all(isinstance(t, TeamConfig) for t in result)
        assert result[0].id == "team-1"
        assert result[1].mode == "coordinate"

    async def test_returns_empty_list(self, team_repo):
        team_repo._collection.find.return_value = _AsyncCursorStub([])
        result = await team_repo.get_active_teams()
        assert result == []

    async def test_raises_on_db_error(self, team_repo, mock_logger):
        team_repo._collection.find.side_effect = RuntimeError("connection lost")

        with pytest.raises(RuntimeError, match="connection lost"):
            await team_repo.get_active_teams()

        mock_logger.error.assert_called_once()


class TestGetTeamById:
    async def test_returns_team_config(self, team_repo):
        doc = {
            "id": "team-1",
            "nome": "Router",
            "model": "qwen3",
            "factory_ia_model": "ollama",
            "mode": "route",
            "member_ids": ["a1"],
            "active": True,
        }
        team_repo._collection.find_one = AsyncMock(return_value=doc)

        result = await team_repo.get_team_by_id("team-1")
        assert isinstance(result, TeamConfig)
        assert result.id == "team-1"

    async def test_returns_none_not_found(self, team_repo):
        team_repo._collection.find_one = AsyncMock(return_value=None)

        result = await team_repo.get_team_by_id("nonexistent")
        assert result is None

    async def test_raises_on_db_error(self, team_repo, mock_logger):
        team_repo._collection.find_one = AsyncMock(
            side_effect=RuntimeError("timeout")
        )

        with pytest.raises(RuntimeError, match="timeout"):
            await team_repo.get_team_by_id("team-1")

        mock_logger.error.assert_called_once()
