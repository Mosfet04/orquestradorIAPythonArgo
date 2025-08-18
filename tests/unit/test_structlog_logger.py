from src.infrastructure.logging.structlog_logger import LoggerFactory, StructlogLogger, DataSanitizer


def test_structlog_logger_basic_flow(monkeypatch):
    # Forçar ambiente de dev
    monkeypatch.setenv('ENVIRONMENT', 'development')
    logger = LoggerFactory.get_logger("test_structlog")
    assert isinstance(logger, StructlogLogger)

    # Bind e contexto
    logger.set_context(user_id="u1", request_id="r1")
    logger.info("msg", action="do")
    logger.performance("perf", duration=1.23)
    logger.security("sec", ip="127.0.0.1")

    # Sanitização simples
    out = DataSanitizer.sanitize_data({"password": "abc12345"})
    assert '***' in out['password']
