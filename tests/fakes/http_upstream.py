"""Upstream HTTP falso das tools: um ``httpx.MockTransport`` que grava os requests (nada sai da máquina).

Uso: ``upstream = RecordingUpstream().install(monkeypatch)``. ``upstream.seen`` lista os requests recebidos e
``upstream.handler`` pode ser trocado no meio do teste (a resposta default é 200 ``{"ok": 1}``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest


class RecordingUpstream:
    """Grava os requests que o ``httpx.AsyncClient`` da tool mandou e responde com ``handler``."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.handler: Callable[[httpx.Request], httpx.Response] = lambda request: httpx.Response(200, json={"ok": 1})

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.handler(request)

    def install(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        wrap: Callable[[httpx.AsyncBaseTransport], httpx.AsyncBaseTransport] | None = None,
        **client_defaults: Any,
    ) -> RecordingUpstream:
        """Troca o transporte de todo ``httpx.AsyncClient`` criado depois.

        ``wrap`` envolve o transporte (ex.: instrumentação do OTel); ``client_defaults`` são argumentos do
        cliente que a tool pode sobrescrever (ex.: ``follow_redirects=True``).
        """
        real_client: Callable[..., httpx.AsyncClient] = httpx.AsyncClient
        mock = httpx.MockTransport(self._handle)

        def client(**kwargs: Any) -> httpx.AsyncClient:
            transport: httpx.AsyncBaseTransport = wrap(mock) if wrap else mock
            return real_client(transport=transport, **{**client_defaults, **kwargs})

        monkeypatch.setattr(httpx, "AsyncClient", client)
        return self
