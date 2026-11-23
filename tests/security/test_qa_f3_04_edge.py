"""F3-04 (QA): as rotas admin do agno 3.x já são recusadas à chave run pela borda real (middleware)."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.types import Message, Receive, Scope, Send

from src.infrastructure.web.api_key_auth import ApiKeyAuthMiddleware, ApiKeys

RUN_KEY = "f304-qa-run-key-" + "r" * 20
ADMIN_KEY = "f304-qa-admin-key-" + "a" * 18
KEYS = ApiKeys(run=RUN_KEY.encode(), admin=ADMIN_KEY.encode())
_calls: list[str] = []


async def _inner(scope: Scope, receive: Receive, send: Send) -> None:
    _calls.append(scope["path"])
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _status(method: str, path: str, key: str | None, root_path: str = "") -> int:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    headers = [] if key is None else [(b"authorization", f"Bearer {key}".encode())]
    scope: dict[str, Any] = {
        "type": "http", "method": method, "path": path, "root_path": root_path, "headers": headers,
        "client": ("10.0.0.2", 1), "server": ("10.0.0.1", 7777), "state": {},
    }
    await ApiKeyAuthMiddleware(_inner, keys=KEYS, docs_require_admin=False)(scope, receive, send)
    return int(sent[0]["status"])


ADMIN_3X = [
    ("GET", "/queue"),
    ("POST", "/queue/dlq/requeue"),
    ("DELETE", "/queue/dlq/abc"),
    ("GET", "/queue/"),
    ("GET", "/service-accounts"),
    ("POST", "/service-accounts"),
    ("DELETE", "/service-accounts/sa1"),
    ("GET", "/filesystem/content"),
    ("GET", "/filesystem/files/"),
    ("GET", "/learnings"),
    ("GET", "/learnings/users"),
    ("DELETE", "/learnings/users/ana"),
    ("PATCH", "/learnings/l1"),
]


@pytest.mark.parametrize(("method", "path"), ADMIN_3X, ids=str)
async def test_chave_run_nas_rotas_admin_do_3x_responde_403_e_nada_chega_ao_app(method: str, path: str):
    _calls.clear()

    assert await _status(method, path, RUN_KEY) == 403
    assert await _status(method, path, None) == 401
    assert _calls == []
    assert await _status(method, path, ADMIN_KEY) == 200
    assert _calls == [path]


async def test_root_path_nao_esconde_rota_admin_do_3x():
    assert await _status("GET", "/api/queue/dlq", RUN_KEY, root_path="/api") == 403
    assert await _status("GET", "/api/learnings", ADMIN_KEY, root_path="/api") == 200


@pytest.mark.parametrize("path", ["/queuex", "/learnings-x", "/filesystem2/files", "/service-accountsx"])
async def test_prefixo_parecido_nao_vira_admin(path: str):
    assert await _status("GET", path, RUN_KEY) == 200
