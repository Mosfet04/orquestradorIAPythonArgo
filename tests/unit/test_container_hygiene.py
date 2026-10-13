"""Higiene de container do F1-02: `.dockerignore`, Dockerfile e docker-compose.

Validação estática (o build real depende do Docker daemon, que a suíte não exige):
o contexto de build é uma allowlist sem segredos nem lixo local, a imagem instala pelo
lock com hash e roda sem root (UID/GID numéricos), o ``docker-compose.yml`` só tem o
app (endurecido) e os serviços de apoio com portas ficam no ``docker-compose.dev.yml``,
sem credencial literal.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.fakes.dockerignore import excluded, parse_dockerignore

ROOT = Path(__file__).resolve().parents[2]
DOCKERIGNORE = ROOT / ".dockerignore"
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
COMPOSE_DEV = ROOT / "docker-compose.dev.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# Portas de serviços de apoio (Mongo, Ollama, mongo-express) que nunca podem ficar
# publicadas pelo compose base.
SUPPORT_PORTS = {"27017", "11434", "8081"}
SUPPORT_SERVICES = {"mongodb", "ollama", "mongo-express"}
SECRET_KEY = re.compile(r"(PASSWORD|PASSWD|USERNAME|SECRET|TOKEN|API_KEY|CONNECTION_STRING)", re.IGNORECASE)
REQUIRED_VAR = re.compile(r"^\$\{[A-Z][A-Z0-9_]*:\?[^}]+\}$")
INTERPOLATION = re.compile(r"\$\{[A-Z][A-Z0-9_]*(?::?[-?][^}]*)?\}")
URL_USERINFO = re.compile(r"://(?P<userinfo>[^/@\s]+)@")


# ── .dockerignore (emulação em tests/fakes/dockerignore.py) ─────────


@pytest.fixture(scope="module")
def dockerignore_rules() -> list[tuple[bool, re.Pattern[str]]]:
    return parse_dockerignore(DOCKERIGNORE.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("rules", "path", "expected"),
    [
        ("*.log", "a.log", True),
        ("*.log", "logs/a.log", False),  # "*" não atravessa "/"
        ("**/*.log", "logs/x/a.log", True),
        ("**/__pycache__", "__pycache__/a.pyc", True),  # "**/" casa zero diretórios
        ("logs", "logs/x/a.log", True),  # diretório pai casa
        ("*\n!src/", "src/a.py", False),  # negação reinclui; "src/" vira "src"
        ("*\n!src\n**/.env", "src/.env", True),  # última regra vence
        ("?.txt", "ab.txt", False),
        ("**/*.py[cod]", "src/a.pyc", True),
        ("a+b.txt", "a+b.txt", True),  # "+" literal, não quantificador
        ("a+b.txt", "aab.txt", False),
        ("(x)|y", "(x)|y", True),
        ("(x)|y", "y", False),
        ("{a}", "{a}", True),
        ("#c", "#c", False),  # comentário: "#" é o 1º caractere
        ("  #c", "#c", True),  # "#" depois de espaço é padrão (trim só depois)
    ],
)
def test_emulador_segue_a_semantica_do_patternmatcher(rules, path, expected):
    assert excluded(path, parse_dockerignore(rules)) is expected


def test_dockerignore_existe_e_nao_esta_no_gitignore():
    assert DOCKERIGNORE.is_file(), ".dockerignore precisa existir e ser versionado"
    gitignore = [line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert ".dockerignore" not in gitignore, ".gitignore não pode esconder o .dockerignore"


def test_dockerignore_e_allowlist():
    first = next(
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    assert first == "*", "o .dockerignore começa excluindo tudo e só reinclui o necessário"


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.development",
        ".env.example",
        ".env.local",
        "src/.env",
        "src/infrastructure/.env.local",
        "src/prod.env",
        "docs/x/.envrc",
        ".envrc",
        "docs/roadmap/F1-estabilizacao.md",
        "docs/qa/relatorio.md",
        "docs/x/.env.prod",
        "docs/debug.log",
        ".git/config",
        ".venv/bin/python",
        ".venv-root-owned/x",
        ".codacy.root-owned.bak/logs/a.log",
        "venv/bin/python",
        "logs/app.log",
        "tests/conftest.py",
        "src/infrastructure/__pycache__/app_config.cpython-312.pyc",
        "__pycache__/app.cpython-312.pyc",
        ".pytest_cache/v/cache/nodeids",
        ".mypy_cache/3.12/app.meta.json",
        ".coverage",
        "htmlcov/index.html",
        ".claude/settings.json",
        "mongo-init/init-db.js",
        "docker-compose.yml",
        "pyproject.toml",
    ],
)
def test_dockerignore_exclui_segredos_e_lixo_local(dockerignore_rules, path):
    assert excluded(path, dockerignore_rules), f"{path} entraria no contexto de build"


@pytest.mark.parametrize(
    "path",
    [
        "app.py",
        "requirements.lock",
        "src/__init__.py",
        "src/infrastructure/web/app_factory.py",
        # documentos do RAG hierárquico são lidos de docs/ em runtime
        "docs/basic-prog.txt",
    ],
)
def test_dockerignore_mantem_o_necessario_para_a_imagem(dockerignore_rules, path):
    assert not excluded(path, dockerignore_rules), f"{path} ficaria fora do contexto de build"


# ── Dockerfile ─────────────────────────────────────────────────────


def _dockerfile_instructions() -> list[tuple[str, str]]:
    """(INSTRUÇÃO, argumentos) com continuações de linha juntadas e comentários removidos."""
    lines = [
        line for line in DOCKERFILE.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
    ]
    logical = re.sub(r"\\\n", " ", "\n".join(lines))
    instructions: list[tuple[str, str]] = []
    for line in logical.splitlines():
        if line.strip():
            keyword, _, args = line.strip().partition(" ")
            instructions.append((keyword.upper(), args.strip()))
    return instructions


@pytest.fixture(scope="module")
def dockerfile() -> list[tuple[str, str]]:
    return _dockerfile_instructions()


def _args(dockerfile: list[tuple[str, str]], keyword: str) -> list[str]:
    return [args for kw, args in dockerfile if kw == keyword]


def test_imagem_base_python_fixada_e_coberta_pela_ci(dockerfile):
    (base,) = _args(dockerfile, "FROM")
    match = re.fullmatch(r"python:(?P<minor>3\.\d+)\.\d+-slim(-[a-z]+)?", base)
    assert match, f"imagem base sem versão de patch fixada: {base}"
    ci = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    matrix = ci["jobs"]["test"]["strategy"]["matrix"]["python-version"]
    assert match["minor"] in matrix, f"Python {match['minor']} da imagem não é testado na CI ({matrix})"


def test_dependencias_instaladas_so_pelo_lock_com_hash(dockerfile):
    installs = [args for args in _args(dockerfile, "RUN") if "pip install" in args]
    assert installs, "Dockerfile não instala dependências"
    for args in installs:
        assert "--require-hashes" in args, args
        assert "-r requirements.lock" in args, args
    copies = " ".join(_args(dockerfile, "COPY"))
    assert "requirements.lock" in copies


def test_roda_como_usuario_nao_root(dockerfile):
    users = _args(dockerfile, "USER")
    assert users, "Dockerfile sem USER: o processo rodaria como root"
    # UID:GID numéricos: runtimes com runAsNonRoot (Kubernetes) só verificam número.
    match = re.fullmatch(r"(?P<uid>\d+):(?P<gid>\d+)", users[-1])
    assert match, f"USER deve ser UID:GID numérico, não {users[-1]!r}"
    assert int(match["uid"]) >= 1000 and int(match["gid"]) >= 1000
    runs = " ".join(_args(dockerfile, "RUN"))
    assert f"--uid {match['uid']}" in runs and f"--gid {match['gid']}" in runs, "usuário criado com UID/GID fixos"
    last_user = max(i for i, (kw, _) in enumerate(dockerfile) if kw == "USER")
    cmd = max(i for i, (kw, _) in enumerate(dockerfile) if kw == "CMD")
    assert last_user < cmd


def test_bind_em_todas_as_interfaces_so_dentro_do_container(dockerfile):
    envs = " ".join(_args(dockerfile, "ENV"))
    assert re.search(r"\bAPP_HOST=0\.0\.0\.0\b", envs), "o container precisa definir APP_HOST=0.0.0.0"


def test_healthcheck_com_python_stdlib_sem_curl(dockerfile):
    (healthcheck,) = _args(dockerfile, "HEALTHCHECK")
    assert "curl" not in healthcheck and "wget" not in healthcheck
    assert "python" in healthcheck and "urllib.request" in healthcheck
    # /livez: público e sem dependências (o /admin/health dá 503 com o Mongo fora).
    assert "/livez" in healthcheck and "/admin/health" not in healthcheck


def test_imagem_sem_compilador(dockerfile):
    runs = " ".join(_args(dockerfile, "RUN"))
    assert "gcc" not in runs, "todo pacote do lock tem wheel; compilador só aumenta a superfície"


# ── docker-compose ─────────────────────────────────────────────────


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return _load(COMPOSE)


@pytest.fixture(scope="module")
def compose_dev() -> dict[str, Any]:
    return _load(COMPOSE_DEV)


def _environment(service: dict[str, Any]) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
    return {str(k): "" if v is None else str(v) for k, v in env.items()}


def _published_container_ports(service: dict[str, Any]) -> set[str]:
    ports: set[str] = set()
    for entry in service.get("ports") or []:
        if isinstance(entry, dict):
            ports.add(str(entry["target"]))
        else:
            ports.add(str(entry).split("/")[0].rsplit(":", 1)[-1])
    return ports


def _required_vars(path: Path) -> set[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    text = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    return set(re.findall(r"\$\{([A-Z][A-Z0-9_]*):\?", text))


@pytest.mark.parametrize("path", [COMPOSE, COMPOSE_DEV], ids=lambda p: p.name)
def test_compose_sem_credencial_literal(path):
    problems = []
    for name, service in _load(path)["services"].items():
        for key, value in _environment(service).items():
            masked = INTERPOLATION.sub("\0", value)  # ${...} pode conter espaços e "/"
            userinfos = [m["userinfo"] for m in URL_USERINFO.finditer(masked)]
            for userinfo in userinfos:
                if userinfo.replace("\0", "").replace(":", ""):
                    problems.append(f"{name}.{key} tem usuário/senha literal na URL")
            # Repasse puro (`KEY:` sem valor) não carrega literal: o valor vem do .env/shell.
            if SECRET_KEY.search(key) and value and not userinfos and not REQUIRED_VAR.match(value):
                problems.append(f"{name}.{key} deve vir de ${{VAR:?mensagem}} ou repasse puro")
    assert not problems, problems


def test_compose_base_so_tem_o_app(compose):
    assert set(compose["services"]) == {"app"}, "serviços de apoio ficam no docker-compose.dev.yml"


def test_compose_base_nao_exige_credencial_de_servico_de_apoio():
    """Modo só-app: obrigatórias só a connection string do Mongo externo e as chaves da borda (F1-04)."""
    assert _required_vars(COMPOSE) == {"MONGO_CONNECTION_STRING", "API_KEY_RUN", "API_KEY_ADMIN"}


def test_compose_base_nao_publica_porta_de_apoio(compose):
    app = compose["services"]["app"]
    assert not _published_container_ports(app) & SUPPORT_PORTS
    assert "depends_on" not in app, "o app base não depende de serviço que só existe no arquivo dev"


def test_compose_app_endurecido(compose):
    app = compose["services"]["app"]
    assert "no-new-privileges:true" in (app.get("security_opt") or [])
    assert app.get("cap_drop") == ["ALL"]
    assert app.get("read_only") is True
    assert not app.get("volumes"), "nada grava em disco no runtime: sem bind mount (ex.: ./logs)"


def test_compose_ollama_base_url_explicito_para_o_container(compose, compose_dev):
    assert "OLLAMA_BASE_URL" in _environment(compose["services"]["app"])
    assert _environment(compose_dev["services"]["app"])["OLLAMA_BASE_URL"] == "http://ollama:11434"


def test_compose_dev_tem_os_servicos_de_apoio_com_portas_no_loopback(compose_dev):
    services = compose_dev["services"]
    assert SUPPORT_SERVICES <= set(services)
    for name in SUPPORT_SERVICES:
        for entry in services[name].get("ports") or []:
            assert str(entry).startswith("127.0.0.1:"), f"{name} publica {entry} em todas as interfaces"


def test_compose_dev_liga_o_app_aos_servicos_locais(compose_dev):
    app = compose_dev["services"]["app"]
    depends_on = app.get("depends_on") or {}
    assert {"mongodb", "ollama"} <= set(depends_on)
    # Atlas Local leva alguns segundos (mongod + mongot) até aceitar conexão: o app espera o healthcheck.
    assert depends_on["mongodb"] == {"condition": "service_healthy"}
    url = _environment(app)["MONGO_CONNECTION_STRING"]
    assert "@mongodb:27017/" in url
    # Réplica de um nó só: sem directConnection o driver tenta o host anunciado pelo replica set.
    assert "directConnection=true" in url


# ── MongoDB Atlas Local (F2-10) ────────────────────────────────────

ATLAS_LOCAL_IMAGE = re.compile(r"mongodb/mongodb-atlas-local:\d+\.\d+\.\d+-\d{8}T\d{6}Z")
ATLAS_LOCAL_DATA_DIRS = {"/data/db", "/data/configdb", "/data/mongot"}
INIT_DIR = "/docker-entrypoint-initdb.d"


def _mounts(service: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(origem, destino, modo) de cada volume na sintaxe curta ``origem:destino[:modo]``."""
    mounts = []
    for entry in service.get("volumes") or []:
        assert isinstance(entry, str), f"use a sintaxe curta de volume: {entry}"
        source, target, *mode = entry.split(":")
        mounts.append((source, target, mode[0] if mode else ""))
    return mounts


