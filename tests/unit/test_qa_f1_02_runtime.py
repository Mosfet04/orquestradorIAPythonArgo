"""QA do F1-02: lacunas entre o que o Dockerfile/compose prometem e o que o app faz.

Cobre (sem Docker daemon, sem rede externa):
- o contexto de build REAL (árvore do repositório) não vaza segredo e contém o que o
  app precisa em runtime (código de ``src/`` e ``docs/``);
- ``read_only: true``: o setup de logging não grava em disco quando ``logs/`` já existe
  (o Dockerfile cria o diretório);
- o HEALTHCHECK do Dockerfile, executado de verdade, respeita ``APP_PORT`` e falha em
  5xx e com a porta fechada;
- ``docker compose config`` (quando o CLI existe) da composição base e base+dev;
- entradas hostis de ``APP_HOST``/``APP_PORT``.
"""

from __future__ import annotations

import http.server
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
import structlog
import yaml

from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.web.server_settings import build_uvicorn_settings
from tests.unit.test_container_hygiene import _excluded, _parse_dockerignore

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
COMPOSE_DEV = ROOT / "docker-compose.dev.yml"
DOCKERIGNORE = ROOT / ".dockerignore"

ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - valor de APP_HOST dentro do container


# ── contexto de build real ─────────────────────────────────────────


def _context_files() -> list[str]:
    """Arquivos da árvore real que entrariam no contexto de build (poda diretórios excluídos)."""
    rules = _parse_dockerignore(DOCKERIGNORE.read_text(encoding="utf-8"))
    included: list[str] = []
    for current, dirs, files in os.walk(ROOT, followlinks=False):
        rel_dir = Path(current).relative_to(ROOT).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        if rel_dir == "":
            # ambientes virtuais/.git são cobertos pelos testes parametrizados do dev; não percorrer
            dirs[:] = [d for d in dirs if not (d.startswith(".venv") or d in {".git", "venv", "node_modules"})]
        for name in files:
            rel = f"{rel_dir}/{name}".lstrip("/")
            if not _excluded(rel, rules):
                included.append(rel)
    return sorted(included)


def test_contexto_real_so_tem_allowlist_e_nenhum_segredo():
    files = _context_files()
    assert "app.py" in files and "requirements.lock" in files
    top_level = {f.split("/")[0] for f in files}
    assert top_level <= {"app.py", "src", "docs", "requirements.lock"}, sorted(top_level)
    leaked = [
        f
        for f in files
        if any(part == ".env" or part.startswith(".env.") for part in f.split("/"))
        or f.endswith((".log", ".pyc", ".pyo", ".pem", ".key"))
        or ".git" in f.split("/")
        or any(part.startswith(".venv") or part == "venv" for part in f.split("/"))
    ]
    assert not leaked, f"entrariam no contexto de build: {leaked}"


