"""Resultado de uma chamada de tool para o log de auditoria (F4-03), comum às tools HTTP e MCP.

``text`` é o que vai ao modelo; o resto é o que a auditoria registra. ``reason`` é um código curto de vocabulário
fixo (nunca o texto da exceção, do upstream ou do servidor MCP) e ``error_type`` é só o nome do tipo da exceção.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CallResult = Literal["ok", "error", "refused"]
"""``refused``: barrada por regra local antes de sair (argumento inválido, guarda de rede, allowlist, schema)."""


@dataclass(frozen=True, slots=True)
class ToolCallOutcome:
    """Texto ao modelo e a classificação da chamada para a auditoria."""

    text: str
    result: CallResult
    reason: str | None = None
    error_type: str | None = None
    http_status: int | None = None

    @classmethod
    def ok(cls, text: str, *, http_status: int | None = None) -> ToolCallOutcome:
        """Chamada que voltou com sucesso."""
        return cls(text, "ok", http_status=http_status)

    @classmethod
    def error(
        cls, text: str, reason: str, *, error_type: str | None = None, http_status: int | None = None
    ) -> ToolCallOutcome:
        """Chamada que falhou (rede, limite, status de erro, erro da própria tool)."""
        return cls(text, "error", reason, error_type, http_status)

    @classmethod
    def refused(cls, text: str, reason: str, *, error_type: str | None = None) -> ToolCallOutcome:
        """Chamada barrada por regra local; nada saiu para o destino."""
        return cls(text, "refused", reason, error_type)
