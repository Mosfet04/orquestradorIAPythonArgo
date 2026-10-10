"""QA do F1-04: auth fail-closed de ponta a ponta com o app real (AppFactory + AgentOS + Agent/Team fake).

Complementa ``test_api_key_auth.py`` (dev): fluxo completo de run com cada chave, varredura
de TODAS as rotas reais do app contra o classificador, rotação de chave, limites de tamanho,
credenciais hostis e tentativas de contornar o classificador por caminho. As chaves são
valores de teste, não segredos.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from agno.agent import Agent
from agno.team import Team
from fastapi import FastAPI
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.types import ASGIApp

from src.infrastructure.config.app_config import API_KEY_MIN_LENGTH
from src.infrastructure.runtime.agno import AgnoRuntime
from src.infrastructure.web.api_key_auth import RouteAccess, classify_route
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel, loopback_client
from tests.fakes.web import agno_runtime, mount_agent_os

ALL_INTERFACES = "0.0.0" + ".0"  # bind recusado sem chaves: é o cenário sob teste
RUN_KEY = "qa-run-key-" + "r" * 21
ADMIN_KEY = "qa-admin-key-" + "a" * 19
ALLOWED = "https://painel.example.com"
KEYS = {"API_KEY_RUN": RUN_KEY, "API_KEY_ADMIN": ADMIN_KEY}
PROD = {"APP_HOST": ALL_INTERFACES, "ENVIRONMENT": "production", **KEYS}
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")
AGUI_BODY = {
    "threadId": "t1",
    "runId": "r1",
    "state": {},
    "messages": [{"id": "m1", "role": "user", "content": "oi"}],
    "tools": [],
    "context": [],
    "forwardedProps": {},
}


def _entities(answer: str = "resposta do agente") -> tuple[Agent, Team]:
    agent = Agent(id="agente-1", name="Agente 1", model=FakeChatModel(responses=[answer] * 20), telemetry=False)
    member = Agent(id="membro-1", name="Membro", model=FakeChatModel(responses=["membro"] * 20), telemetry=False)
    team = Team(
        id="time-1",
        name="Time 1",
        members=[member],
        model=FakeChatModel(responses=["resposta do time"] * 20),
        telemetry=False,
    )
    return agent, team


def _make_app(mp: pytest.MonkeyPatch, entities: tuple[Agent, Team] | None, **env: str) -> FastAPI:
    for name in _ENV_NAMES:
        mp.delenv(name, raising=False)
    mp.setenv("AGNO_TELEMETRY", "false")
    mp.setenv("CORS_ALLOWED_ORIGINS", ALLOWED)
    for name, value in env.items():
        mp.setenv(name, value)
    factory = AppFactory()
    app = factory.create_app()
    if entities is not None:
        agent, team = entities
        mount_agent_os(factory, app, [agent], [team])
    return app


@pytest.fixture
def build_app(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FastAPI]:
    def _build(*, mount: bool = True, **env: str) -> FastAPI:
        return _make_app(monkeypatch, _entities() if mount else None, **env)

    return _build


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _sse_events(text: str) -> list[str]:
    return [line.removeprefix("event: ") for line in text.splitlines() if line.startswith("event: ")]


# ── fluxo completo de run com cada chave ─────────────────────────────

CREDENTIALS = {
    "run-bearer": _bearer(RUN_KEY),
    "run-x-api-key": {"X-API-Key": RUN_KEY},
    "admin-bearer": _bearer(ADMIN_KEY),
    "admin-x-api-key": {"X-API-Key": ADMIN_KEY},
}


@pytest.fixture
def prod(build_app: Callable[..., FastAPI]) -> TestClient:
    """App de produção novo a cada teste (para quem passa da auth e executa handler)."""
    return TestClient(build_app(**PROD), raise_server_exceptions=False)


@pytest.fixture(scope="module")
def shared_prod_app() -> Iterator[FastAPI]:
    """O mesmo app de ``prod``, montado uma vez por módulo (e por worker do xdist).

    Só para testes cujo request não executa handler com estado: a auth recusa (401/403),
    o CORS responde (preflight), o roteador não casa (404/405) ou é ``/livez``. Auth e
    CORS são imutáveis depois do ``create_app`` (as chaves são lidas só ali) e o env é
    restaurado logo após a montagem. A guarda do teardown prova que nenhum teste deste app
    chegou a um modelo.
    """
    agent, team = _entities()
    with pytest.MonkeyPatch.context() as mp:
        app = _make_app(mp, (agent, team), **PROD)
    yield app
    calls = [*agent.model.calls, *team.model.calls]  # type: ignore[union-attr]
    assert calls == [], "teste com o app compartilhado executou um modelo: use a fixture `prod`"


@pytest.fixture
def prod_rejecting(shared_prod_app: FastAPI) -> TestClient:
    """Cliente novo (sem cookies herdados) sobre ``shared_prod_app``; ver as restrições de lá."""
    return TestClient(shared_prod_app, raise_server_exceptions=False)


@pytest.mark.parametrize("headers", CREDENTIALS.values(), ids=CREDENTIALS)
def test_run_de_agente_sem_stream_devolve_o_conteudo(prod: TestClient, headers: dict[str, str]):
    response = prod.post("/agents/agente-1/runs", data={"message": "oi", "stream": "false"}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "resposta do agente"
    assert body["agent_id"] == "agente-1"


@pytest.mark.parametrize("headers", CREDENTIALS.values(), ids=CREDENTIALS)
def test_run_de_agente_com_stream_sse_chega_ao_fim(prod: TestClient, headers: dict[str, str]):
    response = prod.post("/agents/agente-1/runs", data={"message": "oi", "stream": "true"}, headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(response.text)
    assert events[0] == "RunStarted" and events[-1] == "RunCompleted"
    assert "resposta do agente" in response.text
    assert "RunError" not in events


@pytest.mark.parametrize("headers", CREDENTIALS.values(), ids=CREDENTIALS)
def test_agui_sse_completo(prod: TestClient, headers: dict[str, str]):
    response = prod.post("/agui", json=AGUI_BODY, headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "RUN_STARTED" in response.text and "RUN_FINISHED" in response.text
    assert "resposta do agente" in response.text
    assert "RUN_ERROR" not in response.text


@pytest.mark.parametrize("stream", ["false", "true"])
@pytest.mark.parametrize("headers", CREDENTIALS.values(), ids=CREDENTIALS)
def test_run_de_team_com_cada_chave(prod: TestClient, headers: dict[str, str], stream: str):
    response = prod.post("/teams/time-1/runs", data={"message": "oi", "stream": stream}, headers=headers)

    assert response.status_code == 200
    if stream == "false":
        assert response.json()["content"] == "resposta do time"
        assert response.json()["team_id"] == "time-1"
    else:
        events = _sse_events(response.text)
        assert events[0] == "TeamRunStarted" and events[-1] == "TeamRunCompleted"
        assert "resposta do time" in response.text


@pytest.mark.parametrize(
    ("method", "path", "data"),
    [
        ("POST", "/agents/agente-1/runs", {"message": "oi", "stream": "false"}),
        ("POST", "/agents/agente-1/runs", {"message": "oi", "stream": "true"}),
        ("POST", "/teams/time-1/runs", {"message": "oi", "stream": "false"}),
        ("POST", "/teams/time-1/runs", {"message": "oi", "stream": "true"}),
    ],
)
def test_run_sem_chave_ou_com_chave_errada_nao_executa_o_modelo(
    build_app: Callable[..., FastAPI], method: str, path: str, data: dict[str, str]
):
    """401 antes do roteamento: o modelo nunca é chamado (nem gasta token)."""
    client = TestClient(build_app(**PROD), raise_server_exceptions=False)

    for headers in ({}, _bearer(RUN_KEY[:-1] + "x"), {"X-API-Key": ""}):
        response = client.request(method, path, data=data, headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "unauthorized"}
        assert "resposta" not in response.text


def test_modelo_nao_e_chamado_quando_a_auth_recusa(monkeypatch: pytest.MonkeyPatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    for name, value in PROD.items():
        monkeypatch.setenv(name, value)
    agent, team = _entities()
    factory = AppFactory()
    app = factory.create_app()
    mount_agent_os(factory, app, [agent], [team])
    client = TestClient(app, raise_server_exceptions=False)

    client.post("/agents/agente-1/runs", data={"message": "oi", "stream": "false"})
    client.post("/teams/time-1/runs", data={"message": "oi", "stream": "false"})
    client.post("/agui", json=AGUI_BODY)
    assert agent.model.calls == [] and team.model.calls == []  # type: ignore[union-attr]

    client.post("/agents/agente-1/runs", data={"message": "oi", "stream": "false"}, headers=_bearer(RUN_KEY))
    assert len(agent.model.calls) == 1  # type: ignore[union-attr]


# ── varredura de TODAS as rotas reais contra o classificador ─────────


def _concrete(template: str) -> str:
    import re

    return re.sub(r"\{[^}]+\}", lambda m: "x1/x2" if m.group(0).endswith(":path}") else "x1", template)


def _real_http_routes(app: FastAPI) -> list[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for route in app.routes:
        if isinstance(route, Route):
            found.update((m, route.path) for m in (route.methods or ()))
        elif not isinstance(route, WebSocketRoute):
            pytest.fail(f"rota de tipo inesperado: {type(route).__name__}")
    return sorted(found)


@pytest.fixture(scope="module")
def sweep_apps() -> dict[str, FastAPI]:
    """Dois apps reais montados: produção (docs admin) e development (docs run), com chaves."""
    apps: dict[str, FastAPI] = {}
    with pytest.MonkeyPatch.context() as mp:
        for label, environment in (("production", "production"), ("development", "development")):
            for name in _ENV_NAMES:
                mp.delenv(name, raising=False)
            mp.setenv("AGNO_TELEMETRY", "false")
            mp.setenv("ENABLE_DOCS", "true")
            mp.setenv("APP_HOST", ALL_INTERFACES)
            mp.setenv("ENVIRONMENT", environment)
            mp.setenv("API_KEY_RUN", RUN_KEY)
            mp.setenv("API_KEY_ADMIN", ADMIN_KEY)
            factory = AppFactory()
            app = factory.create_app()
            agent, team = _entities()
            mount_agent_os(factory, app, [agent], [team])
            apps[label] = app
    return apps


@pytest.mark.parametrize("environment", ["production", "development"])
def test_varredura_toda_rota_real_respeita_a_classe(sweep_apps: dict[str, FastAPI], environment: str):
    """Para cada (método, rota) REAL do app: sem chave 401; chave run 403 se admin e passa
    (nem 401 nem 403) se run; chave admin sempre passa. Passar = a rota respondeu (qualquer
    status que não seja de auth: 2xx/4xx/5xx do AgentOS sem banco)."""
    app = sweep_apps[environment]
    client = TestClient(app, raise_server_exceptions=False)
    docs_admin = environment != "development"
    routes = _real_http_routes(app)
    assert len(routes) > 80  # o app real (AgentOS 2.5.8) tem ~90; falha se a montagem sumiu
    problems: list[str] = []
    counts = {RouteAccess.ADMIN: 0, RouteAccess.RUN: 0, RouteAccess.PUBLIC: 0}
    for method, template in routes:
        path = _concrete(template)
        access = classify_route(method, path, docs_require_admin=docs_admin)
        counts[access] += 1
        none = client.request(method, path)
        run = client.request(method, path, headers=_bearer(RUN_KEY))
        admin = client.request(method, path, headers={"X-API-Key": ADMIN_KEY})
        label = f"{method} {path}"
        if access is RouteAccess.PUBLIC:
            if none.status_code in (401, 403):
                problems.append(f"{label}: pública recusou ({none.status_code})")
            continue
        body_ok = method == "HEAD"  # HEAD não tem corpo
        if none.status_code != 401 or (not body_ok and none.json() != {"detail": "unauthorized"}):
            problems.append(f"{label}: sem chave -> {none.status_code}")
        if access is RouteAccess.ADMIN:
            if run.status_code != 403 or (not body_ok and run.json() != {"detail": "forbidden"}):
                problems.append(f"{label}: admin com chave run -> {run.status_code}")
        elif run.status_code in (401, 403):
            problems.append(f"{label}: run com chave run -> {run.status_code}")
        if admin.status_code in (401, 403):
            problems.append(f"{label}: chave admin recusada -> {admin.status_code}")
    assert problems == []
    assert counts[RouteAccess.ADMIN] >= 40 and counts[RouteAccess.RUN] >= 40 and counts[RouteAccess.PUBLIC] == 1


@pytest.mark.parametrize(
    ("method", "path", "admin_statuses"),
    [
        ("GET", "/admin/health", {200}),
        ("POST", "/admin/refresh-cache", {200}),
        ("GET", "/metrics/cache", {200}),
        ("POST", "/databases/all/migrate", {200}),
        ("GET", "/registry", {200}),
        ("GET", "/components", {503}),  # AgentOS sem db: stub 503 (e não 401/403)
        ("GET", "/schedules", {503}),
        ("GET", "/approvals", {503}),  # F2-05: decisão sobre run de outro usuário é do operador
        ("POST", "/approvals/qualquer/resolve", {503}),
        ("POST", "/optimize-memories", {422}),
        ("POST", "/knowledge/search", {422}),
        ("POST", "/knowledge/content", {400, 422}),
        ("DELETE", "/knowledge/content", {400, 422}),
        ("DELETE", "/sessions", {400, 404, 422, 500}),
        ("DELETE", "/memories", {400, 404, 422, 500}),
    ],
)
def test_rotas_admin_do_agentos_403_com_run_e_resposta_propria_com_admin(
    prod: TestClient, method: str, path: str, admin_statuses: set[int]
):
    assert prod.request(method, path, headers=_bearer(RUN_KEY)).status_code == 403
    assert prod.request(method, path).status_code == 401
    admin = prod.request(method, path, headers=_bearer(ADMIN_KEY))
    assert admin.status_code in admin_statuses, (admin.status_code, admin.text[:200])


def test_admin_health_devolve_o_corpo_so_para_admin(build_app: Callable[..., FastAPI]):
    """Dependência fora: 503 só chega ao admin; a chave run recebe 403 genérico e sem corpo do health."""
    factory = AppFactory()
    unhealthy = {"status": "unhealthy", "checks": {"mongo": {"status": "error"}}}

    class _Health:
        async def check_async(self) -> dict[str, object]:
            return unhealthy

    factory._container = SimpleNamespace(health_service=_Health())  # type: ignore[assignment]
    import os

    for name in _ENV_NAMES:
        os.environ.pop(name, None)
    with pytest.MonkeyPatch.context() as mp:
        for name, value in PROD.items():
            mp.setenv(name, value)
        mp.setenv("AGNO_TELEMETRY", "false")
        app = factory.create_app()
    client = TestClient(app)

    forbidden = client.get("/admin/health", headers=_bearer(RUN_KEY))
    ok = client.get("/admin/health", headers=_bearer(ADMIN_KEY))

    assert forbidden.status_code == 403 and forbidden.json() == {"detail": "forbidden"}
    assert ok.status_code == 503 and ok.json() == unhealthy


# ── /livez público, com e sem chaves ─────────────────────────────────


@pytest.mark.parametrize(
    "env",
    [PROD, {"APP_HOST": "127.0.0.1", "ENVIRONMENT": "development"}, {"ENVIRONMENT": "test"}],
    ids=["com-chaves", "dev-local", "test-local"],
)
def test_livez_publico_com_e_sem_chaves(build_app: Callable[..., FastAPI], env: dict[str, str]):
    app = build_app(**env)

    for client in (TestClient(app), loopback_client(app), loopback_client(app, client=("203.0.113.9", 1))):
        response = client.get("/livez")
        assert response.status_code == 200 and response.json() == {"status": "ok"}
        assert client.get("/livez", headers=_bearer("invalida")).status_code == 200  # credencial lixo não atrapalha


def test_livez_publico_antes_do_agentos_montar(build_app: Callable[..., FastAPI]):
    """HEALTHCHECK do Docker bate no /livez durante o startup (ainda sem AgentOS)."""
    client = TestClient(build_app(mount=False, **PROD))

    assert client.get("/livez").status_code == 200
    assert client.get("/admin/health").status_code == 401


# ── rotação de chave ─────────────────────────────────────────────────


def test_rotacao_de_chave_recriando_o_app(build_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch):
    old = TestClient(build_app(**PROD))
    assert old.get("/agents", headers=_bearer(RUN_KEY)).status_code == 200

    new_run, new_admin = "chave-nova-run-" + "n" * 17, "chave-nova-admin-" + "m" * 15
    new = TestClient(build_app(**{**PROD, "API_KEY_RUN": new_run, "API_KEY_ADMIN": new_admin}))

    assert new.get("/agents", headers=_bearer(RUN_KEY)).status_code == 401  # chave antiga morreu
    assert new.get("/admin/health", headers=_bearer(ADMIN_KEY)).status_code == 401
    assert new.get("/agents", headers=_bearer(new_run)).status_code == 200
    assert new.get("/admin/health", headers=_bearer(new_admin)).status_code == 200
    assert new.get("/admin/health", headers=_bearer(new_run)).status_code == 403
    # O app antigo (já em memória) segue com as chaves com que nasceu: a troca vale só com restart.
    assert old.get("/agents", headers=_bearer(RUN_KEY)).status_code == 200


def test_trocar_o_env_sem_recriar_o_app_nao_muda_as_chaves(
    build_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch
):
    client = TestClient(build_app(**PROD))
    monkeypatch.setenv("API_KEY_RUN", "outra-chave-" + "z" * 24)

    assert client.get("/agents", headers=_bearer(RUN_KEY)).status_code == 200


def test_chaves_trocadas_run_por_admin_nao_escalam(build_app: Callable[..., FastAPI]):
    """Rotação que inverte os papéis: a chave que era admin vira run e só vale em rota run."""
    client = TestClient(build_app(**{**PROD, "API_KEY_RUN": ADMIN_KEY, "API_KEY_ADMIN": RUN_KEY}))

    assert client.get("/admin/health", headers=_bearer(ADMIN_KEY)).status_code == 403
    assert client.get("/admin/health", headers=_bearer(RUN_KEY)).status_code == 200


# ── limites da chave ─────────────────────────────────────────────────


def test_chave_com_exatamente_32_caracteres_funciona_de_ponta_a_ponta(build_app: Callable[..., FastAPI]):
    run, admin = "R" * API_KEY_MIN_LENGTH, "A" * API_KEY_MIN_LENGTH
    client = TestClient(build_app(**{**PROD, "API_KEY_RUN": run, "API_KEY_ADMIN": admin}))

    assert client.get("/agents", headers=_bearer(run)).status_code == 200
    assert client.get("/admin/health", headers=_bearer(admin)).status_code == 200
    assert client.get("/admin/health", headers=_bearer(run)).status_code == 403
    assert client.get("/agents", headers=_bearer("R" * 31)).status_code == 401


@pytest.mark.parametrize("which", ["API_KEY_RUN", "API_KEY_ADMIN"])
def test_chave_com_31_caracteres_recusa_o_startup_sem_vazar_o_valor(build_app: Callable[..., FastAPI], which: str):
    short = "s3cr3t-" + "k" * 24
    assert len(short) == API_KEY_MIN_LENGTH - 1

    with pytest.raises(ValueError, match=which) as exc:
        build_app(mount=False, **{**PROD, which: short})

    assert short not in str(exc.value)


@pytest.mark.parametrize(
    "bad",
    ["k" * 16 + " " + "k" * 20, "k" * 31 + "é", "k" * 31 + "\t" + "k", "k" * 40 + "​"],
    ids=["espaco-interno", "nao-ascii", "tab", "zero-width"],
)
def test_chave_com_caractere_invalido_recusa_o_startup(build_app: Callable[..., FastAPI], bad: str):
    with pytest.raises(ValueError, match="API_KEY_RUN") as exc:
        build_app(mount=False, **{**PROD, "API_KEY_RUN": bad})

    assert bad not in str(exc.value)


def test_espacos_nas_pontas_da_chave_sao_removidos(build_app: Callable[..., FastAPI]):
    key = "e" * 40
    client = TestClient(build_app(**{**PROD, "API_KEY_RUN": f"  {key}\n"}))

    assert client.get("/agents", headers=_bearer(key)).status_code == 200


def test_chave_vazia_ou_so_espacos_conta_como_ausente(build_app: Callable[..., FastAPI]):
    with pytest.raises(ValueError, match="API_KEY_RUN e API_KEY_ADMIN"):
        build_app(mount=False, **{**PROD, "API_KEY_RUN": "   ", "API_KEY_ADMIN": ""})


def test_mesma_chave_nas_duas_funcoes_recusa(build_app: Callable[..., FastAPI]):
    with pytest.raises(ValueError, match="diferentes") as exc:
        build_app(mount=False, **{**PROD, "API_KEY_ADMIN": RUN_KEY})

    assert RUN_KEY not in str(exc.value)


def test_chave_muito_longa_funciona(build_app: Callable[..., FastAPI]):
    long_run = "L" * 4000
    client = TestClient(build_app(**{**PROD, "API_KEY_RUN": long_run}))

    assert client.get("/agents", headers=_bearer(long_run)).status_code == 200
    assert client.get("/agents", headers=_bearer("L" * 3999)).status_code == 401


# ── credenciais hostis ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw_headers",
    [
        [(b"authorization", b"Bearer \xff\xfe\x00")],
        [(b"x-api-key", b"\xff" * 40)],
        [(b"authorization", b"Bearer " + b"A" * 100_000)],
        [(b"authorization", b"Bearer"), (b"x-api-key", b"")],
    ],
    ids=["bytes-nao-ascii", "x-api-key-binaria", "token-gigante", "bearer-vazio-e-chave-vazia"],
)
def test_credencial_binaria_ou_gigante_e_401_nunca_500(
    prod_rejecting: TestClient, raw_headers: list[tuple[bytes, bytes]]
):
    response = prod_rejecting.get("/agents", headers=raw_headers)

    assert response.status_code == 401 and response.json() == {"detail": "unauthorized"}


def test_primeiro_header_authorization_vale_e_duplicata_nao_ajuda(prod: TestClient):
    """Dois Authorization: vale o primeiro. A chave certa no segundo não passa."""
    wrong_then_right = [(b"authorization", b"Bearer errada"), (b"authorization", f"Bearer {RUN_KEY}".encode())]
    right_then_wrong = [(b"authorization", f"Bearer {RUN_KEY}".encode()), (b"authorization", b"Bearer errada")]

    assert prod.get("/agents", headers=wrong_then_right).status_code == 401
    assert prod.get("/agents", headers=right_then_wrong).status_code == 200


def test_bearer_invalido_com_x_api_key_valida_nao_cai_no_x_api_key(prod_rejecting: TestClient):
    """Bearer presente e malformado do esquema Bearer mas com token errado: não faz fallback para X-API-Key."""
    headers = {"Authorization": "Bearer errada", "X-API-Key": RUN_KEY}

    assert prod_rejecting.get("/agents", headers=headers).status_code == 401


def test_chave_run_no_corpo_ou_na_query_nao_autentica(prod_rejecting: TestClient):
    assert prod_rejecting.get("/agents", params={"api_key": RUN_KEY, "token": RUN_KEY}).status_code == 401
    assert prod_rejecting.post("/agents/agente-1/runs", data={"message": "oi", "api_key": RUN_KEY}).status_code == 401
    cookies = {"api_key": RUN_KEY, "Authorization": f"Bearer {RUN_KEY}"}
    assert prod_rejecting.get("/agents", cookies=cookies).status_code == 401


# ── contorno do classificador por caminho ────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/Admin/health",
        "/ADMIN/HEALTH",
        "/admin//health",
        "/./admin/health",
        "/livez/../admin/health",
        "/livez/./../admin/health",
        "/admin/health%00",
        "/admin%2Fhealth",
        "/%61dmin/health",
        "/admin/health;x=1",
        "/admin/health/..;/health",
        "/\\admin/health",
        "/admin/health.",
        "/ admin/health",
        "/metrics//cache",
        "/%6detrics/cache",
    ],
)
def test_run_key_nao_alcanca_rota_admin_por_caminho_disfarcado(prod_rejecting: TestClient, path: str):
    """O que a chave run recebe nunca pode ser o corpo do handler admin (health/cache)."""
    response = prod_rejecting.get(path, headers=_bearer(RUN_KEY))

    assert response.status_code in (403, 404, 400, 307, 308, 422)
    body = response.text
    assert '"status":"healthy"' not in body and "no_cache" not in body and "cache_refreshed" not in body


async def _asgi_get(app: FastAPI, path: str, key: str | None) -> tuple[int, bytes]:
    """GET direto no ASGI com o path cru (o httpx trata `//x` como URL relativa a protocolo)."""
    sent: list[dict[str, object]] = []
    headers = [(b"host", b"app.example.com")]
    if key is not None:
        headers.append((b"authorization", f"Bearer {key}".encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"", "headers": headers,
        "client": ("203.0.113.9", 4000), "server": ("10.0.0.5", 7777),
    }  # fmt: skip

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await app(scope, receive, send)  # type: ignore[arg-type]
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")  # type: ignore[misc]
    return int(sent[0]["status"]), body  # type: ignore[call-overload]


@pytest.mark.parametrize("path", ["//admin/health", "//metrics/cache", "///admin/health", "//admin/refresh-cache"])
async def test_barra_dupla_no_inicio_nao_vira_admin_com_chave_run(build_app: Callable[..., FastAPI], path: str):
    """Path cru `//admin/health` não é `/admin*` para o classificador; o roteador também não o casa."""
    app = build_app(**PROD)

    status, body = await _asgi_get(app, path, RUN_KEY)
    anon_status, _ = await _asgi_get(app, path, None)

    assert status in (403, 404)
    assert b'"healthy"' not in body and b"no_cache" not in body and b"cache_refreshed" not in body
    assert anon_status == 401


@pytest.mark.parametrize(
    "path", ["/livez/x", "/livez.json", "/livezz", "/livez;", "//livez", "/LIVEZ", "/livez/..", "/%6cvez"]
)
def test_livez_nao_e_curinga(prod_rejecting: TestClient, path: str):
    """Só a rota exata `/livez` é pública (com ou sem barra final)."""
    response = prod_rejecting.get(path)

    assert response.status_code == 401 or (path in ("/livez/..",) and response.status_code in (200, 401))


@pytest.mark.parametrize("method", ["PUT", "PATCH", "TRACE", "CONNECT", "PROPFIND", "MKCOL"])
def test_metodo_incomum_em_rota_admin_exige_admin(prod_rejecting: TestClient, method: str):
    response = prod_rejecting.request(method, "/admin/refresh-cache", headers=_bearer(RUN_KEY))

    assert response.status_code == 403


def test_head_em_rota_get_admin_exige_admin(prod_rejecting: TestClient):
    assert prod_rejecting.head("/admin/health", headers=_bearer(RUN_KEY)).status_code == 403
    assert prod_rejecting.head("/admin/health").status_code == 401
    # passou da auth: o 405 é do roteador (a rota só aceita GET), não 401/403
    assert prod_rejecting.head("/admin/health", headers=_bearer(ADMIN_KEY)).status_code == 405


@pytest.mark.parametrize(
    "override",
    [
        {"X-HTTP-Method-Override": "GET"},
        {"X-Method-Override": "GET"},
        {"X-HTTP-Method": "GET"},
    ],
)
def test_method_override_nao_rebaixa_delete_para_get(prod_rejecting: TestClient, override: dict[str, str]):
    """DELETE /sessions/s1 é admin; um header de override não pode mudar a classificação."""
    response = prod_rejecting.request("DELETE", "/sessions/s1", headers={**_bearer(RUN_KEY), **override})

    assert response.status_code == 403


def test_cabecalhos_de_forward_e_host_nao_mudam_a_exigencia_com_chaves(prod_rejecting: TestClient):
    spoof = {
        "X-Forwarded-For": "127.0.0.1",
        "X-Real-IP": "127.0.0.1",
        "X-Forwarded-Host": "localhost",
        "Host": "localhost",
    }

    assert prod_rejecting.get("/agents", headers=spoof).status_code == 401
    assert prod_rejecting.get("/admin/health", headers={**spoof, **_bearer(RUN_KEY)}).status_code == 403


# ── CORS e 401/403 ───────────────────────────────────────────────────


def test_origem_nao_permitida_leva_401_sem_acao(prod_rejecting: TestClient):
    response = prod_rejecting.get("/agents", headers={"Origin": "https://evil.example.com"})

    assert response.status_code == 401
    assert "access-control-allow-origin" not in response.headers


def test_403_de_origem_permitida_leva_headers_cors(prod_rejecting: TestClient):
    response = prod_rejecting.get("/admin/health", headers={**_bearer(RUN_KEY), "Origin": ALLOWED})

    assert response.status_code == 403
    assert response.headers["access-control-allow-origin"] == ALLOWED


def test_preflight_publico_inclusive_com_headers_de_chave(prod_rejecting: TestClient):
    response = prod_rejecting.options(
        "/agents/agente-1/runs",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,x-api-key,content-type",
        },
    )

    assert response.status_code == 200
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed and "x-api-key" in allowed


def test_preflight_de_origem_nao_permitida_nao_vaza_dado(prod_rejecting: TestClient):
    response = prod_rejecting.options(
        "/admin/health", headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"}
    )

    assert response.status_code == 400  # CORS recusa; nada de corpo do handler
    assert "access-control-allow-origin" not in response.headers


# ── 401/403 sem detalhe ──────────────────────────────────────────────


def test_corpo_de_401_e_403_nao_carrega_detalhe(prod_rejecting: TestClient):
    unauthorized = prod_rejecting.get("/admin/health")
    forbidden = prod_rejecting.get("/admin/health", headers=_bearer(RUN_KEY))

    assert unauthorized.content == b'{"detail":"unauthorized"}'
    assert forbidden.content == b'{"detail":"forbidden"}'
    for response in (unauthorized, forbidden):
        text = (response.text + json.dumps(dict(response.headers))).lower()
        assert RUN_KEY.lower() not in text and ADMIN_KEY.lower() not in text
        assert "traceback" not in text and "x-api-key" not in text
    assert unauthorized.headers["www-authenticate"] == "Bearer"
    assert "www-authenticate" not in forbidden.headers


# ── lifespan real (container fake, AgentOS real) ─────────────────────


class _Controller:
    def __init__(self, agents: list[Agent], teams: list[Team]) -> None:
        self._agents, self._teams = agents, teams

    async def warm_up_cache(self) -> None:
        return None

    async def get_agents(self) -> list[Agent]:
        return self._agents

    async def get_teams(self) -> list[Team]:
        return self._teams

    def get_cache_stats(self) -> dict[str, int]:
        return {"agents": len(self._agents)}

    async def refresh_agents(self) -> None:
        return None


def _lifespan_client(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> tuple[TestClient, AppFactory]:
    from src.infrastructure import dependency_injection as di
    from src.infrastructure.config.app_config import AppConfig
    from src.infrastructure.web import app_factory

    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("OTEL_ENABLED", "false")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    agent, team = _entities()
    runtime = agno_runtime()

    class _Container:
        def __init__(self, config: AppConfig) -> None:
            self.config = config
            self.health_service = None

        def get_orquestrador_controller(self) -> _Controller:
            return _Controller([agent], [team])

        def get_agent_runtime(self) -> AgnoRuntime:
            return runtime

        async def cleanup(self) -> None:
            return None

    async def _create_async(config: AppConfig) -> _Container:
        return _Container(config)

    monkeypatch.setattr(di.DependencyContainer, "create_async", staticmethod(_create_async))
    monkeypatch.setattr(app_factory.DependencyContainer, "create_async", staticmethod(_create_async))
    factory = AppFactory()
    return TestClient(factory.create_app(), raise_server_exceptions=False), factory


def test_lifespan_real_monta_o_agentos_com_a_auth_ativa(monkeypatch: pytest.MonkeyPatch):
    client, _ = _lifespan_client(monkeypatch, PROD)

    with client:  # dispara startup/shutdown reais
        assert client.get("/livez").status_code == 200
        assert client.get("/agents").status_code == 401
        assert client.get("/agents", headers=_bearer(RUN_KEY)).status_code == 200
        assert client.post("/admin/refresh-cache", headers=_bearer(RUN_KEY)).status_code == 403
        assert client.post("/admin/refresh-cache", headers=_bearer(ADMIN_KEY)).json() == {"status": "cache_refreshed"}
        assert client.get("/metrics/cache", headers=_bearer(ADMIN_KEY)).json() == {"agents": 1}
        run = client.post("/agents/agente-1/runs", data={"message": "oi", "stream": "false"}, headers=_bearer(RUN_KEY))
        assert run.json()["content"] == "resposta do agente"
        assert client.post("/agui", json=AGUI_BODY).status_code == 401


def test_lifespan_com_falha_ao_montar_o_agentos_continua_com_auth(monkeypatch: pytest.MonkeyPatch):
    """Falha na montagem: o app segue só com as rotas admin, e elas continuam atrás da auth."""

    class _Quebra:
        def __init__(self, **_: object) -> None:
            raise RuntimeError("montagem quebrou")

    client, _ = _lifespan_client(monkeypatch, PROD)
    monkeypatch.setattr("src.infrastructure.runtime.agno.runtime.AgentOS", _Quebra)

    with client:
        assert client.get("/livez").status_code == 200
        assert client.get("/admin/health").status_code == 401
        assert client.get("/admin/health", headers=_bearer(RUN_KEY)).status_code == 403
        assert client.get("/admin/health", headers=_bearer(ADMIN_KEY)).status_code == 200
        assert client.get("/agents", headers=_bearer(RUN_KEY)).status_code == 404  # sem AgentOS: 404 só após a chave
        assert client.get("/agents").status_code == 401


def test_lifespan_real_no_modo_dev_local(monkeypatch: pytest.MonkeyPatch):
    client, _ = _lifespan_client(monkeypatch, {"APP_HOST": "127.0.0.1", "ENVIRONMENT": "development"})
    local = loopback_client(client.app)

    with local:
        assert local.get("/agents").status_code == 200
        assert client.get("/agents").status_code == 401  # testclient/testserver não é loopback


# ── bordas do middleware isolado ─────────────────────────────────────


async def _raw_status(app: ASGIApp, scope: dict[str, Any]) -> int:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)  # type: ignore[arg-type]
    return int(sent[0]["status"])


def _scope(path: str, *, root_path: str = "", host: bytes = b"127.0.0.1:7777") -> dict[str, Any]:
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "root_path": root_path,
        "headers": [(b"host", host)],
        "client": ("127.0.0.1", 5000),
        "server": ("127.0.0.1", 7777),
    }


async def _teapot(scope: Any, receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 418, "headers": []})
    await send({"type": "http.response.body", "body": b""})


@pytest.mark.parametrize("root_path", ["/api", "/api/"])
async def test_path_igual_ao_root_path_vira_a_raiz_e_exige_chave_run(root_path: str):
    from src.infrastructure.web.api_key_auth import ApiKeyAuthMiddleware, ApiKeys

    keys = ApiKeys(run=RUN_KEY.encode(), admin=ADMIN_KEY.encode())
    middleware = ApiKeyAuthMiddleware(_teapot, keys=keys, docs_require_admin=True)
    scope = _scope("/api", root_path=root_path)

    assert await _raw_status(middleware, scope) == 401
    scope["headers"] = [*scope["headers"], (b"authorization", f"Bearer {RUN_KEY}".encode())]
    assert await _raw_status(middleware, scope) == 418


@pytest.mark.parametrize(
    "host", [b"[127.0.0.1]", b"[127.0.0.1]:7777", b"[localhost]:7777", b"127.0.0.1:", b"127.0.0.1:77 77"]
)
async def test_host_header_malformado_no_modo_dev_local_e_401(host: bytes):
    from src.infrastructure.web.api_key_auth import ApiKeyAuthMiddleware

    middleware = ApiKeyAuthMiddleware(_teapot, keys=None, docs_require_admin=False)

    assert await _raw_status(middleware, _scope("/agents", host=host)) == 401


async def test_websocket_recusado_ignora_mensagem_que_nao_e_connect():
    """Desconexão antes do connect: nada a fechar, e nada vaza nem levanta."""
    from src.infrastructure.web.api_key_auth import ApiKeyAuthMiddleware, ApiKeys

    keys = ApiKeys(run=RUN_KEY.encode(), admin=ADMIN_KEY.encode())
    middleware = ApiKeyAuthMiddleware(_teapot, keys=keys, docs_require_admin=True)
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.disconnect", "code": 1001}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "websocket",
        "path": "/workflows/ws",
        "root_path": "",
        "headers": [],
        "client": None,
        "server": None,
    }
    await middleware(scope, receive, send)  # type: ignore[arg-type]

    assert sent == []
