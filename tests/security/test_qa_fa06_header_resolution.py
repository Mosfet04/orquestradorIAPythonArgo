"""QA do FA-06, segurança: resolução de header por referência na chamada da tool HTTP.

O upstream é um ``httpx.MockTransport`` (nada sai da máquina). Cobre o que o dev não cobriu: ``file:`` com
travessia de caminho e symlink para fora de ``SECRETS_DIR``, rotação do arquivo entre chamadas, valor resolvido com
``{placeholder}``, redirect com header de credencial (não é seguido), exceção de rede que cita o header, variável
vazia e a tool que só tem header em texto (não paga a resolução). Valores são de teste.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.infrastructure.runtime.agno import http_tool_factory
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from tests.fakes.http_upstream import RecordingUpstream
from tests.fakes.logger import RecordingLogger

SECRET = "valor-de-teste-resolvido"  # noqa: S105 - marcador de vazamento, não é segredo
_PLACEHOLDER = "https://api.interna.example/cep/{cep}"
OUTSIDE = "valor-de-teste-fora-da-raiz"


def _tool(headers: dict[str, str], *, route: str = "https://api.interna.example/cep") -> Tool:
    return Tool(
        id="cep",
        name="cep",
        description="Busca CEP",
        route=route,
        http_method=HttpMethod.GET,
        parameters=[ToolParameter(name="cep", type=ParameterType.STRING, description="CEP", required=True)]
        if "{cep}" in route
        else [],
        headers=headers,
    )


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> RecordingUpstream:
    return RecordingUpstream().install(monkeypatch)


async def _call(tool: Tool, logger: RecordingLogger, secrets_dir: str, **arguments: Any) -> str:
    [function] = await HttpToolFactory(logger, secrets_dir=secrets_dir).create_tools_from_configs([tool])
    assert function.entrypoint is not None
    return await function.entrypoint(**arguments)


# ── file: confinado a SECRETS_DIR ─────────────────────────────────────────────────────────────────


async def test_file_com_travessia_para_fora_de_secrets_dir_nao_resolve_e_nao_chama_o_upstream(
    upstream: RecordingUpstream, tmp_path: Path
):
    root = tmp_path / "secrets"
    root.mkdir()
    (tmp_path / "fora").write_text(OUTSIDE)
    logger = RecordingLogger()

    result = await _call(_tool({"Authorization": f"file:{root}/../fora"}), logger, str(root))

    assert upstream.seen == []
    assert result == "Erro na tool cep: header por referência não resolvido (Authorization)"
    assert OUTSIDE not in result + repr(logger.records)


async def test_file_symlink_para_fora_de_secrets_dir_nao_resolve(upstream: RecordingUpstream, tmp_path: Path):
    root = tmp_path / "secrets"
    root.mkdir()
    (tmp_path / "fora").write_text(OUTSIDE)
    (root / "link").symlink_to(tmp_path / "fora")
    logger = RecordingLogger()

    result = await _call(_tool({"X-Api-Key": f"file:{root}/link"}), logger, str(root))

    assert upstream.seen == []
    assert OUTSIDE not in result + repr(logger.records)
    [error] = [r for r in logger.records if r.level == "error"]
    assert error.context == {"tool_id": "cep", "header": "X-Api-Key", "error_type": "SecretResolutionError"}


async def test_file_symlink_para_dentro_de_secrets_dir_resolve(upstream: RecordingUpstream, tmp_path: Path):
    root = tmp_path / "secrets"
    root.mkdir()
    (root / "real").write_text(SECRET)
    (root / "link").symlink_to(root / "real")

    await _call(_tool({"X-Api-Key": f"file:{root}/link"}), RecordingLogger(), str(root))

    assert [r.headers["x-api-key"] for r in upstream.seen] == [SECRET]


async def test_file_aponta_para_diretorio_ou_arquivo_vazio_nao_chama_o_upstream(
    upstream: RecordingUpstream, tmp_path: Path
):
    root = tmp_path / "secrets"
    (root / "dir").mkdir(parents=True)
    (root / "vazio").write_text("  \n")

    for target in ("dir", "vazio"):
        result = await _call(_tool({"X-Api-Key": f"file:{root}/{target}"}), RecordingLogger(), str(root))
        assert result.startswith("Erro na tool cep: header por referência não resolvido")

    assert upstream.seen == []


# ── rotação ───────────────────────────────────────────────────────────────────────────────────────


async def test_rotacao_do_arquivo_vale_na_chamada_seguinte_com_a_mesma_tool(
    upstream: RecordingUpstream, tmp_path: Path
):
    (tmp_path / "cep").write_text("valor-de-teste-v1")
    [function] = await HttpToolFactory(RecordingLogger(), secrets_dir=str(tmp_path)).create_tools_from_configs(
        [_tool({"X-Api-Key": f"file:{tmp_path}/cep"})]
    )
    assert function.entrypoint is not None

    await function.entrypoint()
    (tmp_path / "cep").write_text("valor-de-teste-v2\n")
    await function.entrypoint()
    (tmp_path / "cep").unlink()
    gone = await function.entrypoint()

    assert [r.headers["x-api-key"] for r in upstream.seen] == ["valor-de-teste-v1", "valor-de-teste-v2"]
    assert gone.startswith("Erro na tool cep: header por referência não resolvido")  # falha fechado, não usa o antigo


async def test_variavel_de_ambiente_vazia_ou_removida_na_rotacao_nao_envia_o_header(
    upstream: RecordingUpstream, monkeypatch: pytest.MonkeyPatch
):
    [function] = await HttpToolFactory(RecordingLogger()).create_tools_from_configs(
        [_tool({"Authorization": "env:CEP_API_KEY"})]
    )
    assert function.entrypoint is not None

    monkeypatch.setenv("CEP_API_KEY", SECRET)
    await function.entrypoint()
    monkeypatch.setenv("CEP_API_KEY", "   ")
    blank = await function.entrypoint()
    monkeypatch.delenv("CEP_API_KEY")
    missing = await function.entrypoint()

    assert [r.headers["authorization"] for r in upstream.seen] == [SECRET]
    assert blank == missing == "Erro na tool cep: header por referência não resolvido (Authorization)"


# ── o valor resolvido é opaco ─────────────────────────────────────────────────────────────────────


async def test_valor_resolvido_com_placeholder_vai_como_esta_e_nao_e_substituido(
    upstream: RecordingUpstream, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CEP_API_KEY", "abc{cep}def")

    tool = _tool({"Authorization": "env:CEP_API_KEY"}, route=_PLACEHOLDER)
    await _call(tool, RecordingLogger(), "/run/secrets", cep="01001000")

    [request] = upstream.seen
    assert request.headers["authorization"] == "abc{cep}def"


async def test_placeholder_no_caminho_do_file_nao_e_interpolado_com_o_argumento(
    upstream: RecordingUpstream, tmp_path: Path
):
    """``file:<raiz>/{cep}`` lê o arquivo literal ``{cep}``; o argumento da chamada não escolhe o caminho."""
    (tmp_path / "{cep}").write_text(SECRET)
    (tmp_path / "01001000").write_text("valor-de-teste-do-argumento")

    tool = _tool({"X-Api-Key": f"file:{tmp_path}/{{cep}}"}, route=_PLACEHOLDER)
    await _call(tool, RecordingLogger(), str(tmp_path), cep="01001000")

    [request] = upstream.seen
    assert request.headers["x-api-key"] == SECRET
    assert request.url.path == "/cep/01001000"


async def test_argumento_do_modelo_nao_sobrescreve_nem_le_o_header_resolvido(
    upstream: RecordingUpstream, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CEP_API_KEY", SECRET)
    logger = RecordingLogger()

    tool = _tool({"Authorization": "env:CEP_API_KEY"}, route=_PLACEHOLDER)
    result = await _call(tool, logger, "/run/secrets", cep="Authorization")

    [request] = upstream.seen
    assert request.headers["authorization"] == SECRET
    assert request.url.path == "/cep/Authorization"
    assert SECRET not in result + repr(logger.records)


# ── redirect e exceção de rede ────────────────────────────────────────────────────────────────────


async def test_redirect_com_header_de_credencial_nao_e_seguido_e_a_credencial_nao_sai_para_outro_host(
    upstream: RecordingUpstream, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CEP_API_KEY", SECRET)
    upstream.handler = lambda request: httpx.Response(307, headers={"Location": "https://coletor.example.invalid/x"})
    logger = RecordingLogger()

    result = await _call(_tool({"Authorization": "env:CEP_API_KEY"}), logger, "/run/secrets")

    assert [r.url.host for r in upstream.seen] == ["api.interna.example"]  # um só request, ao host da route
    assert result.startswith("Erro HTTP 307")
    assert SECRET not in result + repr(logger.records)


async def test_excecao_de_rede_que_cita_o_header_nao_vaza_para_o_resultado_nem_para_o_log(
    upstream: RecordingUpstream, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CEP_API_KEY", SECRET)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"falha com Authorization: {request.headers['authorization']}")

    upstream.handler = boom
    logger = RecordingLogger()

    result = await _call(_tool({"Authorization": "env:CEP_API_KEY"}), logger, "/run/secrets")

    assert upstream.seen  # a chamada saiu com o valor, e a falha foi tratada
    assert "ConnectError" in result
    assert SECRET not in result + repr(logger.records)


async def test_upstream_que_ecoa_o_header_na_resposta_de_erro_e_dado_da_propria_tool(
    upstream: RecordingUpstream, monkeypatch: pytest.MonkeyPatch
):
    """Documenta o limite: o corpo do upstream (host do operador) segue como dado da tool; o log não o repete."""
    monkeypatch.setenv("CEP_API_KEY", SECRET)
    upstream.handler = lambda request: httpx.Response(400, text=f"eco {request.headers['authorization']}")
    logger = RecordingLogger()

    result = await _call(_tool({"Authorization": "env:CEP_API_KEY"}), logger, "/run/secrets")

    assert result.startswith("Erro HTTP 400")
    assert SECRET not in repr(logger.records)


# ── custo e fora do loop ──────────────────────────────────────────────────────────────────────────


async def test_tool_sem_referencia_nao_le_arquivo_nem_ambiente(
    upstream: RecordingUpstream, monkeypatch: pytest.MonkeyPatch
):
    def boom(*_: object, **__: object) -> str:
        raise AssertionError("não deveria resolver segredo")

    monkeypatch.setattr(http_tool_factory, "resolve_secret", boom)

    await _call(_tool({"Accept": "text/plain", "X-Nome": "valor"}), RecordingLogger(), "/run/secrets")

    assert [r.headers["x-nome"] for r in upstream.seen] == ["valor"]


async def test_leitura_do_arquivo_roda_fora_do_event_loop(
    upstream: RecordingUpstream, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    (tmp_path / "cep").write_text(SECRET)
    loop_thread = threading.get_ident()
    threads: list[int] = []
    real = http_tool_factory.resolve_secret

    def spy(ref: str, **kwargs: Any) -> str:
        threads.append(threading.get_ident())
        return real(ref, **kwargs)

    monkeypatch.setattr(http_tool_factory, "resolve_secret", spy)

    await _call(_tool({"X-Api-Key": f"file:{tmp_path}/cep"}), RecordingLogger(), str(tmp_path))

    assert threads and loop_thread not in threads
