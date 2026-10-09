"""QA F2-02: bordas do ``ProviderRegistry`` que os testes do dev não cobrem.

- toda chave da allowlist de ``model_params`` de todo built-in chega ao atributo da classe real
  do agno (um nome de kwarg errado só apareceria em runtime como "falha ao instanciar");
- concorrência: o registry é chamado de várias threads (gather de agentes) sem estado cruzado nem
  mutação das specs compartilhadas;
- chave lida na hora da criação (rotação), sem tocar ``os.environ`` nem o ``ModelConfig``;
- ``api_key_ref`` fora de ``SECRETS_DIR`` recusado sem ecoar conteúdo;
- ``LLMSummaryGenerator``: criação recusada cai no fallback sem vazar, não é cacheada e um
  cancelamento durante a criação não trava as próximas chamadas.
"""

from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from src.domain.entities.model_config import ModelConfig
from src.domain.ports import InvalidModelConfigError
from src.infrastructure.providers import ClassSpec, DestinationPolicy, ProviderRegistry, ProviderSpec
from src.infrastructure.providers.builtins import BUILTIN_PROVIDERS
from src.infrastructure.services.llm_summary_generator import LLMSummaryGenerator
from tests.fakes import FakeChatModel, FakeModelFactory, RecordingLogger
from tests.fakes.providers import CHAT_PATH, EMBEDDER_PATH
from tests.unit.test_provider_matrix import optional_sdks_stubbed  # noqa: F401  (fixture compartilhada)

SECRET = "valor-secreto-QA-F202-nao-pode-vazar"  # noqa: S105 - marcador de vazamento, não é segredo
GATEWAY = "gw.qa.invalid"


def _dns(host: str) -> list[str]:
    return ["10.20.30.40"]


def _builtin_registry() -> ProviderRegistry:
    return ProviderRegistry(BUILTIN_PROVIDERS, policy=DestinationPolicy(allowlist=(GATEWAY,), resolver=_dns))


@pytest.fixture
def provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI", "ANTHROPIC", "GEMINI", "GROQ", "AZURE"):
        monkeypatch.setenv(f"{name}_API_KEY", "chave-de-teste-sem-valor")
    monkeypatch.setenv("AZURE_ENDPOINT", "https://tenant.openai.azure.com")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


# ── allowlist de model_params x classes reais ───────────────────────────────────────────────────

_PARAM_VALUES: dict[str, Any] = {"keep_alive": "5m", "stop": "fim", "reasoning_effort": "low"}


def _param_cases() -> list[tuple[str, str, str, str]]:
    cases = []
    for spec in BUILTIN_PROVIDERS:
        for kind, class_spec in (("chat", spec.chat), ("embedder", spec.embedder)):
            if class_spec is not None:
                cases.extend((spec.id, kind, key, target) for key, target in class_spec.params.items())
    return cases


@pytest.mark.usefixtures("optional_sdks_stubbed", "provider_env")
@pytest.mark.parametrize(("provider", "kind", "key", "target"), _param_cases())
def test_toda_chave_da_allowlist_chega_ao_atributo_da_classe_real(provider: str, kind: str, key: str, target: str):
    value = _PARAM_VALUES.get(key, 3)
    base_url = f"https://{GATEWAY}/v1" if provider == "openai_compatible" else None
    config = ModelConfig(provider, "m", params={key: value}, base_url=base_url)
    registry = _builtin_registry()

    obj: Any = registry.create_model(config) if kind == "chat" else registry.create_embedder(config)

    if "." in target:
        container, leaf = target.split(".")
        assert getattr(obj, container)[leaf] == value
    else:
        assert getattr(obj, target) == value


