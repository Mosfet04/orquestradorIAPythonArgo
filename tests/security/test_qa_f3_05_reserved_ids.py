"""QA do F3-05: id reservado do agno 3.x em agente ou team é um documento inválido, não uma queda geral.

O AgentOS 3.x recusa a montagem inteira quando uma entidade usa um id reservado
(``agno/registry/_reserved.py``): um documento só derrubaria todos os agentes e teams. Aqui: a prova
de que o agno recusa mesmo (o motivo da validação), o startup com documentos do Mongo, a escrita pela
API admin e as variantes que não são reservadas.
"""

from __future__ import annotations

from typing import Any

import pytest
from agno.agent import Agent
from agno.registry._reserved import RESERVED_MANAGER_IDS
from agno.team import Team
from starlette.testclient import TestClient

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.domain.entities.validation import InvalidField
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel
from tests.fakes.admin_config_app import ADMIN, admin_world, agent, agent_body, team_body
from tests.fakes.app_boot import agent_doc, boot, team_doc
from tests.fakes.session_db import fake_session_db
from tests.fakes.web import loopback_client, mount_agent_os

RESERVED = sorted(RESERVED_MANAGER_IDS)
# Parecidos que o agno não reserva (comparação exata): seguem válidos aqui também.
LOOKALIKES = ["OS-Manager-Internal", " os-manager-internal", "os-manager-internal ", "os-manager", "internal"]
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")


def _fake_agent(agent_id: str) -> Agent:
    return Agent(id=agent_id, name=agent_id, model=FakeChatModel(responses=["oi"]), telemetry=False)


def _mounted(monkeypatch: pytest.MonkeyPatch, agents: list[Agent], teams: list[Team]) -> TestClient:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    factory = AppFactory()
    app = factory.create_app()
    mount_agent_os(factory, app, agents, teams, session_db=fake_session_db())
    return loopback_client(app)


# ── por que a validação existe: o AgentOS recusa a montagem inteira ──────────────────────────────


@pytest.mark.parametrize("reserved", RESERVED)
@pytest.mark.parametrize("kind", ["agent", "team"])
def test_agentos_recusa_a_montagem_inteira_com_um_id_reservado(
    monkeypatch: pytest.MonkeyPatch, reserved: str, kind: str
) -> None:
    """Controle: sem a validação do domínio, a entidade chegaria ao AgentOS e nenhuma outra subiria.
    Se o agno deixar de recusar, este teste avisa que a regra do domínio pode ser revista."""
    valid = _fake_agent("valido")
    agents = [valid, _fake_agent(reserved)] if kind == "agent" else [valid]
    member = _fake_agent("membro")
    teams = (
        [Team(id=reserved, name="t", members=[member], model=FakeChatModel(responses=["oi"]), telemetry=False)]
        if kind == "team"
        else []
    )

    with pytest.raises(ValueError, match="reserved"):
        _mounted(monkeypatch, agents, teams)


@pytest.mark.parametrize("lookalike", LOOKALIKES)
def test_id_parecido_passa_na_validacao_e_o_agentos_o_serve(monkeypatch: pytest.MonkeyPatch, lookalike: str) -> None:
    """A regra do domínio não é mais larga nem mais estreita que a do agno (comparação exata)."""
    config = AgentConfig(id=lookalike, nome="A", factory_ia_model="ollama", model="m", descricao=None, prompt="p")
    team = TeamConfig(id=lookalike, nome="T", factory_ia_model="ollama", model="m", member_ids=["a"])

    client = _mounted(monkeypatch, [_fake_agent("valido"), _fake_agent(lookalike)], [])
    served = client.get("/agents")

    assert (config.id, team.id) == (lookalike, lookalike)
    assert served.status_code == 200
    assert sorted(item["id"] for item in served.json()) == sorted(["valido", lookalike])


@pytest.mark.parametrize("value", [None, "", 7, ["os-manager-internal"], {"$in": RESERVED}])
def test_id_que_nao_e_texto_segue_recusado_como_antes_e_sem_ecoar_o_valor(value: Any) -> None:
    """``require_entity_id`` mantém o ``require_text``: tipo errado ou vazio é ``InvalidField("id")``
    (e valor não hasheável não estoura no ``in`` do conjunto de reservados)."""
    for build, fields in (
        (AgentConfig, {"nome": "A", "factory_ia_model": "ollama", "model": "m", "descricao": None, "prompt": "p"}),
        (TeamConfig, {"nome": "T", "factory_ia_model": "ollama", "model": "m", "member_ids": ["a"]}),
    ):
        with pytest.raises(InvalidField) as refused:
            build(id=value, **fields)  # type: ignore[arg-type]
        assert refused.value.field == "id"
        assert "os-manager-internal" not in str(refused.value)


# ── startup: documento do Mongo com id reservado ─────────────────────────────────────────────────


@pytest.mark.usefixtures("offline_knowledge")
def test_startup_com_documentos_de_id_reservado_sobe_e_serve_os_validos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Documento existente com id reservado (válido no 2.5.8) vira documento ignorado, com um log por
    documento, e não derruba os demais; o team que o lista como membro sobe com os membros válidos."""
    agents = [agent_doc("valido-1"), *(agent_doc(reserved) for reserved in RESERVED), agent_doc("valido-2")]
    teams = [
        team_doc("time-ok", ["valido-1", "valido-2"]),
        team_doc(RESERVED[0], ["valido-1"]),
        team_doc("time-com-membro-reservado", ["valido-1", RESERVED[1]]),
    ]

    result = boot(monkeypatch, agents, teams)

    assert result.status == (200, 200, 200)
    assert sorted(item["id"] for item in result.agents) == ["valido-1", "valido-2"]
    assert sorted(item["id"] for item in result.teams) == ["time-com-membro-reservado", "time-ok"]
    assert sorted(item["id"] for item in result.config["agents"]) == ["valido-1", "valido-2"]
    ignored = [(message, context) for message, context in result.errors() if message.endswith("inválido ignorado")]
    invalid = {"mongo_id": None, "error_type": "InvalidField"}
    assert ignored == [
        *(("Documento de agente inválido ignorado", {"agent_id": reserved, **invalid}) for reserved in RESERVED),
        ("Documento de team inválido ignorado", {"team_id": RESERVED[0], **invalid}),
    ]


# ── escrita pela API admin ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("reserved", RESERVED)
async def test_api_admin_recusa_criar_agente_ou_team_com_id_reservado(
    monkeypatch: pytest.MonkeyPatch, reserved: str
) -> None:
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        bad_agent = await world.client.post("/admin/config/agents", json=agent_body(reserved), headers=ADMIN)
        bad_team = await world.client.post("/admin/config/teams", json=team_body(reserved, ["a1"]), headers=ADMIN)
        current = await world.client.get("/admin/config/agents/a1", headers=ADMIN)
        # PUT de um agente existente (com o ETag do GET) trocando o id para um reservado: chega à validação.
        bad_put = await world.client.put(
            "/admin/config/agents/a1",
            json=agent_body(reserved),
            headers={**ADMIN, "If-Match": current.headers["etag"]},
        )
        stored_agents = await world.client.get("/admin/config/agents", headers=ADMIN)
        stored_teams = await world.client.get("/admin/config/teams", headers=ADMIN)

    assert current.status_code == 200, current.text
    for refused in (bad_agent, bad_team, bad_put):
        assert refused.status_code == 422, refused.text
        assert refused.json() == {"detail": "Configuração inválida", "field": "id"}
    assert [item["id"] for item in stored_agents.json()["items"]] == ["a1"]
    assert stored_teams.json()["items"] == []
