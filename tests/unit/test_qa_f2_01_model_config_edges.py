"""QA F2-01: lacunas do ``ModelConfig`` e dos mappers com documentos reais/legados e hostis.

Cobre: (1) equivalência com o comportamento anterior (oráculo independente do mapper antigo)
para agents/teams/rag_config com e sem modelo, nas grafias snake_case e camelCase, com e sem os
campos novos; (2) ``model_params``/``base_url``/``api_key_ref`` hostis isolam o documento sem
vazar o valor; (3) caracterização do que ``base_url`` ainda aceita (insumo para a F2-02/F4);
(4) cópia/serialização do value object.
"""

from __future__ import annotations

import copy
import dataclasses
import math
from typing import Any

import pytest

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.model_config import ModelConfig
from src.domain.entities.rag_config import RagConfig, SearchStrategy
from src.domain.entities.team_config import TeamConfig
from src.infrastructure.repositories import mongo_base
from src.infrastructure.repositories.mongo_agent_config_repository import MongoAgentConfigRepository
from src.infrastructure.repositories.mongo_team_config_repository import MongoTeamConfigRepository
from tests.fakes import FakeMongoClient, FakeMongoCollection, RecordingLogger

MARKER = "sk-QA-F201-VALOR-SECRETO"
CONN = "mongodb://mongo.invalid:27017"


async def _agents(monkeypatch: pytest.MonkeyPatch, docs: list[dict[str, Any]], logger: RecordingLogger | None = None):
    client = FakeMongoClient({"agents_config": FakeMongoCollection(docs)})
    monkeypatch.setattr(mongo_base.MongoClientFactory, "get_client", classmethod(lambda cls, _conn: client))
    repo = MongoAgentConfigRepository(connection_string=CONN, logger=logger or RecordingLogger())
    return await repo.get_active_agents()


async def _teams(monkeypatch: pytest.MonkeyPatch, docs: list[dict[str, Any]], logger: RecordingLogger | None = None):
    client = FakeMongoClient({"teams_config": FakeMongoCollection(docs)})
    monkeypatch.setattr(mongo_base.MongoClientFactory, "get_client", classmethod(lambda cls, _conn: client))
    repo = MongoTeamConfigRepository(connection_string=CONN, logger=logger or RecordingLogger())
    return await repo.get_active_teams()


def _agent_doc(**extra: Any) -> dict[str, Any]:
    return {"id": "a", "nome": "A", "model": "m1", "descricao": "d", "prompt": "p", "active": True, **extra}


def _team_doc(**extra: Any) -> dict[str, Any]:
    return {"id": "t", "nome": "T", "model": "m2", "member_ids": ["a"], "active": True, **extra}


# ── (1) equivalência com o mapper anterior ──────────────────────────────────────────────────────


def _legacy_provider(doc: dict[str, Any]) -> str:
    """Oráculo: regra do mapper antes do F2-01 (snake vale sobre camel; default ollama)."""
    return doc.get("factory_ia_model", doc.get("factoryIaModel", "ollama"))


AGENT_PROVIDER_FIELDS = [
    {},
    {"factory_ia_model": "openai"},
    {"factoryIaModel": "anthropic"},
    {"factory_ia_model": "groq", "factoryIaModel": "gemini"},
]
RAG_DOCS: list[dict[str, Any] | None] = [
    None,
    {},
    {"active": False},
    {"active": True},
    {"active": True, "model": "bge-m3"},
    {"active": True, "factory_ia_model": "gemini"},
    {"active": True, "factoryIaModel": "gemini", "model": "gemini-embedding-001"},
    {"active": True, "factory_ia_model": "openai", "factoryIaModel": "gemini", "model": "e"},
    {"active": True, "model": None},
    {"active": True, "model": ""},
    {"active": True, "factory_ia_model": None, "model": "x"},
    {"active": True, "search_strategy": "hierarchical", "doc_name": "manual.md"},
    {"active": False, "model": "x", "factory_ia_model": "y", "doc_name": None},
]


