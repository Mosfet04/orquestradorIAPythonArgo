"""Testes para MongoToolRepository (motor async)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.entities.tool import HttpMethod, ParameterType
from src.infrastructure.repositories.mongo_tool_repository import MongoToolRepository


class _AsyncCursorMock:
    """Cursor mock que suporta ``async for``."""

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


def _make_tool_doc(**overrides) -> dict:
    base = {
        "id": "t1",
        "name": "echo",
        "description": "Echo tool",
        "route": "https://api.example.com/echo",
        "http_method": "GET",
        "parameters": [],
        "instructions": "Call it",
        "headers": {},
        "active": True,
    }
    base.update(overrides)
    return base


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

        repository = MongoToolRepository(
            connection_string="mongodb://localhost:27017",
            database_name="testdb",
            logger=mock_logger,
        )
        repository._collection = mock_collection
        yield repository, mock_collection


class TestGetToolsByIds:
    async def test_returns_tools(self, repo):
        repository, mock_collection = repo
        docs = [_make_tool_doc(id="t1"), _make_tool_doc(id="t2", name="tool2")]
        mock_collection.find.return_value = _AsyncCursorMock(docs)

        tools = await repository.get_tools_by_ids(["t1", "t2"])
        assert len(tools) == 2
        assert tools[0].id == "t1"
        assert tools[1].id == "t2"

    async def test_empty_ids_returns_empty(self, repo):
        repository, _ = repo
        tools = await repository.get_tools_by_ids([])
        assert tools == []

    async def test_no_results(self, repo):
        repository, mock_collection = repo
        mock_collection.find.return_value = _AsyncCursorMock([])
        tools = await repository.get_tools_by_ids(["missing"])
        assert tools == []


class TestGetToolById:
    async def test_found(self, repo):
        repository, mock_collection = repo
        mock_collection.find_one = AsyncMock(return_value=_make_tool_doc(id="t1"))

        tool = await repository.get_tool_by_id("t1")
        assert tool.id == "t1"
        assert tool.http_method == HttpMethod.GET

    async def test_not_found_raises(self, repo):
        repository, mock_collection = repo
        mock_collection.find_one = AsyncMock(return_value=None)

        with pytest.raises(ValueError, match="não encontrada"):
            await repository.get_tool_by_id("missing")


class TestGetAllActiveTools:
    async def test_returns_all(self, repo):
        repository, mock_collection = repo
        docs = [_make_tool_doc(), _make_tool_doc(id="t2")]
        mock_collection.find.return_value = _AsyncCursorMock(docs)

        tools = await repository.get_all_active_tools()
        assert len(tools) == 2


class TestMapToEntity:
    def test_parameter_mapping(self):
        doc = _make_tool_doc(
            parameters=[
                {
                    "name": "q",
                    "type": "string",
                    "description": "query",
                    "required": True,
                    "default_value": None,
                }
            ]
        )
        tool = MongoToolRepository._map_to_entity(doc)
        assert len(tool.parameters) == 1
        assert tool.parameters[0].name == "q"
        assert tool.parameters[0].type == ParameterType.STRING

    def test_http_method_post(self):
        doc = _make_tool_doc(http_method="POST")
        tool = MongoToolRepository._map_to_entity(doc)
        assert tool.http_method == HttpMethod.POST
