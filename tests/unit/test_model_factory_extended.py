"""Testes estendidos para ModelFactory — cobertura de _get_model_class e _instantiate."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from src.application.services.model_factory_service import ModelFactory


@pytest.fixture
def factory(mock_logger):
    return ModelFactory(logger=mock_logger)


class TestModelFactoryExtended:
    def test_get_model_class_gemini_without_api_key(self, factory):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="GEMINI_API_KEY"):
                factory._get_model_class("gemini")

    def test_get_model_class_import_error(self, factory):
        """Quando o módulo não pode ser importado."""
        with patch("builtins.__import__", side_effect=ImportError("no module")):
            # ollama é hardcoded, então usa outro
            with patch.dict(os.environ, {"GEMINI_API_KEY": "fake"}, clear=False):
                with pytest.raises(ValueError, match="indisponível"):
                    factory._get_model_class("gemini")

    def test_instantiate_azure_missing_api_key(self, factory):
        mock_cls = MagicMock()
        with pytest.raises(ValueError, match="AZURE_API_KEY"):
            factory._instantiate("azure", mock_cls, "model-1", None, {})

    def test_instantiate_azure_missing_endpoint(self, factory):
        mock_cls = MagicMock()
        with patch.dict(os.environ, {"AZURE_ENDPOINT": ""}, clear=True):
            with pytest.raises(ValueError, match="AZURE_ENDPOINT"):
                factory._instantiate("azure", mock_cls, "model-1", "fake-key", {})

    def test_instantiate_azure_success(self, factory):
        mock_cls = MagicMock(return_value="azure_model")
        with patch.dict(os.environ, {
            "AZURE_ENDPOINT": "https://my.openai.azure.com",
            "AZURE_VERSION": "2024-02-01",
        }, clear=False):
            result = factory._instantiate("azure", mock_cls, "gpt-4", "key123", {})
        assert result == "azure_model"
        mock_cls.assert_called_once()

    def test_instantiate_non_ollama_missing_api_key(self, factory):
        mock_cls = MagicMock()
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            factory._instantiate("openai", mock_cls, "gpt-4", None, {})

    def test_instantiate_non_ollama_with_api_key(self, factory):
        mock_cls = MagicMock(return_value="openai_model")
        result = factory._instantiate("openai", mock_cls, "gpt-4", "sk-key", {})
        assert result == "openai_model"
        mock_cls.assert_called_once_with(id="gpt-4", api_key="sk-key")

    def test_instantiate_filters_api_key_from_extra(self, factory):
        mock_cls = MagicMock(return_value="model")
        result = factory._instantiate("openai", mock_cls, "gpt-4", "sk-key", {"api_key": "old", "temperature": 0.7})
        assert result == "model"
        mock_cls.assert_called_once_with(id="gpt-4", api_key="sk-key", temperature=0.7)
