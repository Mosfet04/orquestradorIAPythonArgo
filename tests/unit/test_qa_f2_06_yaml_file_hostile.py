"""QA do F2-06: arquivo YAML hostil ou esquisito lido por ``YamlConfigFile`` e os repositórios.

Sem conteúdo do arquivo em erro ou log; sem execução de tag Python; âncora/alias (inclusive recursivo
e "billion laughs") e chave repetida são recusados com erro nosso, rápido e sem conteúdo (rodada 2 da
revisão do F2-06: antes o ``safe_load`` aceitava alias e ficava com a última chave repetida).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.repositories.yaml_config_repository import (
    YamlAgentConfigRepository,
    YamlConfigError,
    YamlConfigFile,
    YamlToolRepository,
)
from tests.fakes import RecordingLogger

MARKER = "SEGREDO-HOSTIL-QA-F206"
OK = "  - {id: ok, nome: Ok, factoryIaModel: ollama, model: m, prompt: p, active: true}\n"


def _write(tmp_path: Path, data: bytes | str, logger: RecordingLogger | None = None) -> YamlConfigFile:
    path = tmp_path / "config.yaml"
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return YamlConfigFile(str(path), logger=logger or RecordingLogger())


async def _agent_ids(source: YamlConfigFile, logger: RecordingLogger | None = None) -> list[str]:
    repo = YamlAgentConfigRepository(source, logger=logger or RecordingLogger())
    return [agent.id for agent in await repo.get_active_agents()]


async def test_bom_utf8_no_inicio_e_aceito(tmp_path: Path) -> None:
    assert await _agent_ids(_write(tmp_path, b"\xef\xbb\xbfagents:\n" + OK.encode())) == ["ok"]


async def test_crlf_e_tabs_de_indentacao_do_editor_do_windows(tmp_path: Path) -> None:
    assert await _agent_ids(_write(tmp_path, ("agents:\r\n" + OK).replace("\n", "\r\n") + "\r\n")) == ["ok"]
    with pytest.raises(YamlConfigError, match="YAML inválido"):
        await _agent_ids(_write(tmp_path, f"agents:\n\t- {MARKER}\n"))


@pytest.mark.parametrize("text", ["", "   \n\n", "# só comentário\n", "---\n", "~\n", "null\n"])
async def test_arquivo_sem_conteudo_util_e_config_vazia(tmp_path: Path, text: str) -> None:
    source = _write(tmp_path, text)

    assert [await source.section(name) for name in ("agents", "teams", "tools")] == [[], [], []]


@pytest.mark.parametrize(
    "data",
    [
        pytest.param("agents: [1]\n---\nagents: [2]\n", id="multi-documento"),
        pytest.param("agents: [", id="truncado"),
        pytest.param(f"agents: !!python/object/apply:os.system ['echo {MARKER}']\n", id="tag-apply"),
        pytest.param(f"agents: [!!python/name:os.system {MARKER}]\n", id="tag-name"),
        pytest.param(f"agents: [!!python/object/new:subprocess.Popen [[{MARKER}]]]\n", id="tag-new"),
        pytest.param(f"agents: [!!binary {MARKER}]\n", id="binary-invalido"),
        pytest.param(f"agents: [!custom {MARKER}]\n", id="tag-desconhecida"),
        pytest.param(f"agents: *naoexiste\n# {MARKER}\n", id="alias-inexistente"),
        pytest.param(b"agents: [\xc3\x28]\n", id="utf8-quebrado"),
        pytest.param("agents: [\x07]\n", id="controle"),
        pytest.param("agents: [a]\x00\n", id="nul"),
    ],
)
async def test_arquivo_hostil_levanta_erro_nosso_sem_conteudo(tmp_path: Path, data: bytes | str) -> None:
    with pytest.raises(YamlConfigError) as exc_info:
        await _write(tmp_path, data).section("agents")

    assert MARKER not in str(exc_info.value) and MARKER not in repr(exc_info.value)
    error = exc_info.value
    # nada encadeado: nem causa, nem contexto visível (erro do PyYAML cita o trecho do arquivo)
    assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)


ALIASES = {
    "alias-recursivo-na-secao": "agents: &a\n  - *a\n" + OK,
    "alias-recursivo-no-documento": f"agents:\n  - &a {{id: ok, nome: {MARKER}, model: m, prompt: p, self: *a}}\n",
    "merge-key-com-alias": f"agents:\n  - &b {{nome: {MARKER}}}\n  - {{<<: *b, id: x}}\n",
    "ancora-sem-alias": f"agents: &a0 [x, {MARKER}]\n",
}


@pytest.mark.parametrize("text", ALIASES.values(), ids=ALIASES.keys())
async def test_ancora_e_alias_sao_recusados_sem_travar_e_sem_conteudo(tmp_path: Path, text: str) -> None:
    start = time.monotonic()

    with pytest.raises(YamlConfigError, match=r"âncoras e aliases \(& e \*\) não são suportados \(linha \d+") as exc:
        await _agent_ids(_write(tmp_path, text))

    assert time.monotonic() - start < 5
    assert MARKER not in str(exc.value)


async def test_billion_laughs_e_recusado_rapido_sem_expandir(tmp_path: Path) -> None:
    levels = ["agents: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    levels += [f"teams: &a{i} [{', '.join([f'*a{i - 1}'] * 10)}]" for i in range(1, 2)]
    levels += [f"t{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 10)}]" for i in range(2, 12)]
    start = time.monotonic()

    with pytest.raises(YamlConfigError, match="âncoras e aliases"):
        await _write(tmp_path, "\n".join(levels) + "\n").section("agents")

    assert time.monotonic() - start < 5


async def test_merge_key_inline_e_chave_nao_textual_dentro_do_documento_nao_quebram_o_mapper(tmp_path: Path) -> None:
    """Merge key sem alias (mapa inline) continua sendo só YAML; não há âncora para expandir."""
    text = (
        "agents:\n"
        "  - {<<: {nome: Ok, factoryIaModel: ollama, model: m, prompt: p, active: true}, id: viaMerge}\n"
        "  - {1: a, id: numerica, nome: N, model: m, prompt: p, active: true}\n"
    )

    assert await _agent_ids(_write(tmp_path, text)) == ["viaMerge", "numerica"]


@pytest.mark.parametrize(
    "text",
    [
        f"agents:\n{OK}agents:\n  - {{id: {MARKER}, nome: N, model: m, prompt: p, active: true}}\n",
        f"agents:\n  - {{id: a, nome: N, nome: {MARKER}, model: m, prompt: p, active: true}}\n",
        f"agents:\n  - {{id: a, rag_config: {{doc_name: x, doc_name: {MARKER}}}}}\n",
    ],
    ids=["secao", "campo", "campo-aninhado"],
)
async def test_chave_repetida_e_recusada_com_linha_e_sem_conteudo(tmp_path: Path, text: str) -> None:
    """Antes o PyYAML ficava com a última em silêncio (duas seções agents: a primeira sumia)."""
    with pytest.raises(YamlConfigError, match=r"chave repetida num mapeamento \(linha \d+, coluna \d+\)") as exc:
        await _agent_ids(_write(tmp_path, text))

    assert MARKER not in str(exc.value)


async def test_itens_que_nao_sao_documento_sao_isolados_com_log(tmp_path: Path) -> None:
    logger = RecordingLogger()
    text = "agents: [x, x, x, x, x, x, x, x, x, x]\n"

    assert await _agent_ids(_write(tmp_path, text, logger), logger) == []
    assert len([r for r in logger.records if r.level == "error"]) == 10


async def test_active_como_yaml_1_1_e_so_o_booleano_true(tmp_path: Path) -> None:
    text = (
        "agents:\n"
        "  - {id: a1, nome: N, model: m, prompt: p, active: yes}\n"
        "  - {id: a2, nome: N, model: m, prompt: p, active: 'true'}\n"
        "  - {id: a3, nome: N, model: m, prompt: p, active: 1}\n"
        "  - {id: a4, nome: N, model: m, prompt: p, active: on}\n"
        "  - {id: a5, nome: N, model: m, prompt: p}\n"
    )

    assert await _agent_ids(_write(tmp_path, text)) == ["a1", "a4"]


async def test_tool_de_tipos_esquisitos_e_isolada(tmp_path: Path) -> None:
    text = (
        "tools:\n"
        f"  - {{id: t1, name: n, description: d, route: 'https://x.example', parameters: {MARKER}, active: true}}\n"
        "  - {id: t2, name: n, description: d, route: 'https://x.example', active: true}\n"
    )
    logger = RecordingLogger()

    tools = await YamlToolRepository(_write(tmp_path, text), logger=logger).get_all_active_tools()

    assert [t.id for t in tools] == ["t2"]
    assert all(MARKER not in repr(r) for r in logger.records)


# ── AppConfig ────────────────────────────────────────────────────────


def test_config_store_vazio_ou_so_espacos_e_mongo(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("", "   ", "MONGO", "Mongo "):
        monkeypatch.setenv("CONFIG_STORE", value)
        monkeypatch.delenv("CONFIG_YAML_PATH", raising=False)
        config = AppConfig.load()
        assert (config.config_store, config.config_yaml_path) == ("mongo", None)


def test_config_yaml_path_inexistente_ou_diretorio_falha_sem_ecoar_o_caminho(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "config.yaml").write_text("agents: []\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_STORE", "yaml")
    for raw in (f"{tmp_path}/nao/existe.yaml", f"{tmp_path}/config.yaml/../config.yaml/x", str(tmp_path)):
        monkeypatch.setenv("CONFIG_YAML_PATH", raw)
        with pytest.raises(ValueError, match="arquivo regular legível") as exc_info:
            AppConfig.load()
        assert str(tmp_path) not in str(exc_info.value)
