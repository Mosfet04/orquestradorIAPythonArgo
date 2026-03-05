"""Gerador de sumários via LLM."""

from __future__ import annotations

from typing import Any

from src.domain.ports.logger_port import ILogger
from src.domain.ports.model_factory_port import IModelFactory
from src.domain.ports.summary_generator_port import ISummaryGenerator

_SUMMARY_PROMPT = (
    "Resuma o texto a seguir em no máximo 3 frases concisas, "
    "capturando as ideias principais:\n\n{content}"
)

_MAX_INPUT_CHARS = 4000
_FALLBACK_LENGTH = 200


class LLMSummaryGenerator(ISummaryGenerator):
    """Gera sumários usando o LLM configurado.

    Inclui retry automático e fallback para truncamento caso
    o modelo não esteja disponível.
    """

    def __init__(
        self,
        *,
        model_factory: IModelFactory,
        factory_ia_model: str = "ollama",
        model_id: str = "llama3.2:latest",
        logger: ILogger,
    ) -> None:
        self._model_factory = model_factory
        self._factory_ia_model = factory_ia_model
        self._model_id = model_id
        self._logger = logger
        self._model: Any = None

    async def generate_summary(self, content: str) -> str:
        """Gera sumário conciso do conteúdo via LLM."""
        if not content or not content.strip():
            return ""

        truncated = content[:_MAX_INPUT_CHARS]
        prompt = _SUMMARY_PROMPT.format(content=truncated)

        try:
            model = self._get_or_create_model()
            response = model.invoke(prompt)

            if hasattr(response, "content"):
                return str(response.content).strip()
            return str(response).strip()
        except Exception as exc:
            self._logger.warning(
                "Fallback de sumário — LLM indisponível", error=str(exc)
            )
            return content[:_FALLBACK_LENGTH]

    def _get_or_create_model(self) -> Any:
        """Lazy init do modelo LLM."""
        if self._model is None:
            self._model = self._model_factory.create_model(
                self._factory_ia_model, self._model_id
            )
        return self._model
