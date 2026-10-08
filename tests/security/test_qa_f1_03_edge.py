"""QA do F1-03: lacunas da borda (rede, CORS no AG-UI, health ok/fora, config inválida, app.py).

Complementa ``test_edge_hygiene.py`` e ``test_agno_telemetry.py``. Nada de rede real: um
bloqueador de ``getaddrinfo``/``connect`` registra e recusa qualquer tentativa.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from agno.agent import Agent
from agno.team import Team
from starlette.testclient import TestClient

from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.dependency_injection import HealthService
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel, RecordingLogger, loopback_client

REPO = Path(__file__).resolve().parents[2]
ALLOWED = "https://painel.example.com"
DENIED = "https://evil.example.com"
AGUI_BODY = {
    "threadId": "t1",
    "runId": "r1",
    "state": {},
    "messages": [{"id": "m1", "role": "user", "content": "oi"}],
    "tools": [],
    "context": [],
    "forwardedProps": {},
}


# ── bloqueador de rede ──────────────────────────────────────────────


class NetworkAttempts:
    """Registra toda tentativa de resolver nome ou conectar socket; nenhuma delas funciona."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def hosts(self) -> list[str]:
        return list(self.attempts)

    def mentions(self, needle: str) -> bool:
        return any(needle in a for a in self.attempts)


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> Iterator[NetworkAttempts]:
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
    yield seen


def _clean_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for name in ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "AGNO_TELEMETRY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def _mounted(monkeypatch: pytest.MonkeyPatch, agents: list[Agent], teams: list[Team] | None = None, **env: str):
    _clean_env(monkeypatch, **env)
    factory = AppFactory()
    app = factory.create_app()
    factory._mount_agent_os(app, agents, teams or [])
    return app


def _agent(telemetry: bool = False, responses: list[str] | None = None) -> Agent:
    return Agent(
        id="agente-1",
        name="Agente 1",
        model=FakeChatModel(responses=responses or ["resposta um", "resposta dois", "resposta tres"]),
        telemetry=telemetry,
    )


# ── nenhuma chamada a os-api.agno.com num run real ──────────────────


def test_run_real_nao_tenta_falar_com_a_agno(monkeypatch: pytest.MonkeyPatch, network: NetworkAttempts):
    """Agent + FakeChatModel + AgentOS: REST e AG-UI executam e nenhuma conexão sai."""
    team = Team(
        id="time-1",
        name="Time 1",
        members=[],
        model=FakeChatModel(responses=["time ok"]),
        telemetry=False,
    )
    app = _mounted(monkeypatch, [_agent()], [team])
    client = loopback_client(app)

    rest = client.post("/agents/agente-1/runs", data={"message": "oi", "stream": "false"})
    agui = client.post("/agui", json=AGUI_BODY)
    team_run = client.post("/teams/time-1/runs", data={"message": "oi", "stream": "false"})

    assert rest.status_code == 200 and rest.json()["content"] == "resposta um"
    assert agui.status_code == 200 and "RUN_FINISHED" in agui.text
    assert team_run.status_code == 200 and team_run.json()["content"] == "time ok"
    assert network.attempts == [], f"tentativas de rede: {network.attempts}"


def test_bloqueador_de_rede_enxerga_a_telemetria_ligada(monkeypatch: pytest.MonkeyPatch, network: NetworkAttempts):
    """Controle positivo: com telemetria explicitamente ligada o bloqueador registra a Agno."""
    app = _mounted(monkeypatch, [_agent(telemetry=True)])
    monkeypatch.setenv("AGNO_TELEMETRY", "true")

    loopback_client(app).post("/agents/agente-1/runs", data={"message": "oi", "stream": "false"})

    assert network.mentions("os-api.agno.com")


def test_startup_mantem_telemetria_desligada_mesmo_com_agent_default(
    monkeypatch: pytest.MonkeyPatch, network: NetworkAttempts
):
    """Um Agent criado SEM ``telemetry=False`` (default True) fica mudo depois do startup do app."""
    _clean_env(monkeypatch)
    AppFactory().create_app()
    agent = Agent(id="agente-1", name="Agente 1", model=FakeChatModel(responses=["ok"]))

    assert agent.run("oi").content == "ok"

    assert network.attempts == []


# ── CORS no AG-UI e nas rotas do AgentOS ────────────────────────────


