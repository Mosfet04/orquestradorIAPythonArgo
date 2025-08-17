import time
import pytest

from src.infrastructure.logging.decorators import log_execution, log_performance


def test_log_execution_success_and_error(monkeypatch):
    @log_execution(logger_name="orquestrador_ia", include_args=True, include_result=True)
    def ok(a, b=2):
        return a + b

    @log_execution(logger_name="orquestrador_ia", include_args=True)
    def boom(x):
        raise RuntimeError("x")

    assert ok(1, b=3) == 4

    with pytest.raises(RuntimeError):
        boom(10)


def test_log_performance_threshold(monkeypatch):
    # Função que deve ultrapassar o threshold
    @log_performance(threshold_seconds=0.01, logger_name="orquestrador_ia")
    def slow():
        time.sleep(0.02)
        return 42

    # Função que não ultrapassa
    @log_performance(threshold_seconds=0.5, logger_name="orquestrador_ia")
    def fast():
        return 7

    assert slow() == 42
    assert fast() == 7