@pytest.mark.usefixtures("optional_sdks_stubbed", "provider_env")
@pytest.mark.parametrize("provider", [spec.id for spec in BUILTIN_PROVIDERS])
@pytest.mark.parametrize("hostile", ["api_key", "base_url", "client_params", "default_headers", "http_client", "id"])
def test_kwargs_sensiveis_do_construtor_nunca_entram_por_model_params(provider: str, hostile: str):
    """``model_params`` não é uma porta dos fundos para kwargs do SDK (headers, cliente HTTP, chave)."""
    base_url = f"https://{GATEWAY}/v1" if provider == "openai_compatible" else None
    config = ModelConfig(provider, "m", params={hostile: "x"}, base_url=base_url)
    registry = _builtin_registry()

    with pytest.raises(InvalidModelConfigError, match="chave não permitida") as caught:
        registry.create_model(config)
    assert caught.value.__cause__ is None


# ── concorrência ────────────────────────────────────────────────────────────────────────────────


def test_criacao_concorrente_em_threads_nao_cruza_kwargs_nem_muta_a_spec():
    chat = ClassSpec(
        class_path=CHAT_PATH,
        params={"temperature": "temperature", "top_k": "options.top_k"},
        base_url_kwarg="base_url",
        untrusted_url_kwargs={"client_params.follow_redirects": False},
    )
    spec = ProviderSpec(
        id="acme", sdk_package="acme-sdk", chat=chat, embedder=ClassSpec(class_path=EMBEDDER_PATH),
        default_hosts=frozenset({"api.acme.example"}), api_key_env="ACME_API_KEY",
    )
    os.environ["ACME_API_KEY"] = "chave-do-ambiente"
    try:
        registry = ProviderRegistry([spec], policy=DestinationPolicy(resolver=_dns))
        barrier = threading.Barrier(16)

        def create(i: int) -> Any:
            if i < 16:
                barrier.wait(timeout=10)  # as primeiras 16 largam juntas
            config = ModelConfig(
                "acme", f"m{i}", params={"temperature": i / 1000, "top_k": i}, base_url=f"https://api.acme.example/v{i}"
            )
            return registry.create_model(config)

        with ThreadPoolExecutor(max_workers=16) as pool:
            models = list(pool.map(create, range(400)))
    finally:
        del os.environ["ACME_API_KEY"]

    for i, model in enumerate(models):
        assert model.kwargs["id"] == f"m{i}"
        assert model.kwargs["temperature"] == i / 1000
        assert model.kwargs["options"] == {"top_k": i}
        assert model.kwargs["base_url"] == f"https://api.acme.example/v{i}"
        assert model.kwargs["client_params"] == {"follow_redirects": False}
    nested = [model.kwargs["options"] for model in models] + [model.kwargs["client_params"] for model in models]
    assert len({id(item) for item in nested}) == len(nested)  # nenhum dict aninhado compartilhado
    assert chat.untrusted_url_kwargs == {"client_params.follow_redirects": False}
    assert dict(chat.params) == {"temperature": "temperature", "top_k": "options.top_k"}