def _is_named_volume(source: str) -> bool:
    return not source.startswith((".", "/", "~"))


def test_compose_dev_mongodb_e_o_atlas_local_com_vector_search(compose_dev):
    """O RAG semântico (MongoDb do agno) cria índice vectorSearch e consulta com $vectorSearch."""
    mongodb = compose_dev["services"]["mongodb"]
    assert ATLAS_LOCAL_IMAGE.fullmatch(mongodb["image"]), "tag datada da imagem do Atlas Local"
    env = _environment(mongodb)
    assert env["MONGODB_INITDB_ROOT_USERNAME"].startswith("${MONGO_ROOT_USERNAME:?")
    assert env["MONGODB_INITDB_ROOT_PASSWORD"].startswith("${MONGO_ROOT_PASSWORD:?")
    assert env["MONGODB_INITDB_DATABASE"] == "${MONGO_DATABASE_NAME:-agno}"
    assert env["DO_NOT_TRACK"] == "1", "sem telemetria da imagem"
    # As variáveis da imagem mongo oficial (MONGO_INITDB_*) são ignoradas pelo Atlas Local:
    # com elas o banco subiria sem usuário root.
    assert not [key for key in env if key.startswith("MONGO_INITDB_")]
    assert "healthcheck" not in mongodb, "o healthcheck embutido na imagem (runner healthcheck) basta"


