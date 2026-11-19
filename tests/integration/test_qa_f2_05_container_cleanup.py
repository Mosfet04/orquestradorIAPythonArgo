"""QA do F2-05 / BUG-F2-03-QA-2: ``DependencyContainer.create_async`` fecha o que abriu quando a
inicialização falha, seja qual for a falha (exceção, cancelamento, ``close`` que também falha)."""

from __future__ import annotations

import asyncio

import pytest

from src.infrastructure import dependency_injection as di
from src.infrastructure.config.app_config import AppConfig
from tests.fakes.startup_world import CLEAN_ENV, Events, startup_world


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CLEAN_ENV:
        monkeypatch.delenv(name, raising=False)


async def test_falha_no_wiring_fecha_o_cliente_do_app_uma_vez_e_re_levanta_a_original() -> None:
    events = Events()
    with startup_world(events, fail_at="container") as clients:
        with pytest.raises(RuntimeError, match="falha injetada: container"):
            await di.DependencyContainer.create_async(AppConfig.load())

    assert [m.closes for m in clients] == [[True]]


async def test_cancelamento_durante_a_inicializacao_fecha_o_cliente_e_propaga_o_cancelamento(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cancelled(**_: object) -> None:
        raise asyncio.CancelledError

    events = Events()
    with startup_world(events) as clients:
        monkeypatch.setattr(di, "MongoToolRepository", cancelled)
        with pytest.raises(asyncio.CancelledError):
            await di.DependencyContainer.create_async(AppConfig.load())

    assert [m.closes for m in clients] == [[True]]


async def test_close_que_tambem_falha_nao_mascara_a_falha_original(monkeypatch: pytest.MonkeyPatch) -> None:
    events = Events()
    with startup_world(events, fail_at="container") as clients:
        original_init = di.DependencyContainer._initialize

        async def init_then_break_close(self: di.DependencyContainer) -> None:
            try:
                await original_init(self)
            finally:
                client = self._mongo_client
                assert client is not None

                def failing_close() -> None:
                    raise OSError("close quebrado")

                client.close = failing_close  # type: ignore[method-assign]

        monkeypatch.setattr(di.DependencyContainer, "_initialize", init_then_break_close)
        with pytest.raises(RuntimeError, match="falha injetada: container"):
            await di.DependencyContainer.create_async(AppConfig.load())

    assert len(clients) == 1


async def test_falha_antes_de_abrir_o_cliente_nao_quebra_o_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    events = Events()
    with startup_world(events) as clients:

        def broken_registry(*_: object, **__: object) -> None:
            raise RuntimeError("registry quebrado")

        monkeypatch.setattr(di, "build_provider_registry", broken_registry)
        with pytest.raises(RuntimeError, match="registry quebrado"):
            await di.DependencyContainer.create_async(AppConfig.load())

    assert [m.closes for m in clients] == [[True] for _ in clients]  # o que chegou a abrir foi fechado


async def test_container_inicializado_com_sucesso_nao_e_fechado_pelo_create_async() -> None:
    events = Events()
    with startup_world(events) as clients:
        container = await di.DependencyContainer.create_async(AppConfig.load())
        assert [m.closes for m in clients] == [[]]
        await container.cleanup()
        assert [m.closes for m in clients] == [[True]]


def test_get_agent_runtime_sem_inicializar_o_container_recusa() -> None:
    container = di.DependencyContainer(AppConfig.load())

    with pytest.raises(RuntimeError, match="não inicializado"):
        container.get_agent_runtime()


async def test_get_agent_runtime_devolve_sempre_o_mesmo_runtime_que_monta_as_entidades() -> None:
    events = Events()
    with startup_world(events):
        container = await di.DependencyContainer.create_async(AppConfig.load())
        try:
            assert container.get_agent_runtime() is container.get_agent_runtime()
        finally:
            await container.cleanup()
