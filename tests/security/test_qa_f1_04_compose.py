"""QA do F1-04: o compose exige as chaves e o ``.env.example`` as documenta.

``docker compose config`` não precisa do daemon; roda num diretório isolado, sem ``.env`` e
com ambiente vazio (``env -i``), para que nada do host (nem um ``.env`` real) entre na conta.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKER = shutil.which("docker")
BASE_ENV = {
    "MONGO_CONNECTION_STRING": "mongodb://mongo.invalid:27017",
    "API_KEY_RUN": "r" * 32,
    "API_KEY_ADMIN": "a" * 32,
}
DEV_ENV = {
    "MONGO_ROOT_USERNAME": "u",
    "MONGO_ROOT_PASSWORD": "p",
    "MONGO_EXPRESS_USERNAME": "u",
    "MONGO_EXPRESS_PASSWORD": "p",
}

needs_docker = pytest.mark.skipif(DOCKER is None, reason="docker CLI ausente")


def _compose_config(tmp_path: Path, env: dict[str, str], *files: str) -> subprocess.CompletedProcess[str]:
    for name in {*files, "docker-compose.yml"}:
        shutil.copy(ROOT / name, tmp_path / name)
    args = [str(DOCKER), "compose"]
    for name in files or ("docker-compose.yml",):
        args += ["-f", name]
    clean = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), **env}
    return subprocess.run(  # noqa: S603 - argumentos fixos do teste
        [*args, "config"], cwd=tmp_path, env=clean, capture_output=True, text=True, timeout=60, check=False
    )


@needs_docker
def test_compose_com_as_tres_variaveis_resolve_e_repassa_as_chaves(tmp_path: Path):
    result = _compose_config(tmp_path, BASE_ENV)

    assert result.returncode == 0, result.stderr
    assert re.search(r"^\s+API_KEY_RUN: " + "r" * 32 + r"$", result.stdout, re.MULTILINE)
    assert re.search(r"^\s+API_KEY_ADMIN: " + "a" * 32 + r"$", result.stdout, re.MULTILINE)


@needs_docker
@pytest.mark.parametrize("missing", ["API_KEY_RUN", "API_KEY_ADMIN", "MONGO_CONNECTION_STRING"])
def test_compose_sem_a_variavel_falha_com_mensagem_que_a_nomeia(tmp_path: Path, missing: str):
    env = {k: v for k, v in BASE_ENV.items() if k != missing}

    result = _compose_config(tmp_path, env)

    assert result.returncode != 0
    assert f"required variable {missing} is missing a value" in result.stderr


@needs_docker
@pytest.mark.parametrize("which", ["API_KEY_RUN", "API_KEY_ADMIN"])
def test_compose_com_chave_vazia_falha(tmp_path: Path, which: str):
    """`${VAR:?}` recusa também o valor vazio (`API_KEY_RUN=` no .env copiado do exemplo)."""
    result = _compose_config(tmp_path, {**BASE_ENV, which: ""})

    assert result.returncode != 0 and which in result.stderr


@needs_docker
@pytest.mark.parametrize("which", ["API_KEY_RUN", "API_KEY_ADMIN"])
def test_compose_dev_continua_exigindo_as_chaves(tmp_path: Path, which: str):
    files = ("docker-compose.yml", "docker-compose.dev.yml")
    ok = _compose_config(tmp_path, {**BASE_ENV, **DEV_ENV}, *files)
    assert ok.returncode == 0, ok.stderr
    assert re.search(r"^\s+ENVIRONMENT: development$", ok.stdout, re.MULTILINE)

    env = {k: v for k, v in {**BASE_ENV, **DEV_ENV}.items() if k != which}
    result = _compose_config(tmp_path, env, *files)

    assert result.returncode != 0
    assert f"required variable {which} is missing a value" in result.stderr


@needs_docker
def test_compose_nao_define_app_host_nem_afrouxa_o_bind(tmp_path: Path):
    """APP_HOST fica com o Dockerfile (0.0.0.0): o compose não o sobrescreve para loopback."""
    result = _compose_config(tmp_path, BASE_ENV)

    assert "APP_HOST" not in result.stdout


def test_env_example_documenta_as_chaves_sem_valor():
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assigned = {m.group(1): m.group(2) for line in lines if (m := re.match(r"^(API_KEY_[A-Z]+)=(.*)$", line))}

    assert assigned == {"API_KEY_RUN": "", "API_KEY_ADMIN": ""}  # presentes e sem segredo no exemplo
    text = "\n".join(lines)
    assert "32 caracteres" in text and "secrets.token_urlsafe" in text
    assert "docker-compose.yml exige as duas" in text or "exige as duas" in text


@pytest.mark.parametrize("readme", ["README.pt-br.md", "README.en.md"])
def test_readme_documenta_autenticacao_e_chaves(readme: str):
    text = (ROOT / readme).read_text(encoding="utf-8")

    for needle in ("API_KEY_RUN", "API_KEY_ADMIN", "X-API-Key", "Bearer", "/livez", "secrets.token_urlsafe"):
        assert needle in text, f"{readme} sem {needle}"
