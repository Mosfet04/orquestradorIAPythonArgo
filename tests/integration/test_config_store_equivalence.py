"""F2-06: ``config.example.yaml`` equivale ao seed do Mongo e o app sobe com ``CONFIG_STORE=yaml``.

- Mesmo conjunto de agentes, teams e tools carregado pelo repositório Mongo (coleções em memória com
  os documentos de ``mongo-init/init-db.js``) e pelo YAML (``config.example.yaml``).
- Startup real (``AppFactory`` -> container -> use cases -> ``AgnoRuntime`` -> AgentOS) com
  ``CONFIG_STORE=yaml``: as entidades vêm do arquivo, nenhuma coleção de config do Mongo é lida, e o
  Mongo (falso) continua sendo o do resto (cliente do container, sessões).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.infrastructure.web import app_factory
from tests.fakes import RecordingLogger
from tests.fakes.config_stores import MongoBackend, YamlBackend
from tests.fakes.startup_world import CLEAN_ENV, Events, startup_world
from tests.fakes.web import loopback_client
from tests.unit.test_seed_tools import SEED

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config.example.yaml"


def _seed_block(collection: str) -> list[dict[str, Any]]:
    """Documentos do ``insertMany`` do seed; ``new Date()`` (só em ``agents_config``) vira ``null``."""
    source = SEED.read_text(encoding="utf-8")
    start = source.index(f"db.{collection}.insertMany(") + len(f"db.{collection}.insertMany(")
    end = source.index("]);", start) + 1
    docs = json.loads(re.sub(r"new Date\(\)", "null", source[start:end]))
    assert isinstance(docs, list) and docs
    return docs


def _seed() -> dict[str, list[dict[str, Any]]]:
    return {
        "agents": _seed_block("agents_config"),
        "teams": _seed_block("teams_config"),
        "tools": _seed_block("tools"),
    }


def _example() -> dict[str, list[dict[str, Any]]]:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def test_exemplo_tem_os_mesmos_documentos_do_seed_sem_os_campos_de_auditoria():
    """Mesmo formato (``factoryIaModel``, snake_case...) e mesmos valores; ``updated_at`` é só do seed."""
    seed = _seed()
    for doc in seed["agents"]:
        doc.pop("updated_at")

    assert _example() == seed


async def test_mongo_com_o_seed_e_yaml_com_o_exemplo_carregam_o_mesmo_conjunto(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    seed, example = _seed(), _example()
    mongo, yaml_store = MongoBackend(monkeypatch), YamlBackend(tmp_path)
    logger = RecordingLogger()
    loaded = []
    for backend, docs in ((mongo, seed), (yaml_store, example)):
        agents = backend.agents(docs["agents"], logger)
        teams = backend.teams(docs["teams"], logger)
        tools = backend.tools(docs["tools"], logger)
        active_agents = await agents.get_active_agents()
        loaded.append(
            (
                active_agents,
                await teams.get_active_teams(),
                await tools.get_all_active_tools(),
                [await tools.get_tools_by_ids(agent.tools_ids or []) for agent in active_agents],
            )
        )

    from_mongo, from_yaml = loaded
    assert from_yaml == from_mongo
    agents, teams, tools, tools_per_agent = from_yaml
    assert [a.id for a in agents] == ["general-assistant", "code-assistant", "analyst-assistant"]
    assert [t.id for t in teams] == ["support-router"]
    assert [t.id for t in tools] == ["weather-tool", "calculator-tool"]
    assert [[t.id for t in ts] for ts in tools_per_agent] == [[], [], ["weather-tool"]]
    assert logger.records == []


async def test_exemplo_lido_pelo_arquivo_real_do_repositorio(tmp_path: Path):
    """O próprio ``config.example.yaml`` (não uma cópia regravada) é lido sem erro nem aviso."""
    from src.infrastructure.repositories.yaml_config_repository import (
        YamlAgentConfigRepository,
        YamlConfigFile,
        YamlTeamConfigRepository,
        YamlToolRepository,
    )

    logger = RecordingLogger()
    source = YamlConfigFile(str(EXAMPLE), logger=logger)

    agents = await YamlAgentConfigRepository(source, logger=logger).get_active_agents()
    teams = await YamlTeamConfigRepository(source, logger=logger).get_active_teams()
    tools = await YamlToolRepository(source, logger=logger).get_all_active_tools()

    assert (len(agents), len(teams), len(tools)) == (3, 1, 2)
    assert logger.records == []


# ── startup real com CONFIG_STORE=yaml ───────────────────────────────


@pytest.fixture
def yaml_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CLEAN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONFIG_STORE", "yaml")
    monkeypatch.setenv("CONFIG_YAML_PATH", str(EXAMPLE))


@pytest.mark.usefixtures("yaml_env", "offline_knowledge")
def test_app_sobe_com_config_yaml_e_serve_as_entidades_do_arquivo():
    events = Events()
    # Coleções de config do Mongo com outro agente: se fossem lidas, ele apareceria.
    with startup_world(events, agents=("so-no-mongo",), teams={"time-so-no-mongo": ["so-no-mongo"]}) as motors:
        from src.infrastructure.repositories import mongo_base

        client = mongo_base.MongoClientFactory.get_client("qualquer")
        app = app_factory.AppFactory().create_app()
        with loopback_client(app) as http:
            agent_ids = sorted(a["id"] for a in http.get("/agents").json())
            team_ids = [t["id"] for t in http.get("/teams").json()]
            health = http.get("/admin/health")

    assert agent_ids == ["analyst-assistant", "code-assistant", "general-assistant"]
    assert team_ids == ["support-router"]
    assert health.status_code == 200
    # Nenhuma consulta às coleções de config do Mongo; o cliente do container (o resto) abriu e fechou.
    assert {name: col.queries for name, col in client.collections.items() if col.queries} == {}
    assert [m.closes for m in motors] == [1]


@pytest.mark.usefixtures("offline_knowledge")
def test_app_nao_sobe_com_config_yaml_quebrado(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Arquivo fora do formato: a carga falha, o lifespan recusa o startup e fecha o que abriu."""
    for name in CLEAN_ENV:
        monkeypatch.delenv(name, raising=False)
    broken = tmp_path / "config.yaml"
    broken.write_text("agentes:\n  - id: x\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_STORE", "yaml")
    monkeypatch.setenv("CONFIG_YAML_PATH", str(broken))

    from src.infrastructure.repositories.yaml_config_repository import YamlConfigError

    with startup_world(Events()) as motors:
        app = app_factory.AppFactory().create_app()
        with pytest.raises(YamlConfigError, match="chave de topo desconhecida"), loopback_client(app):
            pass

    assert [m.closes for m in motors] == [1]
