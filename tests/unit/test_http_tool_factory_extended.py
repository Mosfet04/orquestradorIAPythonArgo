"""Testes estendidos para HttpToolFactory — cobertura de schema, instruções, _resolve_url, _serialize e http_function."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.infrastructure.runtime.agno.http_tool_factory import (
    HttpToolFactory,
    _build_instructions,
    _resolve_url,
    _serialize,
    build_parameters_schema,
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


def _param(name: str) -> ToolParameter:
    return ToolParameter(name=name, type=ParameterType.STRING, description=name)


# ── _resolve_url ────────────────────────────────────────────────────


class TestResolveUrl:
    def test_no_placeholders(self):
        url, remaining = _resolve_url("http://example.com/api", {"q": "hello"})
        assert url == "http://example.com/api"
        assert remaining == {"q": "hello"}

    def test_with_placeholder(self):
        url, remaining = _resolve_url(
            "http://example.com/api/{user_id}",
            {"user_id": "123", "q": "x"},
        )
        assert url == "http://example.com/api/123"
        assert remaining == {"q": "x"}

    def test_placeholder_value_is_percent_encoded(self):
        url, remaining = _resolve_url(
            "http://example.com/api/{user_id}/orders",
            {"user_id": "a/../b?c=1#d {e}"},
        )
        assert url == "http://example.com/api/a%2F..%2Fb%3Fc%3D1%23d%20%7Be%7D/orders"
        assert remaining == {}

    def test_non_string_placeholder_value(self):
        url, _ = _resolve_url("http://example.com/api/{n}", {"n": 42})
        assert url == "http://example.com/api/42"

    def test_legacy_dict_literal_is_not_parsed(self):
        """O hack ``'{"user_id": ...}'`` existia só pelo parâmetro ``kwargs`` antigo."""
        url, remaining = _resolve_url(
            "http://example.com/api/{user_id}",
            {"param": '{"user_id": "123"}'},
        )
        assert url == "http://example.com/api/{user_id}"
        assert remaining == {"param": '{"user_id": "123"}'}

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


# ── schema e instruções ─────────────────────────────────────────────


class TestParametersSchema:
    def test_types_required_and_descriptions(self):
        params = [
            ToolParameter(name="query", type=ParameterType.STRING, description="Search query", required=True),
            ToolParameter(name="limit", type=ParameterType.INTEGER, description="Max results"),
        ]
        assert build_parameters_schema(params) == {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results"},
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    async def test_function_uses_tool_description_and_schema(self, factory):
        params = [ToolParameter(name="q", type=ParameterType.STRING, description="Busca", required=True)]
        (fn,) = await factory.create_tools_from_configs([_make_tool(parameters=params)])
        assert fn.name == "test-tool"
        assert fn.description == "Ferramenta de teste"
        assert fn.parameters == build_parameters_schema(params)

    async def test_invalid_parameter_type_skips_tool_with_error_log(self, factory, mock_logger):
        params = [ToolParameter(name="q", type="texto", description="Busca")]  # type: ignore[arg-type]
        result = await factory.create_tools_from_configs([_make_tool(id="ruim", parameters=params), _make_tool()])
        assert [f.name for f in result] == ["test-tool"]
        assert mock_logger.error.call_args.kwargs["tool_id"] == "ruim"


class TestBuildInstructions:
    def test_with_instructions(self):
        assert _build_instructions(_make_tool(instructions="  Use com cuidado ")) == (
            "Instruções da tool test-tool: Use com cuidado"
        )

    @pytest.mark.parametrize("instructions", [None, "", "   "])
    def test_without_instructions(self, instructions):
        assert _build_instructions(_make_tool(instructions=instructions)) is None


# ── http_function execution ─────────────────────────────────────────


class TestHttpFunction:
    async def _get_entrypoint(self, factory, tool):
        """Helper: cria a ``Function`` e retorna o entrypoint dela."""
        (fn_obj,) = await factory.create_tools_from_configs([tool])
        assert fn_obj.name == "test-tool"
        return fn_obj.entrypoint

    async def test_get_request_success(self, factory):
        tool = _make_tool(http_method=HttpMethod.GET, parameters=[_param("q")])
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
        tool = _make_tool(http_method=HttpMethod.POST, parameters=[_param("data")])
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
            assert result == "Erro na requisição: falha ao chamar a tool (RequestError)"

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
            assert result == "Erro inesperado ao chamar a tool (RuntimeError)"

    async def test_delete_uses_params(self, factory):
        tool = _make_tool(http_method=HttpMethod.DELETE, parameters=[_param("id")])
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
