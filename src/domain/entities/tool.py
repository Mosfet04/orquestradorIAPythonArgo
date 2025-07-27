from dataclasses import dataclass
from typing import Dict, Any, List, Optional
from enum import Enum


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
            raise ValueError("Nome do parâmetro não pode estar vazio")
        if not self.description:
            raise ValueError("Descrição do parâmetro não pode estar vazia")


@dataclass
class Tool:
    """Entidade que representa uma ferramenta HTTP."""
    
    id: str
    name: str
    description: str
    route: str
    http_method: HttpMethod
    parameters: List[ToolParameter]
    instructions: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    active: bool = True
    
    def __post_init__(self):
        if not self.id:
            raise ValueError("ID da tool não pode estar vazio")
        if not self.name:
            raise ValueError("Nome da tool não pode estar vazio")
        if not self.description:
            raise ValueError("Descrição da tool não pode estar vazia")
        if not self.route:
            raise ValueError("Rota da tool não pode estar vazia")
        if self.parameters is None:
            self.parameters = []