def test_contexto_real_contem_todo_modulo_de_src():
    """Se um arquivo de src/ ficar de fora, a imagem quebra no import (build não valida isso)."""
    tracked = subprocess.run(
        ["git", "ls-files", "src"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    if not tracked:
        pytest.skip("repositório sem git")
    files = set(_context_files())
    missing = [f for f in tracked if f not in files and (ROOT / f).exists() and "__pycache__" not in f]
    assert not missing, missing


def test_docs_lidos_em_runtime_estao_no_contexto():
    """AgentFactory lê ``docs/<doc_name>`` relativo ao cwd (/app na imagem)."""
    files = _context_files()
    assert any(f.startswith("docs/") for f in files)
    assert "docs/basic-prog.txt" in files


# ── read_only: setup de logging não grava com logs/ pré-existente ───


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignora permissão de escrita")
def test_setup_structlog_nao_grava_em_disco_com_logs_preexistente(tmp_path, monkeypatch):
    """Dockerfile cria ``logs/``; com a raiz somente leitura o mkdir(exist_ok) não pode falhar."""
    from src.infrastructure.logging import setup_structlog

    app_dir = tmp_path / "app"
    (app_dir / "logs").mkdir(parents=True)
    before = sorted(p.name for p in app_dir.rglob("*"))
    snapshot = structlog.get_config()
    app_dir.chmod(0o555)
    (app_dir / "logs").chmod(0o555)
    monkeypatch.chdir(app_dir)
    try:
        setup_structlog()
        assert sorted(p.name for p in app_dir.rglob("*")) == before
    finally:
        (app_dir / "logs").chmod(0o755)
        app_dir.chmod(0o755)
        structlog.configure(**snapshot)


def test_dockerfile_cria_logs_antes_de_baixar_privilegio():
    """O mkdir de logs/ em runtime só funciona porque a imagem já traz o diretório."""
    lines = [
        line.strip()
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    mkdir = next(i for i, line in enumerate(lines) if "mkdir -p logs" in line)
    user = next(i for i, line in enumerate(lines) if line.startswith("USER "))
    copy_src = next(i for i, line in enumerate(lines) if line.startswith("COPY . "))
    assert copy_src < mkdir < user
    assert "chown 10001:10001 logs" in lines[mkdir]


# ── HEALTHCHECK executado de verdade ───────────────────────────────


def _healthcheck_argv() -> list[str]:
    text = DOCKERFILE.read_text(encoding="utf-8")
    logical = text.replace("\\\n", " ")
    line = next(line for line in logical.splitlines() if line.startswith("HEALTHCHECK"))
    tokens = shlex.split(line)
    cmd_index = tokens.index("CMD")
    return tokens[cmd_index + 1 :]


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self) -> None:
        self.paths.append(self.path)
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: object) -> None:  # silencia stderr
        return


@pytest.fixture
def health_server() -> Iterator[tuple[int, type[_Handler]]]:
    handler = type("H", (_Handler,), {"paths": [], "status": 200})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_healthcheck(port: int | None) -> subprocess.CompletedProcess[str]:
    argv = _healthcheck_argv()
    assert argv[0] == "python"
    env = {"PATH": os.environ.get("PATH", "")}
    if port is not None:
        env["APP_PORT"] = str(port)
    return subprocess.run(  # noqa: S603 - código vem do Dockerfile do repositório
        [sys.executable, *argv[1:]], env=env, capture_output=True, text=True, timeout=30, check=False
    )


def test_healthcheck_usa_app_port_customizado_e_a_rota_de_health(health_server):
    port, handler = health_server
    assert port != 7777
    result = _run_healthcheck(port)
    assert result.returncode == 0, result.stderr
    assert handler.paths == ["/livez"]


def test_healthcheck_falha_quando_a_resposta_e_5xx(health_server):
    port, handler = health_server
    handler.status = 503
    assert _run_healthcheck(port).returncode != 0


def test_healthcheck_falha_com_a_porta_fechada(health_server):
    del health_server  # sem servidor ouvindo na porta efêmera abaixo
    # Socket ligado e SEM listen(), aberto durante o teste: a porta fica reservada (nenhum
    # outro worker do xdist a pega) e o connect leva "connection refused".
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
        assert _run_healthcheck(closed).returncode != 0


def test_healthcheck_sem_app_port_cai_no_default_7777_da_imagem():
    argv = _healthcheck_argv()
    assert "'7777'" in argv[2] or '"7777"' in argv[2]
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "APP_PORT=7777" in dockerfile


# ── docker compose config (quando o CLI existe) ────────────────────

DOCKER = shutil.which("docker") or "docker"
pytestmark_compose = pytest.mark.skipif(shutil.which("docker") is None, reason="CLI docker ausente")

_FAKE_ENV = {
    "MONGO_CONNECTION_STRING": "mongodb://externo.invalid:27017/x",
    "MONGO_ROOT_USERNAME": "qa-user",
    "MONGO_ROOT_PASSWORD": "qa-pass",
    "MONGO_EXPRESS_USERNAME": "qa-me",
    "MONGO_EXPRESS_PASSWORD": "qa-mep",
    # F1-04: o app no container exige as chaves da borda.
    "API_KEY_RUN": "qa-run-" + "r" * 32,
    "API_KEY_ADMIN": "qa-admin-" + "a" * 32,
}
_APP_ONLY_ENV = {k: _FAKE_ENV[k] for k in ("MONGO_CONNECTION_STRING", "API_KEY_RUN", "API_KEY_ADMIN")}


def _compose_config(tmp_path: Path, files: list[Path], env: dict[str, str]) -> dict:
    for f in files:
        shutil.copy(f, tmp_path / f.name)
    cmd = [DOCKER, "compose"]
    for f in files:
        cmd += ["-f", f.name]
    cmd += ["config", "--format", "json"]
    full_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), **env}
    result = subprocess.run(  # noqa: S603 - comando fixo, env fictício, sem .env
        cmd, cwd=tmp_path, env=full_env, capture_output=True, text=True, timeout=60, check=False
    )
    if result.returncode != 0 and "compose" in result.stderr and "not a docker command" in result.stderr:
        pytest.skip("plugin docker compose ausente")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytestmark_compose
def test_compose_base_renderizado_so_app_sem_porta_de_banco(tmp_path):
    cfg = _compose_config(tmp_path, [COMPOSE], _APP_ONLY_ENV)
    assert set(cfg["services"]) == {"app"}
    published = {p["target"] for p in cfg["services"]["app"]["ports"]}
    assert published == {7777}
    assert cfg["services"]["app"]["read_only"] is True
    assert cfg["services"]["app"]["cap_drop"] == ["ALL"]


