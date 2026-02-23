"""Factory de modelos embedder para RAG — agno v2.5."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Type

from src.domain.ports import ILogger


class EmbedderModelFactory:
    """Cria instâncias de embedders para RAG baseado no tipo especificado."""

    _ALIASES: Dict[str, str] = {"google": "gemini", "azureopenai": "azure"}

    # agno v2.5: agno.embedder.* → agno.knowledge.embedder.*
    _IMPORT_SPECS: Dict[str, tuple[str, str, str, str]] = {
        "ollama": (
            "agno.knowledge.embedder.ollama", "OllamaEmbedder", "agno", "Ollama"
        ),
        "openai": (
            "agno.knowledge.embedder.openai", "OpenAIEmbedder", "openai", "OpenAI"
        ),
        "gemini": (
            "agno.knowledge.embedder.google", "GeminiEmbedder", "google-genai", "Gemini"
        ),
        "azure": (
            "agno.knowledge.embedder.azure_openai", "AzureOpenAIEmbedder", "openai",
            "Azure OpenAI",
        ),
    }

    def __init__(self, logger: ILogger) -> None:
        self._logger = logger

    # ── public ──────────────────────────────────────────────────────

    def create_model(self, factory_ia_model: str, model_id: str, **kwargs: Any) -> Any:
        """Cria uma instância do embedder."""
        ft = self._normalize(factory_ia_model)
        self._validate_inputs(ft, model_id)
        model_class = self._get_model_class(ft)
        api_key = kwargs.get("api_key") or os.getenv(f"{ft.upper()}_API_KEY")

        filtered = {k: v for k, v in kwargs.items() if k != "api_key"}
        if ft == "ollama":
            return model_class(id=model_id, **filtered)
        if not api_key:
            raise ValueError(f"{ft.upper()}_API_KEY não configurado")
        return model_class(id=model_id, api_key=api_key, **filtered)

    @staticmethod
    def get_supported_models() -> List[str]:
        return ["ollama", "openai", "gemini", "google", "azure", "azureopenai"]

    @classmethod
    def is_supported_model(cls, factory_ia_model: str) -> bool:
        ft = cls._normalize(factory_ia_model)
        return ft in cls._IMPORT_SPECS

    # ── private ─────────────────────────────────────────────────────

    @classmethod
    def _normalize(cls, value: str) -> str:
        ft = (value or "").lower().strip()
        return cls._ALIASES.get(ft, ft)

    @staticmethod
    def _validate_inputs(factory_type: str, model_id: str) -> None:
        if not factory_type:
            raise ValueError("Tipo de embedder não pode estar vazio")
        if not model_id or not model_id.strip():
            raise ValueError("ID do embedder não pode estar vazio")

    def _get_model_class(self, factory_type: str) -> Type:
        if factory_type == "gemini" and not os.getenv("GEMINI_API_KEY"):
            raise ValueError("GEMINI_API_KEY não configurado")

        if factory_type not in self._IMPORT_SPECS:
            raise ValueError(
                f"Embedder '{factory_type}' não suportado. "
                f"Suportados: {', '.join(self.get_supported_models())}"
            )

        module_path, class_name, pip_pkg, human_name = self._IMPORT_SPECS[factory_type]
        try:
            module = __import__(module_path, fromlist=[class_name])
            return getattr(module, class_name)
        except ImportError:
            raise ValueError(
                f"Embedder {human_name} indisponível. Instale: pip install {pip_pkg}"
            )