@pytest.mark.parametrize(
    "path,method",
    [("/agui", "POST"), ("/agents/agente-1/runs", "POST"), ("/sessions", "GET"), ("/agents", "GET")],
)
def test_preflight_permitida_e_negada_nas_rotas_do_agentos(
    monkeypatch: pytest.MonkeyPatch, path: str, method: str
):
    client = loopback_client(_mounted(monkeypatch, [_agent()], CORS_ALLOWED_ORIGINS=ALLOWED))

    def pre(origin: str):
        return client.options(path, headers={"Origin": origin, "Access-Control-Request-Method": method})

    ok = pre(ALLOWED)
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == ALLOWED
    assert ok.headers["access-control-allow-credentials"] == "true"
    denied = pre(DENIED)
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


def test_resposta_sse_do_agui_ecoa_a_origem_permitida_e_nunca_curinga_com_ela(monkeypatch: pytest.MonkeyPatch):
    client = loopback_client(_mounted(monkeypatch, [_agent()], CORS_ALLOWED_ORIGINS=ALLOWED))

    ok = client.post("/agui", json=AGUI_BODY, headers={"Origin": ALLOWED})

    assert ok.headers.get_list("access-control-allow-origin") == [ALLOWED]
    assert ok.headers["access-control-allow-credentials"] == "true"


@pytest.mark.xfail(
    strict=True,
    reason="BUG-F103-AGUI-ACAO: agno/os/interfaces/agui/router.py:144 fixa 'Access-Control-Allow-Origin: *' "
    "no StreamingResponse; o CORS do app só sobrescreve para origem permitida. Escopo declarado do F1-08 (B7).",
)
def test_resposta_sse_do_agui_nao_carrega_curinga_para_origem_negada(monkeypatch: pytest.MonkeyPatch):
    # Com chaves e chave run válida: no modo dev local (F1-04) a origem negada nem chegaria ao AG-UI.
    client = loopback_client(_mounted(monkeypatch, [_agent()], CORS_ALLOWED_ORIGINS=ALLOWED, **_KEYS))
    run_key = {"Authorization": f"Bearer {_KEYS['API_KEY_RUN']}"}

    denied = client.post("/agui", json=AGUI_BODY, headers={"Origin": DENIED, **run_key})

    assert "access-control-allow-origin" not in denied.headers


def test_cors_ate_nas_respostas_de_erro_do_agentos(monkeypatch: pytest.MonkeyPatch):
    """CORS é o mais externo: 404/422 de rotas do AgentOS também carregam o header da origem permitida."""
    client = loopback_client(_mounted(monkeypatch, [_agent()], CORS_ALLOWED_ORIGINS=ALLOWED))

    missing = client.get("/agents/nao-existe", headers={"Origin": ALLOWED})
    invalid = client.post("/agui", json={"x": 1}, headers={"Origin": ALLOWED})

    assert missing.status_code == 404 and missing.headers["access-control-allow-origin"] == ALLOWED
    assert invalid.status_code == 422 and invalid.headers["access-control-allow-origin"] == ALLOWED


