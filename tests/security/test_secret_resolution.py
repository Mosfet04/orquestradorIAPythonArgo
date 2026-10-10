"""F2-01: ``resolve_secret`` (``env:VAR`` e ``file:/caminho`` confinado a ``SECRETS_DIR``).

O valor do segredo nunca aparece em mensagem de erro, exceção encadeada ou log.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import pytest

from src.infrastructure.config import secrets
from src.infrastructure.config.secrets import (
    DEFAULT_SECRETS_DIR,
    MAX_SECRET_BYTES,
    SecretResolutionError,
    resolve_secret,
)

MARKER = "sk-VALOR-QUE-NAO-PODE-VAZAR"


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    root = tmp_path / "secrets"
    root.mkdir()
    return root


def _write(path: Path, content: str | bytes) -> Path:
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return path


def _assert_no_leak(exc: BaseException) -> None:
    """Nem a mensagem nem a cadeia de exceções carregam o valor."""
    current: BaseException | None = exc
    while current is not None:
        assert MARKER not in str(current)
        assert MARKER not in repr(current.args)
        current = current.__cause__ or current.__context__


# ── env: ────────────────────────────────────────────────────────────


def test_env_resolve_o_valor(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    monkeypatch.setenv("ORQ_TEST_LLM_API_KEY", f"{MARKER}\n")

    with caplog.at_level(logging.DEBUG):
        assert resolve_secret("env:ORQ_TEST_LLM_API_KEY") == MARKER
    assert MARKER not in caplog.text


@pytest.mark.parametrize("value", [None, "", "   "], ids=["ausente", "vazia", "em-branco"])
def test_env_ausente_ou_vazia_e_erro_com_o_nome(monkeypatch: pytest.MonkeyPatch, value: str | None):
    if value is None:
        monkeypatch.delenv("ORQ_TEST_LLM_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ORQ_TEST_LLM_API_KEY", value)

    with pytest.raises(SecretResolutionError, match="ORQ_TEST_LLM_API_KEY"):
        resolve_secret("env:ORQ_TEST_LLM_API_KEY")


@pytest.mark.parametrize("ref", [f"env:{MARKER}", "env:", "env:1A", MARKER, "file:relativo", "vault:x"])
def test_referencia_invalida_e_erro_sem_ecoar(ref: str):
    with pytest.raises(SecretResolutionError, match="api_key_ref") as exc_info:
        resolve_secret(ref)
    _assert_no_leak(exc_info.value)


@pytest.mark.parametrize("name", ["API_KEY_ADMIN", "API_KEY_RUN", "MONGO_CONNECTION_STRING", "HOME"])
def test_env_fora_do_padrao_de_chave_de_provider_nao_e_lida(monkeypatch: pytest.MonkeyPatch, name: str):
    """Quem grava config não é confiável para ler segredos arbitrários do processo."""
    monkeypatch.setenv(name, MARKER)

    with pytest.raises(SecretResolutionError, match="_API_KEY") as exc_info:
        resolve_secret(f"env:{name}")
    _assert_no_leak(exc_info.value)


def test_erro_de_resolucao_e_value_error():
    """Quem já trata ``ValueError`` de configuração não precisa conhecer a classe nova."""
    assert issubclass(SecretResolutionError, ValueError)


# ── file: ───────────────────────────────────────────────────────────


def test_file_resolve_o_valor_sem_a_quebra_de_linha(secrets_dir: Path, caplog: pytest.LogCaptureFixture):
    path = _write(secrets_dir / "openai", f"{MARKER}\n")

    with caplog.at_level(logging.DEBUG):
        assert resolve_secret(f"file:{path}", secrets_dir=str(secrets_dir)) == MARKER
    assert MARKER not in caplog.text


def test_file_aceita_symlink_que_fica_dentro_da_raiz(secrets_dir: Path):
    """Layout de secret do Kubernetes: ``chave -> ..data/chave``, tudo dentro do diretório montado."""
    data = secrets_dir / "..data"
    data.mkdir()
    _write(data / "openai", MARKER)
    (secrets_dir / "openai").symlink_to(Path("..data") / "openai")

    assert resolve_secret(f"file:{secrets_dir / 'openai'}", secrets_dir=str(secrets_dir)) == MARKER


def test_file_nao_le_secrets_dir_do_ambiente(secrets_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """A raiz vem do parâmetro (o ``AppConfig`` lê e valida ``SECRETS_DIR``); sem ele, ``/run/secrets``."""
    path = _write(secrets_dir / "k", MARKER)
    monkeypatch.setenv("SECRETS_DIR", str(secrets_dir))

    with pytest.raises(SecretResolutionError, match=r"fora de SECRETS_DIR \(/run/secrets\)"):
        resolve_secret(f"file:{path}")


def test_file_fora_da_raiz_padrao_e_recusado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SECRETS_DIR", raising=False)
    path = _write(tmp_path / "solto", MARKER)

    with pytest.raises(SecretResolutionError, match="fora de SECRETS_DIR") as exc_info:
        resolve_secret(f"file:{path}")
    assert DEFAULT_SECRETS_DIR == "/run/secrets"
    _assert_no_leak(exc_info.value)


def test_file_symlink_para_fora_da_raiz_e_recusado(secrets_dir: Path, tmp_path: Path):
    outside = _write(tmp_path / "fora", MARKER)
    (secrets_dir / "openai").symlink_to(outside)

    with pytest.raises(SecretResolutionError, match="fora de SECRETS_DIR") as exc_info:
        resolve_secret(f"file:{secrets_dir / 'openai'}", secrets_dir=str(secrets_dir))
    _assert_no_leak(exc_info.value)


def test_file_com_ponto_ponto_para_fora_e_recusado(secrets_dir: Path, tmp_path: Path):
    _write(tmp_path / "fora", MARKER)

    with pytest.raises(SecretResolutionError, match="fora de SECRETS_DIR") as exc_info:
        resolve_secret(f"file:{secrets_dir}/../fora", secrets_dir=str(secrets_dir))
    _assert_no_leak(exc_info.value)


def test_file_ausente_e_erro(secrets_dir: Path):
    with pytest.raises(SecretResolutionError, match="não encontrado"):
        resolve_secret(f"file:{secrets_dir / 'nao-existe'}", secrets_dir=str(secrets_dir))


def test_file_diretorio_e_erro(secrets_dir: Path):
    (secrets_dir / "subdir").mkdir()

    with pytest.raises(SecretResolutionError, match="arquivo regular"):
        resolve_secret(f"file:{secrets_dir / 'subdir'}", secrets_dir=str(secrets_dir))


def test_file_fifo_e_recusado_sem_bloquear(secrets_dir: Path):
    fifo = secrets_dir / "fifo"
    os.mkfifo(fifo)
    outcome: list[BaseException | str] = []

    def run() -> None:
        try:
            outcome.append(resolve_secret(f"file:{fifo}", secrets_dir=str(secrets_dir)))
        except SecretResolutionError as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), "resolve_secret travou abrindo o FIFO"
    assert len(outcome) == 1 and isinstance(outcome[0], SecretResolutionError)
    assert "arquivo regular" in str(outcome[0])


def _swap_dir_on_open(monkeypatch: pytest.MonkeyPatch, inside: Path, outside: Path) -> list[bool]:
    """Troca ``inside`` por symlink para ``outside`` entre a checagem e o ``os.open`` (corrida)."""
    real_open = os.open
    swapped: list[bool] = []

    def swap_then_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path).startswith(str(inside) + os.sep) and not swapped:
            swapped.append(True)
            os.rename(inside, inside.with_name(inside.name + ".velho"))
            os.symlink(outside, inside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(secrets.os, "open", swap_then_open)
    return swapped


def test_diretorio_trocado_por_symlink_depois_da_checagem_nao_escapa(
    secrets_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    outside = tmp_path / "fora"
    outside.mkdir()
    _write(outside / "k", MARKER)
    inside = secrets_dir / "d"
    inside.mkdir()
    _write(inside / "k", "dentro")
    swapped = _swap_dir_on_open(monkeypatch, inside, outside)

    with pytest.raises(SecretResolutionError, match="fora de SECRETS_DIR") as exc_info:
        resolve_secret(f"file:{inside / 'k'}", secrets_dir=str(secrets_dir))
    assert swapped
    _assert_no_leak(exc_info.value)


def _raise_oserror(*_args: object, **_kwargs: object) -> str:
    raise OSError("sem /proc")


def test_sem_proc_falha_fechado(secrets_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Sem ``/proc/self/fd`` não dá para conferir o arquivo aberto: recusa em vez de confiar."""
    path = _write(secrets_dir / "k", MARKER)
    monkeypatch.setattr(secrets.os, "readlink", _raise_oserror)

    with pytest.raises(SecretResolutionError, match="/proc/self/fd") as exc_info:
        resolve_secret(f"file:{path}", secrets_dir=str(secrets_dir))
    _assert_no_leak(exc_info.value)


