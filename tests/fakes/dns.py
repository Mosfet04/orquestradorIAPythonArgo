"""DNS falso: nenhum teste resolve host real (FA-06: a guarda da ``route`` de tool resolve o nome na escrita)."""

from __future__ import annotations

PUBLIC_IP = "93.184.216.34"
"""O que ``public_dns`` responde para todo nome: um endereço público (a guarda da ``route`` aceita)."""


def public_dns(host: str) -> list[str]:
    """Resolver falso: todo nome é público."""
    return [PUBLIC_IP]


def no_dns(host: str) -> list[str]:
    """Resolver que não pode ser chamado: o teste só passa host literal (IP) ou rota recusada antes do DNS."""
    raise AssertionError(f"não deveria resolver DNS: {host}")
