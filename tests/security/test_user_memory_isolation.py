"""F1-06 (B11): memória de usuário isolada por ``user_id``, de ponta a ponta.

Cadeia real: ``AgentFactoryService``/``TeamFactoryService`` -> ``Agent``/``Team`` do agno ->
``AppFactory._mount_agent_os`` (AgentOS 2.5.8, auth com chave run) -> REST e AG-UI. O
modelo é o ``FakeChatModel``; o db do agno é o ``InMemoryDb`` (sem Mongo), compartilhado
por todos, como o Mongo em produção.

Antes: todo Agent/Team nascia com ``user_id="ava"`` e todo chamador lia e gravava as
memórias do mesmo usuário. Sem o valor fixo, o agno 2.5.8 usaria o usuário ``"default"``
para run sem ``user_id`` (``agno/agent/_messages.py``, ``agno/memory/manager.py``): o mesmo
vazamento com outro nome. Por isso entidade com memória de usuário recusa run sem
``user_id`` antes de ler ou gravar memória.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from agno.agent import Agent
from agno.db.in_memory import InMemoryDb
from agno.db.schemas import UserMemory
from agno.team import Team
from starlette.testclient import TestClient

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.infrastructure.runtime.agno import agent_factory_service, team_factory_service
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import (
    FakeChatModel,
    FakeEmbedderFactory,
    FakeModelFactory,
    InMemoryToolRepository,
    RecordingLogger,
)

RUN_KEY = "qa-run-key-" + "r" * 21
ADMIN_KEY = "qa-admin-key-" + "a" * 19
ALL_INTERFACES = "0.0.0" + ".0"  # bind recusado sem chaves: o app sobe em modo produção
AUTH = {"Authorization": f"Bearer {RUN_KEY}"}
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")

MEMORY_OF = {
    "default": "o chamador anônimo anterior mora em Recife",
    "ana": "Ana prefere respostas em espanhol",
    "bruno": "Bruno é alérgico a amendoim",
}


@pytest.fixture
def memory_db(monkeypatch: pytest.MonkeyPatch) -> InMemoryDb:
    """Um db do agno para todas as entidades, com uma memória já gravada por usuário."""
    db = InMemoryDb()
    for user_id, memory in MEMORY_OF.items():
        db.upsert_user_memory(UserMemory(memory=memory, user_id=user_id))
    monkeypatch.setattr(agent_factory_service, "MongoAgentDb", lambda **_: db)
    monkeypatch.setattr(team_factory_service, "MongoAgentDb", lambda **_: db)
    return db


def _agent_config(agent_id: str, *, memory: bool) -> AgentConfig:
    return AgentConfig(
        id=agent_id,
        nome=agent_id,
        factory_ia_model="fake",
        model=agent_id,
        descricao="desc",
        prompt="Você é um agente de teste.",
        user_memory_active=memory,
    )


async def _agent(agent_id: str, *, memory: bool) -> Agent:
    logger = RecordingLogger()
    service = AgentFactoryService(
        db_url="mongodb://mongo.invalid:27017",
        logger=logger,
        model_factory=FakeModelFactory(responses=["resposta"] * 20),
        embedder_factory=FakeEmbedderFactory(),
        tool_factory=_NoTools(),
        tool_repository=InMemoryToolRepository([]),
    )
    return await service.create_agent(_agent_config(agent_id, memory=memory))


def _team(team_id: str, members: list[Agent], *, memory: bool) -> Team:
    service = TeamFactoryService(
        db_url="mongodb://mongo.invalid:27017",
        logger=RecordingLogger(),
        model_factory=FakeModelFactory(responses=["resposta do time"] * 20),
    )
    config = TeamConfig(
        id=team_id,
        nome=team_id,
        factory_ia_model="fake",
        model=team_id,
        member_ids=[m.id for m in members if m.id],
        mode="coordinate",
        user_memory_active=memory,
    )
    return service.create_team(config, members)


class _NoTools:
    async def create_tools_from_configs(self, configs: list[Any]) -> list[Any]:
        return []


@pytest.fixture
def client_for(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def _build(agents: list[Agent], teams: list[Team] | None = None) -> TestClient:
        for name in _ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AGNO_TELEMETRY", "false")
        monkeypatch.setenv("APP_HOST", ALL_INTERFACES)
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("API_KEY_RUN", RUN_KEY)
        monkeypatch.setenv("API_KEY_ADMIN", ADMIN_KEY)
        factory = AppFactory()
        app = factory.create_app()
        factory._mount_agent_os(app, agents, teams or [])
        client = TestClient(app, raise_server_exceptions=False)
        clients.append(client)
        return client

    yield _build
    for client in clients:
        client.close()


def _model(entity: Agent | Team) -> FakeChatModel:
    assert isinstance(entity.model, FakeChatModel)
    return entity.model


def _prompt_text(model: FakeChatModel) -> str:
    """Tudo o que o modelo recebeu (inclui system message com as memórias)."""
    return "\n".join(content for call in model.calls for _, content in call.messages)


def _memories(db: InMemoryDb) -> list[tuple[str | None, str | None]]:
    rows = db.get_user_memories()
    assert isinstance(rows, list)
    return sorted((m.user_id, m.memory) for m in rows if isinstance(m, UserMemory))


def _run(client: TestClient, path: str, **form: str) -> dict[str, Any]:
    response = client.post(path, data={"message": "o que você sabe de mim?", "stream": "false", **form}, headers=AUTH)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _sse(text: str) -> list[dict[str, Any]]:
    return [json.loads(line.removeprefix("data: ")) for line in text.splitlines() if line.startswith("data: ")]


# ── agente com memória ──────────────────────────────────────────────


@pytest.mark.parametrize("missing", [{}, {"user_id": ""}, {"user_id": "   "}], ids=["ausente", "vazio", "branco"])
async def test_dois_chamadores_sem_user_id_nao_compartilham_memoria(
    missing: dict[str, str], memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    agent = await _agent("com-memoria", memory=True)
    client = client_for([agent])
    before = _memories(memory_db)

    first = _run(client, "/agents/com-memoria/runs", **missing)
    second = _run(client, "/agents/com-memoria/runs", **missing)

    for body in (first, second):
        assert body["status"] == "ERROR"
        assert "user_id" in body["content"]
        assert all(memory not in json.dumps(body, ensure_ascii=False) for memory in MEMORY_OF.values())
    assert _model(agent).calls == []  # nada chegou ao modelo: nenhuma memória lida
    assert _memories(memory_db) == before  # nenhuma memória gravada


async def test_sem_user_id_em_streaming_o_run_e_recusado_com_run_error(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    agent = await _agent("com-memoria", memory=True)
    client = client_for([agent])

    response = client.post("/agents/com-memoria/runs", data={"message": "oi", "stream": "true"}, headers=AUTH)

    assert response.status_code == 200
    errors = [e for e in _sse(response.text) if e.get("event") == "RunError"]
    assert len(errors) == 1 and "user_id" in errors[0]["content"]
    assert MEMORY_OF["default"] not in response.text
    assert _model(agent).calls == []


async def test_user_ids_diferentes_tem_memorias_isoladas(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    agent = await _agent("com-memoria", memory=True)
    client = client_for([agent])
    model = _model(agent)

    seen: dict[str, str] = {}
    for user_id in ("ana", "bruno"):
        model.calls.clear()
        body = _run(client, "/agents/com-memoria/runs", user_id=user_id)
        assert body["status"] == "COMPLETED", body
        seen[user_id] = _prompt_text(model)

    assert MEMORY_OF["ana"] in seen["ana"]
    assert MEMORY_OF["bruno"] not in seen["ana"] and MEMORY_OF["default"] not in seen["ana"]
    assert MEMORY_OF["bruno"] in seen["bruno"]
    assert MEMORY_OF["ana"] not in seen["bruno"] and MEMORY_OF["default"] not in seen["bruno"]


async def test_agui_sem_user_id_e_recusado_e_com_user_id_le_so_a_propria_memoria(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    agent = await _agent("com-memoria", memory=True)
    client = client_for([agent])
    model = _model(agent)

    def agui(forwarded: dict[str, str], run_id: str) -> str:
        body = {
            "threadId": f"t-{run_id}",
            "runId": run_id,
            "state": {},
            "messages": [{"id": f"m-{run_id}", "role": "user", "content": "o que você sabe de mim?"}],
            "tools": [],
            "context": [],
            "forwardedProps": forwarded,
        }
        response = client.post("/agui/com-memoria", json=body, headers=AUTH)
        assert response.status_code == 200
        return response.text

    # Desde o F1-08 o run recusado chega como RUN_STARTED + RUN_ERROR com o motivo (antes o
    # AG-UI do agno descartava o RunError). E o modelo não foi chamado (nada de memória).
    refused = agui({}, "r-anon")
    assert [e["type"] for e in _sse(refused)] == ["RUN_STARTED", "RUN_ERROR"]
    assert "user_id obrigatório" in _sse(refused)[-1]["message"]
    assert model.calls == []

    accepted = agui({"user_id": "ana"}, "r-ana")
    assert '"RUN_ERROR"' not in accepted
    assert MEMORY_OF["ana"] in _prompt_text(model)
    assert MEMORY_OF["bruno"] not in _prompt_text(model) and MEMORY_OF["default"] not in _prompt_text(model)


def _agui_body(forwarded: dict[str, Any], run_id: str) -> dict[str, Any]:
    return {
        "threadId": f"t-{run_id}",
        "runId": run_id,
        "state": {},
        "messages": [{"id": f"m-{run_id}", "role": "user", "content": "o que você sabe de mim?"}],
        "tools": [],
        "context": [],
        "forwardedProps": forwarded,
    }


# JSON do AG-UI chega sem validação de tipo em forwardedProps: o guardrail não pode cair num
# caminho que levante outra exceção (o agno engole exceção de pre-hook que não seja
# InputCheckError e o run seguiria com a memória "default") nem aceitar não-string (123 viraria
# um bucket int compartilhado).
NON_STRING_USER_IDS = {"zero": 0, "false": False, "true": True, "objeto": {}, "lista": [], "inteiro": 123, "float": 1.5}


@pytest.mark.parametrize("user_id", list(NON_STRING_USER_IDS.values()), ids=list(NON_STRING_USER_IDS))
@pytest.mark.parametrize("kind", ["agent", "team"])
async def test_agui_com_user_id_nao_string_e_recusado_sem_ler_memoria(
    kind: str, user_id: object, memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    before = _memories(memory_db)
    if kind == "agent":
        entity: Agent | Team = await _agent("com-memoria", memory=True)
        models = [_model(entity)]
        client = client_for([entity])
    else:
        member = await _agent("membro", memory=False)
        entity = _team("time", [member], memory=True)
        models = [_model(entity), _model(member)]
        client = client_for([], [entity])

    response = client.post(f"/agui/{entity.id}", json=_agui_body({"user_id": user_id}, "r-tipo"), headers=AUTH)

    assert response.status_code == 200
    assert [e["type"] for e in _sse(response.text)] == ["RUN_STARTED", "RUN_ERROR"]
    assert all(model.calls == [] for model in models)
    assert all(memory not in response.text for memory in MEMORY_OF.values())
    assert _memories(memory_db) == before


@pytest.mark.parametrize(
    "user_id",
    ["default", "\u200b", "\ufeff", "\u2060", "\x00", "\u200b \ufeff\t"],
    ids=["default-reservado", "zero-width-space", "bom", "word-joiner", "nul", "misto-invisivel"],
)
async def test_user_id_reservado_ou_sem_caractere_visivel_e_recusado(
    user_id: str, memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    agent = await _agent("com-memoria", memory=True)
    client = client_for([agent])

    body = _run(client, "/agents/com-memoria/runs", user_id=user_id)

    assert body["status"] == "ERROR" and "user_id" in body["content"]
    assert _model(agent).calls == []
    assert MEMORY_OF["default"] not in json.dumps(body, ensure_ascii=False)


async def test_agente_sem_memoria_continua_aceitando_run_sem_user_id(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    agent = await _agent("sem-memoria", memory=False)
    client = client_for([agent])

    body = _run(client, "/agents/sem-memoria/runs")

    assert body["status"] == "COMPLETED"
    assert body["content"] == "resposta"
    assert all(memory not in _prompt_text(_model(agent)) for memory in MEMORY_OF.values())


# ── team ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("team_memory", "member_memory"), [(True, False), (False, True)], ids=["memoria-no-team", "memoria-no-membro"]
)
async def test_team_com_memoria_propria_ou_de_membro_recusa_run_sem_user_id(
    team_memory: bool, member_memory: bool, memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    member = await _agent("membro", memory=member_memory)
    team = _team("time", [member], memory=team_memory)
    client = client_for([member], [team])
    before = _memories(memory_db)

    body = _run(client, "/teams/time/runs")

    assert body["status"] == "ERROR"
    assert "user_id" in body["content"]
    assert _model(team).calls == [] and _model(member).calls == []
    assert _memories(memory_db) == before


async def test_team_com_memoria_le_so_a_memoria_do_user_id_da_requisicao(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    member = await _agent("membro", memory=False)
    team = _team("time", [member], memory=True)
    client = client_for([member], [team])

    body = _run(client, "/teams/time/runs", user_id="bruno")

    assert body["status"] == "COMPLETED", body
    prompt = _prompt_text(_model(team))
    assert MEMORY_OF["bruno"] in prompt
    assert MEMORY_OF["ana"] not in prompt and MEMORY_OF["default"] not in prompt
