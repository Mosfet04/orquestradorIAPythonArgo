"""QA do FA-05: bordas da propagação entre réplicas, na pilha completa (``replica_stacks``).

Dois ou três apps (lifespan real cada) no mesmo Mongo falso. Com o poll parado (default), o teste dá a volta
dele com ``app.state.config_admin.sync()``; com ``poll_seconds`` curto, é o laço do lifespan. Chaves são de teste.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from tests.fakes.admin_config_app import ADMIN, RUN, agent_body, if_match, team_body
from tests.fakes.hot_swap_app import CONFIG, POLL_SECONDS, HotStack, replica_stacks, run_rest, served_within

TOOL: dict[str, Any] = {
    "id": "cep",
    "name": "cep",
    "description": "descricao-v1",
    "route": "https://api.example.invalid/{cep}",
    "http_method": "GET",
    "parameters": [{"name": "cep", "type": "string", "description": "CEP", "required": True}],
}


def _sync(stack: HotStack) -> Any:
    return stack.app.state.config_admin.sync()


def _tool_descriptions_of_last_call(stack: HotStack) -> list[str]:
    """Descrição das tools que o agente entregou ao modelo na última chamada."""
    calls = [call for model in stack.models for call in model.calls]
    return [str(tool["function"]["description"]) for tool in calls[-1].tools]


async def _until(condition: Callable[[], Awaitable[bool]], *, seconds: float = 5) -> bool:
    """``condition()`` verdadeira dentro de ``seconds`` (sem depender de um número de ciclos do poll)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not await condition():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(POLL_SECONDS / 5)
    return True


async def test_troca_de_tool_numa_replica_chega_ao_agente_da_outra(monkeypatch: pytest.MonkeyPatch):
    async with replica_stacks(monkeypatch, count=2) as (writer, reader):
        await writer.client.post(f"{CONFIG}/tools", json=TOOL, headers=ADMIN)
        await writer.client.post(f"{CONFIG}/agents", json=agent_body("a1", tools_ids=["cep"]), headers=ADMIN)
        await _sync(reader)
        await run_rest(reader.client, "a1")
        before = _tool_descriptions_of_last_call(reader)

        updated = await writer.client.put(
            f"{CONFIG}/tools/cep", json={**TOOL, "description": "descricao-v2"}, headers=if_match(1)
        )
        stale = await run_rest(reader.client, "a1")
        stale_descriptions = _tool_descriptions_of_last_call(reader)
        followed = await _sync(reader)
        await run_rest(reader.client, "a1")
        after = _tool_descriptions_of_last_call(reader)

    assert updated.status_code == 200 and stale.status_code == 200
    assert before == ["descricao-v1"] and stale_descriptions == ["descricao-v1"]  # antes do poll, a config anterior
    assert followed == 2  # a carga do startup era a revisão 0; a 1ª volta trocou para 1
    assert after == ["descricao-v2"]


async def test_team_criado_e_removido_numa_replica_e_servido_e_retirado_na_outra(monkeypatch: pytest.MonkeyPatch):
    async with replica_stacks(monkeypatch, count=2) as (writer, reader):
        await writer.client.post(f"{CONFIG}/agents", json=agent_body("a1"), headers=ADMIN)
        await writer.client.post(f"{CONFIG}/teams", json=team_body("tm", ["a1"]), headers=ADMIN)
        absent = await reader.client.get("/teams/tm", headers=RUN)
        await _sync(reader)
        present = await reader.client.get("/teams/tm", headers=RUN)

        await writer.client.delete(f"{CONFIG}/teams/tm", params={"hard": "true"}, headers=if_match(1))
        await _sync(reader)
        gone = await reader.client.get("/teams/tm", headers=RUN)
        agent_still = await reader.client.get("/agents/a1", headers=RUN)

    assert (absent.status_code, present.status_code, gone.status_code, agent_still.status_code) == (404, 200, 404, 200)


