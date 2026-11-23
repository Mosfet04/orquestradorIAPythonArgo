"""Bloqueador de rede: registra e recusa toda tentativa de resolver nome ou conectar socket.

Uso: ``network = block_network(monkeypatch)``; depois ``network.attempts`` (ou ``network.mentions(host)``).
Não afeta o ``ASGITransport``/``TestClient`` (não abrem socket). Quem precisa distinguir o evento da
telemetria do agno usa o ``AgnoTelemetryRecorder`` (``tests/fakes/agno_telemetry.py``), que para o
request no ``httpx``, antes do socket.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest


class NetworkAttempts:
    """Tentativas de rede vistas pelo bloqueador, na ordem; nenhuma delas funciona."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def hosts(self) -> list[str]:
        return list(self.attempts)

    def mentions(self, needle: str) -> bool:
        return any(needle in a for a in self.attempts)


def block_network(monkeypatch: pytest.MonkeyPatch) -> NetworkAttempts:
    """Troca ``getaddrinfo``/``connect``/``connect_ex`` por versões que registram e falham."""
    seen = NetworkAttempts()

    def _name(host: object) -> str:
        return host.decode() if isinstance(host, bytes) else str(host)

    def getaddrinfo(host: object, *_: object, **__: object) -> Any:
        seen.attempts.append(f"getaddrinfo:{_name(host)}")
        raise OSError("rede bloqueada pelo teste")

    def connect(_self: socket.socket, address: object) -> Any:
        seen.attempts.append(f"connect:{address}")
        raise OSError("rede bloqueada pelo teste")

    def connect_ex(_self: socket.socket, address: object) -> int:
        seen.attempts.append(f"connect_ex:{address}")
        return 111

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    return seen