@pytestmark_compose
@pytest.mark.parametrize("missing", ["MONGO_CONNECTION_STRING", "API_KEY_RUN", "API_KEY_ADMIN"])
def test_compose_base_exige_connection_string(tmp_path, missing):
    """Inclui as chaves da borda (F1-04): o container faz bind em 0.0.0.0."""
    for f in (COMPOSE,):
        shutil.copy(f, tmp_path / f.name)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), **_APP_ONLY_ENV}
    del env[missing]
    result = subprocess.run(  # noqa: S603
        [DOCKER, "compose", "-f", "docker-compose.yml", "config"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if "not a docker command" in result.stderr:
        pytest.skip("plugin docker compose ausente")
    assert result.returncode != 0
    assert missing in result.stderr


@pytestmark_compose
def test_compose_dev_renderizado_portas_so_no_loopback_e_app_aponta_para_servicos_locais(tmp_path):
    cfg = _compose_config(tmp_path, [COMPOSE, COMPOSE_DEV], _FAKE_ENV)
    assert {"app", "mongodb", "ollama", "mongo-express"} <= set(cfg["services"])
    for name in ("mongodb", "ollama", "mongo-express"):
        for port in cfg["services"][name]["ports"]:
            assert port.get("host_ip") == "127.0.0.1", f"{name}: {port}"
    app = cfg["services"]["app"]
    assert {p["target"] for p in app["ports"]} == {7777}
    assert app["environment"]["OLLAMA_BASE_URL"] == "http://ollama:11434"
    assert "@mongodb:27017/" in app["environment"]["MONGO_CONNECTION_STRING"]
    assert app["read_only"] is True and app["cap_drop"] == ["ALL"]
    # o override não pode derrubar o endurecimento nem montar volume gravável no app
    assert not app.get("volumes")


@pytestmark_compose
def test_compose_dev_exige_credenciais_dos_servicos_de_apoio(tmp_path):
    for f in (COMPOSE, COMPOSE_DEV):
        shutil.copy(f, tmp_path / f.name)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), **_FAKE_ENV}
    del env["MONGO_ROOT_PASSWORD"]
    result = subprocess.run(  # noqa: S603
        [DOCKER, "compose", "-f", "docker-compose.yml", "-f", "docker-compose.dev.yml", "config"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if "not a docker command" in result.stderr:
        pytest.skip("plugin docker compose ausente")
    assert result.returncode != 0
    assert "MONGO_ROOT_PASSWORD" in result.stderr


def test_compose_dev_nao_declara_credencial_literal_nem_imagem_latest():
    dev = yaml.safe_load(COMPOSE_DEV.read_text(encoding="utf-8"))
    text = COMPOSE_DEV.read_text(encoding="utf-8")
    assert "password123" not in text and "admin123" not in text
    for service in dev["services"].values():
        assert not str(service.get("image", "")).endswith(":latest")


# ── entradas hostis de host/porta ──────────────────────────────────


def _settings(**env: str) -> dict[str, object]:
    with patch.dict(os.environ, env, clear=True):
        return build_uvicorn_settings(AppConfig.load())


@pytest.mark.parametrize("port", ["abc", "", "7777.5", "70000x"])
def test_app_port_invalido_falha_no_carregamento_sem_subir_servidor(port):
    with pytest.raises(ValueError):
        _settings(APP_PORT=port)


def test_app_host_vazio_nao_pode_virar_bind_em_todas_as_interfaces():
    """Fora do container o default deve ser loopback; ``APP_HOST=`` (vazio) deve cair nele."""
    assert _settings(APP_HOST="")["host"] == "127.0.0.1"


def test_app_host_so_vem_do_app_config_nao_de_ollama():
    settings = _settings(OLLAMA_BASE_URL="http://ollama:11434")
    assert settings["host"] == "127.0.0.1"
    assert settings["port"] == 7777


# ── OLLAMA_BASE_URL chega ao cliente real do agno ──────────────────


def test_ollama_host_configurado_chega_ao_client_do_agno(mock_logger):
    from src.application.services.model_factory_service import ModelFactory

    factory = ModelFactory(logger=mock_logger, ollama_host="http://ollama:11434")
    client = factory.create_model("ollama", "llama3.2:latest").get_client()
    assert "ollama:11434" in str(client._client.base_url)


def test_ollama_sem_host_configurado_nao_aponta_para_servico_do_compose(mock_logger):
    from src.application.services.model_factory_service import ModelFactory

    model = ModelFactory(logger=mock_logger).create_model("ollama", "llama3.2:latest")
    assert "ollama:11434" not in str(model.get_client()._client.base_url)
