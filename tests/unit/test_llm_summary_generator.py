"""``LLMSummaryGenerator`` chama o modelo pela API pública do agno 2.5.8 (F1-07, B8)."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from agno.models.response import ModelResponse

from src.domain.entities.model_config import ModelConfig
from src.domain.ports.summary_generator_port import SummaryTimeoutError
from src.infrastructure.services.llm_summary_generator import LLMSummaryGenerator
from tests.fakes import FakeChatModel, FakeModelFactory, RecordingLogger
from tests.fakes.models import factory_call

TEXTO = "O orquestrador monta agentes a partir do MongoDB. " * 10


def _generator(factory: FakeModelFactory, logger: RecordingLogger) -> LLMSummaryGenerator:
    return LLMSummaryGenerator(
        model_factory=factory,
        factory_ia_model="openai",
        model_id="gpt-resumo",
        logger=logger,
    )


async def test_resumo_vem_do_modelo_e_nao_do_fallback_de_truncamento():
    factory = FakeModelFactory(responses=["  Resumo gerado pelo modelo.  "])
    logger = RecordingLogger()

    summary = await _generator(factory, logger).generate_summary(TEXTO)

    assert summary == "Resumo gerado pelo modelo."
    assert summary != TEXTO[:200]
    assert logger.messages("warning", "error") == []
    assert [(provider, model_id) for provider, model_id, _ in factory.created] == [("openai", "gpt-resumo")]


async def test_prompt_enviado_ao_modelo_tem_o_texto_como_mensagem_de_usuario():
    factory = FakeModelFactory(responses=["ok"])
    generator = _generator(factory, RecordingLogger())

    await generator.generate_summary("Texto curto sobre RAG.")

    [model] = factory.models
    assert len(model.calls) == 1
    prompt = model.calls[0].last_user_message
    assert prompt is not None and prompt.startswith("Resuma o texto a seguir")
    assert prompt.endswith("Texto curto sobre RAG.")


async def test_modelo_e_criado_uma_vez_e_reaproveitado():
    factory = FakeModelFactory(responses=["um", "dois"])
    generator = _generator(factory, RecordingLogger())

    assert await generator.generate_summary("primeiro") == "um"
    assert await generator.generate_summary("segundo") == "dois"
    assert len(factory.created) == 1


async def test_erro_real_do_modelo_cai_no_fallback_com_log():
    factory = FakeModelFactory(responses=[])  # roteiro vazio: a chamada levanta
    logger = RecordingLogger()

    summary = await _generator(factory, logger).generate_summary(TEXTO)

    assert summary == TEXTO[:200]
    warnings = [r for r in logger.records if r.level == "warning"]
    assert [r.message for r in warnings] == ["Fallback de sumário: falha ao chamar o modelo"]
    assert warnings[0].context == {
        "factory_ia_model": "openai",
        "model_id": "gpt-resumo",
        "error_type": "ScriptExhaustedError",
    }


async def test_resposta_vazia_do_modelo_cai_no_fallback_com_log():
    factory = FakeModelFactory(responses=["   "])
    logger = RecordingLogger()

    summary = await _generator(factory, logger).generate_summary(TEXTO)

    assert summary == TEXTO[:200]
    assert logger.messages("warning") == ["Fallback de sumário: modelo devolveu resposta vazia"]


async def test_conteudo_vazio_nao_chama_o_modelo():
    factory = FakeModelFactory(responses=[])

    assert await _generator(factory, RecordingLogger()).generate_summary("   ") == ""
    assert factory.created == []


class _SecretLeakingModel(FakeChatModel):
    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        raise RuntimeError("401 do provedor: api_key=sk-teste-nao-logar")


class _HangingModel(FakeChatModel):
    """Provedor que nunca responde (ex.: Ollama sem timeout, servidor travado)."""

    cancelled: bool = False

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("inalcançável")


class _SingleModelFactory(FakeModelFactory):
    def __init__(self, model: FakeChatModel) -> None:
        super().__init__()
        self._model = model

    def create_model(self, config: ModelConfig) -> FakeChatModel:
        self.created.append(factory_call(config))
        self.models.append(self._model)
        return self._model


async def test_fallback_loga_so_o_tipo_do_erro_e_nunca_a_mensagem():
    """A mensagem de exceção de SDK pode carregar segredo (chave, URL com credencial)."""
    logger = RecordingLogger()
    generator = _generator(_SingleModelFactory(_SecretLeakingModel()), logger)

    summary = await generator.generate_summary(TEXTO)

    assert summary == TEXTO[:200]
    [warning] = [r for r in logger.records if r.level == "warning"]
    assert warning.context["error_type"] == "RuntimeError"
    assert "error" not in warning.context
    assert "sk-teste-nao-logar" not in repr(logger.records)


async def test_modelo_que_nao_responde_estoura_o_timeout_e_sinaliza_ao_chamador():
    """N5: o timeout vira ``SummaryTimeoutError`` (o indexador cai no truncamento e para de
    chamar o modelo para o resto do documento), com log só do tipo e do prazo."""
    logger = RecordingLogger()
    model = _HangingModel()
    generator = LLMSummaryGenerator(
        model_factory=_SingleModelFactory(model),
        factory_ia_model="ollama",
        model_id="lento",
        logger=logger,
        timeout_seconds=0.05,
    )

    with pytest.raises(SummaryTimeoutError):
        await asyncio.wait_for(generator.generate_summary(TEXTO), timeout=5)

    assert model.cancelled is True
    [warning] = [r for r in logger.records if r.level == "warning"]
    assert warning.message == "Sumário: modelo não respondeu no prazo"
    assert warning.context["error_type"] == "TimeoutError"
    assert warning.context["timeout_s"] == 0.05


def test_timeout_padrao_do_construtor_e_finito():
    default = inspect.signature(LLMSummaryGenerator).parameters["timeout_seconds"].default

    assert 0 < default <= 120


async def test_modelo_e_criado_fora_do_event_loop_e_uma_vez_com_sumarios_concorrentes():
    """F2-02: a criação pode ler segredo (``file:``) e resolver DNS; os sumários rodam em gather."""
    factory = FakeModelFactory(responses=["a", "b", "c", "d", "e"])
    generator = _generator(factory, RecordingLogger())

    summaries = await asyncio.gather(*(generator.generate_summary(f"texto {i}") for i in range(5)))

    assert sorted(summaries) == ["a", "b", "c", "d", "e"]
    assert factory.created == [("openai", "gpt-resumo", {})]
    assert factory.on_event_loop == [False]
