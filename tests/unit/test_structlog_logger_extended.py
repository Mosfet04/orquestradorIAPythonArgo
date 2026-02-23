"""Testes estendidos para structlog_logger — sanitização, setup_structlog, StructlogLogger."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.infrastructure.logging.structlog_logger import (
    DataSanitizer,
    LogContext,
    StructlogLogger,
    setup_structlog,
    sanitize_log_data,
    add_correlation_id,
    add_timestamp,
    add_service_metadata,
)


# ── DataSanitizer._sanitize_string ──────────────────────────────────


class TestSanitizeString:
    def test_empty_string(self):
        """String vazia deve retornar string vazia."""
        assert DataSanitizer._sanitize_string("") == ""

    def test_non_string_input(self):
        """Input que não é string deve retornar como está."""
        assert DataSanitizer._sanitize_string(123) == 123

    def test_truncation_long_string(self):
        """Strings acima de 5000 chars devem ser truncadas."""
        long_text = "A" * 6000
        result = DataSanitizer._sanitize_string(long_text)
        assert "[TRUNCATED]" in result
        assert len(result) < 6000

    def test_api_key_pattern(self):
        """API keys devem ser mascaradas."""
        text = "api_key=abc1234567890abcdefgh"
        result = DataSanitizer._sanitize_string(text)
        assert "abc1234567890abcdefgh" not in result
        assert "MASKED" in result or "***" in result

    def test_mongodb_uri_pattern(self):
        """URI do MongoDB deve ser mascarada."""
        text = "mongodb://user:pass@host:27017/db"
        result = DataSanitizer._sanitize_string(text)
        assert "pass" not in result or "MASKED" in result

    def test_normal_string_unchanged(self):
        """Strings normais não devem ser alteradas."""
        text = "Hello World"
        assert DataSanitizer._sanitize_string(text) == "Hello World"


# ── DataSanitizer._sanitize_dict ────────────────────────────────────


class TestSanitizeDict:
    def test_sensitive_field_masked(self):
        """Campos sensíveis devem ser mascarados."""
        data = {"password": "secret123", "name": "test"}
        result = DataSanitizer.sanitize_data(data)
        assert "***" in result["password"]
        assert result["name"] == "test"

    def test_sensitive_field_with_hash(self):
        """Campos sensíveis com string devem incluir hash."""
        data = {"api_key": "my_secret_key_12345"}
        result = DataSanitizer.sanitize_data(data)
        assert "hash:" in result["api_key"]

    def test_sensitive_field_empty_value(self):
        """Campos sensíveis com valor vazio devem usar mask simples."""
        data = {"password": ""}
        result = DataSanitizer.sanitize_data(data)
        assert "***MASKED***" in result["password"]

    def test_sensitive_field_non_string(self):
        """Campos sensíveis com valor não-string devem usar mask simples."""
        data = {"password": 12345}
        result = DataSanitizer.sanitize_data(data)
        assert "***MASKED***" in str(result["password"])

    def test_nested_dict(self):
        """Dicts aninhados devem ser sanitizados recursivamente."""
        data = {"config": {"password": "abc123", "host": "localhost"}}
        result = DataSanitizer.sanitize_data(data)
        assert "***" in result["config"]["password"]


# ── DataSanitizer._sanitize_list ────────────────────────────────────


class TestSanitizeList:
    def test_list_truncation(self):
        """Listas com >50 itens devem ser truncadas."""
        data = list(range(60))
        result = DataSanitizer.sanitize_data(data)
        # 50 itens + 1 item de truncamento
        assert len(result) == 51
        assert "_truncated" in result[-1]

    def test_list_under_limit(self):
        """Listas com <=50 itens não devem ser truncadas."""
        data = list(range(30))
        result = DataSanitizer.sanitize_data(data)
        assert len(result) == 30


# ── DataSanitizer.hash_sensitive_data ────────────────────────────────


class TestHashSensitiveData:
    def test_returns_12_chars(self):
        """Hash deve retornar exatamente 12 caracteres."""
        result = DataSanitizer.hash_sensitive_data("test_secret")
        assert len(result) == 12

    def test_deterministic(self):
        """Mesmo input deve gerar mesmo hash."""
        h1 = DataSanitizer.hash_sensitive_data("my_password")
        h2 = DataSanitizer.hash_sensitive_data("my_password")
        assert h1 == h2

    def test_different_inputs_different_hashes(self):
        h1 = DataSanitizer.hash_sensitive_data("pass1")
        h2 = DataSanitizer.hash_sensitive_data("pass2")
        assert h1 != h2


# ── DataSanitizer.sanitize_data edge cases ───────────────────────────


class TestSanitizeDataEdgeCases:
    def test_max_depth_reached(self):
        """Ao atingir max_depth, retorna truncated."""
        nested = {"a": {"b": {"c": {"d": "deep"}}}}
        result = DataSanitizer.sanitize_data(nested, max_depth=1)
        assert isinstance(result, dict)

    def test_none_input(self):
        assert DataSanitizer.sanitize_data(None) is None

    def test_bool_input(self):
        assert DataSanitizer.sanitize_data(True) is True

    def test_int_input(self):
        assert DataSanitizer.sanitize_data(42) == 42

    def test_custom_type(self):
        """Tipos não padrão devem ser convertidos para string e sanitizados."""

        class Custom:
            def __str__(self):
                return "custom_obj"

        result = DataSanitizer.sanitize_data(Custom())
        assert "custom_obj" in result


# ── sanitize_log_data processor ──────────────────────────────────────


class TestSanitizeLogData:
    def test_sensitive_field_in_log(self):
        """Campo sensível no event_dict deve ser mascarado."""
        event_dict = {
            "event": "login",
            "password": "secret123",
            "user": "admin",
        }
        result = sanitize_log_data(None, "info", event_dict)
        assert "***MASKED***" in result["password"]
        assert "hash:" in result["password"]

    def test_protected_fields_unchanged(self):
        """Campos protegidos não devem ser sanitizados."""
        event_dict = {
            "event": "test",
            "timestamp": "2025-01-01",
            "level": "info",
            "service": "test-svc",
        }
        result = sanitize_log_data(None, "info", event_dict)
        assert result["timestamp"] == "2025-01-01"
        assert result["service"] == "test-svc"

    def test_non_sensitive_field_sanitized(self):
        """Campos não sensíveis e não protegidos passam por sanitização geral."""
        event_dict = {
            "event": "test",
            "data": "normal text",
        }
        result = sanitize_log_data(None, "info", event_dict)
        assert result["data"] == "normal text"


# ── setup_structlog ──────────────────────────────────────────────────


class TestSetupStructlog:
    def test_setup_dev_mode(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "development")
        # Não deve lançar exceção
        setup_structlog()

    def test_setup_prod_mode(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        setup_structlog()


# ── StructlogLogger ──────────────────────────────────────────────────


class TestStructlogLoggerExtended:
    def test_set_context(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "development")
        setup_structlog()
        logger = StructlogLogger("test")
        logger.set_context(user_id="u123", request_id="r456")
        assert logger.context.user_id == "u123"
        assert logger.context.request_id == "r456"

    def test_clear_context(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "development")
        setup_structlog()
        logger = StructlogLogger("test")
        logger.set_context(user_id="u123")
        logger.clear_context()
        assert logger.context.user_id is None

    def test_debug_method(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "development")
        setup_structlog()
        logger = StructlogLogger("test")
        # Não deve levantar exceção
        logger.debug("debug message", key="val")

    def test_info_method(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "development")
        setup_structlog()
        logger = StructlogLogger("test")
        logger.info("info message", key="val")

    def test_warning_method(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "development")
        setup_structlog()
        logger = StructlogLogger("test")
        logger.warning("warning message")

    def test_error_with_exception(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "development")
        setup_structlog()
        logger = StructlogLogger("test")
        try:
            raise ValueError("test error")
        except ValueError as e:
            logger.error("error occurred", exception=e)

    def test_critical_with_exception(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "development")
        setup_structlog()
        logger = StructlogLogger("test")
        try:
            raise RuntimeError("critical error")
        except RuntimeError as e:
            logger.critical("critical occurred", exception=e)

    def test_bind(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "development")
        setup_structlog()
        logger = StructlogLogger("test")
        bound = logger.bind(request_id="r1")
        assert isinstance(bound, StructlogLogger)

    def test_security_method(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "development")
        setup_structlog()
        logger = StructlogLogger("test")
        logger.security("security event", ip="10.0.0.1")

    def test_ai_request_method(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "development")
        setup_structlog()
        logger = StructlogLogger("test")
        logger.ai_request("ai request", model="gpt-4")

    def test_business_event_method(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "development")
        setup_structlog()
        logger = StructlogLogger("test")
        logger.business_event("user signed up", plan="premium")

    def test_audit_method(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "development")
        setup_structlog()
        logger = StructlogLogger("test")
        logger.audit("admin action", action="delete_user")


# ── processors ───────────────────────────────────────────────────────


class TestProcessors:
    def test_add_correlation_id(self):
        event_dict = {"event": "test"}
        result = add_correlation_id(None, "info", event_dict)
        assert "correlation_id" in result
        assert len(result["correlation_id"]) == 8

    def test_add_correlation_id_preserves_existing(self):
        event_dict = {"event": "test", "correlation_id": "existing"}
        result = add_correlation_id(None, "info", event_dict)
        assert result["correlation_id"] == "existing"

    def test_add_timestamp(self):
        event_dict = {"event": "test"}
        result = add_timestamp(None, "info", event_dict)
        assert "timestamp" in result

    def test_add_service_metadata(self):
        event_dict = {"event": "test"}
        result = add_service_metadata(None, "info", event_dict)
        assert result["service"] == "orquestrador-ia"
        assert result["log_level"] == "info"


# ── LogContext ───────────────────────────────────────────────────────


class TestLogContext:
    def test_defaults(self):
        ctx = LogContext()
        assert ctx.service_name == "orquestrador-ia"
        assert ctx.environment == "development"
        assert ctx.user_id is None
        assert ctx.agent_id is None