@pytest.mark.parametrize("provider_fields", AGENT_PROVIDER_FIELDS)
@pytest.mark.parametrize("rag", RAG_DOCS, ids=lambda r: repr(r)[:60])
async def test_agente_legado_nos_mappers_reais_equivale_ao_oraculo(
    monkeypatch: pytest.MonkeyPatch, provider_fields: dict[str, Any], rag: dict[str, Any] | None
):
    doc = _agent_doc(**provider_fields)
    if rag is not None:
        doc["rag_config"] = rag

    [config] = await _agents(monkeypatch, [doc])

    # campos legados do documento, exatamente como antes
    assert (config.factory_ia_model, config.model) == (_legacy_provider(doc), "m1")
    assert config.model_config == ModelConfig(provider=_legacy_provider(doc), model_id="m1")
    assert (config.model_params, config.base_url, config.api_key_ref) == (None, None, None)
    if not rag:
        assert config.rag_config is None
        return
    legacy_model = rag.get("model", "nomic-embed-text:latest")
    legacy_provider = rag.get("factory_ia_model", rag.get("factoryIaModel", "ollama"))
    assert (config.rag_config.model, config.rag_config.factory_ia_model) == (legacy_model, legacy_provider)
    # o que o indexador/hierárquico usava: ``factory or "ollama"`` / ``model or "nomic..."``
    embedder = config.rag_config.embedder_model_config()
    assert (embedder.provider, embedder.model_id) == (
        legacy_provider or "ollama",
        legacy_model or "nomic-embed-text:latest",
    )
    # o RAG semântico só era criado com os dois textos não vazios
    if legacy_provider and legacy_model:
        assert config.rag_config.model_config == ModelConfig(provider=legacy_provider, model_id=legacy_model)
    else:
        assert config.rag_config.model_config is None


@pytest.mark.parametrize("provider_fields", AGENT_PROVIDER_FIELDS)
@pytest.mark.parametrize("rag", [r for r in RAG_DOCS if r], ids=lambda r: repr(r)[:60])
async def test_campos_novos_nao_alteram_o_que_o_documento_antigo_gerava(
    monkeypatch: pytest.MonkeyPatch, provider_fields: dict[str, Any], rag: dict[str, Any]
):
    """Mesmo documento com os três campos novos válidos: provider/model_id/rag idênticos."""
    legacy_doc = _agent_doc(**provider_fields, rag_config=dict(rag))
    [old] = await _agents(monkeypatch, [legacy_doc])
    new_rag = dict(rag)
    explicit_model = rag.get("model") and (rag.get("factory_ia_model") or rag.get("factoryIaModel"))
    if explicit_model:  # campo novo no RAG exige model e provider explícitos (sem defaults)
        new_rag |= {
            "model_params": {"dimensions": 8},
            "base_url": "http://emb:8080/v1",
            "api_key_ref": "env:EMB_API_KEY",
        }
    new_doc = (
        legacy_doc
        | {
            "model_params": {"temperature": 0.1},
            "base_url": "https://gw.example.invalid/v1",
            "api_key_ref": "env:K_API_KEY",
        }
        | {"rag_config": new_rag}
    )

    [new] = await _agents(monkeypatch, [new_doc])

    assert (new.factory_ia_model, new.model) == (old.factory_ia_model, old.model)
    assert (new.model_config.provider, new.model_config.model_id) == (
        old.model_config.provider,
        old.model_config.model_id,
    )
    assert (new.rag_config.model, new.rag_config.factory_ia_model) == (
        old.rag_config.model,
        old.rag_config.factory_ia_model,
    )
    old_emb, new_emb = old.rag_config.embedder_model_config(), new.rag_config.embedder_model_config()
    assert (new_emb.provider, new_emb.model_id) == (old_emb.provider, old_emb.model_id)


@pytest.mark.parametrize(
    "provider_fields",
    [
        {},
        {"factory_ia_model": "openai"},
        {"factoryIaModel": "gemini"},
        {"factory_ia_model": "a", "factoryIaModel": "b"},
    ],
)
@pytest.mark.parametrize(
    "new_fields", [{}, {"base_url": "http://h:1/v1", "api_key_ref": "env:K_API_KEY", "model_params": {"a": 1}}]
)
async def test_team_legado_equivale_ao_oraculo(
    monkeypatch: pytest.MonkeyPatch, provider_fields: dict[str, Any], new_fields: dict[str, Any]
):
    doc = _team_doc(**provider_fields, **new_fields)

    [team] = await _teams(monkeypatch, [doc])

    assert (team.factory_ia_model, team.model) == (_legacy_provider(doc), "m2")
    assert (team.model_config.provider, team.model_config.model_id) == (_legacy_provider(doc), "m2")
    assert team.base_url == new_fields.get("base_url")


