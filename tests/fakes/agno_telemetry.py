"""Gravador da telemetria do agno: intercepta no ``httpx`` todo request à API do agno (nada sai da máquina).

As duas linhas do agno mandam a telemetria por ``httpx``: o 2.5.8 abre um ``httpx.Client``/
``httpx.AsyncClient`` por evento, na hora (``agno/api/agent.py``, ``agno/api/os.py``); o 3.x põe o
evento numa fila e uma thread daemon o envia por um ``httpx.Client`` próprio (``agno/api/api.py``,
``_TelemetryDispatcher``), e o launch do AgentOS sai num lifespan. Por isso a troca é no ``send`` das
classes do ``httpx`` (vale para cliente já criado e para a thread) e a espera é por um evento de
controle: a fila é FIFO com um só consumidor, então quando o controle chega o que veio antes também
chegou. Request a outro host segue para o ``send`` original (ex.: o ``ASGITransport`` dos testes).

Uso: ``recorder = AgnoTelemetryRecorder().install(monkeypatch)``; depois ``await recorder.wait_for(evento)``
e ``recorder.events()``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from agno.api.settings import agno_api_settings

TelemetryEvent = tuple[str, str | None]
"""``(path, id)``: ``("/telemetry/os", os_id)`` ou ``("/telemetry/runs", agent_id ou team_id)``."""


class AgnoTelemetryRecorder:
    """Responde 200 a todo request à API do agno e o guarda em ``requests``, na ordem de chegada."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._host = httpx.URL(agno_api_settings.api_url).host

    def install(self, monkeypatch: pytest.MonkeyPatch) -> AgnoTelemetryRecorder:
        real_send = httpx.Client.send
        real_async_send = httpx.AsyncClient.send

        def send(client: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
            if request.url.host != self._host:
                return real_send(client, request, **kwargs)
            return self._record(request)

        async def async_send(client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any) -> httpx.Response:
            if request.url.host != self._host:
                return await real_async_send(client, request, **kwargs)
            return self._record(request)

        monkeypatch.setattr(httpx.Client, "send", send)
        monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
        return self

    def _record(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={}, request=request)

    def events(self) -> list[TelemetryEvent]:
        """Os requests gravados como ``(path, id da entidade ou do AgentOS)``."""
        events: list[TelemetryEvent] = []
        for request in list(self.requests):
            body = json.loads(request.content or b"{}")
            data = body.get("data") or {}
            entity_id = body.get("os_id") or data.get("agent_id") or data.get("team_id")
            events.append((request.url.path, entity_id))
        return events

    async def wait_for(self, event: TelemetryEvent, limit: float = 10.0) -> None:
        """Espera ``event`` chegar (o envio do 3.x é numa thread); falha alto depois de ``limit`` segundos."""
        for _ in range(int(limit / 0.01)):
            if event in self.events():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"evento de telemetria {event} não chegou; recebidos: {self.events()}")