async def test_remocao_logica_e_restauracao_numa_replica_chegam_a_outra(monkeypatch: pytest.MonkeyPatch):
    async with replica_stacks(monkeypatch, count=2) as (writer, reader):
        await writer.client.post(f"{CONFIG}/agents", json=agent_body("a1"), headers=ADMIN)
        await _sync(reader)
        await writer.client.delete(f"{CONFIG}/agents/a1", headers=if_match(1))
        await _sync(reader)
        inactive = await reader.client.get("/agents/a1", headers=RUN)

        restored = await writer.client.post(f"{CONFIG}/agents/a1/history/1/restore", headers=if_match(2))
        await _sync(reader)
        active = await reader.client.get("/agents/a1", headers=RUN)
        run = await run_rest(reader.client, "a1")
        history = await reader.client.get(f"{CONFIG}/agents/a1/history", headers=ADMIN)

    assert inactive.status_code == 404
    assert (restored.status_code, restored.json()["active"]) == (200, True)
    assert (active.status_code, run.status_code) == (200, 200)
    # o histórico é do store: a réplica que não escreveu o mostra inteiro
    assert [item["version"] for item in history.json()["items"]] == [3, 2, 1]


async def test_updates_concorrentes_de_replicas_diferentes_com_o_mesmo_if_match_um_vence_o_outro_412(
    monkeypatch: pytest.MonkeyPatch,
):
    async with replica_stacks(monkeypatch, count=2) as (a, b):
        await a.client.post(f"{CONFIG}/agents", json=agent_body("a1"), headers=ADMIN)
        await _sync(b)
        first, second = await asyncio.gather(
            a.client.put(f"{CONFIG}/agents/a1", json=agent_body("a1", prompt="da-a"), headers=if_match(1)),
            b.client.put(f"{CONFIG}/agents/a1", json=agent_body("a1", prompt="da-b"), headers=if_match(1)),
        )
        history = await a.client.get(f"{CONFIG}/agents/a1/history", headers=ADMIN)
        await _sync(a)
        await _sync(b)
        await run_rest(a.client, "a1")
        await run_rest(b.client, "a1")

    assert sorted([first.status_code, second.status_code]) == [200, 412]
    assert history.json()["total"] == 2
    winner = "da-a" if first.status_code == 200 else "da-b"
    assert history.json()["items"][0]["document"]["prompt"] == winner
    assert winner in a.instructions_seen()[-1] and winner in b.instructions_seen()[-1]


async def test_escritas_de_duas_replicas_sem_poll_no_meio_convergem_e_o_historico_ordena_pela_revisao(
    monkeypatch: pytest.MonkeyPatch,
):
    async with replica_stacks(monkeypatch, count=2) as (a, b):
        await a.client.post(f"{CONFIG}/agents", json=agent_body("a1"), headers=ADMIN)  # revisão 1
        await b.client.post(f"{CONFIG}/agents", json=agent_body("b1"), headers=ADMIN)  # revisão 2, sem ter lido a 1
        await a.client.post(f"{CONFIG}/agents", json=agent_body("a2"), headers=ADMIN)  # revisão 3, sem ter lido a 2
        await b.client.put(f"{CONFIG}/agents/a1", json=agent_body("a1", prompt="b-mexeu"), headers=if_match(1))
        before = [await b.client.get(f"/agents/{i}", headers=RUN) for i in ("a1", "a2", "b1")]
        await _sync(a)
        await _sync(b)
        served = {
            name: [(await stack.client.get(f"/agents/{i}", headers=RUN)).status_code for i in ("a1", "a2", "b1")]
            for name, stack in (("a", a), ("b", b))
        }
        history = await a.client.get(f"{CONFIG}/agents/a1/history", headers=ADMIN)
        again = (await _sync(a), await _sync(b))

    # a montagem de cada escrita lê o store inteiro: B já serve tudo o que estava gravado quando escreveu
    assert [r.status_code for r in before] == [200, 200, 200]
    assert served == {"a": [200, 200, 200], "b": [200, 200, 200]}
    assert [(i["version"], i["actor"]) for i in history.json()["items"]] == [(2, "admin"), (1, "admin")]
    assert history.json()["items"][0]["document"]["prompt"] == "b-mexeu"
    assert again == (None, None)


