"""Testes para HttpToolFactory (infrastructure/runtime/agno)."""

from __future__ import annotations

import pytest

from src.domain.entities.tool import HttpMethod, Tool
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory


@pytest.fixture
def factory(mock_logger):
    return HttpToolFactory(logger=mock_logger)


def _make_tool(**overrides) -> Tool:
    defaults = dict(
        id="test-tool",
        name="Test Tool",
        description="Ferramenta de teste",
        route="http://example.com/api/test",
        http_method=HttpMethod.GET,
        parameters=[],
    )
    defaults.update(overrides)
    return Tool(**defaults)


class TestHttpToolFactory:
    async def test_create_tools_from_configs(self, factory):
        tools = [_make_tool(id="t1"), _make_tool(id="t2")]
        result = await factory.create_tools_from_configs(tools)
        assert len(result) == 2

    async def test_create_tools_empty_list(self, factory):
        result = await factory.create_tools_from_configs([])
        assert result == []

    async def test_function_registered_with_tool_id(self, factory):
        tool = _make_tool(id="my-tool")
        result = await factory.create_tools_from_configs([tool])
        assert len(result) == 1
        assert result[0].name == "my-tool"
