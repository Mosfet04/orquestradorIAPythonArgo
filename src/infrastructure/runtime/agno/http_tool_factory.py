"""Factory de tools HTTP async — implementação de IToolFactory."""

from __future__ import annotations

import asyncio
import json
import random
import re
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from agno.tools.function import Function

from src.domain.entities.model_config import is_secret_ref
from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.domain.ports import ILogger, IToolFactory
from src.infrastructure.config.secrets import DEFAULT_SECRETS_DIR, UnresolvedHeaderError, resolve_header_refs
from src.infrastructure.security.guarded_http import GuardedAsyncTransport, cookieless_jar
from src.infrastructure.security.ssrf import BlockedDestinationError, DestinationRule

# ParameterType -> tipo JSON Schema. ``float`` é o nome legado do Mongo para ``number``.
_JSON_SCHEMA_TYPES: dict[ParameterType, str] = {
    ParameterType.STRING: "string",
    ParameterType.INTEGER: "integer",
    ParameterType.FLOAT: "number",
    ParameterType.BOOLEAN: "boolean",
    ParameterType.OBJECT: "object",
    ParameterType.ARRAY: "array",
}
# tipo JSON Schema -> tipos Python aceitos no argumento já decodificado pelo agno
# (bool é tratado à parte: em Python ele é int, no JSON não é integer nem number)
_PYTHON_TYPES: dict[str, tuple[type, ...]] = {
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

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
"""Corpo (já descomprimido) que a tool lê do upstream; acima disso, erro curto ao modelo."""
_ACCEPT_ENCODING = "gzip, deflate"
# gzip e zlib ("deflate" do HTTP): ``wbits`` com 32 detecta o cabeçalho de um ou de outro.
_ZLIB_ENCODINGS = frozenset({"gzip", "x-gzip", "deflate"})
_IDENTITY_ENCODINGS = frozenset({"", "identity"})
DEFAULT_RETRIES = 2
DEFAULT_RETRY_BACKOFF_SECONDS = 0.25
_MAX_BACKOFF_SECONDS = 5.0
_IDEMPOTENT = frozenset({HttpMethod.GET, HttpMethod.PUT, HttpMethod.DELETE})
_RETRY_STATUS = frozenset({502, 503, 504})
# Antes de qualquer byte do request sair: repetir é seguro até em POST/PATCH.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_TRANSIENT = (*_NOT_SENT, httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError)
_JITTER = random.SystemRandom()  # jitter de backoff, não segredo; o do sistema evita o aviso do bandit


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

    Header com valor por referência (``env:``/``file:``, FA-06) é resolvido a cada chamada (``file:``
    fora do event loop), nunca na criação: rotação do segredo vale no request seguinte. Referência que
    não resolve: o upstream não é chamado e o log leva só o nome do header e o tipo do erro. Redirect
    nunca é seguido (``follow_redirects=False`` explícito): o destino é só o da ``route``.

    F4-01: um ``httpx.AsyncClient`` para todas as tools, criado na primeira chamada sobre ``transport``
    (no app, o ``GuardedAsyncTransport`` com a regra da ``TOOL_ROUTE_ALLOWLIST``: o destino é conferido na
    conexão e o TCP abre no IP conferido) e fechado em ``aclose`` (shutdown). Sem ``transport``, o mesmo
    transporte guardado com a regra padrão (só endereço público). Limites: ``timeout`` por request e
    ``max_response_bytes`` do corpo; retry com backoff exponencial e jitter (``retries`` vezes) em falha
    transiente ou 502/503/504, só em método idempotente (GET/PUT/DELETE) ou quando a conexão nem abriu.
    ``timeout`` vale por operação do httpx e também como prazo total de cada tentativa (upstream que
    goteja bytes não segura a tool); o pior caso é ``(retries + 1) * timeout`` mais o backoff.
    O corpo é lido cru e descomprimido aqui (gzip/deflate, os do ``Accept-Encoding`` enviado) com saída
    limitada ao que falta do ``max_response_bytes``: um gzip pequeno que abre em gigas não chega a abrir.
    Outro ``Content-Encoding`` é recusado com erro curto. O cliente não guarda cookie: um ``Set-Cookie``
    de uma tool não vai na chamada de outra (nem de outro agente ou usuário).
    Placeholder só no caminho e na query: no esquema ou no ``host[:porta]`` a tool é recusada na criação
    (documento legado; a API admin já recusa na escrita). A query fixa da ``route`` é mantida junto dos
    argumentos de GET/DELETE.

    Falha de rede ou erro inesperado: o resultado devolvido leva só o tipo da exceção, nunca o
    texto (o do httpx/h11 pode citar um header inteiro, e o resultado vai ao contexto do LLM, ao
    cliente AG-UI, a ``agno_sessions`` e ao span). Resposta com status de erro devolve o corpo do
    upstream, que é dado da própria tool.
    """

    def __init__(
        self,
        logger: ILogger,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        secrets_dir: str = DEFAULT_SECRETS_DIR,
        transport: httpx.AsyncBaseTransport | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        retries: int = DEFAULT_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    ) -> None:
        """``secrets_dir``: raiz das referências ``file:`` (``SECRETS_DIR``). ``transport``: do composition root."""
        self._logger = logger
        self._secrets_dir = secrets_dir
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._closed = False
        self._limits = _CallLimits(timeout, max_response_bytes, retries, retry_backoff)

    async def aclose(self) -> None:
        """Fecha o cliente compartilhado e o transporte (o injetado também, se nenhuma chamada saiu); idempotente.

        Depois dele, chamada de tool não sai (erro curto ao modelo), em vez de reabrir com outra regra.
        """
        if self._closed:
            return
        self._closed = True
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()  # fecha o transporte dele
        elif self._transport is not None:
            await self._transport.aclose()

    def _shared_client(self) -> httpx.AsyncClient:
        if self._closed:
            raise RuntimeError("cliente HTTP das tools já fechado (shutdown)")
        if self._client is None:
            # Sem transport injetado (uso avulso): o guardado com a regra padrão; monta o TLS (lê disco) uma vez.
            transport = self._transport or GuardedAsyncTransport(DestinationRule())
            self._client = httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                cookies=cookieless_jar(),
                headers={"Accept-Encoding": _ACCEPT_ENCODING},
            )
        return self._client

    # ── IToolFactory ────────────────────────────────────────────────

    async def create_tools_from_configs(self, tools: list[Tool]) -> list[Any]:
        """Cria uma ``Function`` do agno por tool HTTP; tool inválida é descartada com log."""
        functions: list[Function] = []
        for tool in tools:
            if tool.is_mcp:  # a route é o servidor MCP, não um endpoint a chamar com http_method
                self._logger.error("Tool MCP recusada pela fábrica HTTP (vai pela fábrica MCP)", tool_id=tool.id)
                continue
            invalid = _invalid_route_placeholders(tool)
            if invalid:
                self._logger.error(
                    "Tool recusada: placeholder da rota sem parâmetro obrigatório declarado",
                    tool_id=tool.id,
                    placeholders=invalid,
                )
                continue
            if _placeholder_outside_path(tool.route):
                self._logger.error(
                    "Tool recusada: placeholder no esquema ou no host da rota (só no caminho e na query)",
                    tool_id=tool.id,
                )
                continue
            try:
                functions.append(self._create_function(tool))
            except Exception as exc:  # noqa: BLE001 - logado; uma tool inválida não derruba as outras
                self._logger.error(
                    "Erro ao criar ferramenta",
                    tool_id=tool.id,
                    tool_name=tool.name,
                    error_type=type(exc).__name__,
                )
        return functions

    # ── private ─────────────────────────────────────────────────────

    def _create_function(self, tool: Tool) -> Function:
        logger = self._logger
        limits = self._limits
        secrets_dir = self._secrets_dir
        shared_client = self._shared_client
        configured_headers = tool.headers or {}
        # Sem referência (o caso comum), a chamada não paga nada além da cópia de antes.
        has_refs = any(is_secret_ref(value) for value in configured_headers.values())

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
            try:
                headers = (
                    await resolve_header_refs(configured_headers, secrets_dir) if has_refs else dict(configured_headers)
                )
            except UnresolvedHeaderError as exc:
                logger.error(
                    "Header por referência da tool não resolvido; a chamada não saiu",
                    tool_id=tool.id,
                    header=exc.name,
                    error_type=exc.error_type,
                )
                return f"Erro na tool {tool.id}: header por referência não resolvido ({exc.name})"
            return await _send(shared_client, tool, arguments, headers, limits, logger)

        instructions = build_instructions(tool)
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


def build_parameters_schema(parameters: list[ToolParameter]) -> dict[str, Any]:
    """JSON Schema (objeto) dos argumentos da tool, como o modelo deve chamá-la.

    Só o que a entidade expressa: nome, tipo, descrição e ``required``. ``array`` leva
    ``items: {}`` (qualquer item): OpenAI e Gemini exigem ``items`` em array.
    ``additionalProperties: false`` para o modelo não inventar argumento.
    """
    properties: dict[str, Any] = {}
    for param in parameters:
        prop: dict[str, Any] = {"type": _JSON_SCHEMA_TYPES[param.type]}
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


def build_instructions(tool: Tool) -> str | None:
    """Texto que o agno põe no system message; ``None`` se a tool não tem instruções."""
    text = (tool.instructions or "").strip()
    if not text:
        return None
    return f"Instruções da tool {tool.id}: {text}"


def _invalid_route_placeholders(tool: Tool) -> list[str]:
    """Placeholders ``{x}`` da rota sem parâmetro ``x`` obrigatório declarado (ordem da rota)."""
    required = {p.name for p in tool.parameters if p.required}
    names = dict.fromkeys(_PLACEHOLDER.findall(tool.route))
    return [name for name in names if name not in required]


def _placeholder_outside_path(route: str) -> bool:
    """``True`` se há ``{`` ou ``}`` no esquema ou no ``host[:porta]`` (o modelo escolheria o destino)."""
    scheme, separator, rest = route.partition("://")
    authority = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0] if separator else route
    return any(ch in scheme + authority for ch in "{}")


class _ArgumentError(ValueError):
    """Argumentos do tool call fora do contrato da tool; ``names`` vai para o log (sem valores)."""

    def __init__(self, message: str, names: list[str]) -> None:
        super().__init__(message)
        self.names = names


def _has_json_type(value: Any, expected: str) -> bool:
    if isinstance(value, bool):
        return expected == "boolean"
    return isinstance(value, _PYTHON_TYPES[expected])


def _validated_arguments(tool: Tool, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Confere os argumentos do modelo contra ``Tool.parameters`` (sem engine de JSON Schema).

    Só chaves declaradas; ``null`` em opcional conta como ausente (o agno também troca as
    strings "null"/"none" por ``None``: indistinguíveis); obrigatórios presentes;
    tipo básico pelo ``type`` declarado; parâmetro de rota não pode ser ``.`` nem ``..``
    (o ``quote`` não codifica ponto e o segmento seria normalizado pelo httpx) nem vazio
    (``/clientes/{id}`` viraria ``/clientes/``, outro recurso).
    """
    declared = {p.name: p for p in tool.parameters}
    _reject_unknown(declared, kwargs)
    arguments = _present_arguments(declared, kwargs)
    _reject_missing(tool, arguments)
    _reject_wrong_types(declared, arguments)
    _reject_dot_segments(tool, arguments)
    return arguments


def _reject_unknown(declared: dict[str, ToolParameter], kwargs: dict[str, Any]) -> None:
    """Recusa chave não declarada; o log leva até ``_MAX_UNKNOWN_NAMES`` nomes truncados."""
    unknown = sorted(name for name in kwargs if name not in declared)
    if unknown:
        shown = [name[:_MAX_NAME_LENGTH] for name in unknown[:_MAX_UNKNOWN_NAMES]]
        if len(unknown) > _MAX_UNKNOWN_NAMES:
            shown.append("...")
        raise _ArgumentError(f"argumento não declarado: {', '.join(shown)}", shown)


def _present_arguments(declared: dict[str, ToolParameter], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Argumentos sem os ``None`` (ausentes), com o booleano de ``string`` desfeito."""
    arguments: dict[str, Any] = {}
    for name, value in kwargs.items():
        if value is None:
            continue
        # agno 2.5.8 (agno/utils/functions.py, get_function_call) troca as strings
        # "true"/"false" por bool antes do entrypoint: para ``string`` declarado, desfaz
        if isinstance(value, bool) and declared[name].type is ParameterType.STRING:
            value = "true" if value else "false"
        arguments[name] = value
    return arguments


def _reject_missing(tool: Tool, arguments: dict[str, Any]) -> None:
    """Recusa a falta de argumento obrigatório."""
    missing = [p.name for p in tool.parameters if p.required and p.name not in arguments]
    if missing:
        raise _ArgumentError(f"argumento obrigatório ausente: {', '.join(missing)}", missing)


def _reject_wrong_types(declared: dict[str, ToolParameter], arguments: dict[str, Any]) -> None:
    """Recusa argumento fora do tipo básico declarado."""
    for name, value in arguments.items():
        expected = _JSON_SCHEMA_TYPES[declared[name].type]
        if not _has_json_type(value, expected):
            raise _ArgumentError(f"tipo inválido para {name}: esperado {expected}", [name])


def _reject_dot_segments(tool: Tool, arguments: dict[str, Any]) -> None:
    """Recusa ``.``, ``..`` e vazio em parâmetro de rota (o httpx normalizaria o segmento; vazio muda o recurso)."""
    for name in _PLACEHOLDER.findall(tool.route):
        if arguments.get(name) in _DOT_SEGMENTS:
            raise _ArgumentError(
                f"valor inválido para {name}: '.' e '..' não são permitidos na rota", [name]
            )
        if arguments.get(name) == "":
            raise _ArgumentError(f"valor inválido para {name}: vazio não é permitido na rota", [name])


@dataclass(frozen=True)
class _CallLimits:
    """Limites de uma chamada: timeout (por operação e por tentativa), corpo máximo e retry."""

    timeout: float
    max_response_bytes: int
    retries: int
    retry_backoff: float


class _ResponseTooLarge(Exception):
    """Corpo do upstream acima de ``max_response_bytes``."""


class _UnsupportedEncoding(Exception):
    """``Content-Encoding`` da resposta fora de gzip/deflate/identity."""


def backoff_delay(attempt: int, base: float, uniform: Callable[[float, float], float] = _JITTER.uniform) -> float:
    """Espera antes da repetição ``attempt`` (0, 1...): "full jitter" entre 0 e ``base * 2**attempt`` (teto 5 s)."""
    return uniform(0.0, min(_MAX_BACKOFF_SECONDS, base * 2**attempt))


def _retryable_error(method: HttpMethod, exc: httpx.RequestError) -> bool:
    if isinstance(exc, _NOT_SENT):
        return True
    return method in _IDEMPOTENT and isinstance(exc, _TRANSIENT)


async def _send(
    shared_client: Callable[[], httpx.AsyncClient],
    tool: Tool,
    arguments: dict[str, Any],
    headers: dict[str, str],
    limits: _CallLimits,
    logger: ILogger,
) -> str:
    """Faz a requisição da tool; toda falha vira log nosso e resultado sem o texto da exceção."""
    try:
        # Montar a requisição também pode falhar (ex.: argumento de rota que o quote não
        # codifica): dentro do try, para a falha ter log nosso e resultado sem o texto dela.
        headers.setdefault("Content-Type", "application/json")
        url, remaining = _resolve_url(tool.route, arguments)
        req_kwargs = _build_request_kwargs(tool, url, headers, remaining, limits.timeout)
        status, body, encoding = await _send_with_retry(shared_client(), tool, req_kwargs, limits, logger)
        return _result(tool, status, body, encoding, logger)
    except BlockedDestinationError as exc:
        logger.error("Destino da tool recusado pela guarda de rede", tool_id=tool.id, error_type=type(exc).__name__)
        return f"Erro na tool {tool.id}: destino recusado pela guarda de rede"
    except _ResponseTooLarge:
        logger.error("Resposta da tool acima do limite", tool_id=tool.id, limit=limits.max_response_bytes)
        return f"Erro na tool {tool.id}: resposta acima do limite de {limits.max_response_bytes} bytes"
    except _UnsupportedEncoding:
        logger.error("Content-Encoding da resposta da tool sem suporte", tool_id=tool.id)
        return f"Erro na tool {tool.id}: Content-Encoding da resposta sem suporte"
    except httpx.RequestError as exc:
        logger.error(
            "Request error",
            tool_id=tool.id,
            error_type=type(exc).__name__,
        )
        kind = "timeout" if isinstance(exc, httpx.TimeoutException) else "falha"
        return f"Erro na requisição: {kind} ao chamar a tool ({type(exc).__name__})"
    except Exception as exc:  # noqa: BLE001 - logado; o modelo recebe o erro como resultado da tool
        logger.error(
            "Erro inesperado",
            tool_id=tool.id,
            error_type=type(exc).__name__,
        )
        return f"Erro inesperado ao chamar a tool ({type(exc).__name__})"


def _result(tool: Tool, status: int, body: bytes, encoding: str, logger: ILogger) -> str:
    """Resultado ao modelo: o corpo de erro como texto; o de sucesso, serializado."""
    if not 200 <= status < 300:
        logger.error("HTTP error", tool_id=tool.id, status=status)
        return f"Erro HTTP {status}: {body.decode(encoding, errors='replace')}"
    logger.info("HTTP OK", tool_id=tool.id, status=status)
    return _serialize(body, encoding)


async def _send_with_retry(
    client: httpx.AsyncClient,
    tool: Tool,
    req_kwargs: dict[str, Any],
    limits: _CallLimits,
    logger: ILogger,
) -> tuple[int, bytes, str]:
    """Status, corpo (até o limite) e encoding; repete falha transiente/502-504 conforme o método."""
    attempt = 0
    while True:
        try:
            status, body, encoding = await _read_within_deadline(client, req_kwargs, limits)
        except httpx.RequestError as exc:
            if attempt >= limits.retries or not _retryable_error(tool.http_method, exc):
                raise
            retry_reason: object = type(exc).__name__
        else:
            if attempt >= limits.retries or status not in _RETRY_STATUS or tool.http_method not in _IDEMPOTENT:
                return status, body, encoding
            retry_reason = status
        logger.warning("Repetindo a chamada da tool", tool_id=tool.id, attempt=attempt + 1, reason=retry_reason)
        await asyncio.sleep(backoff_delay(attempt, limits.retry_backoff))
        attempt += 1


async def _read_within_deadline(
    client: httpx.AsyncClient, req_kwargs: dict[str, Any], limits: _CallLimits
) -> tuple[int, bytes, str]:
    """Uma tentativa com prazo total ``limits.timeout`` (o do httpx é por operação); estouro vira ``ReadTimeout``."""
    try:
        async with asyncio.timeout(limits.timeout):
            return await _read_once(client, req_kwargs, limits.max_response_bytes)
    except TimeoutError:
        raise httpx.ReadTimeout("prazo total da tentativa esgotado") from None


async def _read_once(client: httpx.AsyncClient, req_kwargs: dict[str, Any], max_bytes: int) -> tuple[int, bytes, str]:
    """Um request com o corpo lido cru em streaming e descomprimido até ``max_bytes`` (``_ResponseTooLarge`` acima)."""
    async with client.stream(**req_kwargs) as response:
        declared = response.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise _ResponseTooLarge
        decompressor = _decompressor(response.headers)
        if response.is_stream_consumed:  # transporte que devolve o corpo já lido e aberto (ex.: MockTransport)
            if len(response.content) > max_bytes:
                raise _ResponseTooLarge
            return response.status_code, response.content, response.encoding or "utf-8"
        body = bytearray()
        async for raw in response.aiter_raw():
            chunk = raw if decompressor is None else _inflate(decompressor, raw, max_bytes - len(body) + 1)
            body += chunk
            if len(body) > max_bytes:
                raise _ResponseTooLarge
        return response.status_code, bytes(body), response.encoding or "utf-8"


def _decompressor(headers: httpx.Headers) -> zlib._Decompress | None:
    """Descompressor do ``Content-Encoding`` (``None``: corpo sem compressão); outro valor é recusado."""
    content_encoding = headers.get("Content-Encoding", "").strip().lower()
    if content_encoding in _IDENTITY_ENCODINGS:
        return None
    if content_encoding in _ZLIB_ENCODINGS:
        return zlib.decompressobj(zlib.MAX_WBITS | 32)
    raise _UnsupportedEncoding


def _inflate(decompressor: zlib._Decompress, raw: bytes, budget: int) -> bytes:
    """Descomprime ``raw`` com saída de no máximo ``budget`` bytes (o que falta do limite + 1).

    Saída menor que ``budget`` quer dizer que a entrada acabou (nada fica em ``unconsumed_tail``); saída
    igual estoura o limite e o chamador para. Assim nenhum pedaço abre mais que o limite.
    """
    try:
        return decompressor.decompress(raw, budget)
    except zlib.error as exc:
        raise httpx.DecodingError("corpo comprimido inválido") from exc


def _build_request_kwargs(
    tool: Tool,
    url: str,
    headers: dict[str, str],
    remaining: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    """Constrói o dicionário de kwargs para ``httpx.AsyncClient.stream``.

    GET/DELETE: os argumentos restantes entram na query junto da query fixa da ``route`` (o ``params=``
    do httpx a substituiria inteira).
    """
    req_kwargs: dict[str, Any] = {
        "method": tool.http_method.value,
        "url": url,
        "headers": headers,
        "timeout": timeout,
    }
    if tool.http_method in (HttpMethod.GET, HttpMethod.DELETE):
        if remaining:
            req_kwargs["url"] = httpx.URL(url).copy_merge_params(remaining)
    else:
        req_kwargs["json"] = remaining or None
    return req_kwargs


def _resolve_url(
    route: str, kwargs: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
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


def _serialize(body: bytes, encoding: str) -> str:
    """JSON vira a representação Python (como antes); o resto, texto no charset da resposta.

    JSON aninhado demais para o decoder (``RecursionError``) também volta como texto.
    """
    text = body.decode(encoding, errors="replace")
    try:
        return str(json.loads(text))
    except (ValueError, RecursionError):
        return text
