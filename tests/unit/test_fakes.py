"""Comportamento dos fakes de ``tests/fakes`` usados no lugar de LLM, embedder e logger reais."""

from __future__ import annotations

import asyncio
import math

import pytest
from agno.agent import Agent
from agno.models.message import Message
from agno.run.base import RunStatus

from src.domain.entities.model_config import ModelConfig
from src.domain.ports import InvalidModelConfigError
from tests.fakes import (
    FakeChatModel,
    FakeEmbedder,
    FakeEmbedderFactory,
    FakeModelFactory,
    RecordingLogger,
    ScriptExhaustedError,
)

# ── FakeChatModel ───────────────────────────────────────────────────


async def test_fake_chat_model_responde_o_roteiro_dentro_de_um_agent_agno():
    model = FakeChatModel(responses=["primeira", "segunda"])
    agent = Agent(model=model, instructions="seja breve", telemetry=False)

    first = await agent.arun("olá")
    second = await agent.arun("tudo bem?")

    assert first.content == "primeira"
    assert second.content == "segunda"
    assert len(model.calls) == 2
    assert model.calls[0].last_user_message == "olá"
    assert model.calls[1].last_user_message == "tudo bem?"


def test_fake_chat_model_responde_em_execucao_sincrona_e_streaming():
    model = FakeChatModel(responses=["sync", "stream"])
    agent = Agent(model=model, telemetry=False)

    assert agent.run("a").content == "sync"
    chunks = [ev.content for ev in agent.run("b", stream=True) if getattr(ev, "content", None)]

    assert "".join(chunks) == "stream"
    assert [c.last_user_message for c in model.calls] == ["a", "b"]


async def test_fake_chat_model_responde_pela_api_publica_aresponse_com_message():
    model = FakeChatModel(responses=["resumo"])

    response = await model.aresponse(messages=[Message(role="user", content="Resuma isto")])

    assert response.content == "resumo"
    assert model.calls[0].last_user_message == "Resuma isto"


def test_fake_chat_model_recusa_prompt_solto_como_o_provider_real_do_agno():
    """F1-07 (B8): ``invoke("texto")`` quebra no provider real; o fake não pode mascarar isso."""
    model = FakeChatModel(responses=["resumo"])

    with pytest.raises(TypeError, match="List\\[Message\\]"):
        model.invoke("Resuma isto")
    assert model.calls == []


def test_fake_chat_model_falha_alto_quando_o_roteiro_acaba():
    model = FakeChatModel(responses=["única"])
    model.invoke(messages=[Message(role="user", content="1")])
    assert model.exhausted is False

    with pytest.raises(ScriptExhaustedError, match="roteiro"):
        model.invoke(messages=[Message(role="user", content="2")])
    assert model.exhausted is True


def test_fake_chat_model_registra_chamada_sem_messages_e_segue_o_roteiro_em_ordem():
    model = FakeChatModel(responses=["a", "b"])

    first = model.invoke()
    second = model.invoke(tools=[])

    assert (first.content, second.content) == ("a", "b")
    assert [c.messages for c in model.calls] == [(), ()]
    with pytest.raises(ScriptExhaustedError):
        model.invoke()
    assert len(model.calls) == 3


def test_fake_chat_model_com_roteiro_esgotado_dentro_do_agent_vira_run_status_error():
    """O agno captura a exceção do modelo: o teste precisa olhar ``status``/``exhausted``."""
    model = FakeChatModel(responses=[])
    agent = Agent(model=model, telemetry=False)

    result = agent.run("olá")

    assert result.status == RunStatus.error
    assert model.exhausted is True


# ── FakeEmbedder ────────────────────────────────────────────────────


def test_fake_embedder_e_deterministico_e_normalizado():
    embedder = FakeEmbedder(dimensions=8)

    a1 = embedder.get_embedding("gato")
    a2 = FakeEmbedder(dimensions=8).get_embedding("gato")
    b = embedder.get_embedding("cachorro")

    assert a1 == a2
    assert a1 != b
    assert len(a1) == 8
    assert math.isclose(math.sqrt(sum(x * x for x in a1)), 1.0)
    assert embedder.calls == ["gato", "cachorro"]


async def test_fake_embedder_expoe_api_async_e_usage_do_agno():
    embedder = FakeEmbedder(dimensions=4)

    vec, usage = await embedder.async_get_embedding_and_usage("x")

    assert vec == embedder.get_embedding("x")
    assert usage is None


# ── factories ───────────────────────────────────────────────────────


def test_fake_model_factory_cria_modelo_com_id_e_provider_pedidos():
    factory = FakeModelFactory(responses=["oi"])

    model = factory.create_model(ModelConfig("openai", "gpt-x", params={"temperature": 0.2}))

    assert isinstance(model, FakeChatModel)
    assert (model.id, model.provider) == ("gpt-x", "openai")
    assert factory.created == [("openai", "gpt-x", {"model_params": {"temperature": 0.2}})]
    assert factory.on_event_loop == [False]


def test_fake_model_factory_config_legada_registra_sem_campos_novos():
    factory = FakeModelFactory()

    factory.create_model(ModelConfig("ollama", "llama3"))

    assert factory.created == [("ollama", "llama3", {})]


def test_fake_model_factory_pode_simular_config_invalida():
    factory = FakeModelFactory(invalid_models={"quebrado"})

    with pytest.raises(InvalidModelConfigError, match="'quebrado' marcado como inválido"):
        factory.create_model(ModelConfig("openai", "quebrado"))
    assert factory.models == []


async def test_fake_factories_registram_se_rodaram_no_event_loop():
    models, embedders = FakeModelFactory(), FakeEmbedderFactory()

    models.create_model(ModelConfig("openai", "gpt-x"))
    await asyncio.to_thread(embedders.create_embedder, ModelConfig("ollama", "nomic"))

    assert models.on_event_loop == [True]
    assert embedders.on_event_loop == [False]


def test_fake_embedder_factory_cria_embedder_identificado():
    factory = FakeEmbedderFactory(dimensions=16)

    embedder = factory.create_embedder(ModelConfig("ollama", "nomic", base_url="http://ollama.invalid"))

    assert isinstance(embedder, FakeEmbedder)
    assert (embedder.id, embedder.provider, embedder.dimensions) == ("nomic", "ollama", 16)
    assert factory.created == [("ollama", "nomic", {"base_url": "http://ollama.invalid"})]


# ── RecordingLogger ─────────────────────────────────────────────────


def test_recording_logger_registra_nivel_mensagem_e_contexto():
    logger = RecordingLogger()

    logger.info("a", x=1)
    logger.warning("b")
    logger.error("c", err="boom")
    logger.debug("d")

    assert [(r.level, r.message) for r in logger.records] == [
        ("info", "a"),
        ("warning", "b"),
        ("error", "c"),
        ("debug", "d"),
    ]
    assert logger.records[0].context == {"x": 1}
    assert logger.messages("warning", "error") == ["b", "c"]
