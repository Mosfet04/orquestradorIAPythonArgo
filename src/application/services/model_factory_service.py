"""Factory de modelos de IA — desacoplada de infraestrutura."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Type

from agno.models.ollama import Ollama

from src.domain.ports import ILogger


class ModelFactory:
    """Cria instâncias de modelos de IA baseado no tipo especificado."""

    _ALIASES: Dict[str, str] = {"google": "gemini", "azureopenai": "azure"}

    _IMPORT_SPECS: Dict[str, tuple[str, str, str, str]] = {
        "openai": ("agno.models.openai.chat", "OpenAIChat", "openai", "OpenAI"),
        "anthropic": ("agno.models.anthropic.claude", "Claude", "anthropic", "Anthropic"),
        "gemini": ("agno.models.google.gemini", "Gemini", "google-genai", "Gemini"),
        "groq": ("agno.models.groq.chat", "GroqChat", "groq", "Groq"),
        "azure": ("agno.models.azure.openai_chat", "AzureOpenAI", "openai", "Azure OpenAI"),
    }

    def __init__(self, logger: ILogger) -> None:
        self._logger = logger

    # ── public ──────────────────────────────────────────────────────

    def create_model(self, factory_ia_model: str, model_id: str, **kwargs: Any) -> Any:
        """Cria uma instância do modelo baseado no tipo especificado."""
        factory_type = self._normalize(factory_ia_model)
        self._validate_inputs(factory_type, model_id)
        model_class = self._get_model_class(factory_type)
        api_key = kwargs.get("api_key") or os.getenv(f"{factory_type.upper()}_API_KEY")
        return self._instantiate(factory_type, model_class, model_id, api_key, kwargs)

    def validate_model_config(self, factory_ia_model: str, model_id: str) -> Dict[str, Any]:
        """Valida a configuração sem criar a instância."""
        result: Dict[str, Any] = {
            "valid": True,
            "factory_type": self._normalize(factory_ia_model),
            "model_id": model_id,
            "supported": False,
            "available": False,
            "errors": [],
        }
        ft = result["factory_type"]

        if not self.is_supported_model(ft):
            result["valid"] = False
            result["errors"].append(
                f"Tipo '{factory_ia_model}' não suportado. "
                f"Suportados: {', '.join(self.get_supported_models())}"
            )
        else:
            result["supported"] = True

        if not model_id or not model_id.strip():
            result["valid"] = False
            result["errors"].append("ID do modelo não pode estar vazio")

        if result["supported"]:
            try:
                self._get_model_class(ft)
                result["available"] = True
            except ValueError as exc:
                result["valid"] = False
                result["errors"].append(str(exc))

        return result

    # ── class helpers ───────────────────────────────────────────────

    @staticmethod
    def get_supported_models() -> List[str]:
        return [
            "ollama", "openai", "anthropic", "gemini",
            "google", "groq", "azure", "azureopenai",
        ]

    @classmethod
    def is_supported_model(cls, factory_ia_model: str) -> bool:
        ft = cls._normalize(factory_ia_model)
        return ft in cls.get_supported_models() or ft in cls._ALIASES

    # ── private ─────────────────────────────────────────────────────

    @classmethod
    def _normalize(cls, value: str) -> str:
        ft = (value or "").lower().strip()
        return cls._ALIASES.get(ft, ft)

    @staticmethod
    def _validate_inputs(factory_type: str, model_id: str) -> None:
        if not factory_type:
            raise ValueError("Tipo de modelo não pode estar vazio")
        if not model_id or not model_id.strip():
            raise ValueError("ID do modelo não pode estar vazio")

    def _get_model_class(self, factory_type: str) -> Type:
        if factory_type == "ollama":
            return Ollama

        if factory_type not in self._IMPORT_SPECS:
            raise ValueError(
                f"Tipo '{factory_type}' não suportado. "
                f"Suportados: {', '.join(self.get_supported_models())}"
            )

        if factory_type == "gemini" and not os.getenv("GEMINI_API_KEY"):
            raise ValueError("GEMINI_API_KEY não está configurado no ambiente")

        module_path, class_name, pip_pkg, human_name = self._IMPORT_SPECS[factory_type]
        try:
            module = __import__(module_path, fromlist=[class_name])
            return getattr(module, class_name)
        except ImportError:
            raise ValueError(
                f"Modelo {human_name} indisponível. "
                f"Instale com: pip install {pip_pkg}"
            )

    def _instantiate(
        self,
        factory_type: str,
        model_class: Type,
        model_id: str,
        api_key: str | None,
        extra: Dict[str, Any],
    ) -> Any:
        filtered = {k: v for k, v in extra.items() if k != "api_key"}

        if factory_type == "ollama":
            return model_class(id=model_id, **filtered)

        if factory_type == "azure":
            azure_endpoint = os.getenv("AZURE_ENDPOINT")
            api_version = os.getenv("AZURE_VERSION")
            if not api_key:
                raise ValueError("AZURE_API_KEY não configurado")
            if not azure_endpoint:
                raise ValueError("AZURE_ENDPOINT não configurado")
            return model_class(
                id=model_id,
                api_key=api_key,
                azure_endpoint=azure_endpoint,
                api_version=api_version,
                **filtered,
            )

        if not api_key:
            raise ValueError(f"{factory_type.upper()}_API_KEY não configurado")
        return model_class(id=model_id, api_key=api_key, **filtered)
