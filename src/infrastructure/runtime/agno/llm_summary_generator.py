"""Gerador de sumários via LLM."""

from __future__ import annotations

import asyncio
from typing import cast

from agno.models.base import Model
from agno.models.message import Message

from src.domain.entities.model_config import ModelConfig
from src.domain.ports.logger_port import ILogger
from src.domain.ports.model_factory_port import IModelFactory
from src.domain.ports.summary_generator_port import (
    ISummaryGenerator,
    SummaryTimeoutError,
)

_SUMMARY_PROMPT = (
    "Resuma o texto a seguir em no máximo 3 frases concisas, "
    "capturando as ideias principais:\n\n{content}"
)

_MAX_INPUT_CHARS = 4000
_FALLBACK_LENGTH = 200
# Teto por sumário: o cliente Ollama não tem timeout e a indexação hierárquica faz uma
# chamada por nó interno no startup; um provedor travado não pode segurar o startup.
_SUMMARY_TIMEOUT_SECONDS = 60.0


class LLMSummaryGenerator(ISummaryGenerator):
    """Gera sumários usando o LLM configurado.

    Chama o modelo pela API pública assíncrona do agno 2.5.8
    (``Model.aresponse(messages=[Message(...)])``, ``agno/models/base.py``); o
    ``invoke(prompt)`` anterior não batia com a assinatura dos providers
    (``invoke(messages, assistant_message, ...)``) e sempre caía no fallback (F1-07, B8).
    Erro real do modelo ou resposta vazia: fallback para truncamento, com log só do tipo
    do erro (a mensagem de SDK pode carregar segredo). Sem resposta em ``timeout_seconds``:
    log e ``SummaryTimeoutError``, para o indexador parar de chamar o modelo no documento.
    """

    def __init__(
        self,
        *,
        model_factory: IModelFactory,
        factory_ia_model: str = "ollama",
        model_id: str = "llama3.2:latest",
        logger: ILogger,
        timeout_seconds: float = _SUMMARY_TIMEOUT_SECONDS,
    ) -> None:
        self._model_factory = model_factory
        self._timeout_seconds = timeout_seconds
        self._model_config = ModelConfig(provider=factory_ia_model, model_id=model_id)
        self._logger = logger
        self._model: Model | None = None
        # Single-flight da criação: os sumários de um documento rodam em gather.
        self._model_lock = asyncio.Lock()

    async def generate_summary(self, content: str) -> str:
        """Gera sumário conciso do conteúdo via LLM."""
        if not content or not content.strip():
            return ""

        truncated = content[:_MAX_INPUT_CHARS]
        prompt = _SUMMARY_PROMPT.format(content=truncated)

        try:
            model = await self._get_or_create_model()
            response = await asyncio.wait_for(
                model.aresponse(messages=[Message(role="user", content=prompt)]),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            self._logger.warning(
                "Sumário: modelo não respondeu no prazo",
                factory_ia_model=self._model_config.provider,
                model_id=self._model_config.model_id,
                error_type=type(exc).__name__,
                timeout_s=self._timeout_seconds,
            )
            raise SummaryTimeoutError(
                f"modelo de sumário sem resposta em {self._timeout_seconds} s"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - logado; qualquer falha do modelo cai no truncamento
            self._logger.warning(
                "Fallback de sumário: falha ao chamar o modelo",
                factory_ia_model=self._model_config.provider,
                model_id=self._model_config.model_id,
                error_type=type(exc).__name__,
            )
            return content[:_FALLBACK_LENGTH]

        summary = str(response.content).strip() if response.content is not None else ""
        if not summary:
            self._logger.warning(
                "Fallback de sumário: modelo devolveu resposta vazia",
                factory_ia_model=self._model_config.provider,
                model_id=self._model_config.model_id,
            )
            return content[:_FALLBACK_LENGTH]
        return summary

    async def _get_or_create_model(self) -> Model:
        """Lazy init do modelo LLM, uma vez só; a criação (segredo file:, DNS) roda fora do loop."""
        async with self._model_lock:
            if self._model is None:
                created = await asyncio.to_thread(self._model_factory.create_model, self._model_config)
                # A fábrica do runtime agno devolve um ``agno.models.base.Model``.
                self._model = cast(Model, created)
            return self._model