def test_compose_dev_mongodb_com_hostname_fixo(compose_dev):
    """O replica set do Atlas Local guarda o hostname em /data/db.

    Sem hostname fixo, recriar o container (down/up, troca de imagem) muda o hostname e o nó
    não se acha na config salva: fica sem primário e nunca sai de "starting" (reproduzido no F2-10).
    """
    assert compose_dev["services"]["mongodb"].get("hostname") == "mongodb"


def test_compose_dev_mongodb_com_volumes_proprios_e_seed_somente_leitura(compose_dev):
    mounts = _mounts(compose_dev["services"]["mongodb"])
    named = {target: source for source, target, _mode in mounts if _is_named_volume(source)}
    assert set(named) == ATLAS_LOCAL_DATA_DIRS
    # O volume do mongo:7 não é reaproveitado: migração por mongodump/mongorestore (README).
    assert "mongodb_data" not in named.values()
    assert len(set(named.values())) == len(named), "um volume por diretório de dados"
    assert [m for m in mounts if m[1] == INIT_DIR] == [("./mongo-init", INIT_DIR, "ro")]


def test_compose_dev_sem_volume_orfao(compose_dev):
    used = {
        source
        for service in compose_dev["services"].values()
        for source, _target, _mode in _mounts(service)
        if _is_named_volume(source)
    }
    assert set(compose_dev.get("volumes") or {}) == used


