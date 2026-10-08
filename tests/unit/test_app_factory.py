"""Testes para AppFactory."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from starlette.testclient import TestClient

from src.infrastructure.web.app_factory import AppFactory


class TestAppFactory:
    def test_create_app_returns_fastapi(self):
        """create_app é síncrono e retorna FastAPI."""
        factory = AppFactory()
        app = factory.create_app()
        assert isinstance(app, FastAPI)

    def test_create_app_has_admin_endpoints(self):
        """Verifica que endpoints admin foram registrados."""
        factory = AppFactory()
        app = factory.create_app()
        routes = [r.path for r in app.routes]
        assert "/admin/health" in routes
        assert "/metrics/cache" in routes
        assert "/admin/refresh-cache" in routes


@pytest.mark.usefixtures("reset_otel_providers")
def test_create_app_instrumenta_fastapi_antes_da_pilha_de_middleware():
    """O span de servidor HTTP sai mesmo com o TracerProvider definido depois (no lifespan)."""
    app = AppFactory().create_app()

    @app.get("/sonda")
    async def sonda() -> dict[str, str]:
        return {"ok": "sim"}

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    client = TestClient(app)  # sem `with`: o lifespan (Mongo, AgentOS) não roda
    assert client.get("/sonda").status_code == 200
    assert client.get("/admin/health").status_code == 200

    servidor = [s for s in exporter.get_finished_spans() if s.kind is SpanKind.SERVER]
    assert [s.attributes.get("http.route") for s in servidor] == ["/sonda"]  # /admin/health excluído


@pytest.mark.usefixtures("reset_otel_providers")
@pytest.mark.parametrize("host", ["livez", "admin", "metrics", "x.livez.example"])
def test_exclusao_do_otel_casa_so_o_path_e_nao_o_host(host: str):
    """``excluded_urls`` é regex buscada em ``scheme://<Host><path>``: o Host do cliente não pode apagar spans."""
    app = AppFactory().create_app()

    @app.get("/sonda")
    async def sonda() -> dict[str, str]:
        return {"ok": "sim"}

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    client = TestClient(app)
    assert client.get("/sonda", headers={"Host": host}).status_code == 200
    assert client.get("/livez", headers={"Host": host}).status_code == 200

    servidor = [s for s in exporter.get_finished_spans() if s.kind is SpanKind.SERVER]
    rotas = [s.attributes.get("http.route") for s in servidor]
    assert "/sonda" in rotas  # rota real com Host hostil continua rastreada
    assert "/livez" not in rotas  # a sonda de liveness segue excluída
