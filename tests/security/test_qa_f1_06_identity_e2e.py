"""QA F1-06 (B11): lacunas de ponta a ponta da identidade por ``user_id``.

Cadeia real como em ``test_user_memory_isolation.py``: factories -> Agent/Team do agno ->
``AppFactory._mount_agent_os`` (auth por chaves run/admin) -> REST. Modelo ``FakeChatModel``,
db ``InMemoryDb`` compartilhado. Cobre o que o teste do dev não cobre: memória agêntica (o modelo
chamando a tool de memória do agno), ``user_id`` explícito ``"default"`` (reservado), formatos hostis de
``user_id``, sessão compartilhada por dois usuários, team delegando a membro com memória e o que as
rotas ``/memories`` do AgentOS deixam cada chave ler/gravar.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from agno.agent import Agent
from agno.db.in_memory import InMemoryDb
from agno.db.schemas import UserMemory
from agno.models.response import ModelResponse
from agno.team import Team
from starlette.testclient import TestClient

from src.application.services import agent_factory_service, team_factory_service
from src.application.services.agent_factory_service import AgentFactoryService
from src.application.services.team_factory_service import TeamFactoryService
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import (
    FakeChatModel,
    FakeEmbedderFactory,
    FakeModelCall,
    FakeModelFactory,
    InMemoryToolRepository,
    RecordingLogger,
)

RUN_KEY = "qa6-run-key-" + "r" * 22
ADMIN_KEY = "qa6-admin-key-" + "a" * 20
ALL_INTERFACES = "0.0.0" + ".0"
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")

LEGACY = "memória legada do usuário default (antes do F1-06)"
ANA = "Ana prefere respostas em espanhol"
BRUNO = "Bruno é alérgico a amendoim"
SECRETS = (LEGACY, ANA, BRUNO)


# ── montagem ─────────────────────────────────────────────────────────


@pytest.fixture
def memory_db(monkeypatch: pytest.MonkeyPatch) -> InMemoryDb:
    db = InMemoryDb()
    for user_id, memory in (("default", LEGACY), ("ana", ANA), ("bruno", BRUNO)):
        db.upsert_user_memory(UserMemory(memory=memory, user_id=user_id))
    monkeypatch.setattr(agent_factory_service, "MongoAgentDb", lambda **_: db)
    monkeypatch.setattr(team_factory_service, "MongoAgentDb", lambda **_: db)
    return db


class _NoTools:
    async def create_tools_from_configs(self, configs: list[Any]) -> list[Any]:
        return []


async def _agent(
    agent_id: str, *, memory: bool, summary: bool = False, model: FakeChatModel | None = None
) -> Agent:
    service = AgentFactoryService(
        db_url="mongodb://mongo.invalid:27017",
        logger=RecordingLogger(),
        model_factory=FakeModelFactory(responses=["resposta"] * 30),
        embedder_factory=FakeEmbedderFactory(),
        tool_factory=_NoTools(),
        tool_repository=InMemoryToolRepository([]),
    )
    agent = await service.create_agent(
        AgentConfig(
            id=agent_id,
            nome=agent_id,
            factory_ia_model="fake",
            model=agent_id,
            descricao="desc",
            prompt="Você é um agente de teste.",
            user_memory_active=memory,
            summary_active=summary,
        )
    )
    if model is not None:
        agent.model = model
    return agent


def _team(
    team_id: str, members: list[Agent], *, memory: bool, model: FakeChatModel | None = None, mode: str = "coordinate"
) -> Team:
    service = TeamFactoryService(
        db_url="mongodb://mongo.invalid:27017",
        logger=RecordingLogger(),
        model_factory=FakeModelFactory(responses=["resposta do time"] * 30),
    )
    team = service.create_team(
        TeamConfig(
            id=team_id,
            nome=team_id,
            factory_ia_model="fake",
            model=team_id,
            member_ids=[m.id for m in members if m.id],
            mode=mode,
            user_memory_active=memory,
        ),
        members,
    )
    if model is not None:
        team.model = model
    return team


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


def _tool_call(name: str, arguments: dict[str, Any], call_id: str = "c1") -> ModelResponse:
    return ModelResponse(
        role="assistant",
        tool_calls=[
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
        ],
    )


def _prompt(model: FakeChatModel) -> str:
    return "\n".join(content for call in model.calls for _, content in call.messages)


def _rows(db: InMemoryDb) -> list[tuple[str | None, str | None]]:
    rows = db.get_user_memories()
    assert isinstance(rows, list)
    return sorted((m.user_id, m.memory) for m in rows if isinstance(m, UserMemory))


def _run(client: TestClient, path: str, **form: str) -> dict[str, Any]:
    response = client.post(path, data={"message": "o que você sabe de mim?", "stream": "false", **form}, headers=RUN)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


@dataclass
class _MemoryBrain(FakeChatModel):
    """Modelo que, ao ver a tool ``update_user_memory``, a chama; o gerenciador de memória do agno
    (cópia do modelo, vê ``add_memory``) grava a ``task`` recebida como memória.

    O roteiro é por papel (pelas tools oferecidas), não por posição: o agno copia o modelo para o
    gerenciador de memória e a ordem das chamadas entre as cópias não é estável.
    """

    task: str = ""

    def _next_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        call: FakeModelCall = self._as_call(args, kwargs)
        self.calls.append(call)
        has_result = any(role == "tool" for role, _ in call.messages)
        if "add_memory" in call.tool_names:
            if has_result:
                return ModelResponse(role="assistant", content="memória salva")
            text = call.last_user_message or ""
            return _tool_call("add_memory", {"memory": text, "topics": ["qa"]}, "m1")
        if "update_user_memory" in call.tool_names and self.task and not has_result:
            return _tool_call("update_user_memory", {"task": self.task}, "a1")
        return ModelResponse(role="assistant", content="anotado")


# ── memória agêntica (o modelo chama a tool de memória) ──────────────


async def test_memoria_agentica_grava_no_user_id_da_requisicao_e_so_ele_a_le(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    fact = "FATO-AGENTICO: a Ana mora em Lisboa"
    brain = _MemoryBrain(task=fact)
    agent = await _agent("com-memoria", memory=True, model=brain)
    client = client_for([agent])

    body = _run(client, "/agents/com-memoria/runs", user_id="ana")

    assert body["status"] == "COMPLETED", body
    # a tool foi de fato chamada pelo modelo e o gerenciador de memória do agno a executou
    assert any("update_user_memory" in c.tool_names for c in brain.calls)
    stored = [(u, m) for u, m in _rows(memory_db) if m and fact in m]
    assert stored and all(u == "ana" for u, _ in stored), _rows(memory_db)
    # nada vazou para o usuário "default" nem para o outro usuário
    assert not [1 for u, m in _rows(memory_db) if m and fact in m and u != "ana"]
    assert ("default", LEGACY) in _rows(memory_db) and ("bruno", BRUNO) in _rows(memory_db)

    # outro usuário não vê o fato recém-gravado no contexto
    brain.task = ""
    brain.calls.clear()
    _run(client, "/agents/com-memoria/runs", user_id="bruno")
    assert fact not in _prompt(brain) and ANA not in _prompt(brain)
    assert BRUNO in _prompt(brain)


async def test_memoria_agentica_de_dois_usuarios_nao_se_mistura(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    brain = _MemoryBrain()
    agent = await _agent("com-memoria", memory=True, model=brain)
    client = client_for([agent])

    for user_id in ("ana", "bruno"):
        brain.task = f"FATO-DE-{user_id.upper()}"
        assert _run(client, "/agents/com-memoria/runs", user_id=user_id)["status"] == "COMPLETED"

    by_user = {(u, m) for u, m in _rows(memory_db) if m and m.startswith("FATO-DE-")}
    assert by_user == {("ana", "FATO-DE-ANA"), ("bruno", "FATO-DE-BRUNO")}


async def test_run_recusado_nao_executa_a_tool_de_memoria(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    brain = _MemoryBrain(task="FATO-ANONIMO")
    agent = await _agent("com-memoria", memory=True, model=brain)
    client = client_for([agent])
    before = _rows(memory_db)

    body = _run(client, "/agents/com-memoria/runs")

    assert body["status"] == "ERROR"
    assert brain.calls == []
    assert _rows(memory_db) == before


# ── user_id explícito "default" (memória legada compartilhada) ───────


async def test_user_id_default_explicito_e_recusado_e_nao_le_a_memoria_legada(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    """``default`` é o usuário do fallback do agno 2.5.8 para memória sem ``user_id``: é onde cai o
    que escapa do guardrail (ex.: ``continue_run``, que não roda pre-hooks) e o que o próprio agno
    grava sem usuário. Enviá-lo explícito leria esse pool compartilhado; o guardrail o trata como
    reservado (só o literal exato; sem normalização, ``"Default"`` é outro usuário). O pool legado
    deste app, de antes do F1-06, é ``"ava"`` (ver README)."""
    brain = FakeChatModel(responses=["ok"] * 5)
    agent = await _agent("com-memoria", memory=True, model=brain)
    client = client_for([agent])
    before = _rows(memory_db)

    body = _run(client, "/agents/com-memoria/runs", user_id="default")

    assert body["status"] == "ERROR" and "user_id" in body["content"]
    assert brain.calls == []
    assert LEGACY not in json.dumps(body, ensure_ascii=False)
    assert _rows(memory_db) == before


# ── formatos de user_id ──────────────────────────────────────────────

HOSTIL_IDS = {
    "espacos-nas-pontas": " ana ",
    "unicode": "ánà-ü-日本語-😀",
    "muito-longo": "x" * 20_000,
    "caractere-de-controle": "ana\x01\x7f",
    "tentativa-de-regex-ou-query": "ana|bruno' || '1'=='1",
    "prefixo-de-ana": "an",
}


@pytest.mark.parametrize("user_id", list(HOSTIL_IDS.values()), ids=list(HOSTIL_IDS))
async def test_user_id_hostil_ou_atipico_nao_le_a_memoria_de_outro_usuario(
    user_id: str, memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    brain = FakeChatModel(responses=["ok"] * 5)
    agent = await _agent("com-memoria", memory=True, model=brain)
    client = client_for([agent])

    body = _run(client, "/agents/com-memoria/runs", user_id=user_id)

    assert body["status"] == "COMPLETED", body
    assert not any(secret in _prompt(brain) for secret in SECRETS)


async def test_user_id_com_espacos_nas_pontas_e_outro_usuario_que_o_sem_espacos(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    """Registro: o valor não é normalizado (``" ana "`` != ``"ana"``). Isola, mas fragmenta a memória."""
    brain = FakeChatModel(responses=["ok"] * 5)
    agent = await _agent("com-memoria", memory=True, model=brain)
    client = client_for([agent])

    _run(client, "/agents/com-memoria/runs", user_id=" ana ")
    assert ANA not in _prompt(brain)
    brain.calls.clear()
    _run(client, "/agents/com-memoria/runs", user_id="ana")
    assert ANA in _prompt(brain)


@pytest.mark.parametrize("blank", ["\t", "\n", "\r\n", " \t\n ", "\u00a0", "\u3000", "\u2003"])
async def test_user_id_so_com_espaco_unicode_e_recusado(
    blank: str, memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    brain = FakeChatModel(responses=["ok"] * 5)
    agent = await _agent("com-memoria", memory=True, model=brain)
    client = client_for([agent])

    body = _run(client, "/agents/com-memoria/runs", user_id=blank)

    assert body["status"] == "ERROR" and "user_id" in body["content"]
    assert brain.calls == []


@pytest.mark.parametrize("invisible", ["\u200b", "\ufeff", "\u2060", "\x00"])
async def test_user_id_invisivel_e_recusado(
    invisible: str, memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    """``str.strip`` não remove U+200B/U+FEFF/U+2060/NUL: sem a checagem de caractere visível, um
    cliente mandaria um "anônimo compartilhado" que parece vazio. O guardrail exige ao menos um
    caractere fora das categorias unicode de separador (Z*) e controle/formatação (C*)."""
    brain = FakeChatModel(responses=["ok"] * 5)
    agent = await _agent("com-memoria", memory=True, model=brain)
    client = client_for([agent])

    body = _run(client, "/agents/com-memoria/runs", user_id=invisible)

    assert body["status"] == "ERROR" and "user_id" in body["content"]
    assert brain.calls == []


# ── sessão compartilhada por dois usuários ───────────────────────────


async def test_dois_user_ids_com_o_mesmo_session_id_nao_vazam_historico(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    brain = FakeChatModel(responses=["resposta-para-ana", "resposta-para-bruno", "resposta-2-ana"])
    agent = await _agent("com-memoria", memory=True, model=brain)
    client = client_for([agent])

    def post(user_id: str, message: str) -> dict[str, Any]:
        return _run(client, "/agents/com-memoria/runs", user_id=user_id, session_id="sessao-comum", message=message)

    ana = post("ana", "SEGREDO-DA-ANA-4711")
    assert ana["status"] == "COMPLETED", ana
    bruno = post("bruno", "mensagem do bruno")
    assert bruno["status"] == "COMPLETED", bruno

    bruno_prompt = "\n".join(c for _, c in brain.calls[1].messages)
    assert "SEGREDO-DA-ANA-4711" not in bruno_prompt
    assert "resposta-para-ana" not in bruno_prompt

    # e a sessão da Ana não foi sobrescrita nem contaminada pelo Bruno
    post("ana", "de novo")
    ana_prompt = "\n".join(c for _, c in brain.calls[2].messages)
    assert "SEGREDO-DA-ANA-4711" in ana_prompt
    assert "mensagem do bruno" not in ana_prompt and "resposta-para-bruno" not in ana_prompt


async def test_sessao_de_um_usuario_nao_e_listada_nem_lida_pelo_outro(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    """Registro do que a chave run enxerga em /sessions: filtra por ``user_id`` se informado."""
    brain = FakeChatModel(responses=["ok"] * 5)
    agent = await _agent("com-memoria", memory=True, model=brain)
    client = client_for([agent])
    _run(client, "/agents/com-memoria/runs", user_id="ana", session_id="s-ana", message="SEGREDO-DA-ANA-4711")

    own = client.get("/sessions", params={"user_id": "ana", "type": "agent"}, headers=RUN)
    other = client.get("/sessions", params={"user_id": "bruno", "type": "agent"}, headers=RUN)

    assert own.status_code == 200 and other.status_code == 200
    assert [s["session_id"] for s in own.json()["data"]] == ["s-ana"]
    assert other.json()["data"] == []


# ── team delegando a membro com memória ──────────────────────────────


@pytest.mark.parametrize("mode", ["coordinate", "route"])
async def test_team_repassa_o_user_id_ao_membro_que_tem_memoria(
    mode: str, memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    member_model = FakeChatModel(responses=["membro-respondeu"] * 5)
    member = await _agent("membro", memory=True, model=member_model)
    team_model = FakeChatModel(
        responses=[_tool_call("delegate_task_to_member", {"member_id": "membro", "task": "diga o que sabe"}), "fim"]
    )
    team = _team("time", [member], memory=False, model=team_model, mode=mode)
    client = client_for([member], [team])

    body = _run(client, "/teams/time/runs", user_id="ana")

    assert body["status"] == "COMPLETED", body
    assert member_model.calls, "o membro não foi chamado"
    assert ANA in _prompt(member_model)
    assert BRUNO not in _prompt(member_model) and LEGACY not in _prompt(member_model)
    # o team sem memória própria não lê memória de ninguém
    assert not any(secret in _prompt(team_model) for secret in SECRETS)


async def test_team_sem_memoria_e_sem_membro_com_memoria_aceita_run_sem_user_id(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    member = await _agent("membro", memory=False)
    team = _team("time", [member], memory=False)
    client = client_for([member], [team])

    body = _run(client, "/teams/time/runs")

    assert body["status"] == "COMPLETED"
    assert not any(secret in _prompt(team.model) for secret in SECRETS)  # type: ignore[arg-type]


async def test_team_com_memoria_e_membro_com_memoria_cada_um_le_so_o_user_id_da_requisicao(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    member_model = FakeChatModel(responses=["membro-respondeu"] * 5)
    member = await _agent("membro", memory=True, model=member_model)
    team_model = FakeChatModel(
        responses=[_tool_call("delegate_task_to_member", {"member_id": "membro", "task": "diga"}), "fim"] * 3
    )
    team = _team("time", [member], memory=True, model=team_model)
    client = client_for([member], [team])

    body = _run(client, "/teams/time/runs", user_id="bruno")

    assert body["status"] == "COMPLETED", body
    for model in (member_model, team_model):
        assert BRUNO in _prompt(model)
        assert ANA not in _prompt(model) and LEGACY not in _prompt(model)


async def test_team_recusa_run_sem_user_id_em_streaming(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    member = await _agent("membro", memory=True)
    team = _team("time", [member], memory=False)
    client = client_for([member], [team])

    response = client.post("/teams/time/runs", data={"message": "oi", "stream": "true"}, headers=RUN)

    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line.startswith("data: ")]
    events = [json.loads(line.removeprefix("data: ")) for line in lines]
    assert [e for e in events if e.get("event") in ("RunError", "TeamRunError")], response.text
    assert not any(secret in response.text for secret in SECRETS)


# ── agente sem memória ───────────────────────────────────────────────


@pytest.mark.parametrize("form", [{}, {"user_id": ""}, {"user_id": "   "}, {"user_id": "ana"}])
async def test_agente_sem_memoria_aceita_qualquer_user_id_e_nao_le_memoria(
    form: dict[str, str], memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    brain = FakeChatModel(responses=["ok"] * 5)
    agent = await _agent("sem-memoria", memory=False, summary=True, model=brain)
    client = client_for([agent])
    before = _rows(memory_db)

    body = _run(client, "/agents/sem-memoria/runs", **form)

    assert body["status"] == "COMPLETED"
    assert not any(secret in _prompt(brain) for secret in SECRETS)
    assert _rows(memory_db) == before


# ── rotas /memories do AgentOS: o que cada chave pode ───────────────


async def test_chave_run_lista_memorias_por_user_id_registro_de_risco(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    """Caracterização: a chave run (cliente final) lê as memórias de QUALQUER ``user_id`` em
    ``GET /memories?user_id=`` e, sem ``user_id``, as de TODOS. O ``user_id`` não é amarrado à
    credencial (modelo atual: confiado ao chamador). A admin lê igual."""
    agent = await _agent("com-memoria", memory=True)
    client = client_for([agent])

    for headers in (RUN, ADMIN):
        one = client.get("/memories", params={"user_id": "ana"}, headers=headers)
        every = client.get("/memories", headers=headers)
        assert one.status_code == 200 and every.status_code == 200
        assert {m["memory"] for m in one.json()["data"]} == {ANA}
        assert {m["user_id"] for m in every.json()["data"]} == {"default", "ana", "bruno"}


async def test_chave_run_nao_apaga_memoria_mas_admin_apaga(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    agent = await _agent("com-memoria", memory=True)
    client = client_for([agent])
    ana_id = next(m.memory_id for m in memory_db.get_user_memories() if m.user_id == "ana")  # type: ignore[union-attr]

    assert client.delete(f"/memories/{ana_id}", params={"user_id": "ana"}, headers=RUN).status_code == 403
    assert ("ana", ANA) in _rows(memory_db)
    bulk = client.request("DELETE", "/memories", json={"memory_ids": [ana_id], "user_id": "ana"}, headers=RUN)
    assert bulk.status_code == 403
    assert ("ana", ANA) in _rows(memory_db)
    assert client.delete(f"/memories/{ana_id}", params={"user_id": "ana"}, headers=ADMIN).status_code == 204
    assert ("ana", ANA) not in _rows(memory_db)


async def test_chave_run_grava_memoria_em_nome_de_outro_user_id_registro_de_risco(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    """Caracterização: ``POST /memories`` é rota run (só DELETE é admin). Quem tem a chave run grava
    texto na memória de qualquer usuário, que depois entra no prompt dele."""
    brain = FakeChatModel(responses=["ok"] * 5)
    agent = await _agent("com-memoria", memory=True, model=brain)
    client = client_for([agent])

    payload = {"memory": "INJETADA-VIA-REST", "user_id": "ana", "topics": []}
    created = client.post("/memories", json=payload, headers=RUN)

    assert created.status_code == 200, created.text
    _run(client, "/agents/com-memoria/runs", user_id="ana")
    assert "INJETADA-VIA-REST" in _prompt(brain)


async def test_chave_run_nao_cria_memoria_sem_user_id(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    agent = await _agent("com-memoria", memory=True)
    client = client_for([agent])
    before = _rows(memory_db)

    response = client.post("/memories", json={"memory": "sem dono"}, headers=RUN)

    assert response.status_code >= 400
    assert _rows(memory_db) == before


async def test_chave_run_le_sessao_de_qualquer_usuario_sem_informar_user_id_registro_de_risco(
    memory_db: InMemoryDb, client_for: Callable[..., TestClient]
) -> None:
    """Caracterização: como em ``/memories``, ``GET /sessions/{id}/runs`` aceita a chave run e não
    amarra o ``user_id`` à credencial: quem sabe o ``session_id`` lê o histórico de outro usuário."""
    brain = FakeChatModel(responses=["ok"] * 5)
    agent = await _agent("com-memoria", memory=True, model=brain)
    client = client_for([agent])
    _run(client, "/agents/com-memoria/runs", user_id="ana", session_id="s-ana", message="SEGREDO-DA-ANA-4711")

    response = client.get("/sessions/s-ana/runs", params={"type": "agent"}, headers=RUN)

    assert response.status_code == 200, response.text
    assert "SEGREDO-DA-ANA-4711" in response.text
