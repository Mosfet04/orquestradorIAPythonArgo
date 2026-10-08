"""Higiene de container do F1-02: `.dockerignore`, Dockerfile e docker-compose.

Validação estática (o build real depende do Docker daemon, que a suíte não exige):
o contexto de build é uma allowlist sem segredos nem lixo local, a imagem instala pelo
lock com hash e roda sem root (UID/GID numéricos), o ``docker-compose.yml`` só tem o
app (endurecido) e os serviços de apoio com portas ficam no ``docker-compose.dev.yml``,
sem credencial literal.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

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


# ── .dockerignore (emulação do moby/patternmatcher) ────────────────


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Converte um padrão do `.dockerignore` em regex como o ``patternmatcher.compile``.

    ``**`` casa qualquer coisa (inclusive ``/``; ``**/`` também casa zero diretórios),
    ``*`` casa tudo menos ``/``, ``?`` casa um caractere que não é ``/``; os
    metacaracteres ``.+()|{}$`` são escapados, o resto passa como regex (ex.: ``[cod]``).
    Ancorado nas duas pontas.
    """
    regex = "^"
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                i += 1
                if i + 1 < len(pattern) and pattern[i + 1] == "/":
                    i += 1
                regex += ".*" if i + 1 >= len(pattern) else "(.*/)?"
            else:
                regex += "[^/]*"
        elif ch == "?":
            regex += "[^/]"
        elif ch in ".+()|{}$":
            regex += "\\" + ch
        elif ch == "\\" and i + 1 < len(pattern):
            i += 1
            regex += re.escape(pattern[i])
        else:
            regex += ch
        i += 1
    return re.compile(regex + "$")


def _parse_dockerignore(text: str) -> list[tuple[bool, re.Pattern[str]]]:
    """(é negação, regex), na ordem do arquivo, como o ``ignorefile.ReadAll``.

    Comentário só se ``#`` for o 1º caractere da linha ANTES do trim (``"  #x"`` é padrão).
    """
    rules: list[tuple[bool, re.Pattern[str]]] = []
    for raw in text.splitlines():
        if raw.startswith("#"):
            continue
        line = raw.strip()
        if not line:
            continue
        negate = line.startswith("!")
        pattern = (line[1:] if negate else line).strip()
        pattern = posixpath.normpath(pattern)  # filepath.Clean: "src/" -> "src"
        if len(pattern) > 1 and pattern.startswith("/"):
            pattern = pattern[1:]
        rules.append((negate, _compile_pattern(pattern)))
    return rules


def _excluded(path: str, rules: list[tuple[bool, re.Pattern[str]]]) -> bool:
    """``PatternMatcher.MatchesOrParentMatches``: o arquivo ou um diretório pai casa;
    a última regra aplicável vence (negação ``!`` reinclui)."""
    parents = posixpath.dirname(path).split("/") if "/" in path else []
    prefixes = ["/".join(parents[: i + 1]) for i in range(len(parents))]
    matched = False
    for negate, regex in rules:
        if negate != matched:
            continue
        if regex.match(path) or any(regex.match(p) for p in prefixes):
            matched = not negate
    return matched


@pytest.fixture(scope="module")
def dockerignore_rules() -> list[tuple[bool, re.Pattern[str]]]:
    return _parse_dockerignore(DOCKERIGNORE.read_text(encoding="utf-8"))


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
    assert _excluded(path, _parse_dockerignore(rules)) is expected


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
    assert _excluded(path, dockerignore_rules), f"{path} entraria no contexto de build"


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
    assert not _excluded(path, dockerignore_rules), f"{path} ficaria fora do contexto de build"


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
    # /livez chega na F1-03; até lá o health é o /admin/health.
    assert "/admin/health" in healthcheck or "/livez" in healthcheck


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
    """Modo só-app: a única variável obrigatória é a connection string do Mongo externo."""
    assert _required_vars(COMPOSE) == {"MONGO_CONNECTION_STRING"}


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
    depends_on = app.get("depends_on") or []
    assert {"mongodb", "ollama"} <= set(depends_on)
    assert "@mongodb:27017" in _environment(app)["MONGO_CONNECTION_STRING"]


@pytest.mark.parametrize("path", [COMPOSE, COMPOSE_DEV], ids=lambda p: p.name)
def test_compose_imagens_com_tag_fixa(path):
    for name, service in _load(path)["services"].items():
        image = service.get("image")
        if image is None:
            continue
        _repo, sep, tag = image.rpartition(":")
        assert sep and "/" not in tag, f"{name}: imagem {image} sem tag"
        assert tag != "latest" and re.search(r"\d+\.\d+", tag), f"{name}: tag {tag} não é fixa"
