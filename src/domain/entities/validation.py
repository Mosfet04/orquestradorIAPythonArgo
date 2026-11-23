"""Validações comuns das entidades de configuração."""

from __future__ import annotations

from collections.abc import Sequence


class InvalidField(ValueError):
    """Campo do documento de config recusado.

    ``field`` é o nome do campo no documento (``rag_config.model`` quando aninhado) e ``indexes``, as
    posições recusadas numa lista. A mensagem cita o campo, nunca o valor: quem grava config pode ter
    colado um segredo no lugar errado.
    """

    def __init__(self, field: str, message: str, indexes: Sequence[int] = ()) -> None:
        super().__init__(message)
        self.field = field
        self.indexes = tuple(indexes)


def require_text(value: object, field: str, label: str) -> None:
    """Exige ``str`` não vazia; ``InvalidField`` com o nome do campo, nunca com o valor.

    As entidades de config vêm de documento do Mongo sem schema: um tipo errado (ex.: id
    objeto ou lista) passava pelo ``if not valor`` e quebrava o AgentOS na montagem,
    derrubando todos os agentes (F1-10).
    """
    if not isinstance(value, str):
        raise InvalidField(field, f"{label} deve ser texto")
    if not value:
        raise InvalidField(field, f"{label} não pode estar vazio")


RESERVED_ENTITY_IDS = frozenset(
    {
        "os-manager-internal",
        "platform-builder-internal",
        "platform-manager-internal",
        "company-manager-internal",
        "product-manager-internal",
    }
)
"""Ids das entidades internas do runtime (agno 3.x reserva mesmo com o recurso desligado).

Um agente ou team com um deles faz o AgentOS recusar a montagem inteira (todas as entidades caem).
Lista acompanhada por teste contra a do agno (``tests/unit/test_agno_runtime.py``).
"""


def require_entity_id(value: object, label: str) -> None:
    """``require_text`` do campo ``id`` e recusa de id reservado pelo runtime (sem ecoar o valor)."""
    require_text(value, "id", label)
    if value in RESERVED_ENTITY_IDS:
        raise InvalidField("id", f"{label} é reservado pelo runtime; escolha outro")


def require_optional_text(value: object, field: str, label: str) -> None:
    """Aceita ``None`` ou ``str`` (vazia inclusive); outro tipo é ``InvalidField`` sem o valor.

    Ex.: ``descricao`` vai para o ``description: str`` das respostas do AgentOS; um número ou
    objeto dava 500 em ``GET /agents``, ``/teams`` e ``/config`` para todas as entidades.
    """
    if value is not None and not isinstance(value, str):
        raise InvalidField(field, f"{label} deve ser texto")


def require_bool(value: object, field: str) -> None:
    """Exige ``bool``: ``1`` ou ``"false"`` (verdadeiro em Python) é ``InvalidField``, sem o valor."""
    if not isinstance(value, bool):
        raise InvalidField(field, f"{field} deve ser booleano")


def require_int_in_range(value: object, field: str, minimum: int, maximum: int) -> None:
    """Exige ``int`` (``bool`` não conta) entre ``minimum`` e ``maximum``; ``InvalidField`` sem o valor."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidField(field, f"{field} deve ser inteiro")
    if not minimum <= value <= maximum:
        raise InvalidField(field, f"{field} deve estar entre {minimum} e {maximum}")
