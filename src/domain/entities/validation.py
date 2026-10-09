"""Validações comuns das entidades de configuração."""

from __future__ import annotations


def require_text(value: object, field_label: str) -> None:
    """Exige ``str`` não vazia; ``ValueError`` com o nome do campo, nunca com o valor.

    As entidades de config vêm de documento do Mongo sem schema: um tipo errado (ex.: id
    objeto ou lista) passava pelo ``if not valor`` e quebrava o AgentOS na montagem,
    derrubando todos os agentes (F1-10).
    """
    if not isinstance(value, str):
        raise ValueError(f"{field_label} deve ser texto")
    if not value:
        raise ValueError(f"{field_label} não pode estar vazio")


def require_optional_text(value: object, field_label: str) -> None:
    """Aceita ``None`` ou ``str`` (vazia inclusive); outro tipo é ``ValueError`` sem o valor.

    Ex.: ``descricao`` vai para o ``description: str`` das respostas do AgentOS; um número ou
    objeto dava 500 em ``GET /agents``, ``/teams`` e ``/config`` para todas as entidades.
    """
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{field_label} deve ser texto")
