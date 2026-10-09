"""Guardrail de ``user_id`` para Agent/Team do agno que guardam memória de usuário (F1-06)."""

from __future__ import annotations

import unicodedata

from agno.agent import Agent
from agno.exceptions import CheckTrigger, InputCheckError
from agno.guardrails.base import BaseGuardrail
from agno.run.agent import RunInput
from agno.run.team import TeamRunInput
from agno.team import Team

# Usuário que o agno 2.5.8 usa para memória sem ``user_id`` (``agno/agent/_messages.py``,
# ``agno/memory/manager.py``): pool compartilhado, nunca um usuário de requisição.
_RESERVED_USER_IDS = frozenset({"default"})


def _has_visible_char(value: str) -> bool:
    """Algum caractere fora de separador (Z*) e controle/formatação/não atribuído (C*).

    ``str.strip`` não remove U+200B, U+FEFF, U+2060 nem NUL: sem isto, um valor que parece
    vazio viraria um usuário "anônimo" compartilhado.
    """
    return any(unicodedata.category(char)[0] not in ("Z", "C") for char in value)


def is_valid_user_id(user_id: object) -> bool:
    """``user_id`` aceito para memória: string com caractere visível e não reservada.

    Total (nunca levanta): o AG-UI repassa ``forwardedProps.user_id`` sem validar o tipo. Sem
    normalização: ``" ana "`` e ``"ana"`` são usuários diferentes.
    """
    return (
        isinstance(user_id, str)
        and user_id not in _RESERVED_USER_IDS
        and _has_visible_char(user_id)
    )


class UserIdRequiredGuardrail(BaseGuardrail):
    """Recusa run sem ``user_id`` válido de Agent/Team que guarda memória de usuário.

    Sem ``user_id``, o agno 2.5.8 lê e grava a memória no usuário ``"default"``
    (``agno/agent/_messages.py``, ``agno/agent/_managers.py``, ``agno/memory/manager.py``),
    compartilhado por todo chamador anônimo. Guardrail roda antes de o run ler memória
    ou chamar o modelo e sempre de forma síncrona (``agno/agent/_hooks.py``), inclusive
    com hooks em background. O ``user_id`` chega pelo nome do parâmetro
    (``agno/utils/hooks.py``, ``filter_hook_args``). Exceção que não seja
    ``InputCheckError`` é engolida pelo agno e o run segue: a checagem é total.
    """

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id

    def check(
        self,
        run_input: RunInput | TeamRunInput,
        user_id: object = None,
    ) -> None:
        if not is_valid_user_id(user_id):
            raise InputCheckError(
                f"user_id obrigatório: '{self.entity_id}' guarda memória por usuário. "
                "Envie user_id como string não vazia, com caractere visível e diferente "
                "de 'default' (campo do form em /agents|/teams/{id}/runs; "
                "forwardedProps.user_id no AG-UI).",
                check_trigger=CheckTrigger.VALIDATION_FAILED,
            )

    async def async_check(
        self,
        run_input: RunInput | TeamRunInput,
        user_id: object = None,
    ) -> None:
        self.check(run_input, user_id)


def needs_user_id(*, user_memories: bool, agentic_memory: bool) -> bool:
    """Predicado único (Agent e Team): memória de usuário ligada exige ``user_id``."""
    return user_memories or agentic_memory


def requires_user_id(entity: Agent | Team) -> bool:
    """``True`` se o Agent/Team lê ou grava memória de usuário (precisa de ``user_id``)."""
    return needs_user_id(
        user_memories=getattr(entity, "enable_user_memories", None) is True,
        agentic_memory=getattr(entity, "enable_agentic_memory", None) is True,
    )
