"""Lacunas de `test_ci_workflow.py` achadas por mutação do `ci.yml` (QA do F0-02).

Cada teste aqui falharia com uma regressão que a suíte do dev deixava passar:
instalador alternativo (`pip3`, `uv pip`), cobertura que some sem `--cov` e o envio ao
Codacy rodando sem esperar o artefato de cobertura.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
CI = WORKFLOWS_DIR / "ci.yml"
INSTALLER = re.compile(r"\b(pip3?|pipx|uv|poetry|pipenv|conda)\b[^\n|;&]*\b(install|add|sync)\b|setup\.py\s+install")
# instalação "de verdade": só `python -m pip install --require-hashes -r <lock> ...`
CANONICAL_INSTALL = re.compile(r"^python -m pip install --require-hashes( -r requirements(-dev)?\.lock)+$")


@pytest.fixture(scope="module")
def ci() -> dict[str, Any]:
    data = yaml.safe_load(CI.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _run_lines(ci: dict[str, Any]):
    for job_id, job in ci["jobs"].items():
        for step in job.get("steps", []):
            run = re.sub(r"\\\n", " ", step.get("run", ""))
            for line in run.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    yield job_id, line


def test_nenhum_instalador_alternativo_alem_do_pip_com_lock(ci):
    """`pip3 install x`, `uv pip install`, `pipx install`... furam a regra "só os locks"."""
    installs = [(job, line) for job, line in _run_lines(ci) if INSTALLER.search(line)]
    assert installs, "nenhuma instalação encontrada (regex quebrada?)"
    for job, line in installs:
        assert CANONICAL_INSTALL.match(line), f"{job}: instalação fora do padrão dos locks: {line}"


def test_job_de_teste_gera_e_publica_cobertura(ci):
    test_job = ci["jobs"]["test"]
    pytest_cmds = [s["run"] for s in test_job["steps"] if "pytest" in s.get("run", "")]
    assert len(pytest_cmds) == 1
    # `--cov` como argumento próprio: `--cov-report` sozinho não mede nada nem gera coverage.xml
    args = pytest_cmds[0].split()
    assert "--cov" in args
    assert "--cov-report=xml" in args

    uploads = [s for s in test_job["steps"] if s.get("uses", "").startswith("actions/upload-artifact@")]
    assert len(uploads) == 1
    assert uploads[0]["with"]["name"] == "coverage-py3.12"
    assert "coverage.xml" in uploads[0]["with"]["path"]


def test_envio_ao_codacy_espera_o_job_de_testes_e_baixa_o_mesmo_artefato(ci):
    codacy = ci["jobs"]["codacy-coverage"]
    needs = codacy["needs"]
    assert "test" in ([needs] if isinstance(needs, str) else needs)

    uploaded = next(s for s in ci["jobs"]["test"]["steps"] if s.get("uses", "").startswith("actions/upload-artifact@"))
    downloaded = next(s for s in codacy["steps"] if s.get("uses", "").startswith("actions/download-artifact@"))
    assert downloaded["with"]["name"] == uploaded["with"]["name"]
