"""``RegisteredRunCancellationManager`` (F1-10): cancela só run registrado, sem estado para o resto."""

from __future__ import annotations

import pytest
from agno.exceptions import RunCancelledException
from agno.run import cancel
from agno.run.cancellation_management.base import BaseRunCancellationManager

from src.infrastructure.web.run_cancellation import (
    RegisteredRunCancellationManager,
    install_run_cancellation_manager,
)


def test_e_um_gerenciador_do_agno():
    assert isinstance(RegisteredRunCancellationManager(), BaseRunCancellationManager)


def test_cancel_de_run_nao_registrado_devolve_false_e_nao_guarda_nada():
    manager = RegisteredRunCancellationManager()

    assert manager.cancel_run("fantasma") is False
    assert manager.get_active_runs() == {}
    assert manager.is_cancelled("fantasma") is False


async def test_cancel_async_de_run_nao_registrado_devolve_false_e_nao_guarda_nada():
    manager = RegisteredRunCancellationManager()

    assert await manager.acancel_run("fantasma") is False
    assert await manager.aget_active_runs() == {}
    assert await manager.ais_cancelled("fantasma") is False


def test_run_registrado_e_cancelado_e_levanta_no_ponto_de_checagem():
    manager = RegisteredRunCancellationManager()
    manager.register_run("r1")

    assert manager.is_cancelled("r1") is False
    assert manager.cancel_run("r1") is True
    assert manager.is_cancelled("r1") is True
    with pytest.raises(RunCancelledException):
        manager.raise_if_cancelled("r1")


async def test_run_registrado_async_e_cancelado_e_levanta_no_ponto_de_checagem():
    manager = RegisteredRunCancellationManager()
    await manager.aregister_run("r1")

    assert await manager.acancel_run("r1") is True
    assert await manager.ais_cancelled("r1") is True
    with pytest.raises(RunCancelledException):
        await manager.araise_if_cancelled("r1")


def test_registrar_de_novo_preserva_o_cancelamento_de_run_ja_registrado():
    """O agno registra o run de background antes da task e de novo dentro dela (setdefault)."""
    manager = RegisteredRunCancellationManager()
    manager.register_run("bg")
    manager.cancel_run("bg")

    manager.register_run("bg")

    assert manager.is_cancelled("bg") is True


def test_cleanup_remove_o_run_e_cancel_depois_do_fim_nao_recria_estado():
    manager = RegisteredRunCancellationManager()
    manager.register_run("r1")
    manager.cleanup_run("r1")

    assert manager.cancel_run("r1") is False
    assert manager.get_active_runs() == {}
    manager.cleanup_run("r1")  # idempotente


async def test_acleanup_remove_o_run():
    manager = RegisteredRunCancellationManager()
    await manager.aregister_run("r1")
    await manager.acleanup_run("r1")

    assert await manager.aget_active_runs() == {}
    await manager.araise_if_cancelled("r1")  # nada a levantar


def test_get_active_runs_devolve_copia():
    manager = RegisteredRunCancellationManager()
    manager.register_run("r1")

    manager.get_active_runs()["r1"] = True

    assert manager.is_cancelled("r1") is False


def test_install_troca_o_gerenciador_global_uma_vez():
    """Usa a API pública do agno; o conftest devolve o original no teardown."""
    first = install_run_cancellation_manager()
    second = install_run_cancellation_manager()

    assert cancel.get_cancellation_manager() is first
    assert second is first
    assert cancel.cancel_run("fantasma") is False
    assert cancel.get_active_runs() == {}
