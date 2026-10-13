"""Testes para a tool de busca hierárquica (Toolkit async)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from agno.tools import Toolkit

from src.domain.entities.search_result import SearchResult
from src.infrastructure.runtime.agno.hierarchical_search_tool import (
    create_hierarchical_search_tool,
)
from tests.fakes import RecordingLogger

SEARCH_ERROR = "Erro ao buscar no knowledge base; a busca não está disponível agora."


def _get_func(toolkit: Toolkit):
    """Retorna o Function registrado (async_functions para async def)."""
    return toolkit.async_functions["search_knowledge"]


class TestCreateHierarchicalSearchTool:
    """Verifica que create_hierarchical_search_tool retorna Toolkit funcional."""

    def setup_method(self):
        self.mock_strategy = AsyncMock()

    def test_returns_toolkit(self):
        toolkit = create_hierarchical_search_tool(self.mock_strategy, logger=RecordingLogger())
        assert isinstance(toolkit, Toolkit)

    def test_toolkit_has_search_knowledge_function(self):
        toolkit = create_hierarchical_search_tool(self.mock_strategy, logger=RecordingLogger())
        assert "search_knowledge" in toolkit.async_functions

    @pytest.mark.asyncio
    async def test_search_returns_formatted_results(self):
        results = [
            SearchResult(
                content="Conteúdo sobre SOLID",
                score=0.95,
                node_id="doc::node::0",
                metadata={"title": "SOLID Principles", "level": "1"},
            ),
            SearchResult(
                content="Outro trecho relevante",
                score=0.80,
                node_id="doc::node::1",
                metadata={"title": "Clean Code", "level": "1"},
            ),
        ]
        self.mock_strategy.search = AsyncMock(return_value=results)

        toolkit = create_hierarchical_search_tool(self.mock_strategy, logger=RecordingLogger())
        func = _get_func(toolkit)
        output = await func.entrypoint(query="princípios SOLID")

        assert "SOLID Principles" in output
        assert "Conteúdo sobre SOLID" in output
        assert "Clean Code" in output
        assert "score: 0.95" in output
        self.mock_strategy.search.assert_awaited_once_with("princípios SOLID", top_k=5)

    @pytest.mark.asyncio
    async def test_search_returns_no_results_message(self):
        self.mock_strategy.search = AsyncMock(return_value=[])

        toolkit = create_hierarchical_search_tool(self.mock_strategy, logger=RecordingLogger())
        func = _get_func(toolkit)
        output = await func.entrypoint(query="algo inexistente")

        assert "Nenhuma informação relevante" in output

    @pytest.mark.asyncio
    async def test_search_returns_error_message_on_exception(self):
        self.mock_strategy.search = AsyncMock(
            side_effect=RuntimeError("MongoDB unreachable")
        )

        toolkit = create_hierarchical_search_tool(self.mock_strategy, logger=RecordingLogger())
        func = _get_func(toolkit)
        output = await func.entrypoint(query="qualquer coisa")

        # Texto de driver/SDK não vai ao modelo, ao cliente AG-UI nem à sessão (F2-04, R1).
        assert output == SEARCH_ERROR
        assert "MongoDB unreachable" not in output

    @pytest.mark.asyncio
    async def test_custom_top_k(self):
        self.mock_strategy.search = AsyncMock(return_value=[])

        toolkit = create_hierarchical_search_tool(self.mock_strategy, top_k=3, logger=RecordingLogger())
        func = _get_func(toolkit)
        await func.entrypoint(query="test")

        self.mock_strategy.search.assert_awaited_once_with("test", top_k=3)

    @pytest.mark.asyncio
    async def test_results_use_node_id_when_no_title(self):
        results = [
            SearchResult(
                content="Conteúdo sem título",
                score=0.70,
                node_id="doc::node::5",
                metadata={"level": "0"},
            ),
        ]
        self.mock_strategy.search = AsyncMock(return_value=results)

        toolkit = create_hierarchical_search_tool(self.mock_strategy, logger=RecordingLogger())
        func = _get_func(toolkit)
        output = await func.entrypoint(query="busca")

        assert "doc::node::5" in output


async def test_falha_da_busca_vai_ao_log_com_o_tipo_sem_o_texto():
    """F2-04: o ``except Exception`` da tool loga (tipo, sem o texto, que pode vir de driver/SDK)."""
    strategy = AsyncMock()
    strategy.search = AsyncMock(side_effect=RuntimeError("mongodb://usuario:senha@host"))
    logger = RecordingLogger()

    output = await _get_func(create_hierarchical_search_tool(strategy, logger=logger)).entrypoint(query="q")

    assert output == SEARCH_ERROR
    assert "senha" not in output
    assert [(r.level, r.message, r.context) for r in logger.records] == [
        ("warning", "Erro na busca hierárquica; a tool devolve o erro ao modelo", {"error_type": "RuntimeError"})
    ]