@pytest.mark.parametrize(
    "camel_fields",
    [{"modelParams": {"a": 1}}, {"baseUrl": "http://h"}, {"apiKeyRef": "env:K"}],
    ids=["modelParams", "baseUrl", "apiKeyRef"],
)
async def test_campos_novos_em_camelcase_sao_ignorados_hoje(
    monkeypatch: pytest.MonkeyPatch, camel_fields: dict[str, Any]
):
    """Caracterização: só snake_case vale (decisão do roadmap). A grafia camel é ignorada em silêncio
    (agente, rag_config e team): quem escrever ``apiKeyRef`` sobe sem chave. F2-02 deve avisar."""
    [agent] = await _agents(monkeypatch, [_agent_doc(**camel_fields)])
    [team] = await _teams(monkeypatch, [_team_doc(**camel_fields)])

    for config in (agent, team):
        assert config.model_config == ModelConfig(provider="ollama", model_id=config.model)


def test_entidades_continuam_copiaveis_e_replace_revalida():
    agent = AgentConfig(
        id="a", nome="A", factory_ia_model="openai", model="m", descricao="d", prompt="p",
        model_params={"t": 1}, base_url="http://h", api_key_ref="env:K_API_KEY",
        rag_config=RagConfig(active=True, model="e", factory_ia_model="ollama", model_params={"d": 2}),
    )  # fmt: skip

    clone = copy.deepcopy(agent)
    swapped = dataclasses.replace(agent, base_url="https://outro.example.invalid")

    assert clone == agent and clone.model_config == agent.model_config
    assert swapped.model_config.base_url == "https://outro.example.invalid"
    with pytest.raises(ValueError, match="base_url"):
        dataclasses.replace(agent, base_url="ftp://x")
    with pytest.raises(ValueError, match="api_key_ref"):
        dataclasses.replace(agent, api_key_ref=MARKER)
    # asdict de entidade de config (usado por logs/serialização) segue funcionando
    assert dataclasses.asdict(agent)["model_params"] == {"t": 1}


# ── (2) campos novos hostis isolam o documento sem vazar ────────────────────────────────────────

HOSTILE_PARAMS: list[Any] = [
    {"a": {"b": 1}},
    {"a": [1, 2]},
    {"a": None},
    {"a": {"$ne": MARKER}},
    {"a": {"nested": {"deep": {"deeper": MARKER}}}},
    {"a": b"bytes"},
    {"": 1},
    [MARKER],
    MARKER,
    123,
    0,
    False,
    True,
    "",
    [],
]


@pytest.mark.parametrize("params", HOSTILE_PARAMS, ids=lambda p: repr(p)[:40])
async def test_model_params_hostil_isola_agente_team_e_rag(monkeypatch: pytest.MonkeyPatch, params: Any):
    logger = RecordingLogger()
    agents = await _agents(
        monkeypatch,
        [
            _agent_doc(id="ok-1"),
            _agent_doc(id="ruim", model_params=params),
            _agent_doc(id="ruim-rag", rag_config={"active": True, "model": "e", "model_params": params}),
            _agent_doc(id="ok-2"),
        ],
        logger,
    )
    teams = await _teams(monkeypatch, [_team_doc(id="t-ok"), _team_doc(id="t-ruim", model_params=params)], logger)

    assert [a.id for a in agents] == ["ok-1", "ok-2"]
    assert [t.id for t in teams] == ["t-ok"]
    failed = [r.context.get("agent_id") or r.context.get("team_id") for r in logger.records if r.level == "error"]
    assert sorted(failed) == ["ruim", "ruim-rag", "t-ruim"]
    assert all(r.context.get("error_type") == "ValueError" for r in logger.records if r.level == "error")
    assert MARKER not in repr(logger.records)


@pytest.mark.parametrize(
    "url", [0, False, True, "", " ", ["http://h"], {"u": "http://h"}, b"http://h", MARKER, f"//{MARKER}"]
)
async def test_base_url_de_tipo_ou_forma_hostil_isola_o_documento(monkeypatch: pytest.MonkeyPatch, url: Any):
    logger = RecordingLogger()

    agents = await _agents(monkeypatch, [_agent_doc(id="ok"), _agent_doc(id="ruim", base_url=url)], logger)
    teams = await _teams(monkeypatch, [_team_doc(id="ruim", base_url=url)], logger)

    assert [a.id for a in agents] == ["ok"] and teams == []
    assert MARKER not in repr(logger.records)


@pytest.mark.parametrize(
    "ref", [0, False, True, "", " ", ["env:K"], {"k": "env:K"}, b"env:K", MARKER, f"env:{MARKER} ", f"env:{MARKER}\n"]
)
async def test_api_key_ref_de_tipo_ou_forma_hostil_isola_o_documento(monkeypatch: pytest.MonkeyPatch, ref: Any):
    logger = RecordingLogger()

    agents = await _agents(monkeypatch, [_agent_doc(id="ok"), _agent_doc(id="ruim", api_key_ref=ref)], logger)
    teams = await _teams(monkeypatch, [_team_doc(id="ruim", api_key_ref=ref)], logger)

    assert [a.id for a in agents] == ["ok"] and teams == []
    assert MARKER not in repr(logger.records)


