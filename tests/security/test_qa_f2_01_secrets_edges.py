"""QA F2-01: ``resolve_secret`` com caminhos hostis, ambiente hostil e corrida de symlink.

Complementa ``test_secret_resolution.py``: confusão de prefixo, loops, nomes longos, raiz
inválida, modos de leitura, fronteira de tamanho, ambiente com valor estranho e TOCTOU.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import pytest

from src.infrastructure.config import secrets
from src.infrastructure.config.secrets import MAX_SECRET_BYTES, SecretResolutionError, resolve_secret

MARKER = "sk-QA-F201-NAO-PODE-VAZAR"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "secrets"
    path.mkdir()
    return path


def _no_leak(exc: BaseException) -> None:
    current: BaseException | None = exc
    while current is not None:
        assert MARKER not in str(current) and MARKER not in repr(current.args)
        current = current.__cause__ or current.__context__


# ── confinamento ────────────────────────────────────────────────────────────────────────────────


def test_irmao_com_o_mesmo_prefixo_da_raiz_e_recusado(tmp_path: Path, root: Path):
    """``/x/secrets-evil`` começa com ``/x/secrets``: comparação por prefixo de texto deixaria passar."""
    evil = tmp_path / "secrets-evil"
    evil.mkdir()
    (evil / "k").write_text(MARKER)

    with pytest.raises(SecretResolutionError, match="fora de SECRETS_DIR") as exc_info:
        resolve_secret(f"file:{evil / 'k'}", secrets_dir=str(root))
    _no_leak(exc_info.value)


def test_raiz_que_e_symlink_vale_pelo_destino_real(tmp_path: Path, root: Path):
    (root / "k").write_text(MARKER)
    alias = tmp_path / "alias"
    alias.symlink_to(root)

    assert resolve_secret(f"file:{alias / 'k'}", secrets_dir=str(alias)) == MARKER
    assert resolve_secret(f"file:{root / 'k'}", secrets_dir=str(alias)) == MARKER


def test_diretorio_symlink_para_fora_e_recusado(tmp_path: Path, root: Path):
    outside = tmp_path / "fora"
    outside.mkdir()
    (outside / "k").write_text(MARKER)
    (root / "d").symlink_to(outside)

    with pytest.raises(SecretResolutionError, match="fora de SECRETS_DIR") as exc_info:
        resolve_secret(f"file:{root / 'd' / 'k'}", secrets_dir=str(root))
    _no_leak(exc_info.value)


def test_ponto_ponto_que_sai_e_volta_para_dentro_vale(root: Path):
    (root / "sub").mkdir()
    (root / "k").write_text(MARKER)

    assert resolve_secret(f"file:{root}/sub/../k", secrets_dir=str(root)) == MARKER


def test_barras_duplicadas_e_ponto_sao_normalizados(root: Path):
    (root / "k").write_text(MARKER)

    assert resolve_secret(f"file:{root}//./k", secrets_dir=str(root)) == MARKER


def test_referencia_para_a_propria_raiz_e_diretorio_nao_segredo(root: Path):
    with pytest.raises(SecretResolutionError, match="arquivo regular"):
        resolve_secret(f"file:{root}", secrets_dir=str(root))


def test_barra_final_no_arquivo_e_normalizada_pelo_realpath(root: Path):
    """Caracterização: ``k/`` vira ``k`` (realpath); inofensivo, só não é erro."""
    (root / "k").write_text(MARKER)

    assert resolve_secret(f"file:{root}/k/", secrets_dir=str(root)) == MARKER


def test_raiz_do_sistema_de_arquivos_como_referencia_e_recusada(root: Path):
    with pytest.raises(SecretResolutionError, match="fora de SECRETS_DIR"):
        resolve_secret("file:/", secrets_dir=str(root))


def test_dispositivo_fora_da_raiz_nao_e_aberto(root: Path):
    with pytest.raises(SecretResolutionError, match="fora de SECRETS_DIR"):
        resolve_secret("file:/dev/zero", secrets_dir=str(root))


def test_symlink_circular_e_erro_sem_travar(root: Path):
    (root / "a").symlink_to(root / "b")
    (root / "b").symlink_to(root / "a")

    with pytest.raises(SecretResolutionError, match="ilegível"):
        resolve_secret(f"file:{root / 'a'}", secrets_dir=str(root))


def test_symlink_pendente_dentro_da_raiz_e_nao_encontrado(root: Path):
    (root / "a").symlink_to(root / "nao-existe")

    with pytest.raises(SecretResolutionError, match="não encontrado"):
        resolve_secret(f"file:{root / 'a'}", secrets_dir=str(root))


def test_nome_de_arquivo_longo_demais_e_erro_claro(root: Path):
    with pytest.raises(SecretResolutionError):
        resolve_secret(f"file:{root}/{'a' * 300}", secrets_dir=str(root))
    with pytest.raises(SecretResolutionError):
        resolve_secret("file:/" + "a/" * 5000, secrets_dir=str(root))


@pytest.mark.parametrize("name", ["com espaço", "açaí", "-rf", "k;rm", "k\nx", "$(id)", "k*", "k‮"])
def test_nomes_de_arquivo_exoticos_sao_literais(root: Path, name: str):
    """Nada de shell/glob: o nome é opaco."""
    (root / name).write_text(MARKER)

    assert resolve_secret(f"file:{root}/{name}", secrets_dir=str(root)) == MARKER


def test_arquivo_que_nao_existe_com_nome_de_variavel_nao_cai_em_env(root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("K", MARKER)

    with pytest.raises(SecretResolutionError, match="não encontrado") as exc_info:
        resolve_secret(f"file:{root}/K", secrets_dir=str(root))
    _no_leak(exc_info.value)


# ── raiz (SECRETS_DIR) ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [" ", "relativo/dir", "./x", "~/secrets", "$HOME", "\t"])
def test_secrets_dir_nao_absoluto_e_erro(root: Path, value: str):
    """Rodada 2: a raiz vem por parâmetro (o ``AppConfig`` lê ``SECRETS_DIR``; ver test_app_config)."""
    (root / "k").write_text(MARKER)

    with pytest.raises(SecretResolutionError, match="SECRETS_DIR") as exc_info:
        resolve_secret(f"file:{root / 'k'}", secrets_dir=value)
    _no_leak(exc_info.value)


def test_sem_parametro_a_raiz_e_run_secrets(root: Path):
    (root / "k").write_text(MARKER)

    with pytest.raises(SecretResolutionError, match=r"fora de SECRETS_DIR \(/run/secrets\)"):
        resolve_secret(f"file:{root / 'k'}")


def test_parametro_secrets_dir_vale_sobre_o_ambiente(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    other = tmp_path / "outra"
    other.mkdir()
    (root / "k").write_text(MARKER)
    monkeypatch.setenv("SECRETS_DIR", str(other))

    assert resolve_secret(f"file:{root / 'k'}", secrets_dir=str(root)) == MARKER


def test_secrets_dir_explicito_vazio_e_erro_nao_default(root: Path):
    (root / "k").write_text(MARKER)

    with pytest.raises(SecretResolutionError, match="SECRETS_DIR"):
        resolve_secret(f"file:{root / 'k'}", secrets_dir="")


def test_secrets_dir_inexistente_e_erro_claro(tmp_path: Path):
    ghost = tmp_path / "nao-existe"

    with pytest.raises(SecretResolutionError) as exc_info:
        resolve_secret(f"file:{ghost}/k", secrets_dir=str(ghost))
    _no_leak(exc_info.value)


# ── conteúdo ────────────────────────────────────────────────────────────────────────────────────


def test_fronteira_do_tamanho_maximo(root: Path):
    exact = root / "exato"
    exact.write_bytes(b"x" * MAX_SECRET_BYTES)
    over = root / "acima"
    over.write_bytes(b"x" * (MAX_SECRET_BYTES + 1))

    assert resolve_secret(f"file:{exact}", secrets_dir=str(root)) == "x" * MAX_SECRET_BYTES
    with pytest.raises(SecretResolutionError, match="maior que"):
        resolve_secret(f"file:{over}", secrets_dir=str(root))


def test_conteudo_so_com_espacos_e_quebras_nas_pontas_e_aparado_e_o_miolo_fica(root: Path):
    (root / "k").write_text(f"\r\n\t {MARKER} com espaço\t\r\n")

    assert resolve_secret(f"file:{root / 'k'}", secrets_dir=str(root)) == f"{MARKER} com espaço"


def test_bom_utf8_nao_e_removido_e_fica_no_valor(root: Path):
    """Caracterização: BOM vira parte do segredo (U+FEFF não é espaço para ``str.strip``)."""
    (root / "k").write_bytes(b"\xef\xbb\xbf" + MARKER.encode())

    assert resolve_secret(f"file:{root / 'k'}", secrets_dir=str(root)) == "﻿" + MARKER


def test_nul_no_conteudo_e_devolvido_como_texto(root: Path):
    """Caracterização: NUL é UTF-8 válido; o consumidor (header HTTP) rejeitará."""
    (root / "k").write_bytes(b"ab\x00cd")

    assert resolve_secret(f"file:{root / 'k'}", secrets_dir=str(root)) == "ab\x00cd"


def test_arquivo_modo_zero_e_erro_sem_conteudo(root: Path):
    if os.geteuid() == 0:
        pytest.skip("root lê qualquer arquivo")
    path = root / "k"
    path.write_text(MARKER)
    path.chmod(0)

    with pytest.raises(SecretResolutionError, match="ilegível") as exc_info:
        resolve_secret(f"file:{path}", secrets_dir=str(root))
    _no_leak(exc_info.value)


def test_fd_nao_vaza_em_erro_nem_em_sucesso(root: Path):
    (root / "ok").write_text(MARKER)
    (root / "vazio").write_text("")
    (root / "grande").write_bytes(b"x" * (MAX_SECRET_BYTES + 1))
    (root / "d").mkdir()
    before = len(os.listdir("/proc/self/fd"))

    for _ in range(50):
        resolve_secret(f"file:{root / 'ok'}", secrets_dir=str(root))
        for name in ("vazio", "grande", "d", "nao-existe"):
            with pytest.raises(SecretResolutionError):
                resolve_secret(f"file:{root / name}", secrets_dir=str(root))

    assert len(os.listdir("/proc/self/fd")) <= before + 1


# ── env: ────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["  ", "\n", "\t \r\n"])
def test_env_so_com_espacos_e_ausente(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv("QA_F201_API_KEY", value)

    with pytest.raises(SecretResolutionError, match="QA_F201_API_KEY"):
        resolve_secret("env:QA_F201_API_KEY")


def test_env_com_valor_multilinha_devolve_so_aparado(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("QA_F201_API_KEY", f"\n{MARKER}\nsegunda-linha\n")

    assert resolve_secret("env:QA_F201_API_KEY") == f"{MARKER}\nsegunda-linha"


def test_env_nome_em_minusculas_e_recusado(monkeypatch: pytest.MonkeyPatch):
    """Só ``<PROVEDOR>_API_KEY`` em maiúsculas (rodada 2): minúsculas nem chegam a ler o ambiente."""
    monkeypatch.setenv("qa_f201_api_key", MARKER)

    with pytest.raises(SecretResolutionError, match="api_key_ref inválido") as exc_info:
        resolve_secret("env:qa_f201_api_key")
    _no_leak(exc_info.value)


@pytest.mark.parametrize(
    "ref", ["env:QA F201", "env:A=B", "env:A\x00B", "env:../x", "env:$HOME", "env:${HOME}", "env:A,B"]
)
def test_env_nome_com_sintaxe_de_shell_ou_igual_e_recusado(ref: str):
    with pytest.raises(SecretResolutionError, match="api_key_ref inválido"):
        resolve_secret(ref)


def test_env_nao_registra_nada_nem_em_debug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, root: Path
):
    monkeypatch.setenv("QA_F201_API_KEY", MARKER)
    (root / "k").write_text(MARKER)

    with caplog.at_level(logging.DEBUG):
        resolve_secret("env:QA_F201_API_KEY")
        resolve_secret(f"file:{root / 'k'}", secrets_dir=str(root))
        with pytest.raises(SecretResolutionError):
            resolve_secret("env:QA_F201_AUSENTE_API_KEY")

    assert caplog.records == []


def test_referencia_nao_string_e_erro_de_resolucao():
    for ref in (None, 123, b"env:K", ["env:K"]):
        with pytest.raises(SecretResolutionError):
            resolve_secret(ref)  # type: ignore[arg-type]


# ── corrida (TOCTOU) ────────────────────────────────────────────────────────────────────────────


def test_symlink_trocado_no_ultimo_componente_apos_a_checagem_nao_vaza(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
):
    """Entre a checagem e a abertura o arquivo vira symlink para fora: ``O_NOFOLLOW`` recusa."""
    outside = tmp_path / "fora"
    outside.write_text(MARKER)
    target = root / "k"
    target.write_text("dentro")
    real_open = os.open
    swapped: list[bool] = []

    def swap_then_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path) == str(target) and not swapped:
            swapped.append(True)
            target.unlink()
            target.symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(secrets.os, "open", swap_then_open)

    with pytest.raises(SecretResolutionError) as exc_info:
        resolve_secret(f"file:{target}", secrets_dir=str(root))

    assert swapped
    _no_leak(exc_info.value)


def test_arquivo_trocado_por_fifo_apos_a_checagem_nao_trava(root: Path, monkeypatch: pytest.MonkeyPatch):
    target = root / "k"
    target.write_text("x")
    real_open = os.open
    swapped: list[bool] = []

    def swap_then_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path) == str(target) and not swapped:
            swapped.append(True)
            target.unlink()
            os.mkfifo(target)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(secrets.os, "open", swap_then_open)
    result: list[BaseException | str] = []

    def run() -> None:
        try:
            result.append(resolve_secret(f"file:{target}", secrets_dir=str(root)))
        except BaseException as exc:  # noqa: BLE001 - o teste inspeciona qualquer desfecho
            result.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive(), "resolve_secret travou num FIFO"
    assert isinstance(result[0], SecretResolutionError)


def test_diretorio_intermediario_trocado_por_symlink_apos_a_checagem_nao_escapa(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
):
    outside = tmp_path / "fora"
    outside.mkdir()
    (outside / "k").write_text(MARKER)
    inside = root / "d"
    inside.mkdir()
    (inside / "k").write_text("dentro")
    real_open = os.open
    swapped: list[bool] = []

    def swap_then_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path) == str(inside / "k") and not swapped:
            swapped.append(True)
            os.rename(inside, root / "d.velho")
            os.symlink(outside, inside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(secrets.os, "open", swap_then_open)

    try:
        value = resolve_secret(f"file:{inside / 'k'}", secrets_dir=str(root))
    except SecretResolutionError:
        value = None  # recusar é o comportamento correto

    assert swapped
    assert value != MARKER, "o segredo de FORA da raiz foi lido"
