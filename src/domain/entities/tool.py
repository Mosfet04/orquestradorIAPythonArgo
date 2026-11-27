"""Entidades de tool: método, parâmetros e definição da tool (HTTP ou servidor MCP, F4-02)."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from src.domain.entities.validation import InvalidField, require_text

MCP_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
"""Nome de tool MCP aceito na allowlist: o que todo provider aceita como nome de função."""

MCP_SCHEMA_HASH = re.compile(r"[0-9a-f]{64}")
"""Hash fixado do schema de uma tool MCP: SHA-256 em hexadecimal minúsculo."""


class HttpMethod(Enum):
    """Métodos HTTP suportados."""

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"


class ParameterType(Enum):
    """Tipos de parâmetros suportados."""

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    OBJECT = "object"
    ARRAY = "array"


@dataclass
class ToolParameter:
    """Parâmetro de uma tool."""

    name: str
    type: ParameterType
    description: str
    required: bool = False
    default_value: Optional[Any] = None

    def __post_init__(self):
        if not self.name:
            raise InvalidField("name", "Nome do parâmetro não pode estar vazio")
        if not self.description:
            raise InvalidField("description", "Descrição do parâmetro não pode estar vazia")


def _validate_mcp_tools(mcp_tools: object, parameters: List[ToolParameter]) -> None:
    """Allowlist não vazia de nome -> hash; os argumentos vêm do schema do servidor, não de ``parameters``."""
    if not isinstance(mcp_tools, Mapping) or not mcp_tools:
        raise InvalidField("mcp_tools", "mcp_tools deve ser um mapa não vazio de nome da tool para hash do schema")
    for name, pinned in mcp_tools.items():
        if not isinstance(name, str) or not MCP_TOOL_NAME.fullmatch(name):
            raise InvalidField("mcp_tools", "nome de tool MCP só com letras, dígitos, _ e -, até 64 caracteres")
        if not isinstance(pinned, str) or not MCP_SCHEMA_HASH.fullmatch(pinned):
            raise InvalidField("mcp_tools", "hash do schema deve ser SHA-256 em hexadecimal minúsculo (64 caracteres)")
    if parameters:
        raise InvalidField("parameters", "tool MCP não declara parameters: os argumentos vêm do servidor")


@dataclass
class Tool:
    """Ferramenta do agente: endpoint HTTP ou, com ``mcp_tools``, servidor MCP (F4-02).

    ``mcp_tools``: allowlist das tools do servidor MCP em ``route`` (streamable HTTP), nome -> hash
    SHA-256 do schema fixado; tool do servidor fora dela não chega ao agente, e schema que não bate com o
    hash é recusado. ``None`` (documentos de antes) = tool HTTP.
    """

    id: str
    name: str
    description: str
    route: str
    http_method: HttpMethod
    parameters: List[ToolParameter]
    instructions: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    active: bool = True
    mcp_tools: Optional[Dict[str, str]] = None

    def __post_init__(self):
        require_text(self.id, "id", "ID da tool")
        if not self.name:
            raise InvalidField("name", "Nome da tool não pode estar vazio")
        if not self.description:
            raise InvalidField("description", "Descrição da tool não pode estar vazia")
        if not self.route:
            raise InvalidField("route", "Rota da tool não pode estar vazia")
        if self.parameters is None:
            self.parameters = []
        if self.mcp_tools is not None:
            _validate_mcp_tools(self.mcp_tools, self.parameters)

    @property
    def is_mcp(self) -> bool:
        """Servidor MCP (``mcp_tools`` definido) em vez de endpoint HTTP."""
        return self.mcp_tools is not None
