"""FA-06: header de tool por referência (``env:``/``file:``) resolvido só na chamada, nunca no log nem no resultado.

O upstream é um ``httpx.MockTransport`` (nada sai da máquina). Valores são de teste.
"""

from __future__ import annotations

import pytest

from src.domain.entities.tool import HttpMethod, Tool
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from tests.fakes.http_upstream import RecordingUpstream
from tests.fakes.logger import RecordingLogger

SECRET = "valor-de-teste-resolvido"  # noqa: S105 - marcador de vazamento, não é segredo


def _tool(headers: dict[str, str]) -> Tool:
    return Tool(
        id="cep",
        name="cep",
        description="Busca CEP",
        route="https://api.interna.example/cep",
        http_method=HttpMethod.GET,
        parameters=[],
        headers=headers,
    )


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> RecordingUpstream:
    """Requests que a tool mandou (``upstream.seen``); o upstream responde 200."""
    return RecordingUpstream().install(monkeypatch)


async def _call(tool: Tool, logger: RecordingLogger, secrets_dir: str = "/run/secrets") -> str:
    [function] = await HttpToolFactory(logger, secrets_dir=secrets_dir).create_tools_from_configs([tool])
    assert function.entrypoint is not None
    return await function.entrypoint()


async def test_referencia_env_e_resolvida_a_cada_chamada_e_nao_na_criacao(upstream, monkeypatch):
    monkeypatch.delenv("CEP_API_KEY", raising=False)
    logger = RecordingLogger()
    [function] = await HttpToolFactory(logger).create_tools_from_configs(
        [_tool({"Authorization": "env:CEP_API_KEY", "Accept": "text/plain"})]
    )
    assert function.entrypoint is not None

    monkeypatch.setenv("CEP_API_KEY", SECRET)  # só existe depois de criar a tool
    await function.entrypoint()
    monkeypatch.setenv("CEP_API_KEY", "valor-de-teste-rotacionado")
    await function.entrypoint()

    assert [r.headers["authorization"] for r in upstream.seen] == [SECRET, "valor-de-teste-rotacionado"]
    assert [r.headers["accept"] for r in upstream.seen] == ["text/plain", "text/plain"]
    assert SECRET not in repr(logger.records)


async def test_referencia_file_le_o_arquivo_em_secrets_dir(upstream, tmp_path):
    (tmp_path / "cep").write_text(f"{SECRET}\n")
    logger = RecordingLogger()

    result = await _call(_tool({"X-Api-Key": f"file:{tmp_path}/cep"}), logger, secrets_dir=str(tmp_path))

    assert [r.headers["x-api-key"] for r in upstream.seen] == [SECRET]
    assert SECRET not in result and SECRET not in repr(logger.records)


@pytest.mark.parametrize("ref", ["env:CEP_AUSENTE_API_KEY", "file:/run/secrets/cep-ausente", "env:minusculo"])
async def test_referencia_que_nao_resolve_nao_chama_o_upstream_e_loga_so_o_tipo(upstream, monkeypatch, ref):
    monkeypatch.delenv("CEP_AUSENTE_API_KEY", raising=False)
    logger = RecordingLogger()

    result = await _call(_tool({"Authorization": ref}), logger)

    assert upstream.seen == []
    assert result == "Erro na tool cep: header por referência não resolvido (Authorization)"
    [record] = [r for r in logger.records if r.level == "error"]
    assert record.context == {"tool_id": "cep", "header": "Authorization", "error_type": "SecretResolutionError"}
    assert ref.partition(":")[2] not in repr(logger.records) + result


async def test_header_em_texto_segue_enviado_como_esta(upstream):
    """Compatibilidade: header legado em texto (sem ``env:``/``file:``) vai como está."""
    await _call(_tool({"Authorization": "Bearer legado"}), RecordingLogger())

    assert [r.headers["authorization"] for r in upstream.seen] == ["Bearer legado"]