async def test_rag_inativo_com_campo_novo_invalido_tambem_isola(monkeypatch: pytest.MonkeyPatch):
    """Caracterização: o campo novo é validado mesmo com ``active: false`` (documento inválido é inválido)."""
    agents = await _agents(
        monkeypatch,
        [_agent_doc(id="ruim", rag_config={"active": False, "base_url": "ftp://x"}), _agent_doc(id="ok")],
    )

    assert [a.id for a in agents] == ["ok"]


async def test_rag_com_campo_novo_e_modelo_explicitamente_nulo_isola(monkeypatch: pytest.MonkeyPatch):
    agents = await _agents(
        monkeypatch,
        [
            _agent_doc(id="a", rag_config={"active": True, "model": None, "api_key_ref": "env:K"}),
            _agent_doc(id="b", rag_config={"active": True, "factory_ia_model": None, "base_url": "http://h"}),
            _agent_doc(id="c", rag_config={"active": True, "model": "", "model_params": {}}),
            _agent_doc(id="ok"),
        ],
    )

    assert [a.id for a in agents] == ["ok"]


@pytest.mark.parametrize("params", [{"temperature": 0.2}, {}])
def test_model_params_nao_vaza_mutacao_do_documento_para_o_model_config(params: dict[str, Any]):
    original = dict(params)
    agent = AgentConfig(
        id="a", nome="A", factory_ia_model="o", model="m", descricao="", prompt="p", model_params=params
    )
    cfg = agent.model_config

    params["injetado"] = 1  # mutação posterior do dict de origem não altera o ModelConfig já criado

    assert dict(cfg.params) == original


# ── (3) parâmetros escalares extremos (caracterização) ──────────────────────────────────────────


def test_params_com_valores_numericos_extremos_sao_aceitos_hoje():
    """Caracterização: NaN/inf/inteiros gigantes passam (são escalares). Não são JSON válido
    (``allow_nan``) e um provider pode rejeitar tarde; a allowlist por provider da F2-02 decide."""
    cfg = ModelConfig(
        provider="p", model_id="m", params={"nan": math.nan, "inf": math.inf, "big": 10**400, "neg": -(10**30)}
    )

    assert math.isnan(cfg.params["nan"]) and cfg.params["inf"] == math.inf and cfg.params["big"] == 10**400


def test_params_bool_nao_vira_int_e_ordem_se_mantem():
    cfg = ModelConfig(provider="p", model_id="m", params={"z": True, "a": 1, "m": "x"})

    assert list(cfg.params) == ["z", "a", "m"] and cfg.params["z"] is True


def test_params_e_somente_leitura_mesmo_com_referencia_vazada():
    cfg = ModelConfig(provider="p", model_id="m", params={"a": 1})

    with pytest.raises(TypeError):
        cfg.params["a"] = 2  # type: ignore[index]
    with pytest.raises(TypeError):
        del cfg.params["a"]  # type: ignore[attr-defined]
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.params = {}  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.api_key_ref = "env:X"  # type: ignore[misc]


# ── (4) caracterização de base_url ──────────────────────────────────────────────────────────────

REJECTED_URLS = [
    "http://user:pass@h",
    "http://:pass@h",
    "http://user@h",
    "http://a@b@c",
    "http://h\\@evil",
    "http://h:80@h2",
    "http://[::1]@evil",
    "javascript:alert(1)",
    "JAVASCRIPT:alert(1)",
    "file:///etc/passwd",
    "ftp://h",
    "gopher://h",
    "data:text/plain,x",
    "ws://h",
    "//h/x",
    "http:/h",
    "http:h",
    "http:///h",
    "http://",
    "https://",
    "h:80",
    "",
    " http://h",
    "http://h ",
    "http://h/a b",
    "http://h\t",
    "http://h\n",
    "http://h\r\nHost: evil",
    "http://h\x00",
    "http://h\x7f",
    "http://h\u00a0",
    "http://h\u2028",
    "http://h​.com",
    "http://h﻿",
    "https://‮host",
    "http://h:99999",
    "http://h:65536",
    "http://h:-1",
    "http://h:80:90",
    "http://[::1",
    "http://::1/",
    "http://h/?",
    "http://h?x=1",
    "http://h/#",
    "http://h/#frag",
    "http://h/?api_key=x",
    # endurecido na rodada 2 do F2-01 (antes em ACCEPTED_BUT_RISKY)
    "http://[fe80::1%25eth0]/",
    "http://h\\evil",
    "http://%68ost",
    "http://\uff48.com",
    "http://bücher.example/",
    "http://h:0",
    "http://h:",
]


