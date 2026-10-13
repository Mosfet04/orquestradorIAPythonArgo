"""Confinamento do ``doc_name`` do RAG ao diretório ``docs/`` (F1-07, S3)."""

from __future__ import annotations

from pathlib import Path

DOCS_DIR = Path("docs")
"""Diretório dos documentos do RAG, relativo ao diretório de trabalho do processo."""

_RULE = (
    "doc_name deve ser o caminho relativo de um arquivo regular dentro de docs/ "
    "(sem caminho absoluto, sem '..', sem diretório e sem symlink para fora de docs/)"
)


class DocumentPathError(ValueError):
    """``doc_name`` recusado: apontaria para fora de ``docs/``."""


def resolve_document_path(doc_name: object, docs_dir: Path = DOCS_DIR) -> Path:
    """Caminho seguro do documento: ``docs_dir/<caminho real relativo a docs_dir>``.

    Recusa (``DocumentPathError``) valor que não seja ``str``, vazio, com NUL, absoluto,
    com componente ``..``, qualquer caminho cujo destino real (symlinks resolvidos) fique
    fora de ``docs_dir`` e destino existente que não seja arquivo regular: diretório
    (inclusive o próprio ``docs_dir``; o ``Knowledge.insert`` do agno 2.5.8 percorre
    diretório seguindo symlink de dentro dele para fora), FIFO, device. Não abre o
    arquivo; só consulta metadados (rode fora do event loop). Arquivo inexistente dentro
    de ``docs_dir`` passa: a ausência é tratada por quem lê.

    O retorno usa o caminho já resolvido (symlink interno vira o arquivo real). Não
    protege contra quem troca arquivos de ``docs/`` entre a checagem e a leitura: o
    diretório é do operador, não de quem configura agentes.
    """
    if not isinstance(doc_name, str) or not doc_name or "\x00" in doc_name:
        raise DocumentPathError(f"{_RULE}: valor inválido {doc_name!r}")
    candidate = Path(doc_name)
    if candidate.is_absolute():
        raise DocumentPathError(f"{_RULE}: caminho absoluto {doc_name!r}")
    if ".." in candidate.parts:
        raise DocumentPathError(f"{_RULE}: '..' em {doc_name!r}")

    try:
        root = docs_dir.resolve()
        target = (root / candidate).resolve()
        relative = target.relative_to(root)
        # is_dir/exists/is_file levantam OSError fora de ENOENT/ENOTDIR/ELOOP
        # (ex.: ENAMETOOLONG, EACCES): também viram DocumentPathError.
        is_dir = relative == Path(".") or target.is_dir()
        not_regular = not is_dir and target.exists() and not target.is_file()
    except ValueError:
        raise DocumentPathError(f"{_RULE}: {doc_name!r} aponta para fora de docs/") from None
    except (OSError, RuntimeError) as exc:  # loop de symlink, permissão, nome longo
        raise DocumentPathError(
            f"{_RULE}: {doc_name!r} não pôde ser resolvido ({type(exc).__name__})"
        ) from exc
    if is_dir:
        raise DocumentPathError(f"{_RULE}: {doc_name!r} é um diretório")
    if not_regular:
        raise DocumentPathError(f"{_RULE}: {doc_name!r} não é um arquivo regular")
    return docs_dir / relative