def test_raiz_do_sistema_como_secrets_dir_e_recusada(tmp_path: Path):
    path = _write(tmp_path / "k", MARKER)
    alias = tmp_path / "raiz"
    alias.symlink_to("/")

    for root in ("/", str(alias)):
        with pytest.raises(SecretResolutionError, match="SECRETS_DIR") as exc_info:
            resolve_secret(f"file:{path}", secrets_dir=root)
        _assert_no_leak(exc_info.value)


def test_file_grande_demais_e_erro(secrets_dir: Path):
    path = _write(secrets_dir / "grande", MARKER + "x" * MAX_SECRET_BYTES)

    with pytest.raises(SecretResolutionError, match="maior que") as exc_info:
        resolve_secret(f"file:{path}", secrets_dir=str(secrets_dir))
    _assert_no_leak(exc_info.value)


@pytest.mark.parametrize("content", ["", " \n\t\n"], ids=["vazio", "em-branco"])
def test_file_vazio_e_erro(secrets_dir: Path, content: str):
    path = _write(secrets_dir / "vazio", content)

    with pytest.raises(SecretResolutionError, match="vazio"):
        resolve_secret(f"file:{path}", secrets_dir=str(secrets_dir))


def test_file_nao_utf8_e_erro_sem_os_bytes(secrets_dir: Path):
    path = _write(secrets_dir / "binario", MARKER.encode() + b"\xff\xfe")

    with pytest.raises(SecretResolutionError, match="UTF-8") as exc_info:
        resolve_secret(f"file:{path}", secrets_dir=str(secrets_dir))
    _assert_no_leak(exc_info.value)
    assert exc_info.value.__context__ is None


def test_file_sem_permissao_de_leitura_e_erro(secrets_dir: Path):
    if os.geteuid() == 0:
        pytest.skip("root lê arquivo sem permissão")
    path = _write(secrets_dir / "fechado", MARKER)
    path.chmod(0)

    with pytest.raises(SecretResolutionError, match="ilegível"):
        resolve_secret(f"file:{path}", secrets_dir=str(secrets_dir))


def test_secrets_dir_relativo_e_erro(secrets_dir: Path):
    path = _write(secrets_dir / "k", MARKER)

    with pytest.raises(SecretResolutionError, match="SECRETS_DIR"):
        resolve_secret(f"file:{path}", secrets_dir="secrets")
