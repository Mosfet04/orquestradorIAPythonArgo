"""Testes para EmbedderModelFactory."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.application.services.embedder_model_factory_service import EmbedderModelFactory


@pytest.fixture
def factory(mock_logger):
    return EmbedderModelFactory(logger=mock_logger)


class TestEmbedderModelFactory:
    def test_create_ollama_embedder(self, factory):
        with patch.object(factory, "_get_model_class") as mock_cls:
            mock_cls.return_value = MagicMock(return_value=MagicMock())
            result = factory.create_model("ollama", "nomic-embed-text")
            assert result is not None

    def test_unsupported_embedder_raises(self, factory):
        with pytest.raises(ValueError, match="não suportado"):
            factory.create_model("unsupported_type", "model")

    def test_empty_factory_type_raises(self, factory):
        with pytest.raises(ValueError, match="Tipo de embedder não pode estar vazio"):
            factory.create_model("", "model")

    def test_empty_model_id_raises(self, factory):
        with pytest.raises(ValueError, match="ID do embedder não pode estar vazio"):
            factory.create_model("ollama", "")

    def test_get_supported_models(self):
        models = EmbedderModelFactory.get_supported_models()
        assert "ollama" in models
        assert "openai" in models

    def test_is_supported_model(self):
        assert EmbedderModelFactory.is_supported_model("ollama") is True
        assert EmbedderModelFactory.is_supported_model("OPENAI") is True
        assert EmbedderModelFactory.is_supported_model("nonexistent") is False
