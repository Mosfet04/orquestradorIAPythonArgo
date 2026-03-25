"""Serviço de health check da aplicação."""

from __future__ import annotations

import asyncio
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient


class HealthService:
    """Verifica saúde de MongoDB, memória e OTLP."""

    def __init__(self, mongo_client: AsyncIOMotorClient) -> None:
        self._mongo_client = mongo_client

    async def check_async(self) -> dict:
        start = asyncio.get_event_loop().time()
        checks = await asyncio.gather(
            self._check_mongodb(),
            self._check_memory(),
            self._check_otlp(),
            return_exceptions=True,
        )
        elapsed = asyncio.get_event_loop().time() - start

        def _ok(c: Any) -> bool:
            return not isinstance(c, Exception) and c.get("status") not in (
                "error",
                "unhealthy",
            )

        return {
            "status": "healthy" if all(_ok(c) for c in checks) else "unhealthy",
            "checks": {
                "mongodb": checks[0] if not isinstance(checks[0], Exception) else {"status": "error", "error": str(checks[0])},
                "memory": checks[1] if not isinstance(checks[1], Exception) else {"status": "error", "error": str(checks[1])},
                "otlp": checks[2] if not isinstance(checks[2], Exception) else {"status": "error", "error": str(checks[2])},
            },
            "response_time_ms": round(elapsed * 1000, 2),
        }

    async def _check_mongodb(self) -> dict:
        try:
            await self._mongo_client.admin.command("ping")
            return {"status": "healthy"}
        except Exception as exc:
            return {"status": "unhealthy", "error": str(exc)}

    @staticmethod
    async def _check_memory() -> dict:
        try:
            import psutil

            mem = psutil.virtual_memory()
            return {
                "status": "healthy" if mem.percent < 90 else "warning",
                "usage_percent": mem.percent,
                "available_gb": round(mem.available / (1024**3), 2),
            }
        except ImportError:
            return {"status": "unavailable", "message": "psutil não instalado"}

    @staticmethod
    async def _check_otlp() -> dict:
        """Verifica se o endpoint OTLP (Grafana LGTM) está acessível."""
        try:
            from opentelemetry import trace

            provider = trace.get_tracer_provider()
            if provider and hasattr(provider, "force_flush"):
                provider.force_flush(timeout_millis=2000)
                return {"status": "healthy", "provider": type(provider).__name__}
            return {"status": "not_configured"}
        except Exception as exc:
            return {"status": "warning", "error": str(exc)}