async def test_replica_que_sobe_depois_das_escritas_serve_tudo_sem_recarregar_no_primeiro_poll(
    monkeypatch: pytest.MonkeyPatch,
):
    async with replica_stacks(monkeypatch, count=1) as replicas:
        first = replicas[0]
        await first.client.post(f"{CONFIG}/agents", json=agent_body("a1"), headers=ADMIN)
        await first.client.put(f"{CONFIG}/agents/a1", json=agent_body("a1", prompt="v2"), headers=if_match(1))
        late = await replicas.add()  # sobe agora no mesmo store: lê a revisão antes de carregar
        served = await late.client.get("/agents/a1", headers=RUN)
        run = await run_rest(late.client, "a1")
        followed = await _sync(late)

    assert served.status_code == 200 and run.status_code == 200
    assert "v2" in late.instructions_seen()[-1]
    assert followed is None  # a carga do startup já era a revisão do store


async def test_o_poll_para_no_shutdown_sem_deixar_task_pendente(monkeypatch: pytest.MonkeyPatch):
    def pollers() -> list[asyncio.Task[Any]]:
        return [t for t in asyncio.all_tasks() if t.get_coro().__name__ == "poll_config_revision"]  # type: ignore[union-attr]

    async with replica_stacks(monkeypatch, count=2, poll_seconds=str(POLL_SECONDS)):
        running = pollers()
        await asyncio.sleep(POLL_SECONDS * 3)

    assert len(running) == 2
    assert all(t.cancelled() for t in running)
    assert pollers() == []


async def test_store_fora_por_varios_ciclos_o_runtime_anterior_segue_e_a_replica_se_recupera(
    monkeypatch: pytest.MonkeyPatch,
):
    attempts: list[None] = []

    def store_down(*_: object, **__: object) -> None:
        attempts.append(None)
        raise ConnectionError("sem rota")

    async with replica_stacks(monkeypatch, count=2, poll_seconds=str(POLL_SECONDS)) as (writer, reader):
        await writer.client.post(f"{CONFIG}/agents", json=agent_body("a1", prompt="v1"), headers=ADMIN)
        assert (await served_within(reader.client, "/agents/a1")).status_code == 200
        served_revision = await reader.revision()

        # O servidor "cai" ANTES de a revisão mudar (nada corre contra o poll): outra réplica escreveu, mas a
        # releitura do reader falha em todo ciclo. A revisão sobe direto no contador, como a escrita faria.
        collection = reader.store["agents_config"]
        collection.find = store_down  # type: ignore[method-assign]
        await reader.store["config_revision"].find_one_and_update(
            {"_id": "config"}, {"$inc": {"revision": 1}}, upsert=True
        )
        failing_cycles = await _until(_at_least(attempts, 3))
        during = await run_rest(reader.client, "a1")
        instructions_during = reader.instructions_seen()[-1]
        revision_during = await reader.revision()

        del collection.find  # o servidor voltou
        recovered_revision = await _until(_revision_above(reader, served_revision))
        await writer.client.put(f"{CONFIG}/agents/a1", json=agent_body("a1", prompt="v2"), headers=if_match(1))
        serves_v2 = await _until(_prompt_served(reader, "a1", "v2"))

    assert failing_cycles  # o poll tentou reler várias vezes com o store fora
    assert during.status_code == 200 and "v1" in instructions_during  # segue servindo a config anterior
    assert revision_during == served_revision  # nenhuma troca com o store fora
    assert recovered_revision  # o ciclo seguinte à volta do servidor relê e troca, sem escrita nova
    assert serves_v2


def _at_least(items: list[None], count: int) -> Callable[[], Awaitable[bool]]:
    async def condition() -> bool:
        return len(items) >= count

    return condition


def _revision_above(stack: HotStack, revision: int) -> Callable[[], Awaitable[bool]]:
    async def condition() -> bool:
        return await stack.revision() > revision

    return condition


def _prompt_served(stack: HotStack, entity_id: str, prompt: str) -> Callable[[], Awaitable[bool]]:
    async def condition() -> bool:
        await run_rest(stack.client, entity_id)
        return prompt in stack.instructions_seen()[-1]

    return condition