def test_default_de_origens_vale_sem_a_env(monkeypatch: pytest.MonkeyPatch):
    client = loopback_client(_mounted(monkeypatch, [_agent()]))

    for origin in ("https://os.agno.com", "http://localhost:3000", "http://localhost:7777"):
        resp = client.options("/agui", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
        assert resp.headers["access-control-allow-origin"] == origin
    # O AgentOS acrescentaria os-stg.agno.com; o app não deixa.
    resp = client.options(
        "/agui", headers={"Origin": "https://os-stg.agno.com", "Access-Control-Request-Method": "POST"}
    )
    assert resp.status_code == 400


# ── docs ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mounted", [False, True], ids=["so-base-app", "com-agentos"])
def test_docs_extras_e_enable_docs_tambem_fora_de_production(monkeypatch: pytest.MonkeyPatch, mounted: bool):
    """ENABLE_DOCS=false vence o default de development; o redirect OAuth2 do Swagger também some."""
    _clean_env(monkeypatch, ENVIRONMENT="development", ENABLE_DOCS="false")
    factory = AppFactory()
    app = factory.create_app()
    if mounted:
        factory._mount_agent_os(app, [_agent()], [])
    client = loopback_client(app)

    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404, path


# Fora de development o app exige chaves (F1-04) e docs são rota admin. Valores de teste.
_KEYS = {"API_KEY_RUN": "chave-de-teste-run-" + "r" * 21, "API_KEY_ADMIN": "chave-de-teste-admin-" + "a" * 19}
_ADMIN_HEADERS = {"Authorization": f"Bearer {_KEYS['API_KEY_ADMIN']}"}


def test_docs_ligados_por_padrao_em_development_e_explicitos_em_production(monkeypatch: pytest.MonkeyPatch):
    _clean_env(monkeypatch)
    assert loopback_client(AppFactory().create_app()).get("/docs").status_code == 200
    _clean_env(monkeypatch, ENVIRONMENT="production", ENABLE_DOCS="true", **_KEYS)
    client = loopback_client(AppFactory().create_app(), headers=_ADMIN_HEADERS)
    assert client.get("/openapi.json").status_code == 200


def test_openapi_nao_e_regenerado_com_docs_desligados(monkeypatch: pytest.MonkeyPatch):
    """``app.openapi_schema = None`` após a montagem não pode reabrir o schema por outra rota."""
    app = _mounted(monkeypatch, [_agent()], ENVIRONMENT="production", **_KEYS)
    client = loopback_client(app, headers=_ADMIN_HEADERS)

    assert client.get("/openapi.json").status_code == 404
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not paths & {"/docs", "/redoc", "/openapi.json"}


# ── /livez e /admin/health ──────────────────────────────────────────


class _OkAdmin:
    async def command(self, name: str) -> dict[str, int]:
        return {"ok": 1}


class _MongoOk:
    admin = _OkAdmin()


class _Container:
    def __init__(self, service: HealthService) -> None:
        self.health_service = service

    async def cleanup(self) -> None:
        return None


def _health_client(monkeypatch: pytest.MonkeyPatch, service: HealthService) -> TestClient:
    _clean_env(monkeypatch)
    factory = AppFactory()
    app = factory.create_app()
    factory._container = _Container(service)  # type: ignore[assignment]
    return loopback_client(app)


def test_health_com_mongo_ok_responde_200(monkeypatch: pytest.MonkeyPatch):
    client = _health_client(monkeypatch, HealthService(_MongoOk(), RecordingLogger()))  # type: ignore[arg-type]

    response = client.get("/admin/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["checks"]["mongodb"] == {"status": "healthy"}


def test_health_excecao_inesperada_num_check_vira_503_sem_texto(monkeypatch: pytest.MonkeyPatch):
    logger = RecordingLogger()
    service = HealthService(_MongoOk(), logger)  # type: ignore[arg-type]

    async def boom() -> dict:
        raise RuntimeError("segredo-no-traceback /etc/passwd")

    monkeypatch.setattr(HealthService, "_check_memory", staticmethod(boom))
    response = _health_client(monkeypatch, service).get("/admin/health")

    assert response.status_code == 503
    assert response.json()["checks"]["memory"] == {"status": "error"}
    assert "segredo-no-traceback" not in response.text and "/etc/passwd" not in response.text
    assert "segredo-no-traceback" not in str([r.context for r in logger.records])


def test_livez_independe_do_container_e_do_mongo(monkeypatch: pytest.MonkeyPatch):
    """Mesmo com Mongo fora (health 503), /livez segue 200 e só diz ok."""

    class _Down:
        class admin:
            @staticmethod
            async def command(name: str) -> dict:
                raise ConnectionError("fora")

    client = _health_client(monkeypatch, HealthService(_Down(), RecordingLogger()))  # type: ignore[arg-type]

    assert client.get("/admin/health").status_code == 503
    live = client.get("/livez")
    assert live.status_code == 200 and live.json() == {"status": "ok"}


# ── config inválida ─────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["prod", "dev", "Production2", "local", " staging!"])
def test_environment_invalido_falha_no_create_app(monkeypatch: pytest.MonkeyPatch, value: str):
    _clean_env(monkeypatch, ENVIRONMENT=value)

    with pytest.raises(ValueError, match="ENVIRONMENT inválido"):
        AppFactory().create_app()


@pytest.mark.parametrize("value", [" PRODUCTION ", "Staging", "TEST"])
def test_environment_e_normalizado(monkeypatch: pytest.MonkeyPatch, value: str):
    _clean_env(monkeypatch, ENVIRONMENT=value)

    assert AppConfig.load().environment == value.strip().lower()


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "null",
        "NULL",
        "http://",
        "https://",
        "ftp://host",
        "javascript:alert(1)",
        "//host",
        "host.example.com",
        "https://host/",
        "https://host/path",
        "https://host?x=1",
        "https://host#frag",
        "https://host:abc",
        "https://host:99999",
        "https://user@host",
        "https://user:pw@host",
    ],
)
def test_cors_origem_hostil_falha_no_load(monkeypatch: pytest.MonkeyPatch, origin: str):
    _clean_env(monkeypatch, CORS_ALLOWED_ORIGINS=f"https://ok.example.com,{origin}")

    with pytest.raises(ValueError, match="CORS_ALLOWED_ORIGINS"):
        AppConfig.load()


