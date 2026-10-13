"""F2-06: ``CONFIG_STORE=mongo|yaml`` e ``CONFIG_YAML_PATH`` no ``AppConfig`` e no composition root,
e a leitura do arquivo YAML (estrutura e erros sem conteúdo do arquivo)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.infrastructure import dependency_injection as di
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.repositories.mongo_agent_config_repository import MongoAgentConfigRepository
from src.infrastructure.repositories.mongo_team_config_repository import MongoTeamConfigRepository
from src.infrastructure.repositories.mongo_tool_repository import MongoToolRepository
from src.infrastructure.repositories.yaml_config_repository import (
    YamlAgentConfigRepository,
    YamlConfigError,
    YamlConfigFile,
    YamlTeamConfigRepository,
    YamlToolRepository,
)
from tests.fakes import RecordingLogger

MARKER = "SEGREDO-DO-ARQUIVO"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CONFIG_STORE", "CONFIG_YAML_PATH"):
        monkeypatch.delenv(name, raising=False)


def _yaml_file(tmp_path: Path, text: str = "agents: []\n") -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# ── AppConfig ────────────────────────────────────────────────────────


def test_padrao_e_mongo_sem_caminho() -> None:
    config = AppConfig.load()

    assert (config.config_store, config.config_yaml_path) == ("mongo", None)


def test_yaml_com_arquivo_regular_legivel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = _yaml_file(tmp_path)
    monkeypatch.setenv("CONFIG_STORE", " YAML ")
    monkeypatch.setenv("CONFIG_YAML_PATH", f" {path} ")

    config = AppConfig.load()

    assert (config.config_store, config.config_yaml_path) == ("yaml", str(path))


def test_caminho_relativo_vira_absoluto_a_partir_do_diretorio_de_trabalho(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _yaml_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONFIG_STORE", "yaml")
    monkeypatch.setenv("CONFIG_YAML_PATH", "config.yaml")

    assert AppConfig.load().config_yaml_path == str(tmp_path / "config.yaml")


def test_symlink_para_arquivo_regular_vale(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ConfigMap do Kubernetes monta o arquivo como symlink (``..data``)."""
    link = tmp_path / "link.yaml"
    link.symlink_to(_yaml_file(tmp_path))
    monkeypatch.setenv("CONFIG_STORE", "yaml")
    monkeypatch.setenv("CONFIG_YAML_PATH", str(link))

    assert AppConfig.load().config_yaml_path == str(link)


