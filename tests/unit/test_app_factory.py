"""Testes para AppFactory."""

from __future__ import annotations

from fastapi import FastAPI

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