def test_compose_dev_mongo_express_exige_basic_auth(compose_dev):
    """O mongo-express dá acesso root ao banco: login obrigatório, nunca desligado."""
    env = _environment(compose_dev["services"]["mongo-express"])
    assert env["ME_CONFIG_BASICAUTH_USERNAME"].startswith("${MONGO_EXPRESS_USERNAME:?")
    assert env["ME_CONFIG_BASICAUTH_PASSWORD"].startswith("${MONGO_EXPRESS_PASSWORD:?")
    # ausente (default da imagem: ligado) ou exatamente "true"; qualquer outro valor pode desligar o login
    assert env.get("ME_CONFIG_BASICAUTH", "true").strip().lower() == "true"


def test_compose_dev_mongo_express_conecta_direto_e_espera_o_mongodb(compose_dev):
    mongo_express = compose_dev["services"]["mongo-express"]
    assert "directConnection=true" in _environment(mongo_express)["ME_CONFIG_MONGODB_URL"]
    assert (mongo_express.get("depends_on") or {}).get("mongodb") == {"condition": "service_healthy"}


@pytest.mark.parametrize("path", [COMPOSE, COMPOSE_DEV], ids=lambda p: p.name)
def test_compose_imagens_com_tag_fixa(path):
    for name, service in _load(path)["services"].items():
        image = service.get("image")
        if image is None:
            continue
        _repo, sep, tag = image.rpartition(":")
        assert sep and "/" not in tag, f"{name}: imagem {image} sem tag"
        assert tag != "latest" and re.search(r"\d+\.\d+", tag), f"{name}: tag {tag} não é fixa"


# ── ENVIRONMENT (F1-03) ────────────────────────────────────────────


def test_imagem_assume_production(dockerfile):
    """Docs desligados e log JSON por padrão no container; development é opt-in."""
    envs = " ".join(_args(dockerfile, "ENV"))
    assert re.search(r"\bENVIRONMENT=production\b", envs)


def test_compose_base_nao_fixa_environment(compose):
    """Repasse puro: sem ENVIRONMENT no .env/shell, vale o production da imagem."""
    assert compose["services"]["app"]["environment"]["ENVIRONMENT"] is None


def test_compose_dev_roda_o_app_em_development(compose_dev):
    assert _environment(compose_dev["services"]["app"])["ENVIRONMENT"] == "development"


def test_env_example_nao_sobrescreve_o_environment_da_imagem():
    """Linha ativa no .env seria repassada pelo compose e venceria o ENV da imagem."""
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    active = [line for line in lines if re.match(r"\s*ENVIRONMENT\s*=", line)]
    documented = [line for line in lines if re.match(r"\s*#\s*ENVIRONMENT\s*=", line)]
    assert not active and documented