@pytest.mark.parametrize("value", ["redis", "mongodb", "yml", MARKER])
def test_config_store_invalido_falha_sem_ecoar_o_valor(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CONFIG_STORE", value)

    with pytest.raises(ValueError, match=r"CONFIG_STORE inválido\. Use mongo ou yaml") as exc_info:
        AppConfig.load()

    assert value not in str(exc_info.value)


@pytest.mark.parametrize("path_value", [None, "", "   "])
def test_yaml_sem_caminho_falha(monkeypatch: pytest.MonkeyPatch, path_value: str | None) -> None:
    monkeypatch.setenv("CONFIG_STORE", "yaml")
    if path_value is not None:
        monkeypatch.setenv("CONFIG_YAML_PATH", path_value)

    with pytest.raises(ValueError, match="CONFIG_YAML_PATH é obrigatória com CONFIG_STORE=yaml"):
        AppConfig.load()


@pytest.mark.parametrize("kind", ["inexistente", "diretorio", "fifo"])
def test_yaml_com_caminho_que_nao_e_arquivo_regular_falha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    target = tmp_path / MARKER
    if kind == "diretorio":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    monkeypatch.setenv("CONFIG_STORE", "yaml")
    monkeypatch.setenv("CONFIG_YAML_PATH", str(target))

    with pytest.raises(ValueError, match="CONFIG_YAML_PATH não aponta para um arquivo regular legível") as exc_info:
        AppConfig.load()

    assert MARKER not in str(exc_info.value)


@pytest.mark.skipif(os.geteuid() == 0, reason="root lê arquivo sem permissão")
def test_yaml_com_arquivo_sem_permissao_de_leitura_falha(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = _yaml_file(tmp_path)
    path.chmod(0o000)
    monkeypatch.setenv("CONFIG_STORE", "yaml")
    monkeypatch.setenv("CONFIG_YAML_PATH", str(path))
    try:
        with pytest.raises(ValueError, match="arquivo regular legível"):
            AppConfig.load()
    finally:
        path.chmod(0o600)


def test_mongo_ignora_config_yaml_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CONFIG_STORE", "mongo")
    monkeypatch.setenv("CONFIG_YAML_PATH", str(tmp_path / "nao-existe.yaml"))

    assert AppConfig.load().config_yaml_path is None


# ── composition root ─────────────────────────────────────────────────


def _container(config: AppConfig) -> di.DependencyContainer:
    return di.DependencyContainer(config)


def test_composition_root_com_yaml_usa_os_repositorios_yaml_sobre_o_mesmo_arquivo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _yaml_file(tmp_path)
    monkeypatch.setenv("CONFIG_STORE", "yaml")
    monkeypatch.setenv("CONFIG_YAML_PATH", str(path))

    agents, teams, tools = _container(AppConfig.load())._build_config_repositories()

    assert (type(agents), type(teams), type(tools)) == (
        YamlAgentConfigRepository,
        YamlTeamConfigRepository,
        YamlToolRepository,
    )
    assert {repo._source.path for repo in (agents, teams, tools)} == {str(path)}


def test_composition_root_com_yaml_nao_cria_repositorio_de_config_do_mongo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def forbidden(**_: object) -> None:
        raise AssertionError("com CONFIG_STORE=yaml, nenhuma coleção de config do Mongo é usada")

    for name in ("MongoAgentConfigRepository", "MongoTeamConfigRepository", "MongoToolRepository"):
        monkeypatch.setattr(di, name, forbidden)
    monkeypatch.setenv("CONFIG_STORE", "yaml")
    monkeypatch.setenv("CONFIG_YAML_PATH", str(_yaml_file(tmp_path)))

    repos = _container(AppConfig.load())._build_config_repositories()

    assert len(repos) == 3


def test_composition_root_padrao_usa_os_repositorios_mongo(monkeypatch: pytest.MonkeyPatch) -> None:
    from collections import defaultdict

    from src.infrastructure.repositories import mongo_base
    from tests.fakes import FakeMongoClient, FakeMongoCollection

    client = FakeMongoClient(defaultdict(FakeMongoCollection))
    monkeypatch.setattr(mongo_base.MongoClientFactory, "get_client", classmethod(lambda cls, _conn: client))

    agents, teams, tools = _container(AppConfig.load())._build_config_repositories()

    assert (type(agents), type(teams), type(tools)) == (
        MongoAgentConfigRepository,
        MongoTeamConfigRepository,
        MongoToolRepository,
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"config_store": "yaml", "config_yaml_path": None}, "CONFIG_YAML_PATH é obrigatória"),
        ({"config_store": "yaml", "config_yaml_path": ""}, "CONFIG_YAML_PATH é obrigatória"),
        ({"config_store": "redis"}, r"CONFIG_STORE inválido\. Use mongo ou yaml"),
    ],
)
def test_app_config_montado_a_mao_tambem_e_validado(changes: dict[str, object], message: str) -> None:
    """``AppConfig`` fora do ``load`` (testes, ``dataclasses.replace``) não chega ao composition root
    com yaml sem caminho (que cairia no Mongo) nem com backend desconhecido."""
    import dataclasses

    with pytest.raises(ValueError, match=message):
        dataclasses.replace(AppConfig.load(), **changes)  # type: ignore[arg-type]


# ── leitura do arquivo ───────────────────────────────────────────────


def _source(path: Path, logger: RecordingLogger | None = None) -> YamlConfigFile:
    return YamlConfigFile(str(path), logger=logger or RecordingLogger())


AGENT = "  - {{id: {id}, nome: N, factoryIaModel: ollama, model: m, prompt: p, active: true}}\n"


async def test_arquivo_vazio_e_secoes_ausentes_ou_nulas_sao_listas_vazias(tmp_path: Path) -> None:
    for text in ("", "agents:\n", "tools: []\n"):
        source = _source(_yaml_file(tmp_path, text))
        assert [await source.section(name) for name in ("agents", "teams", "tools")] == [[], [], []]


