"""Factory de tools HTTP async — implementação de IToolFactory."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import httpx
from agno.tools.function import Function

from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.domain.ports import ILogger, IToolFactory

# ParameterType -> tipo JSON Schema. ``float`` é o nome legado do Mongo para ``number``.
_JSON_SCHEMA_TYPES: Dict[ParameterType, str] = {
    ParameterType.STRING: "string",
    ParameterType.INTEGER: "integer",
    ParameterType.FLOAT: "number",
    ParameterType.BOOLEAN: "boolean",
    ParameterType.OBJECT: "object",
    ParameterType.ARRAY: "array",
}
# tipo JSON Schema -> tipos Python aceitos no argumento já decodificado pelo agno
# (bool é tratado à parte: em Python ele é int, no JSON não é integer nem number)
_PYTHON_TYPES: Dict[str, Tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_DOT_SEGMENTS = (".", "..")
# nomes não declarados na mensagem/log: no máximo 10, cada um com até 64 caracteres
_MAX_UNKNOWN_NAMES = 10
_MAX_NAME_LENGTH = 64


class HttpToolFactory(IToolFactory):
    """Cria uma ``Function`` agno por ``Tool``, executada com httpx async.

    O modelo recebe ``parameters`` = JSON Schema gerado de ``Tool.parameters`` e as
    ``instructions`` da tool vão para o system message do agente
    (``Function.add_instructions``; agno 2.5.8, ``agno/agent/_tools.py``, ``parse_tools``).
    O entrypoint é async: use ``Agent.arun``; no ``Agent.run`` síncrono o agno recusa o
    run com erro explícito (``_raise_if_async_tools_in_list``).

    Com ``skip_entrypoint_processing`` o agno não valida os argumentos: ``http_function``
    confere chaves declaradas, obrigatórios, tipo básico e segmento ``.``/``..`` em
    parâmetro de rota e, em violação, devolve erro curto ao modelo sem chamar o upstream.
    Tool cuja rota tem ``{x}`` sem parâmetro ``x`` obrigatório é recusada na criação.
    """

    def __init__(self, logger: ILogger, *, timeout: float = 30.0) -> None:
        self._logger = logger
        self._timeout = timeout

    # ── IToolFactory ────────────────────────────────────────────────

    async def create_tools_from_configs(self, tools: List[Tool]) -> List[Any]:
        functions: List[Function] = []
        for tool in tools:
            invalid = _invalid_route_placeholders(tool)
            if invalid:
                self._logger.error(
                    "Tool recusada: placeholder da rota sem parâmetro obrigatório declarado",
                    tool_id=tool.id,
                    placeholders=invalid,
                )
                continue
            try:
                functions.append(self._create_function(tool))
            except Exception as exc:
                self._logger.error(
                    "Erro ao criar ferramenta",
                    tool_id=tool.id,
                    tool_name=tool.name,
                    error=str(exc),
                )
        return functions

    # ── private ─────────────────────────────────────────────────────

    def _create_function(self, tool: Tool) -> Function:
        logger = self._logger
        timeout = self._timeout

        async def http_function(**kwargs: Any) -> str:
            """Executa a requisição HTTP para o tool."""
            try:
                arguments = _validated_arguments(tool, kwargs)
            except _ArgumentError as exc:
                logger.warning(
                    "Argumentos inválidos no tool call",
                    tool_id=tool.id,
                    arguments=exc.names,
                )
                return f"Erro nos argumentos da tool {tool.id}: {exc}"
            headers = (tool.headers or {}).copy()
            headers.setdefault("Content-Type", "application/json")
            url, remaining = _resolve_url(tool.route, arguments)
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
                # timeout do httpx costuma vir com str(exc) vazio
                detail = str(exc) or type(exc).__name__
                logger.error(
                    "Request error",
                    tool_id=tool.id,
                    error_type=type(exc).__name__,
                    error=detail,
                )
                if isinstance(exc, httpx.TimeoutException):
                    return f"Erro na requisição: timeout ao chamar a tool ({type(exc).__name__})"
                return f"Erro na requisição: {detail}"
            except Exception as exc:
                logger.error(
                    "Erro inesperado",
                    tool_id=tool.id,
                    error=str(exc),
                )
                return f"Erro inesperado: {exc}"

        instructions = _build_instructions(tool)
        return Function(
            name=tool.id,
            description=tool.description,
            parameters=build_parameters_schema(tool.parameters),
            instructions=instructions,
            add_instructions=instructions is not None,
            entrypoint=http_function,
            # sem isto o agno recalcula ``required`` pela assinatura (**kwargs) e
            # sobrescreve o schema (agno/tools/function.py, ``process_entrypoint``)
            skip_entrypoint_processing=True,
        )


# ── helpers puros ───────────────────────────────────────────────────


def build_parameters_schema(parameters: List[ToolParameter]) -> Dict[str, Any]:
    """JSON Schema (objeto) dos argumentos da tool, como o modelo deve chamá-la.

    Só o que a entidade expressa: nome, tipo, descrição e ``required``. ``array`` leva
    ``items: {}`` (qualquer item): OpenAI e Gemini exigem ``items`` em array.
    ``additionalProperties: false`` para o modelo não inventar argumento.
    """
    properties: Dict[str, Any] = {}
    for param in parameters:
        prop: Dict[str, Any] = {"type": _JSON_SCHEMA_TYPES[param.type]}
        if param.type is ParameterType.ARRAY:
            prop["items"] = {}
        prop["description"] = param.description
        properties[param.name] = prop
    return {
        "type": "object",
        "properties": properties,
        "required": [p.name for p in parameters if p.required],
        "additionalProperties": False,
    }


def _build_instructions(tool: Tool) -> str | None:
    """Texto que o agno põe no system message; ``None`` se a tool não tem instruções."""
    text = (tool.instructions or "").strip()
    if not text:
        return None
    return f"Instruções da tool {tool.id}: {text}"


def _invalid_route_placeholders(tool: Tool) -> List[str]:
    """Placeholders ``{x}`` da rota sem parâmetro ``x`` obrigatório declarado (ordem da rota)."""
    required = {p.name for p in tool.parameters if p.required}
    names = dict.fromkeys(_PLACEHOLDER.findall(tool.route))
    return [name for name in names if name not in required]


class _ArgumentError(ValueError):
    """Argumentos do tool call fora do contrato da tool; ``names`` vai para o log (sem valores)."""

    def __init__(self, message: str, names: List[str]) -> None:
        super().__init__(message)
        self.names = names


def _has_json_type(value: Any, expected: str) -> bool:
    if isinstance(value, bool):
        return expected == "boolean"
    return isinstance(value, _PYTHON_TYPES[expected])


def _validated_arguments(tool: Tool, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Confere os argumentos do modelo contra ``Tool.parameters`` (sem engine de JSON Schema).

    Só chaves declaradas; ``null`` em opcional conta como ausente (o agno também troca as
    strings "null"/"none" por ``None``: indistinguíveis); obrigatórios presentes;
    tipo básico pelo ``type`` declarado; parâmetro de rota não pode ser ``.`` nem ``..``
    (o ``quote`` não codifica ponto e o segmento seria normalizado pelo httpx).
    """
    declared = {p.name: p for p in tool.parameters}
    unknown = sorted(name for name in kwargs if name not in declared)
    if unknown:
        shown = [name[:_MAX_NAME_LENGTH] for name in unknown[:_MAX_UNKNOWN_NAMES]]
        if len(unknown) > _MAX_UNKNOWN_NAMES:
            shown.append("...")
        raise _ArgumentError(f"argumento não declarado: {', '.join(shown)}", shown)
    arguments: Dict[str, Any] = {}
    for name, value in kwargs.items():
        if value is None:
            continue
        # agno 2.5.8 (agno/utils/functions.py, get_function_call) troca as strings
        # "true"/"false" por bool antes do entrypoint: para ``string`` declarado, desfaz
        if isinstance(value, bool) and declared[name].type is ParameterType.STRING:
            value = "true" if value else "false"
        arguments[name] = value
    missing = [p.name for p in tool.parameters if p.required and p.name not in arguments]
    if missing:
        raise _ArgumentError(f"argumento obrigatório ausente: {', '.join(missing)}", missing)
    for name, value in arguments.items():
        expected = _JSON_SCHEMA_TYPES[declared[name].type]
        if not _has_json_type(value, expected):
            raise _ArgumentError(f"tipo inválido para {name}: esperado {expected}", [name])
    for name in _PLACEHOLDER.findall(tool.route):
        if arguments.get(name) in _DOT_SEGMENTS:
            raise _ArgumentError(
                f"valor inválido para {name}: '.' e '..' não são permitidos na rota", [name]
            )
    return arguments


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
    """Troca ``{nome}`` da rota pelo argumento ``nome`` e devolve os argumentos restantes.

    O valor vai percent-encoded por inteiro (``/``, ``?``, ``#`` não mudam a rota).
    """
    url = route
    remaining = kwargs.copy()
    for key, value in kwargs.items():
        placeholder = f"{{{key}}}"
        if placeholder in url:
            url = url.replace(placeholder, quote(str(value), safe=""))
            remaining.pop(key)
    return url, remaining


def _serialize(response: httpx.Response) -> str:
    try:
        return str(response.json())
    except (ValueError, httpx.DecodingError):
        return response.text
