"""Contratos do ``lint-imports`` (F2-07): import estático do agno só em ``src/infrastructure/runtime/agno``.

Sonda: o ``lint-imports`` de verdade roda sobre uma cópia de ``src`` com o ``pyproject.toml`` do
repositório; um ``import agno`` plantado fora do pacote do runtime tem de quebrar o contrato
``agno-so-no-runtime``, e o mesmo import dentro dele não. As baselines (``ignore_imports``) são
conferidas no próprio ``pyproject.toml``: só o composition root pode restar. O contrato não vê import
por nome (``importlib``): o ``ProviderRegistry`` carrega assim as classes ``agno.*`` das specs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
AGNO_CONTRACT = "agno-so-no-runtime"
COMPOSITION_ROOT_IMPORT = (
    "src.infrastructure.dependency_injection -> src.presentation.controllers.orquestrador_controller"
)
_RUN_LINT_IMPORTS = "import sys; from importlinter.cli import lint_imports_command; sys.exit(lint_imports_command())"


def _contracts() -> dict[str, dict[str, Any]]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {contract["id"]: contract for contract in pyproject["tool"]["importlinter"]["contracts"]}


@pytest.fixture
def project_copy(tmp_path: Path) -> Path:
    """Cópia de ``src`` e do ``pyproject.toml``: a sonda nunca escreve no repositório."""
    shutil.copytree(ROOT / "src", tmp_path / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    return tmp_path


def _lint_imports(project: Path, contract: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - interpretador do próprio venv, argumentos fixos
        [sys.executable, "-c", _RUN_LINT_IMPORTS, "--no-cache", "--no-logo", "--contract", contract],
        cwd=project,
        env={**os.environ, "NO_COLOR": "1", "COLUMNS": "200"},  # saída sem cor nem quebra de linha
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _plant(project: Path, relative_path: str, source: str) -> str:
    """Grava o módulo-sonda (criando o pacote, se for novo) e devolve o nome pontilhado dele."""
    target = project / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    init = target.parent / "__init__.py"
    if not init.exists():
        init.write_text("", encoding="utf-8")
    target.write_text(source, encoding="utf-8")
    return relative_path.removesuffix(".py").replace("/", ".")


def test_o_contrato_do_agno_passa_no_codigo_atual(project_copy: Path) -> None:
    result = _lint_imports(project_copy, AGNO_CONTRACT)

    assert result.returncode == 0, result.stdout[-2000:]
    assert "Contracts: 1 kept, 0 broken." in result.stdout


@pytest.mark.parametrize(
    ("relative_path", "source"),
    [
        ("src/infrastructure/web/probe.py", "import agno\n"),
        ("src/infrastructure/web/probe.py", "from agno.agent import Agent\n"),
        # import preguiçoso (dentro de função) também é import para o grafo
        ("src/infrastructure/web/probe.py", "def build():\n    from agno.team import Team\n    return Team\n"),
        ("src/infrastructure/probe.py", "import agno\n"),
        ("src/infrastructure/runtime/probe.py", "import agno\n"),
        # subpacote novo de infrastructure: coberto sem ninguém editar o contrato
        ("src/infrastructure/novo_pacote/probe.py", "import agno\n"),
        # nome que só começa igual ao do pacote permitido não herda a exceção
        ("src/infrastructure/runtime/agno_extra/probe.py", "import agno\n"),
        ("src/presentation/controllers/probe.py", "import agno\n"),
        ("src/application/services/probe.py", "import agno\n"),
        ("src/domain/probe.py", "import agno\n"),
    ],
    ids=[
        "web-import",
        "web-from-import",
        "web-import-em-funcao",
        "raiz-de-infrastructure",
        "runtime-fora-de-agno",
        "subpacote-novo",
        "irmao-com-prefixo-igual",
        "presentation",
        "application",
        "domain",
    ],
)
def test_importar_o_agno_fora_do_runtime_quebra_o_contrato(
    project_copy: Path, relative_path: str, source: str
) -> None:
    module = _plant(project_copy, relative_path, source)

    result = _lint_imports(project_copy, AGNO_CONTRACT)

    assert result.returncode == 1, result.stdout[-2000:]
    assert "Contracts: 0 kept, 1 broken." in result.stdout
    assert f"{module} -> agno" in result.stdout


def test_importar_o_agno_dentro_do_runtime_e_permitido(project_copy: Path) -> None:
    _plant(project_copy, "src/infrastructure/runtime/agno/probe.py", "from agno.agent import Agent\n")
    _plant(project_copy, "src/infrastructure/runtime/agno/sub/probe.py", "import agno\n")

    result = _lint_imports(project_copy, AGNO_CONTRACT)

    assert result.returncode == 0, result.stdout[-2000:]


def test_o_contrato_do_agno_protege_o_pacote_inteiro_com_uma_excecao_so() -> None:
    contract = _contracts()[AGNO_CONTRACT]

    assert contract["type"] == "protected"
    assert contract["protected_modules"] == ["agno"]
    assert contract["allowed_importers"] == ["src.infrastructure.runtime.agno"]
    assert "ignore_imports" not in contract


@pytest.mark.parametrize("contract_id", ["nucleo-sem-frameworks", "apresentacao-sem-runtime"])
def test_contratos_do_nucleo_nao_tem_baseline(contract_id: str) -> None:
    assert "ignore_imports" not in _contracts()[contract_id]


def test_baseline_de_camadas_no_maximo_o_composition_root() -> None:
    """Subconjunto: a baseline pode encolher até zerar sem quebrar o teste; crescer, não."""
    assert set(_contracts()["camadas"].get("ignore_imports", [])) <= {COMPOSITION_ROOT_IMPORT}