INVALID_FILES = [
    pytest.param(f"agents: [{{id: {MARKER}\n", r"YAML inválido \(linha \d+, coluna \d+\)", id="yaml-quebrado"),
    pytest.param(f"- {MARKER}\n", "o topo do arquivo deve ser um mapeamento", id="topo-lista"),
    pytest.param(
        f"agent:\n  - id: {MARKER}\n", "chave de topo desconhecida; use só agents, teams e tools", id="chave-errada"
    ),
    pytest.param(f"agents:\n  id: {MARKER}\n", "a seção agents deve ser uma lista de documentos", id="secao-mapa"),
    pytest.param(f"!!python/object/apply:os.system ['{MARKER}']\n", "YAML inválido", id="tag-python"),
    pytest.param(
        f"agents:\n  - &a {{id: {MARKER}}}\n  - *a\n",
        r"âncoras e aliases \(& e \*\) não são suportados \(linha 2, coluna 5\)",
        id="ancora",
    ),
    pytest.param(
        f"agents: []\nteams: *{MARKER}\n", r"âncoras e aliases .* \(linha 2, coluna 8\)", id="alias-sem-ancora"
    ),
    pytest.param(
        f"agents:\n  - {{id: a, nome: {MARKER}}}\nagents: []\n",
        r"chave repetida num mapeamento \(linha 3, coluna 1\)",
        id="secao-repetida",
    ),
    pytest.param(
        f"agents:\n  - id: a\n    nome: x\n    nome: {MARKER}\n",
        r"chave repetida num mapeamento \(linha 4, coluna 5\)",
        id="campo-repetido",
    ),
    pytest.param(
        f"agents:\n  - {{id: a, criado: 2024-13-45, nome: {MARKER}}}\n",
        r"valor inválido no YAML \(ValueError\)",
        id="data-impossivel-sem-aspas",
    ),
    pytest.param(
        "agents: " + "[" * 100_000 + MARKER + "]" * 100_000 + "\n",
        r"aninhamento acima de 64 níveis \(linha 1, coluna 72\)",
        id="aninhamento-profundo",
    ),
]


@pytest.mark.parametrize(("text", "message"), INVALID_FILES)
async def test_arquivo_fora_do_formato_levanta_erro_sem_conteudo_do_arquivo(
    tmp_path: Path, text: str, message: str
) -> None:
    source = _source(_yaml_file(tmp_path, text))

    with pytest.raises(YamlConfigError, match=message) as exc_info:
        await source.section("agents")

    error = exc_info.value
    assert MARKER not in str(error)
    # O erro do PyYAML (que cita o trecho do arquivo) não fica encadeado no traceback.
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__ is True


