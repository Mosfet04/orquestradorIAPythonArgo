"""QA do F3-05: o bump em si (pin, lock, venv e o que saiu dos gates).

Complementa ``test_project_config.py`` (pin e lock) e ``test_ci_workflow.py`` (ci.yml): a suíte só
prova o 3.1.12 se a venv em que ela roda for a do lock; o ``--ignore-vuln`` também tinha de sair dos
comandos documentados; e as dependências que o AgentOS 3.x usa sem declarar têm de importar.
"""

from __future__ import annotations

import importlib
import re
from importlib.metadata import version
from pathlib import Path

import pytest
from agno.db.mongo import AsyncMongoDb

from src.infrastructure.runtime.agno.session_db import SessionDb

ROOT = Path(__file__).resolve().parents[2]
LOCK = (ROOT / "requirements.lock").read_text(encoding="utf-8")


def _locked(package: str) -> str:
    found = re.search(rf"^{re.escape(package)}==(\S+) \\$", LOCK, re.MULTILINE)
    assert found, f"{package} fora do requirements.lock"
    return found.group(1)


@pytest.mark.parametrize("package", ["agno", "python-multipart", "sqlalchemy", "greenlet", "ag-ui-protocol", "motor"])
def test_versao_instalada_e_a_do_lock(package: str) -> None:
    """A suíte verde só prova o 3.1.12 se o ambiente do teste for o do lock (e não um agno de outra venv)."""
    assert version(package) == _locked(package)


@pytest.mark.parametrize("line", ["python-multipart>=0.0.20", "sqlalchemy[asyncio]>=2.0.0"])
def test_dependencias_nao_declaradas_pelo_agentos_estao_no_requirements_in(line: str) -> None:
    lines = [raw.strip() for raw in (ROOT / "requirements.in").read_text(encoding="utf-8").splitlines()]

    assert line in lines


@pytest.mark.parametrize("module", ["python_multipart", "sqlalchemy.ext.asyncio", "greenlet", "agno.db.postgres"])
def test_modulos_que_o_agentos_3_importa_em_runtime_existem(module: str) -> None:
    """Sem eles o form das rotas de run e a montagem de um Team (``AgentOS._nearest_postgres``) quebram
    só em runtime; aqui falha na suíte."""
    assert importlib.import_module(module).__name__ == module


@pytest.mark.parametrize(
    "path", ["CLAUDE.md", "CONTRIBUTING.md", "README.md", "README.pt-br.md", "README.en.md"]
)
def test_nenhum_comando_documentado_ignora_vulnerabilidade(path: str) -> None:
    """O ``--ignore-vuln PYSEC-2026-2333`` saiu com o agno 3.1.12: gate e docs sem exceção."""
    text = (ROOT / path).read_text(encoding="utf-8")

    commands = re.findall(r"^.*pip-audit -r requirements\.lock.*$", text, re.MULTILINE)

    assert "--ignore-vuln" not in text
    if path in ("CLAUDE.md", "CONTRIBUTING.md"):  # os que documentam o comando (os README só remetem a eles)
        assert commands, f"{path} deixou de documentar o comando do pip-audit"
    for command in commands:
        assert "--require-hashes" in command and "--disable-pip" in command, command


def test_session_db_usa_o_get_session_do_agno() -> None:
    """O override do F2-08 saiu: no 3.x a leitura do agno já não filtra por ``session_type``
    (o comportamento em si é coberto por ``test_qa_f2_08_session_reuse.py``)."""
    assert SessionDb.get_session is AsyncMongoDb.get_session
    assert SessionDb.upsert_run is not AsyncMongoDb.upsert_run
