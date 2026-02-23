"""Testes estendidos para EmbedderModelFactory — linhas faltantes."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from src.application.services.embedder_model_factory_service import EmbedderModelFactory


@pytest.fixture
def factory(mock_logger):
    return EmbedderModelFactory(logger=mock_logger)


class TestEmbedderModelFactoryExtended:
    def test_create_non_ollama_missing_api_key(self, factory):
        """Non-ollama sem API key deve dar erro."""
        with patch.object(factory, "_get_model_class") as mock_cls:
            mock_cls.return_value = MagicMock()
            with patch.dict(os.environ, {}, clear=True):
                with pytest.raises(ValueError, match="API_KEY não configurado"):
                    factory.create_model("openai", "text-embedding-3-small")

    def test_create_non_ollama_with_api_key(self, factory):
        """Non-ollama com API key deve funcionar."""
        mock_model = MagicMock()
        with patch.object(factory, "_get_model_class") as mock_cls:
            mock_cls.return_value = MagicMock(return_value=mock_model)
            result = factory.create_model("openai", "embed-model", api_key="sk-key")
        assert result is mock_model

    def test_get_model_class_gemini_missing_key(self, factory):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="GEMINI_API_KEY"):
                factory._get_model_class("gemini")

    def test_get_model_class_import_error(self, factory):
        """Quando módulo não pode ser importado."""
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake"}, clear=False):
            with patch("builtins.__import__", side_effect=ImportError("no module")):
                with pytest.raises(ValueError, match="indisponível"):
                    factory._get_model_class("gemini")

    def test_alias_google_to_gemini(self, factory):
        """Alias 'google' deve normalizar para 'gemini'."""
        ft = factory._normalize("google")
        assert ft == "gemini"

    def test_alias_azureopenai_to_azure(self, factory):
        ft = factory._normalize("azureopenai")
        assert ft == "azure"