@pytest.mark.parametrize("url", REJECTED_URLS)
def test_base_url_hostil_e_recusada_sem_ecoar(url: str):
    with pytest.raises(ValueError, match="base_url") as exc_info:
        ModelConfig(provider="p", model_id="m", base_url=url)

    assert exc_info.value.__cause__ is None
    assert url == "" or url not in str(exc_info.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://[::1]:80",
        "http://[::ffff:127.0.0.1]/",
        "http://[2001:db8::1]:8443/v1",
        "http://xn--bcher-kva.example/",
        "HTTP://H",
        "HtTpS://h/v1",
        "https://api.example.invalid:443/",
        "http://h:65535",
        "http://h/v1/",
        "http://h/a/b/c",
    ],
)
def test_base_url_legitima_e_aceita(url: str):
    assert ModelConfig(provider="p", model_id="m", base_url=url).base_url == url


# Aceitas hoje que parecem perigosas para quem vai USAR a URL (F2-02 cria o cliente, F4 expõe
# tools/MCP). Caracterização: se a validação endurecer, estes casos migram para REJECTED_URLS.
ACCEPTED_BUT_RISKY = {
    "http://169.254.169.254/latest/meta-data": "metadados de nuvem (SSRF); sem política de destino",
    "http://localhost": "loopback (legítimo para ollama, mas sem distinção por ambiente)",
    "http://0.0.0.0": "0.0.0.0 conecta no host local em Linux",
    "http://127.1": "loopback em forma abreviada (bypass de denylist textual)",
    "http://2130706433": "IP decimal = 127.0.0.1",
    "http://0x7f.1": "IP em hexa/abreviado",
    "http://[::ffff:169.254.169.254]/": "IPv4-mapped para metadados",
    "http://.": "host '.'",
    "http://h.com.": "FQDN com ponto final (bypass de allowlist textual)",
    "http://h/../../x": "traversal no caminho",
    "http://h/%00": "NUL percent-encoded no caminho",
    "http://h;p=1": "parâmetro de caminho no netloc",
}


@pytest.mark.parametrize(("url", "why"), list(ACCEPTED_BUT_RISKY.items()), ids=list(ACCEPTED_BUT_RISKY))
def test_base_url_aceita_mas_arriscada_documentada(url: str, why: str):
    """Registro (não é endosso): ``ModelConfig`` valida forma, não destino. Ver ``why``."""
    assert ModelConfig(provider="p", model_id="m", base_url=url).base_url == url, why


def test_base_url_tem_limite_de_tamanho():
    """URL acima de 2048 caracteres é recusada (documento Mongo chega a 16 MB)."""
    at_limit = "http://h/" + "a" * (2048 - len("http://h/"))

    assert ModelConfig(provider="p", model_id="m", base_url=at_limit).base_url == at_limit
    with pytest.raises(ValueError, match="base_url maior que 2048"):
        ModelConfig(provider="p", model_id="m", base_url=at_limit + "a")


# ── (5) value object: cópia e serialização ─────────────────────────────────────────────────────


def test_model_config_copy_replace_e_repr_estaveis():
    cfg = ModelConfig(provider="p", model_id="m", params={"a": 1}, base_url="http://h", api_key_ref="env:K_API_KEY")

    assert copy.copy(cfg) == cfg
    assert dataclasses.replace(cfg, model_id="z").params == cfg.params
    assert "K_API_KEY" not in repr(cfg) and "'a'" not in repr(cfg)
    assert str(cfg) == repr(cfg)


def test_model_config_suporta_deepcopy():
    cfg = ModelConfig(provider="p", model_id="m", params={"a": 1})

    assert copy.deepcopy(cfg) == cfg


def test_model_config_suporta_asdict():
    cfg = ModelConfig(provider="p", model_id="m", params={"a": 1})

    assert dataclasses.asdict(cfg)["provider"] == "p"


def test_search_strategy_hierarquica_nao_exige_modelo_do_rag():
    rag = RagConfig(active=True, search_strategy=SearchStrategy.HIERARCHICAL)

    assert rag.model_config is None
    assert rag.embedder_model_config() == ModelConfig(provider="ollama", model_id="nomic-embed-text:latest")


def test_team_config_valida_na_construcao_nao_so_no_acesso():
    with pytest.raises(ValueError, match="model_params"):
        TeamConfig(id="t", nome="T", factory_ia_model="o", model="m", member_ids=["a"], model_params={"a": {}})  # type: ignore[arg-type]
