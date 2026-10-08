"""Testes para ModelFactory."""

from __future__ import annotations

import pytest

from src.application.services.model_factory_service import ModelFactory


@pytest.fixture
def factory(mock_logger):
    return ModelFactory(logger=mock_logger)


class TestModelFactory:
    def test_create_ollama_model(self, factory):
        model = factory.create_model("ollama", "llama3.2:latest")
        assert model is not None

    def test_unsupported_model_raises(self, factory):
        with pytest.raises(ValueError):
            factory.create_model("nonexistent", "model")

    def test_empty_model_id_raises(self, factory):
        with pytest.raises(ValueError, match="ID do modelo não pode estar vazio"):
            factory.create_model("ollama", "")

    def test_empty_factory_type_raises(self, factory):
        with pytest.raises(ValueError, match="Tipo de modelo não pode estar vazio"):
            factory.create_model("", "model")

    def test_validate_valid_config(self, factory):
        result = factory.validate_model_config("ollama", "llama3.2:latest")
        assert result["valid"] is True
        assert result["errors"] == []

    def test_validate_invalid_factory(self, factory):
        result = factory.validate_model_config("nonexistent", "model")
        assert result["valid"] is False
        assert len(result["errors"]) > 0

    def test_validate_empty_model_id(self, factory):
        result = factory.validate_model_config("ollama", "")
        assert result["valid"] is False

    def test_get_supported_models(self):
        models = ModelFactory.get_supported_models()
        assert "ollama" in models
        assert "openai" in models

    def test_is_supported_model(self):
        assert ModelFactory.is_supported_model("ollama") is True
        assert ModelFactory.is_supported_model("google") is True
        assert ModelFactory.is_supported_model("nonexistent") is False

    def test_alias_google_to_gemini(self, factory):
        result = factory.validate_model_config("google", "gemini-pro")
        assert result["factory_type"] == "gemini"


class TestOllamaHost:
    """OLLAMA_BASE_URL (via composition root) chega ao modelo Ollama do agno."""

    def test_ollama_usa_host_configurado(self, mock_logger):
        factory = ModelFactory(logger=mock_logger, ollama_host="http://ollama:11434")
        model = factory.create_model("ollama", "llama3.2:latest")
        assert model.host == "http://ollama:11434"

    def test_host_explicito_do_chamador_prevalece(self, mock_logger):
        factory = ModelFactory(logger=mock_logger, ollama_host="http://ollama:11434")
        model = factory.create_model("ollama", "llama3.2:latest", host="http://outro:11434")
        assert model.host == "http://outro:11434"

    def test_sem_host_configurado_mantem_default_do_cliente(self, factory):
        model = factory.create_model("ollama", "llama3.2:latest")
        assert model.host is None

    def test_host_nao_vaza_para_outros_providers(self, mock_logger):
        factory = ModelFactory(logger=mock_logger, ollama_host="http://ollama:11434")
        model = factory.create_model("openai", "gpt-4o-mini", api_key="sk-test-fake")
        assert not hasattr(model, "host") or model.host != "http://ollama:11434"
