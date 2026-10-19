"""Contrato de escrita das portas de config (FA-01): concorrência otimista por ``version``.

- ``list_all``: todos os documentos (inclusive inativos e ids repetidos), na ordem estável, com a
  versão; documento sem ``version`` (legado, como todo documento semeado aqui) é a versão 0. Roda em
  todos os backends.
- ``save``/``delete`` com ``expected_version``: versão divergente ⇒ ``ConfigVersionConflict`` e nada
  muda; id que não é texto (ex.: operador ``{"$ne": ""}``) é recusado com ``TypeError`` antes de
  chegar ao store. Roda nos backends graváveis: memória e Mongo (coleção fake e, ``live``, Mongo
  real), sem o índice único de ``id`` do seed: a escrita não pode depender dele.
- YAML é só leitura: ``ConfigStoreReadOnly`` sem tocar o arquivo.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.domain.repositories.versioning import ConfigStoreReadOnly, ConfigVersionConflict, Versioned
from tests.fakes.config_stores import (
    ENTITY_BACKEND_PARAMS,
    EntityBackend,
    MemoryBackend,
    YamlBackend,
    entity_backend,
    live_mongo_for,
)

Backend = MemoryBackend | EntityBackend


@pytest.fixture(params=ENTITY_BACKEND_PARAMS)
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Backend:
    """Todos os backends por entidades (substitui a do conftest)."""
    return entity_backend(request.param, monkeypatch, tmp_path, live_mongo_for(request))


# ── list_all ────────────────────────────────────────────────────────


async def test_list_all_inclui_inativos_e_repetidos_na_ordem_com_legado_na_versao_0(backend, kind):
    items = [kind.make("b"), kind.make("a", active=False), kind.make("b", "repetido")]
    repo = kind.repo(backend, items)

    assert await repo.list_all() == [Versioned(item, 0) for item in items]


# ── save / delete ───────────────────────────────────────────────────


async def test_save_na_versao_esperada_grava_devolve_a_proxima_e_o_runtime_le(writable, kind):
    repo = kind.repo(writable, [kind.make("a"), kind.make("b")])

    assert await repo.save(kind.make("a", "v1"), 0) == 1
    assert await repo.save(kind.make("a", "v2", active=False), 1) == 2

    assert await repo.list_all() == [Versioned(kind.make("a", "v2", active=False), 2), Versioned(kind.make("b"), 0)]
    assert await kind.get(repo, "a") == kind.make("a", "v2", active=False)


async def test_dois_saves_concorrentes_na_mesma_versao_um_vence_o_outro_conflita(writable, kind):
    repo = kind.repo(writable, [kind.make("a")])

    results = await asyncio.gather(
        repo.save(kind.make("a", "x"), 0), repo.save(kind.make("a", "y"), 0), return_exceptions=True
    )

    (winner,) = [i for i, r in enumerate(results) if r == 1]
    (loser,) = [r for r in results if isinstance(r, ConfigVersionConflict)]
    assert (loser.entity_id, loser.expected_version) == ("a", 0)
    assert await repo.list_all() == [Versioned(kind.make("a", "xy"[winner]), 1)]


async def test_save_em_versao_divergente_conflita_sem_gravar(writable, kind):
    """Inclusive a versão 0 velha: não vira uma segunda criação do mesmo id."""
    repo = kind.repo(writable, [kind.make("a")])
    await repo.save(kind.make("a", "v1"), 0)

    for stale in (0, 2):
        with pytest.raises(ConfigVersionConflict):
            await repo.save(kind.make("a", "perdida"), stale)

    assert await repo.list_all() == [Versioned(kind.make("a", "v1"), 1)]


async def test_save_de_id_novo_so_na_versao_0_e_criar_de_novo_conflita(writable, kind):
    repo = kind.repo(writable, [])

    with pytest.raises(ConfigVersionConflict):
        await repo.save(kind.make("novo"), 3)
    assert await repo.save(kind.make("novo"), 0) == 1
    with pytest.raises(ConfigVersionConflict):
        await repo.save(kind.make("novo", "outra criação"), 0)

    assert await repo.list_all() == [Versioned(kind.make("novo"), 1)]


async def test_delete_na_versao_esperada_remove_e_divergente_ou_inexistente_conflita(writable, kind):
    repo = kind.repo(writable, [kind.make("legado"), kind.make("b")])
    await repo.save(kind.make("b", "v1"), 0)

    await repo.delete("legado", 0)
    for entity_id, stale in (("b", 0), ("b", 2), ("inexistente", 0)):
        with pytest.raises(ConfigVersionConflict):
            await repo.delete(entity_id, stale)

    assert await repo.list_all() == [Versioned(kind.make("b", "v1"), 1)]


async def test_id_ou_versao_operador_e_recusado_sem_tocar_o_store(writable, kind):
    """Injeção NoSQL: id ou versão operador casaria (e trocaria ou apagaria) outro documento."""
    repo = kind.repo(writable, [kind.make("vitima")])
    forged = kind.make("x")
    forged.id = {"$ne": ""}  # entidade adulterada depois da validação

    with pytest.raises(TypeError):
        await repo.save(forged, 0)
    with pytest.raises(TypeError):
        await repo.delete({"$ne": ""}, 0)
    with pytest.raises(TypeError):
        await repo.delete("vitima", {"$gte": 0})

    assert await repo.list_all() == [Versioned(kind.make("vitima"), 0)]


# ── YAML: só leitura ────────────────────────────────────────────────


async def test_yaml_recusa_escrita_sem_tocar_o_arquivo(tmp_path, kind):
    yaml_backend = YamlBackend(tmp_path)
    repo = kind.repo(EntityBackend(yaml_backend), [kind.make("a")])
    before = (yaml_backend.path.read_bytes(), yaml_backend.path.stat().st_mtime_ns)

    with pytest.raises(ConfigStoreReadOnly):
        await repo.save(kind.make("a", "v1"), 0)
    with pytest.raises(ConfigStoreReadOnly):
        await repo.delete("a", 0)

    assert (yaml_backend.path.read_bytes(), yaml_backend.path.stat().st_mtime_ns) == before
    assert await repo.list_all() == [Versioned(kind.make("a"), 0)]
