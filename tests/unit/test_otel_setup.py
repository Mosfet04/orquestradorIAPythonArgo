from src.infrastructure.telemetry import otel_setup


class DummyConfig:
    def __init__(self, enabled: bool = False):
        self.otel_enabled = enabled
        self.otel_exporter_endpoint = "http://localhost:4317"
        self.otel_service_name = "orquestrador-ia"


def test_setup_telemetry_disabled_does_not_raise():
    cfg = DummyConfig(enabled=False)
    # Deve retornar sem exceção
    otel_setup.setup_telemetry(cfg)
