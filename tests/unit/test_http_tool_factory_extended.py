"""Testes estendidos para HttpToolFactory — cobertura de _build_description, _resolve_url, _serialize e http_function."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.domain.entities.tool import HttpMethod, ToolParameter, Tool
from src.infrastructure.http.http_tool_factory import (
    HttpToolFactory,
    _resolve_url,
    _serialize,
)


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


# ── _resolve_url ────────────────────────────────────────────────────


class TestResolveUrl:
    def test_no_placeholders(self):
        url, remaining = _resolve_url("http://example.com/api", {"q": "hello"})
        assert url == "http://example.com/api"
        assert remaining == {"q": "hello"}

    def test_with_placeholder(self):
        url, remaining = _resolve_url(
            "http://example.com/api/{user_id}",
            {"param": '{"user_id": "123"}'},
        )
        assert url == "http://example.com/api/123"
        assert "param" not in remaining

    def test_non_dict_value(self):
        url, remaining = _resolve_url(
            "http://example.com/api",
            {"key": "simple_value"},
        )
        assert url == "http://example.com/api"
        assert remaining == {"key": "simple_value"}

    def test_invalid_literal(self):
        url, remaining = _resolve_url(
            "http://example.com/api",
            {"key": "not a dict {bad}"},
        )
        assert url == "http://example.com/api"


# ── _serialize ──────────────────────────────────────────────────────


class TestSerialize:
    def test_json_response(self):
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = {"status": "ok"}
        result = _serialize(resp)
        assert "ok" in result

    def test_text_fallback(self):
        resp = MagicMock(spec=httpx.Response)
        resp.json.side_effect = ValueError("not json")
        resp.text = "plain text"
        result = _serialize(resp)
        assert result == "plain text"


# ── _build_description ──────────────────────────────────────────────


class TestBuildDescription:
    def test_basic_description(self, factory):
        tool = _make_tool()
        desc = factory._build_description(tool)
        assert "Ferramenta de teste" in desc
        assert "GET" in desc

    def test_with_instructions(self, factory):
        tool = _make_tool(instructions="Use com cuidado")
        desc = factory._build_description(tool)
        assert "Use com cuidado" in desc
        assert "Instruções" in desc

    def test_with_parameters(self, factory):
        params = [
            ToolParameter(name="query", type="string", description="Search query", required=True),
            ToolParameter(name="limit", type="integer", description="Max results", required=False),
        ]
        tool = _make_tool(parameters=params)
        desc = factory._build_description(tool)
        assert "query" in desc
        assert "(obrigatório)" in desc
        assert "limit" in desc
        assert "(opcional)" in desc


# ── http_function execution ─────────────────────────────────────────


class TestHttpFunction:
    async def _get_entrypoint(self, factory, tool):
        """Helper: cria toolkit e retorna o entrypoint da função registrada."""
        toolkits = await factory.create_tools_from_configs([tool])
        fn_obj = toolkits[0].async_functions.get("test-tool")
        assert fn_obj is not None
        return fn_obj.entrypoint

    async def test_get_request_success(self, factory):
        tool = _make_tool(http_method=HttpMethod.GET)
        fn = await self._get_entrypoint(factory, tool)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": "ok"}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await fn(q="test")
            assert "ok" in result

    async def test_post_request_success(self, factory):
        tool = _make_tool(http_method=HttpMethod.POST)
        fn = await self._get_entrypoint(factory, tool)

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"created": True}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await fn(data="payload")
            assert "created" in result

    async def test_http_status_error(self, factory):
        tool = _make_tool(http_method=HttpMethod.GET)
        fn = await self._get_entrypoint(factory, tool)

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"
        error = httpx.HTTPStatusError("404", request=MagicMock(), response=mock_response)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(side_effect=error)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await fn()
            assert "Erro HTTP 404" in result

    async def test_request_error(self, factory):
        tool = _make_tool(http_method=HttpMethod.GET)
        fn = await self._get_entrypoint(factory, tool)

        error = httpx.RequestError("timeout", request=MagicMock())

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(side_effect=error)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await fn()
            assert "Erro na requisição" in result

    async def test_unexpected_error(self, factory):
        tool = _make_tool(http_method=HttpMethod.GET)
        fn = await self._get_entrypoint(factory, tool)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(side_effect=RuntimeError("unexpected"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await fn()
            assert "Erro inesperado" in result

    async def test_delete_uses_params(self, factory):
        tool = _make_tool(http_method=HttpMethod.DELETE)
        fn = await self._get_entrypoint(factory, tool)

        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_response.json.return_value = {"deleted": True}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await fn(id="123")
            assert "deleted" in result
