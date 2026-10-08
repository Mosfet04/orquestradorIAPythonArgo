"""Protege a configuração de ferramentas/dependências do F0-01 contra regressão.

Não testa comportamento do produto: apenas invariantes do pyproject.toml, dos
arquivos de requirements e dos markers do pytest.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = ROOT / "tests"

# Markers embutidos do pytest e de plugins instalados (não precisam constar em `markers`).
BUILTIN_MARKS = {
    "parametrize",
    "skip",
    "skipif",
    "xfail",
    "usefixtures",
    "filterwarnings",
    "asyncio",
    "anyio",
    "no_cover",
}


@pytest.fixture(scope="module")
def pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def _requirement_lines(path: Path) -> list[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _marks_used_in_tests() -> set[str]:
    used: set[str] = set()
    for path in TESTS_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            # pytest.mark.<nome>
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "mark"
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "pytest"
            ):
                used.add(node.attr)
    return used


def test_pytest_ini_nao_existe():
    assert not (ROOT / "pytest.ini").exists(), "configuração de pytest vive só no pyproject.toml"


def test_pytest_usa_markers_estritos(pyproject):
    opts = pyproject["tool"]["pytest"]["ini_options"]
    assert opts.get("strict_markers") is True
    assert opts["testpaths"] == ["tests"]


def test_markers_usados_nos_testes_estao_declarados(pyproject):
    declared = {m.split(":", 1)[0].strip() for m in pyproject["tool"]["pytest"]["ini_options"]["markers"]}
    undeclared = _marks_used_in_tests() - declared - BUILTIN_MARKS
    assert not undeclared, f"markers usados mas não declarados no pyproject.toml: {sorted(undeclared)}"
    assert {"unit", "contract", "integration", "security", "eval", "live", "slow"} <= declared


def test_teste_em_tests_unit_recebe_marker_unit_pelo_conftest(request):
    """O marker vem do diretório (tests/conftest.py), não de decorator no arquivo."""
    layer_marks = {m.name for m in request.node.iter_markers()} & {"unit", "contract", "integration"}
    assert layer_marks == {"unit"}


def test_cobertura_so_mede_src(pyproject):
    run = pyproject["tool"]["coverage"]["run"]
    assert run["source"] == ["src"]
    assert run["branch"] is True
    assert {"tests/*", ".venv/*"} <= set(run["omit"])


def test_agno_fixado_na_versao_validada():
    assert "agno==2.5.8" in _requirement_lines(ROOT / "requirements.in")
    lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    assert re.search(r"^agno==2\.5\.8 \\\n\s+--hash=sha256:[0-9a-f]{64}", lock, re.MULTILINE)


def test_uvloop_so_fora_do_windows():
    uvloop = [line for line in _requirement_lines(ROOT / "requirements.in") if line.startswith("uvloop")]
    assert len(uvloop) == 1
    assert 'sys_platform != "win32"' in uvloop[0]


@pytest.mark.parametrize("lock_name", ["requirements.lock", "requirements-dev.lock"])
def test_todo_pino_dos_locks_tem_hash(lock_name):
    """`pip install --require-hashes` exige hash em todo requisito; um pino sem hash quebra o install."""
    text = (ROOT / lock_name).read_text(encoding="utf-8")
    # junta continuações de linha ("\") em uma entrada lógica
    entries = [e for e in re.sub(r"\\\n", " ", text).splitlines() if re.match(r"^[A-Za-z0-9_.\-]+==", e)]
    assert entries, f"{lock_name} sem pinos"
    sem_hash = [e.split()[0] for e in entries if "--hash=sha256:" not in e]
    assert not sem_hash, f"pinos sem hash em {lock_name}: {sem_hash}"


def test_requirements_txt_aponta_para_o_lock():
    assert "-r requirements.lock" in _requirement_lines(ROOT / "requirements.txt")


def test_dev_lock_restrito_ao_lock_de_runtime():
    assert "-c requirements.lock" in _requirement_lines(ROOT / "requirements-dev.in")
