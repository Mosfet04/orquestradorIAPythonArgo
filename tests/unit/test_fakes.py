"""Comportamento dos fakes de ``tests/fakes`` usados no lugar de LLM, embedder e logger reais."""

from __future__ import annotations

import math

import pytest
from agno.agent import Agent
from agno.run.base import RunStatus

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


def test_fake_chat_model_aceita_prompt_posicional_como_o_llm_summary_generator():
    model = FakeChatModel(responses=["resumo"])

    response = model.invoke("Resuma isto")

    assert response.content == "resumo"
    assert model.calls[0].last_user_message == "Resuma isto"


def test_fake_chat_model_falha_alto_quando_o_roteiro_acaba():
    model = FakeChatModel(responses=["única"])
    model.invoke("1")
    assert model.exhausted is False

    with pytest.raises(ScriptExhaustedError, match="roteiro"):
        model.invoke("2")
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

    model = factory.create_model("openai", "gpt-x", temperature=0.2)

    assert isinstance(model, FakeChatModel)
    assert (model.id, model.provider) == ("gpt-x", "openai")
    assert factory.validate_model_config("openai", "gpt-x")["valid"] is True
    assert factory.created == [("openai", "gpt-x", {"temperature": 0.2})]


def test_fake_model_factory_pode_simular_config_invalida():
    factory = FakeModelFactory(invalid_models={"quebrado"})

    result = factory.validate_model_config("openai", "quebrado")

    assert result["valid"] is False
    assert result["errors"]


def test_fake_embedder_factory_cria_embedder_identificado():
    factory = FakeEmbedderFactory(dimensions=16)

    embedder = factory.create_model("ollama", "nomic", base_url="http://ollama.invalid")

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