@pytest.mark.usefixtures("provider_env")
def test_builtins_nao_sao_alterados_por_uso():
    snapshot = [
        (s.id, s.aliases, s.default_hosts, s.api_key_env, [(c.class_path, dict(c.params), dict(c.untrusted_url_kwargs))
                                                        for c in (s.chat, s.embedder) if c is not None])
        for s in BUILTIN_PROVIDERS
    ]
    registry = _builtin_registry()

    def use(i: int) -> None:
        registry.create_model(ModelConfig("ollama", "m", params={"num_ctx": i}, base_url=f"http://{GATEWAY}:11434"))
        registry.create_embedder(ModelConfig("ollama", "e", base_url=f"http://{GATEWAY}:11434"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(use, range(100)))

    assert snapshot == [
        (s.id, s.aliases, s.default_hosts, s.api_key_env, [(c.class_path, dict(c.params), dict(c.untrusted_url_kwargs))
                                                        for c in (s.chat, s.embedder) if c is not None])
        for s in BUILTIN_PROVIDERS
    ]


# ── estado: chave lida a cada criação; nada global é alterado ───────────────────────────────────


@pytest.mark.usefixtures("provider_env")
def test_chave_e_lida_a_cada_criacao_e_nao_fica_em_cache(monkeypatch: pytest.MonkeyPatch):
    registry = _builtin_registry()
    monkeypatch.setenv("OPENAI_API_KEY", "chave-antiga")
    first = registry.create_model(ModelConfig("openai", "m"))
    monkeypatch.setenv("OPENAI_API_KEY", "chave-nova")
    second = registry.create_model(ModelConfig("openai", "m"))
    monkeypatch.delenv("OPENAI_API_KEY")

    assert (first.api_key, second.api_key) == ("chave-antiga", "chave-nova")  # type: ignore[attr-defined]
    with pytest.raises(InvalidModelConfigError, match="OPENAI_API_KEY não configurado"):
        registry.create_model(ModelConfig("openai", "m"))


@pytest.mark.usefixtures("provider_env")
def test_criar_modelos_nao_altera_o_ambiente_nem_o_model_config():
    before = dict(os.environ)
    config = ModelConfig(
        "openai_compatible", "m", params={"temperature": 0.1}, base_url=f"https://{GATEWAY}/v1",
        api_key_ref="env:OPENAI_API_KEY",
    )
    snapshot = (config.provider, config.model_id, dict(config.params), config.base_url, config.api_key_ref)
    registry = _builtin_registry()

    registry.create_model(config)
    registry.create_embedder(
        ModelConfig(config.provider, "e", base_url=config.base_url, api_key_ref=config.api_key_ref)
    )

    assert dict(os.environ) == before
    assert snapshot == (config.provider, config.model_id, dict(config.params), config.base_url, config.api_key_ref)


@pytest.mark.usefixtures("provider_env")
@pytest.mark.parametrize("provider", ["OpenAI", " openai ", "OPENAI"])
def test_provider_do_builtin_sem_caixa_nem_espacos_como_no_legado(provider: str):
    assert _builtin_registry().create_model(ModelConfig(provider, "m")).id == "m"


def test_embedder_de_provider_so_de_chat_lista_os_suportados_de_embedder():
    expected = r"Suportados: azure, azureopenai, gemini, google, ollama"
    with pytest.raises(InvalidModelConfigError, match=expected) as caught:
        _builtin_registry().create_embedder(ModelConfig("anthropic", "m"))
    assert "openai_compatible" in str(caught.value) and "groq" not in str(caught.value)


# ── ollama: chave só com api_key_ref ────────────────────────────────────────────────────────────


def test_ollama_chat_com_api_key_ref_recebe_a_chave_e_o_embedder_recusa(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", SECRET)
    registry = _builtin_registry()
    config = ModelConfig("ollama", "m", api_key_ref="env:OLLAMA_CLOUD_API_KEY")

    model = registry.create_model(config)

    assert model.api_key == SECRET  # type: ignore[attr-defined]
    with pytest.raises(InvalidModelConfigError, match="não aceita api_key_ref") as caught:
        registry.create_embedder(config)
    assert SECRET not in str(caught.value)


# ── segredo por arquivo: confinamento via registry ──────────────────────────────────────────────


def test_api_key_ref_file_fora_do_secrets_dir_ou_por_symlink_e_recusado_sem_ecoar_conteudo(tmp_path: Path):
    secrets = tmp_path / "secrets"
    outside = tmp_path / "fora"
    secrets.mkdir()
    outside.mkdir()
    (outside / "chave").write_text(SECRET, encoding="utf-8")
    (secrets / "atalho").symlink_to(outside / "chave")
    registry = ProviderRegistry(
        BUILTIN_PROVIDERS, secrets_dir=str(secrets), policy=DestinationPolicy(allowlist=(GATEWAY,), resolver=_dns)
    )

    for ref in (f"file:{outside}/chave", f"file:{secrets}/atalho", f"file:{secrets}/../fora/chave"):
        with pytest.raises(InvalidModelConfigError) as caught:
            registry.create_model(
                ModelConfig("openai_compatible", "m", base_url=f"https://{GATEWAY}/v1", api_key_ref=ref)
            )
        assert SECRET not in str(caught.value) and caught.value.__cause__ is None


# ── OPENAI_BASE_URL do operador x base_url da config ────────────────────────────────────────────


@pytest.mark.usefixtures("provider_env")
def test_openai_base_url_do_ambiente_vale_sem_config_e_a_base_url_da_config_prevalece(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.operador.invalid/v1")
    registry = _builtin_registry()

    from_env = registry.create_model(ModelConfig("openai", "m")).get_client()  # type: ignore[attr-defined]
    from_config = registry.create_model(
        ModelConfig("openai_compatible", "m", base_url=f"https://{GATEWAY}/v1")
    ).get_client()  # type: ignore[attr-defined]

    assert str(from_env.base_url).startswith("https://proxy.operador.invalid/v1")
    assert str(from_config.base_url).startswith(f"https://{GATEWAY}/v1")


# ── LLMSummaryGenerator ─────────────────────────────────────────────────────────────────────────

TEXTO = "O orquestrador monta agentes a partir do MongoDB. " * 10


class _FlakyFactory(FakeModelFactory):
    """Recusa a config nas primeiras ``refusals`` chamadas (texto nosso) e depois entrega o modelo."""

    def __init__(self, refusals: int, reason: str) -> None:
        super().__init__(responses=["resumo do modelo"] * 10)
        self._refusals = refusals
        self._reason = reason

    def create_model(self, config: ModelConfig) -> FakeChatModel:
        if len(self.configs) < self._refusals:
            self.configs.append(config)
            raise InvalidModelConfigError(self._reason)
        return super().create_model(config)


def _generator(factory: FakeModelFactory, logger: RecordingLogger) -> LLMSummaryGenerator:
    return LLMSummaryGenerator(model_factory=factory, factory_ia_model="openai", model_id="gpt-resumo", logger=logger)


async def test_criacao_recusada_cai_no_fallback_sem_vazar_e_a_proxima_chamada_tenta_de_novo():
    logger = RecordingLogger()
    reason = f"modelo do provider 'openai': host {GATEWAY} fora da allowlist, chave {SECRET}"
    generator = _generator(_FlakyFactory(refusals=1, reason=reason), logger)

    first = await generator.generate_summary(TEXTO)
    second = await generator.generate_summary(TEXTO)

    assert first == TEXTO[:200]
    assert second == "resumo do modelo"
    [warning] = [r for r in logger.records if r.level == "warning"]
    assert warning.context["error_type"] == "InvalidModelConfigError"
    assert SECRET not in repr(logger.records) and GATEWAY not in repr(logger.records)


async def test_sumarios_concorrentes_com_criacao_recusada_nao_travam_nem_criam_duas_vezes_depois_do_sucesso():
    factory = _FlakyFactory(refusals=3, reason="recusado")
    generator = _generator(factory, RecordingLogger())

    results = await asyncio.wait_for(
        asyncio.gather(*(generator.generate_summary(f"{TEXTO} {i}") for i in range(8))), timeout=10
    )

    assert sum(1 for r in results if r == "resumo do modelo") >= 1
    assert sum(1 for r in results if r.startswith("O orquestrador")) == 3  # fallback das 3 recusadas
    assert len(factory.created) == 1  # depois do primeiro sucesso, o modelo é reaproveitado


async def test_cancelamento_durante_a_criacao_libera_o_lock_e_a_proxima_chamada_funciona():
    started = threading.Event()
    release = threading.Event()

    class _SlowFactory(FakeModelFactory):
        def create_model(self, config: ModelConfig) -> FakeChatModel:
            started.set()
            release.wait(timeout=10)
            return super().create_model(config)

    factory = _SlowFactory(responses=["ok"])
    generator = _generator(factory, RecordingLogger())

    task = asyncio.create_task(generator.generate_summary(TEXTO))
    await asyncio.to_thread(started.wait, 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    summary = await asyncio.wait_for(generator.generate_summary(TEXTO), timeout=10)

    assert summary == "ok"