async def test_recursion_error_do_loader_vira_erro_nosso_sem_conteudo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loader Python (sem libyaml) estoura a recursão antes do limite de profundidade em pilha pequena."""
    from src.infrastructure.repositories import yaml_config_repository as module

    def deep(_: str) -> object:
        raise RecursionError(MARKER)

    monkeypatch.setattr(module, "_parse", deep)

    with pytest.raises(YamlConfigError, match=r"valor inválido no YAML \(RecursionError\)") as exc_info:
        await _source(_yaml_file(tmp_path)).section("agents")

    assert MARKER not in str(exc_info.value) and exc_info.value.__suppress_context__


def test_loader_e_o_seguro_da_libyaml_quando_disponivel() -> None:
    import yaml

    from src.infrastructure.repositories import yaml_config_repository as module

    assert module.SAFE_LOADER is (yaml.CSafeLoader if yaml.__with_libyaml__ else yaml.SafeLoader)


async def test_arquivo_que_nao_e_utf8_levanta_erro_claro(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes(b"agents:\n  - id: \xff\xfe\n")

    with pytest.raises(YamlConfigError, match="não é UTF-8 válido"):
        await _source(path).section("agents")


async def test_arquivo_removido_depois_do_startup_levanta_erro_com_o_tipo(tmp_path: Path) -> None:
    path = _yaml_file(tmp_path)
    source = _source(path)
    await source.section("agents")
    path.unlink()

    with pytest.raises(YamlConfigError, match=r"não foi possível ler o arquivo \(FileNotFoundError\)"):
        await source.section("agents")


# ── cache: relê só quando o arquivo muda ─────────────────────────────


@pytest.fixture
def parse_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from src.infrastructure.repositories import yaml_config_repository as module

    calls: list[str] = []
    original = module._parse

    def spy(text: str) -> object:
        calls.append(text)
        return original(text)

    monkeypatch.setattr(module, "_parse", spy)
    return calls


async def test_consultas_concorrentes_fazem_um_parse_so(tmp_path: Path, parse_spy: list[str]) -> None:
    import asyncio

    text = "tools:\n" + "".join(
        f"  - {{id: t{i}, name: n, description: d, route: 'https://x.example', active: true}}\n" for i in range(20)
    )
    repo = YamlToolRepository(_source(_yaml_file(tmp_path, text)), logger=RecordingLogger())

    results = await asyncio.gather(*(repo.get_tools_by_ids([f"t{i}"]) for i in range(20)))

    assert [[t.id for t in r] for r in results] == [[f"t{i}"] for i in range(20)]
    assert len(parse_spy) == 1


async def test_arquivo_alterado_e_relido(tmp_path: Path, parse_spy: list[str]) -> None:
    """Como cada consulta ao Mongo: o refresh-cache enxerga o arquivo editado (sem watcher)."""
    path = _yaml_file(tmp_path, "agents: []\n")
    repo = YamlAgentConfigRepository(_source(path), logger=RecordingLogger())
    assert await repo.get_active_agents() == []
    assert await repo.get_active_agents() == []

    path.write_text("agents:\n" + AGENT.format(id="novo"), encoding="utf-8")

    assert [c.id for c in await repo.get_active_agents()] == ["novo"]
    assert len(parse_spy) == 2


async def test_symlink_apontado_para_outro_arquivo_e_relido(tmp_path: Path, parse_spy: list[str]) -> None:
    """Troca do ConfigMap do Kubernetes: o symlink passa a apontar para outro arquivo (outro inode)."""
    first = tmp_path / "v1.yaml"
    second = tmp_path / "v2.yaml"
    first.write_text("agents:\n" + AGENT.format(id="um"), encoding="utf-8")
    second.write_text("agents:\n" + AGENT.format(id="do"), encoding="utf-8")  # mesmo tamanho
    link = tmp_path / "config.yaml"
    link.symlink_to(first)
    repo = YamlAgentConfigRepository(_source(link), logger=RecordingLogger())
    assert [c.id for c in await repo.get_active_agents()] == ["um"]

    link.unlink()
    link.symlink_to(second)

    assert [c.id for c in await repo.get_active_agents()] == ["do"]
    assert len(parse_spy) == 2


async def test_entidade_devolvida_nao_aponta_para_o_cache(tmp_path: Path) -> None:
    text = "agents:\n  - {id: a, nome: N, factoryIaModel: ollama, model: m, prompt: p, tools_ids: [t], active: true}\n"
    repo = YamlAgentConfigRepository(_source(_yaml_file(tmp_path, text)), logger=RecordingLogger())

    (listed,) = await repo.get_active_agents()
    listed.tools_ids.append("x")
    by_id = await repo.get_agent_by_id("a")
    by_id.tools_ids.append("y")

    assert (await repo.get_agent_by_id("a")).tools_ids == ["t"]
    assert [c.tools_ids for c in await repo.get_active_agents()] == [["t"]]


async def test_item_que_nao_e_mapeamento_e_logado_uma_vez_por_leitura_do_arquivo(tmp_path: Path) -> None:
    text = f"agents:\n  - {MARKER}\n  - [lista]\n" + AGENT.format(id="ok")
    path = _yaml_file(tmp_path, text)
    logger = RecordingLogger()
    repo = YamlAgentConfigRepository(_source(path, logger), logger=logger)

    for _ in range(3):
        assert [c.id for c in await repo.get_active_agents()] == ["ok"]

    expected = [
        ("Documento de agente inválido ignorado", {"agent_id": None, "position": 1, "error_type": "TypeError"}),
        ("Documento de agente inválido ignorado", {"agent_id": None, "position": 2, "error_type": "TypeError"}),
    ]
    assert [(r.message, r.context) for r in logger.records if r.level == "error"] == expected
    assert all(MARKER not in repr(r) for r in logger.records)

    path.write_text(text + AGENT.format(id="o2"), encoding="utf-8")
    await repo.get_active_agents()

    assert [(r.message, r.context) for r in logger.records if r.level == "error"] == expected * 2


async def test_leitura_roda_fora_do_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from src.infrastructure.repositories import yaml_config_repository as module

    threads: list[str] = []
    original = module._parse

    def spy(text: str) -> object:
        threads.append(threading.current_thread().name)
        return original(text)

    monkeypatch.setattr(module, "_parse", spy)

    await _source(_yaml_file(tmp_path)).section("agents")

    assert threads and threads[0] != threading.main_thread().name
