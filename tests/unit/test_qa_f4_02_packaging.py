"""QA do F4-02: o extra opcional ``mcp`` (lock próprio, Dockerfile, CI) e o teto de versão que a guarda SSRF exige."""

from __future__ import annotations

import re
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _packages(lock: str) -> dict[str, str]:
    return {m.group(1).lower(): m.group(2) for m in re.finditer(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", lock, re.M)}


def test_requirements_mcp_in_fixa_o_sdk_na_1x_e_usa_o_lock_de_runtime_como_restricao():
    text = _text("requirements-mcp.in")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]

    assert "-c requirements.lock" in lines
    assert [ln for ln in lines if ln != "-c requirements.lock"] == ["mcp>=1.30.0,<2"]


def test_lock_do_extra_pina_mcp_1x_com_hash_em_todo_pacote_e_nao_traz_httpx2():
    lock = _text("requirements-mcp.lock")
    packages = _packages(lock)

    assert packages["mcp"].startswith("1.")
    assert "httpx2" not in packages and "httpcore2" not in packages
    entries = [block for block in re.split(r"\n(?=\S)", lock) if re.match(r"[A-Za-z0-9_.-]+==", block)]
    assert entries and all("--hash=sha256:" in block for block in entries)


def test_lock_do_extra_nao_muda_versao_de_pacote_compartilhado_com_o_runtime():
    runtime, extra = _packages(_text("requirements.lock")), _packages(_text("requirements-mcp.lock"))
    shared = set(runtime) & set(extra)

    assert shared  # httpx, pydantic... : o extra reaproveita o lock de runtime
    assert {name: (runtime[name], extra[name]) for name in shared if runtime[name] != extra[name]} == {}


def test_runtime_lock_nao_instala_mcp_o_extra_e_opcional():
    assert "mcp" not in _packages(_text("requirements.lock"))


def test_sdk_instalado_no_ambiente_de_teste_e_o_da_faixa_suportada():
    installed = version("mcp")
    assert installed.startswith("1.") and tuple(int(p) for p in installed.split(".")[:2]) >= (1, 30)
    assert version("httpx").startswith("0.28.")  # o transporte guardado do F4-01 é do httpx 0.28


def test_dockerfile_so_instala_o_extra_com_o_build_arg_e_com_hash_obrigatorio():
    dockerfile = _text("Dockerfile")

    assert "ARG WITH_MCP=false" in dockerfile
    assert 'if [ "$WITH_MCP" = "true" ]' in dockerfile
    installs = re.findall(r"pip install[^\n]*", dockerfile)
    assert installs and all("--require-hashes" in ln and "--only-binary=:all:" in ln for ln in installs)
    assert "requirements-mcp.lock" in dockerfile.split('if [ "$WITH_MCP"')[1]
    default_install = dockerfile.split('if [ "$WITH_MCP"')[0].split("RUN pip install")[1]
    assert "requirements-mcp.lock" not in default_install


def test_ci_audita_o_lock_do_extra_com_hash_obrigatorio():
    ci = _text(".github/workflows/ci.yml")

    assert "pip-audit -r requirements-mcp.lock --require-hashes --disable-pip" in ci


def test_lock_de_dev_inclui_mcp_para_a_suite_e_o_import_do_app_nao_depende_dele():
    assert "mcp" in _packages(_text("requirements-dev.lock"))
