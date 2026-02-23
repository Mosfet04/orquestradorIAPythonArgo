"""Factory de tools HTTP async — implementação de IToolFactory."""

from __future__ import annotations

import ast
from typing import Any, Dict, List, Tuple

import httpx
from agno.tools import Toolkit

from src.domain.entities.tool import HttpMethod, Tool
from src.domain.ports import ILogger, IToolFactory


class HttpToolFactory(IToolFactory):
    """Cria ``Toolkit`` agno a partir de ``Tool`` configs usando httpx async."""

    def __init__(self, logger: ILogger, *, timeout: float = 30.0) -> None:
        self._logger = logger
        self._timeout = timeout

    # ── IToolFactory ────────────────────────────────────────────────

    async def create_tools_from_configs(self, tools: List[Tool]) -> List[Any]:
        toolkits: List[Toolkit] = []
        for tool in tools:
            try:
                toolkits.append(self._create_toolkit(tool))
            except Exception as exc:
                self._logger.error(
                    "Erro ao criar ferramenta",
                    tool_id=tool.id,
                    tool_name=tool.name,
                    error=str(exc),
                )
        return toolkits

    # ── private ─────────────────────────────────────────────────────

    def _create_toolkit(self, tool: Tool) -> Toolkit:
        logger = self._logger
        timeout = self._timeout

        async def http_function(**kwargs: Any) -> str:
            """Executa a requisição HTTP para o tool."""
            headers = (tool.headers or {}).copy()
            headers.setdefault("Content-Type", "application/json")
            url, remaining = _resolve_url(tool.route, kwargs)
            req_kwargs = _build_request_kwargs(tool, url, headers, remaining, timeout)

            try:
                async with httpx.AsyncClient(verify=True) as client:
                    response = await client.request(**req_kwargs)
                    response.raise_for_status()
                logger.info(
                    "HTTP OK",
                    tool_id=tool.id,
                    status=response.status_code,
                )
                return _serialize(response)
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "HTTP error",
                    tool_id=tool.id,
                    status=getattr(exc.response, "status_code", None),
                )
                return _format_http_error(exc)
            except httpx.RequestError as exc:
                logger.error(
                    "Request error",
                    tool_id=tool.id,
                    error=str(exc),
                )
                return f"Erro na requisição: {exc}"
            except Exception as exc:
                logger.error(
                    "Erro inesperado",
                    tool_id=tool.id,
                    error=str(exc),
                )
                return f"Erro inesperado: {exc}"

        description = self._build_description(tool)
        toolkit = Toolkit(name=tool.id, instructions=description)
        toolkit.register(function=http_function, name=tool.id)
        return toolkit

    @staticmethod
    def _build_description(tool: Tool) -> str:
        parts = [tool.description]
        if tool.instructions:
            parts.append(f"\nInstruções: {tool.instructions}")
        parts.append(f"\nRota: {tool.http_method.value} {tool.route}")
        if tool.parameters:
            parts.append("\nParâmetros:")
            for p in tool.parameters:
                req = " (obrigatório)" if p.required else " (opcional)"
                parts.append(f"  - {p.name}: {p.description}{req}")
        return "\n".join(parts)


# ── helpers puros ───────────────────────────────────────────────────


def _build_request_kwargs(
    tool: Tool,
    url: str,
    headers: Dict[str, str],
    remaining: Dict[str, Any],
    timeout: float,
) -> Dict[str, Any]:
    """Constrói o dicionário de kwargs para ``httpx.AsyncClient.request``."""
    req_kwargs: Dict[str, Any] = {
        "method": tool.http_method.value,
        "url": url,
        "headers": headers,
        "timeout": timeout,
    }
    if tool.http_method in (HttpMethod.GET, HttpMethod.DELETE):
        req_kwargs["params"] = remaining or None
    else:
        req_kwargs["json"] = remaining or None
    return req_kwargs


def _format_http_error(exc: httpx.HTTPStatusError) -> str:
    """Formata mensagem de erro para respostas HTTP com status de erro."""
    resp = exc.response
    code = resp.status_code if resp else "?"
    text = resp.text if resp else ""
    return f"Erro HTTP {code}: {text}"


def _resolve_url(
    route: str, kwargs: Dict[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    url = route
    remaining = kwargs.copy()
    for key, value in kwargs.items():
        try:
            parsed = ast.literal_eval(value) if isinstance(value, str) else value
        except (ValueError, SyntaxError):
            parsed = value
        if isinstance(parsed, dict) and parsed:
            param_key = next(iter(parsed))
            placeholder = f"{{{param_key}}}"
            if placeholder in url:
                url = url.replace(placeholder, str(parsed[param_key]))
                remaining.pop(key, None)
    return url, remaining


def _serialize(response: httpx.Response) -> str:
    try:
        return str(response.json())
    except (ValueError, httpx.DecodingError):
        return response.text
