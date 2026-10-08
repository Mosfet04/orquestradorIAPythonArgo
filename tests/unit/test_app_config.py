"""Testes unitários para AppConfig."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from src.infrastructure.config.app_config import AppConfig

ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - valor de APP_HOST dentro do container


class TestAppConfig:
    def test_load_with_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            config = AppConfig.load()
        assert config.mongo_database_name == "agno"
        assert config.app_port == 7777

    def test_load_from_env(self):
        env = {
            "MONGO_CONNECTION_STRING": "mongodb://custom:9999",
            "MONGO_DATABASE_NAME": "mydb",
            "APP_TITLE": "Custom",
            "APP_HOST": "127.0.0.1",
            "APP_PORT": "8888",
            "LOG_LEVEL": "DEBUG",
            "OLLAMA_BASE_URL": "http://ollama:11434",
        }
        with patch.dict(os.environ, env, clear=True):
            config = AppConfig.load()
        assert config.mongo_connection_string == "mongodb://custom:9999"
        assert config.app_port == 8888
        assert config.log_level == "DEBUG"

    def test_host_padrao_e_loopback_fora_do_container(self):
        with patch.dict(os.environ, {}, clear=True):
            config = AppConfig.load()
        assert config.app_host == "127.0.0.1"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_app_host_vazio_ou_em_branco_cai_no_loopback(self, value):
        with patch.dict(os.environ, {"APP_HOST": value}, clear=True):
            config = AppConfig.load()
        assert config.app_host == "127.0.0.1"

    def test_app_host_e_ollama_sem_espacos_nas_pontas(self):
        env = {"APP_HOST": " 10.0.0.5 ", "OLLAMA_BASE_URL": " http://ollama:11434 "}
        with patch.dict(os.environ, env, clear=True):
            config = AppConfig.load()
        assert config.app_host == "10.0.0.5"
        assert config.ollama_base_url == "http://ollama:11434"

    @pytest.mark.parametrize("env", [{}, {"OLLAMA_BASE_URL": ""}, {"OLLAMA_BASE_URL": "   "}])
    def test_sem_ollama_base_url_preserva_defaults_do_cliente(self, env):
        """Sem valor, o agno/ollama decide (OLLAMA_HOST, localhost ou Ollama Cloud com chave)."""
        with patch.dict(os.environ, env, clear=True):
            config = AppConfig.load()
        assert config.ollama_base_url is None

    def test_host_e_ollama_vem_do_ambiente(self):
        env = {"APP_HOST": ALL_INTERFACES, "APP_PORT": "9000", "OLLAMA_BASE_URL": "http://ollama:11434"}
        with patch.dict(os.environ, env, clear=True):
            config = AppConfig.load()
        assert config.app_host == ALL_INTERFACES
        assert config.app_port == 9000
        assert config.ollama_base_url == "http://ollama:11434"

    def test_frozen_cannot_change(self):
        config = AppConfig.load()
        with pytest.raises(AttributeError):
            config.app_port = 1234  # type: ignore[misc]

    def test_validate_empty_connection_string(self):
        with patch.dict(os.environ, {"MONGO_CONNECTION_STRING": ""}, clear=True):
            with pytest.raises(ValueError, match="MONGO_CONNECTION_STRING"):
                AppConfig.load()

    def test_validate_empty_database_name(self):
        with patch.dict(
            os.environ,
            {"MONGO_CONNECTION_STRING": "mongodb://x", "MONGO_DATABASE_NAME": ""},
            clear=True,
        ):
            with pytest.raises(ValueError, match="MONGO_DATABASE_NAME"):
                AppConfig.load()


# ── F1-03: ENVIRONMENT, ENABLE_DOCS, CORS_ALLOWED_ORIGINS ───────────

DEFAULT_ORIGINS = (
    "https://app.agno.com",
    "https://www.agno.com",
    "http://localhost:3000",
    "http://localhost:7777",
    "https://os.agno.com",
)


def _load(**env: str) -> AppConfig:
    with patch.dict(os.environ, env, clear=True):
        return AppConfig.load()


class TestEnvironment:
    def test_default_e_development(self):
        assert _load().environment == "development"

    @pytest.mark.parametrize("value", ["development", "test", "staging", "production", " Production "])
    def test_valores_validos(self, value):
        assert _load(ENVIRONMENT=value).environment == value.strip().lower()

    @pytest.mark.parametrize("value", ["prod", "dev", "qualquer"])
    def test_valor_invalido_falha_com_mensagem_clara(self, value):
        with pytest.raises(ValueError, match=r"ENVIRONMENT inválido.*development, test, staging, production"):
            _load(ENVIRONMENT=value)


class TestEnableDocs:
    @pytest.mark.parametrize(
        ("environment", "expected"),
        [("development", True), ("test", False), ("staging", False), ("production", False)],
    )
    def test_default_ligado_so_em_development(self, environment, expected):
        assert _load(ENVIRONMENT=environment).enable_docs is expected

    @pytest.mark.parametrize(("value", "expected"), [("true", True), ("1", True), ("FALSE", False), ("0", False)])
    def test_valor_explicito_vence_o_default(self, value, expected):
        assert _load(ENVIRONMENT="production", ENABLE_DOCS=value).enable_docs is expected
        assert _load(ENVIRONMENT="development", ENABLE_DOCS=value).enable_docs is expected

    def test_vazio_usa_o_default(self):
        assert _load(ENABLE_DOCS="  ").enable_docs is True

    def test_valor_invalido_falha(self):
        with pytest.raises(ValueError, match="ENABLE_DOCS"):
            _load(ENABLE_DOCS="talvez")


class TestCorsAllowedOrigins:
    @pytest.mark.parametrize("env", [{}, {"CORS_ALLOWED_ORIGINS": ""}, {"CORS_ALLOWED_ORIGINS": " , "}])
    def test_default_sao_as_origens_atuais(self, env):
        assert _load(**env).cors_allowed_origins == DEFAULT_ORIGINS

    def test_lista_separada_por_virgula(self):
        config = _load(CORS_ALLOWED_ORIGINS=" https://a.example.com,https://b.example.com , ")
        assert config.cors_allowed_origins == ("https://a.example.com", "https://b.example.com")

    def test_curinga_e_rejeitado(self):
        """Com credenciais, ``*`` faria o Starlette ecoar qualquer origem."""
        with pytest.raises(ValueError, match="CORS_ALLOWED_ORIGINS"):
            _load(CORS_ALLOWED_ORIGINS="https://a.example.com,*")

    @pytest.mark.parametrize(
        "origin",
        ["http://localhost:3000", "https://os.agno.com", "http://127.0.0.1:7777", "https://[::1]:8443"],
    )
    def test_origem_valida(self, origin):
        assert _load(CORS_ALLOWED_ORIGINS=origin).cors_allowed_origins == (origin,)

    @pytest.mark.parametrize(
        ("origin", "motivo"),
        [
            ("https://a.example.com/", "barra final"),
            ("https://a.example.com/app", "path"),
            ("https://a.example.com?x=1", "query"),
            ("https://a.example.com#frag", "fragment"),
            ("null", "null"),
            ("a.example.com", "http"),
            ("ftp://a.example.com", "http"),
            ("https://", "host"),
            ("https://user:pw@a.example.com", "credenciais"),
            ("https://a.example.com:porta", "porta"),
        ],
    )
    def test_origem_invalida_falha_com_mensagem_clara(self, origin, motivo):
        with pytest.raises(ValueError, match=rf"CORS_ALLOWED_ORIGINS.*{motivo}"):
            _load(CORS_ALLOWED_ORIGINS=f"https://ok.example.com,{origin}")


def test_dataclass_sem_env_nao_liga_docs():
    """Default seguro também para quem constrói o AppConfig direto (sem ``load``)."""
    config = AppConfig(
        mongo_connection_string="mongodb://x",
        mongo_database_name="db",
        app_title="t",
        app_host="127.0.0.1",
        app_port=7777,
        log_level="INFO",
        ollama_base_url=None,
    )
    assert config.enable_docs is False
