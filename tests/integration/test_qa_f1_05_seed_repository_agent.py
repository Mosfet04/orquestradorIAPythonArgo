"""QA do F1-05: seed -> ``MongoToolRepository`` -> ``AgentFactoryService`` -> Agent, sem Mongo real.

A coleção é um fake que aplica o mesmo filtro da consulta do repositório
(``{"id": {"$in": ids}, "active": True}``); o ``MongoToolRepository`` e o mapper são os reais.
Quando o ``node`` está instalado, o seed também é executado de verdade (com um ``db`` de
mentira) para provar que o arquivo inteiro é JS válido e que o bloco lido como JSON é o
que o mongosh inseriria.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from agno.models.response import ModelResponse

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.model_config import ModelConfig
from src.infrastructure.http.http_tool_factory import HttpToolFactory
from src.infrastructure.repositories import mongo_base
from src.infrastructure.repositories.mongo_tool_repository import MongoToolRepository
from src.infrastructure.runtime.agno import agent_factory_service
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from tests.fakes import FakeChatModel, FakeEmbedderFactory, RecordingLogger
from tests.unit.test_seed_tools import SEED, _seed_tool_docs


class _Cursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._it = iter(docs)

    def sort(self, *_: object) -> _Cursor:
        return self  # documentos do seed sem _id: a ordem da lista é a de inserção

    def __aiter__(self) -> _Cursor:
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return copy.deepcopy(next(self._it))
        except StopIteration:
            raise StopAsyncIteration from None


class _FakeCollection:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs
        self.queries: list[dict[str, Any]] = []

    def find(self, query: dict[str, Any]) -> _Cursor:
        self.queries.append(query)
        wanted = set(query["id"]["$in"])
        return _Cursor([d for d in self.docs if d.get("id") in wanted and d.get("active") == query["active"]])


class _FakeModelFactory:
    def __init__(self, model: FakeChatModel) -> None:
        self.model = model

    def create_model(self, config: ModelConfig) -> FakeChatModel:
        return self.model


@pytest.fixture(autouse=True)
def no_mongo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_factory_service, "MongoAgentDb", lambda **_: None)


def _repository(
    docs: list[dict[str, Any]], logger: RecordingLogger, monkeypatch: pytest.MonkeyPatch
) -> tuple[MongoToolRepository, _FakeCollection]:
    collection = _FakeCollection(docs)

    class _Client:
        def __getitem__(self, name: str) -> Any:
            return {"tools": collection}

    monkeypatch.setattr(mongo_base.MongoClientFactory, "get_client", lambda *a, **k: _Client())
    repository = MongoToolRepository(connection_string="mongodb://x.invalid:27017", logger=logger)
    repository._collection = collection  # type: ignore[assignment]
    return repository, collection


def _service(logger: RecordingLogger, repository: MongoToolRepository, model: FakeChatModel) -> AgentFactoryService:
    return AgentFactoryService(
        db_url="mongodb://mongo.invalid:27017",
        logger=logger,
        model_factory=_FakeModelFactory(model),  # type: ignore[arg-type]
        embedder_factory=FakeEmbedderFactory(),
        tool_factory=HttpToolFactory(logger=logger),
        tool_repository=repository,
    )


def _config(*tools_ids: str) -> AgentConfig:
    return AgentConfig(
        id="analyst-assistant",
        nome="Analyst",
        factory_ia_model="fake",
        model="m",
        descricao="d",
        prompt="p",
        tools_ids=list(tools_ids),
    )


def _errors(logger: RecordingLogger) -> list[tuple[str, dict[str, Any]]]:
    return [(r.message, r.context) for r in logger.records if r.level == "error"]


# ── seed completo pelo repositório até o schema do modelo ────────────


async def test_agente_do_seed_carrega_as_tools_semeadas_e_o_schema_chega_ao_modelo(monkeypatch: pytest.MonkeyPatch):
    logger = RecordingLogger()
    repository, collection = _repository(_seed_tool_docs(), logger, monkeypatch)
    model = FakeChatModel(responses=["ok"])

    agent = await _service(logger, repository, model).create_agent(_config("weather-tool", "calculator-tool"))
    await agent.arun("oi")

    assert collection.queries == [{"id": {"$in": ["weather-tool", "calculator-tool"]}, "active": True}]
    assert _errors(logger) == []
    sent = {t["function"]["name"]: t["function"] for t in model.calls[0].tools}
    assert set(sent) == {"weather-tool", "calculator-tool"}
    weather = sent["weather-tool"]["parameters"]
    assert weather["required"] == ["latitude", "longitude", "current"]
    assert {k: v["type"] for k, v in weather["properties"].items()} == {
        "latitude": "number",
        "longitude": "number",
        "current": "string",
    }
    assert weather["additionalProperties"] is False
    assert sent["calculator-tool"]["parameters"]["required"] == ["expr"]
    system = next(c for role, c in model.calls[0].messages if role == "system")
    assert "Instruções da tool weather-tool:" in system
    assert "Instruções da tool calculator-tool:" in system


async def test_weather_tool_do_seed_executa_get_com_query_string_sem_chave_de_api(monkeypatch: pytest.MonkeyPatch):
    logger = RecordingLogger()
    repository, _ = _repository(_seed_tool_docs(), logger, monkeypatch)
    call = ModelResponse(
        role="assistant",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "weather-tool",
                    "arguments": json.dumps(
                        {"latitude": -23.55, "longitude": -46.63, "current": "temperature_2m,wind_speed_10m"}
                    ),
                },
            }
        ],
    )
    model = FakeChatModel(responses=[call, "faz calor"])
    agent = await _service(logger, repository, model).create_agent(_config("weather-tool"))
    seen: list[httpx.Request] = []
    real_client: Callable[..., httpx.AsyncClient] = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"current": {"temperature_2m": 25.1}})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))

    output = await agent.arun("clima em São Paulo")

    assert output.content == "faz calor"
    (request,) = seen
    assert request.method == "GET"
    assert (request.url.scheme, request.url.host, request.url.path) == ("https", "api.open-meteo.com", "/v1/forecast")
    assert dict(request.url.params) == {
        "latitude": "-23.55",
        "longitude": "-46.63",
        "current": "temperature_2m,wind_speed_10m",
    }
    assert request.content == b""
    assert not {"authorization", "x-api-key", "cookie"} & {h.lower() for h in request.headers}


# ── documentos legados/inválidos e tool referenciada que não carrega ─

LEGADO_HTTP_CONFIG = {
    "id": "legado",
    "name": "Legado",
    "description": "No formato http_config que o repositório nunca leu",
    "http_config": {"base_url": "https://x.invalid", "method": "GET", "endpoint": "/", "parameters": []},
    "active": True,
}


def _doc(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "ok",
        "name": "Ok",
        "description": "Tool ok",
        "route": "https://api.example.invalid/ok",
        "http_method": "GET",
        "parameters": [],
        "active": True,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(LEGADO_HTTP_CONFIG, id="formato-http_config-sem-route"),
        pytest.param(_doc(id="ruim", parameters=None), id="parameters-null"),
        pytest.param(_doc(id="ruim", parameters={"a": 1}), id="parameters-dict"),
        pytest.param(_doc(id="ruim", parameters=["q"]), id="parametro-string"),
        pytest.param(
            _doc(id="ruim", parameters=[{"name": "q", "type": "number", "description": "x"}]), id="tipo-number"
        ),
        pytest.param(_doc(id="ruim", parameters=[{"name": "", "type": "string", "description": "x"}]), id="nome-vazio"),
        pytest.param(_doc(id="ruim", http_method="FETCH"), id="metodo-invalido"),
        pytest.param(_doc(id="ruim", name=""), id="nome-da-tool-vazio"),
    ],
)
async def test_documento_invalido_e_isolado_loga_ids_e_o_agente_sobe_com_as_outras(
    monkeypatch: pytest.MonkeyPatch, bad: dict[str, Any]
):
    bad = {**bad, "id": bad.get("id", "legado")}
    bad_id = bad["id"]
    logger = RecordingLogger()
    repository, _ = _repository([bad, _doc()], logger, monkeypatch)

    agent = await _service(logger, repository, FakeChatModel(responses=["ok"])).create_agent(_config(bad_id, "ok"))

    assert [t.name for t in agent.tools] == ["ok"]
    errors = _errors(logger)
    repo_errors = [ctx for msg, ctx in errors if msg == "Documento de tool inválido ignorado"]
    assert [c["tool_id"] for c in repo_errors] == [bad_id]
    assert repo_errors[0]["error_type"] in {"ValueError", "KeyError", "TypeError", "AttributeError"}
    agent_errors = [ctx for msg, ctx in errors if "não encontrada" in msg]
    assert agent_errors == [{"agent_id": "analyst-assistant", "tool_id": bad_id}]


async def test_falha_do_banco_loga_agente_e_ids_e_o_agente_sobe_sem_tools(monkeypatch: pytest.MonkeyPatch):
    logger = RecordingLogger()
    repository, collection = _repository([_doc()], logger, monkeypatch)

    def boom(query: dict[str, Any]) -> _Cursor:
        raise ConnectionError("mongo caiu")

    collection.find = boom  # type: ignore[method-assign]

    agent = await _service(logger, repository, FakeChatModel(responses=["ok"])).create_agent(_config("ok"))

    assert agent.tools == []
    messages = [msg for msg, _ in _errors(logger)]
    assert "Erro ao buscar tools por IDs" in messages
    agent_ctx = next(ctx for msg, ctx in _errors(logger) if msg.startswith("Erro ao buscar tools do agente"))
    assert agent_ctx["agent_id"] == "analyst-assistant" and agent_ctx["tool_ids"] == ["ok"]


async def test_tool_inativa_e_tool_ausente_geram_erro_por_id_sem_duplicar(monkeypatch: pytest.MonkeyPatch):
    logger = RecordingLogger()
    repository, _ = _repository([_doc(), _doc(id="off", active=False)], logger, monkeypatch)

    agent = await _service(logger, repository, FakeChatModel(responses=["ok"])).create_agent(
        _config("ok", "off", "off", "nunca-existiu")
    )

    assert [t.name for t in agent.tools] == ["ok"]
    assert sorted(ctx["tool_id"] for _, ctx in _errors(logger)) == ["nunca-existiu", "off"]


# ── o arquivo do seed é JS válido (node opcional) ────────────────────

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node não instalado")
def test_seed_executa_em_js_e_insere_as_mesmas_tools_que_o_teste_le_como_json(tmp_path: Path):
    runner = tmp_path / "run_seed.js"
    runner.write_text(
        textwrap.dedent(
            """
            const fs = require('fs');
            const inserted = {};
            const collection = (name) => new Proxy({}, {get: (_, method) => (...args) => {
              if (method === 'insertMany') inserted[name] = args[0];
            }});
            const db = new Proxy({}, {get: (_, name) => {
              if (name === 'getSiblingDB') return () => db;
              if (name === 'createCollection') return () => {};
              return collection(name);
            }});
            global.print = () => {};
            new Function('db', 'print', fs.readFileSync(process.argv[2], 'utf8'))(db, global.print);
            console.log(JSON.stringify(inserted));
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(  # noqa: S603  # node do PATH, script e arquivo de seed do próprio repositório
        [str(NODE), str(runner), str(SEED)], capture_output=True, text=True, timeout=30, check=False
    )

    assert result.returncode == 0, result.stderr
    inserted = json.loads(result.stdout)
    assert inserted["tools"] == _seed_tool_docs()
    assert [d["id"] for d in inserted["agents_config"]] == ["general-assistant", "code-assistant", "analyst-assistant"]