def test_cors_ipv6_malformado_falha_no_load(monkeypatch: pytest.MonkeyPatch):
    _clean_env(monkeypatch, CORS_ALLOWED_ORIGINS="https://[::1")

    with pytest.raises(ValueError):
        AppConfig.load()


@pytest.mark.parametrize(
    "origin", ["http://localhost:3000", "https://a.b.example.com", "http://127.0.0.1:8080", "http://[::1]:3000"]
)
def test_cors_origem_valida_e_aceita(monkeypatch: pytest.MonkeyPatch, origin: str):
    _clean_env(monkeypatch, CORS_ALLOWED_ORIGINS=origin)

    assert AppConfig.load().cors_allowed_origins == (origin,)


@pytest.mark.parametrize("value", ["sim", "2", "enabled", "truee"])
def test_enable_docs_invalido_falha(monkeypatch: pytest.MonkeyPatch, value: str):
    _clean_env(monkeypatch, ENABLE_DOCS=value)

    with pytest.raises(ValueError, match="ENABLE_DOCS"):
        AppConfig.load()


# ── python app.py com config inválida (sem ler .env) ────────────────


def _run_app_py(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Executa uma cópia de app.py num cwd temporário, com ambiente vazio (``env -i``)."""
    shutil.copy(REPO / "app.py", tmp_path / "app.py")
    full_env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
    full_env.update(env)
    return subprocess.run(
        [sys.executable, "app.py"],
        cwd=tmp_path,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize(
    "env,message",
    [
        ({"ENVIRONMENT": "prod"}, "ENVIRONMENT inválido"),
        ({"CORS_ALLOWED_ORIGINS": "*"}, "CORS_ALLOWED_ORIGINS"),
        ({"ENABLE_DOCS": "talvez"}, "ENABLE_DOCS"),
    ],
)
def test_python_app_py_com_config_invalida_falha_cedo(tmp_path: Path, env: dict[str, str], message: str):
    result = _run_app_py(tmp_path, **env)

    assert result.returncode != 0
    assert message in result.stderr
    assert "Uvicorn running" not in result.stderr + result.stdout  # nunca chegou ao bind


# ── uvloop com guarda ───────────────────────────────────────────────

_DRIVER = textwrap.dedent(
    """
    import json, runpy, sys, types
    captured = {}
    fake = types.ModuleType("uvicorn")
    class Config:
        def __init__(self, **kw): captured.update(kw)
    class Server:
        def __init__(self, config): pass
        async def serve(self): return None
    fake.Config, fake.Server = Config, Server
    sys.modules["uvicorn"] = fake
    if __BLOCK__:
        sys.modules["uvloop"] = None  # import levanta ImportError
    runpy.run_path("app.py", run_name="__main__")
    print("CAPTURED" + json.dumps({"loop": captured.get("loop"), "host": captured.get("host")}))
    """
)


@pytest.mark.parametrize("block_uvloop,expected_loop", [(False, "uvloop"), (True, None)])
def test_app_py_uvloop_com_guarda(tmp_path: Path, block_uvloop: bool, expected_loop: str | None):
    if not block_uvloop:
        pytest.importorskip("uvloop")
    shutil.copy(REPO / "app.py", tmp_path / "app.py")
    (tmp_path / "driver.py").write_text(_DRIVER.replace("__BLOCK__", str(block_uvloop)))
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO), "ENVIRONMENT": "test"}

    result = subprocess.run(
        [sys.executable, "driver.py"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False
    )

    assert result.returncode == 0, result.stderr
    captured = json.loads(result.stdout.split("CAPTURED", 1)[1].splitlines()[0])
    assert captured["loop"] == expected_loop
    assert captured["host"] == "127.0.0.1"


# ── código removido ─────────────────────────────────────────────────


def test_modulos_de_logging_mortos_removidos_e_import_nao_cria_arquivo(tmp_path: Path):
    for name in ("config.py", "secure_logger.py"):
        assert not (REPO / "src/infrastructure/logging" / name).exists()
    code = "import src.infrastructure.logging, src.infrastructure.web.app_factory"
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO)}
    result = subprocess.run(  # noqa: S603 - comando fixo do próprio interpretador
        [sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    # O diretório logs/ vazio vem do setup_structlog (herdado); o app.log do FileHandler removido não.
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == [], "importar a borda não pode criar arquivos no cwd"

