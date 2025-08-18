import logging

from src.infrastructure.logging.secure_logger import SecureLogger, DataSanitizer


def test_secure_logger_basic_methods(caplog, tmp_path, monkeypatch):
    # Garantir nível de captura amplo
    caplog.set_level(logging.DEBUG)

    logger = SecureLogger("test_secure")
    logger.set_context(user_id="u1", request_id="r1")

    # Métodos de logging não devem lançar exceção
    logger.debug("dbg", {"k": "v"})
    logger.info("info", {"k": "v"})
    logger.warning("warn", {"k": "v"})
    logger.performance("perf", {"t": 1})
    logger.ai_request("ai", {"m": "gpt"})
    logger.security("sec", {"ip": "127.0.0.1"})

    # Erro e crítico com exceção
    try:
        raise ValueError("boom")
    except ValueError as e:
        logger.error("err", {"code": 500}, exception=e)
        logger.critical("crit", {"code": 501}, exception=e)

    # Deve haver registros capturados
    assert any("\"level\":\"ERROR\"" in rec.message or '"level":"ERROR"' in rec.message for rec in caplog.records)
    assert any("\"level\":\"CRITICAL\"" in rec.message or '"level":"CRITICAL"' in rec.message for rec in caplog.records)


def test_data_sanitizer_masks_sensitive_fields():
    payload = {
        "password": "secret1234",
        "token": "abcd" * 6,
        "nested": {"api_key": "XYZXYZXYZXYZXYZXYZ"},
        "list": [{"authorization": "Bearer abcdefghijkl"}, "plain"],
        "email": "user@example.com"
    }

    sanitized = DataSanitizer.sanitize_data(payload)

    # password deve estar mascarado com formato especial
    assert isinstance(sanitized, dict)
    assert "password" in sanitized
    assert "***" in sanitized["password"]
    assert "[len:" in sanitized["password"]

    # Campos sensíveis em nested e list devem estar mascarados (máscara parcial para SecureLogger)
    assert "***" in sanitized["nested"]["api_key"]
    assert "***" in sanitized["list"][0]["authorization"]
