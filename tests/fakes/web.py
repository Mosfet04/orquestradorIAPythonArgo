"""Cliente HTTP de teste que se apresenta como acesso local (loopback).

No modo dev local (sem ``API_KEY_RUN``/``API_KEY_ADMIN``) a auth da borda só libera
request com cliente, servidor e ``Host`` em loopback; o ``TestClient`` padrão usa
``testclient``/``testserver``, que não são loopback.
"""

from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient
from starlette.types import ASGIApp

LOOPBACK_BASE_URL = "http://127.0.0.1:7777"
LOOPBACK_CLIENT = ("127.0.0.1", 50000)


def loopback_client(app: ASGIApp, **kwargs: Any) -> TestClient:
    """``TestClient`` com cliente, servidor e ``Host`` em 127.0.0.1."""
    kwargs.setdefault("base_url", LOOPBACK_BASE_URL)
    kwargs.setdefault("client", LOOPBACK_CLIENT)
    return TestClient(app, **kwargs)
