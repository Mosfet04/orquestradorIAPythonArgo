"""Resolução de segredo por referência: ``env:<PROVEDOR>_API_KEY`` e ``file:/caminho`` (F2-01).

Função simples (sem porta): despacho por esquema. O formato da referência é o do domínio
(``parse_secret_ref``: ``env:`` só aceita nomes ``<PROVEDOR>_API_KEY``). O valor nunca é logado
nem entra em mensagem de erro ou exceção encadeada. ``file:`` fica confinado à raiz
``secrets_dir`` (o ``AppConfig`` lê ``SECRETS_DIR``; default ``/run/secrets``, onde Docker e
Kubernetes montam secrets): symlink vale só se o destino final também estiver dentro, e o
arquivo efetivamente aberto é conferido de novo em ``/proc/self/fd`` (corrida entre a checagem e
o ``open``). Sem ``/proc`` (ex.: macOS) ``file:`` falha fechado; use ``env:`` nesse caso.

Síncrona e com I/O de arquivo: no caminho async, chame via ``asyncio.to_thread``.
"""

from __future__ import annotations

import os
import stat

from src.domain.entities.model_config import ENV_SCHEME, parse_secret_ref

DEFAULT_SECRETS_DIR = "/run/secrets"
MAX_SECRET_BYTES = 64 * 1024


class SecretResolutionError(ValueError):
    """Referência de segredo que não resolve. A mensagem nunca traz o valor."""


def resolve_secret(ref: str, *, secrets_dir: str = DEFAULT_SECRETS_DIR) -> str:
    """Valor do segredo apontado por ``ref``, sem espaços/quebras de linha nas pontas.

    Erro: ``SecretResolutionError`` (subclasse de ``ValueError``) com o nome da variável ou o
    caminho, nunca o conteúdo.
    """
    try:
        scheme, target = parse_secret_ref(ref)
    except ValueError as exc:
        raise SecretResolutionError(str(exc)) from None
    if scheme == ENV_SCHEME:
        return _from_env(target)
    return _from_file(target, secrets_dir)


def _from_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise SecretResolutionError(
            f"variável de ambiente {name} (api_key_ref) ausente ou vazia"
        )
    return value


def _inside(path: str, root: str) -> bool:
    return os.path.commonpath([root, path]) == root


def _from_file(path: str, root: str) -> str:
    if not os.path.isabs(root):
        raise SecretResolutionError("SECRETS_DIR deve ser um caminho absoluto")
    real_root = os.path.realpath(root)
    if real_root == os.path.sep:
        raise SecretResolutionError("SECRETS_DIR não pode ser a raiz do sistema de arquivos")
    real_path = os.path.realpath(path)
    if not _inside(real_path, real_root):
        raise SecretResolutionError(
            f"arquivo do api_key_ref fora de SECRETS_DIR ({real_root}): {path}"
        )
    data = _read_regular_file(real_path, path, real_root)
    value: str | None
    try:
        value = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        value = None  # erro levantado fora do except: a exceção do codec carrega os bytes
    if value is None:
        raise SecretResolutionError(f"arquivo do api_key_ref não é texto UTF-8: {path}")
    if not value:
        raise SecretResolutionError(f"arquivo do api_key_ref vazio: {path}")
    return value


def _escaped_root(fd: int, real_root: str, path: str) -> bool:
    """O fd aberto caiu fora da raiz? (diretório do caminho trocado por symlink na corrida)

    Destino real do fd em ``/proc/self/fd``. Sem ele, falha fechado: comparar inode só
    estreitaria a janela (troca logo depois do ``realpath`` passaria).
    """
    try:
        opened = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        raise SecretResolutionError(
            "api_key_ref file: exige /proc/self/fd (Linux) para conferir o arquivo aberto; "
            f"use env: neste sistema: {path}"
        ) from None
    return not _inside(opened, real_root)


def _read_regular_file(real_path: str, path: str, real_root: str) -> bytes:
    """Lê até ``MAX_SECRET_BYTES`` de um arquivo regular, sem seguir symlink no último passo.

    ``O_NONBLOCK``: abrir um FIFO não trava esperando escritor (ele é recusado em seguida).
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(real_path, flags)
    except FileNotFoundError:
        raise SecretResolutionError(f"arquivo do api_key_ref não encontrado: {path}") from None
    except OSError as exc:
        raise SecretResolutionError(
            f"arquivo do api_key_ref ilegível ({type(exc).__name__}): {path}"
        ) from None
    try:
        if _escaped_root(fd, real_root, path):
            raise SecretResolutionError(
                f"arquivo do api_key_ref fora de SECRETS_DIR ({real_root}): {path}"
            )
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SecretResolutionError(f"api_key_ref não aponta para um arquivo regular: {path}")
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_SECRET_BYTES:
            chunk = os.read(fd, MAX_SECRET_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > MAX_SECRET_BYTES:
        raise SecretResolutionError(
            f"arquivo do api_key_ref maior que {MAX_SECRET_BYTES} bytes: {path}"
        )
    return b"".join(chunks)
