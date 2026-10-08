"""`.env.example` e `.env.development` (F1-02).

O `.env.example` documenta exatamente as variáveis que o projeto lê: as lidas pelo
código (``os.getenv``/``os.environ``), as do padrão ``<PROVIDER>_API_KEY`` das
factories de modelo e as interpoladas pelo ``docker-compose.yml``. O
``.env.development`` é versionado, então só pode ter placeholders.

As mensagens de falha citam só NOMES de chaves, nunca valores.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.example"
ENV_DEVELOPMENT = ROOT / ".env.development"
COMPOSE_FILES = [ROOT / "docker-compose.yml", ROOT / "docker-compose.dev.yml"]
CODE_FILES = [ROOT / "app.py", *sorted((ROOT / "src").rglob("*.py"))]

# Providers cujas factories leem os.getenv(f"{provider.upper()}_API_KEY").
PROVIDERS = {"OPENAI", "ANTHROPIC", "GEMINI", "GROQ", "AZURE", "OLLAMA"}
PROVIDER_API_KEYS = {f"{p}_API_KEY" for p in PROVIDERS}

ENV_LINE = re.compile(r"^\s*(?P<comment>#\s*)?(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")
COMPOSE_VAR = re.compile(r"\$\{(?P<name>[A-Z][A-Z0-9_]*)(?P<op>:?[-?])?[^}]*\}")
SECRET_KEY = re.compile(r"(PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|_KEY$)")
URL_USERINFO = re.compile(r"://(?P<userinfo>[^/@\s]+)@")
PLACEHOLDER = "CHANGE_ME"


def _is_environ(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ"


def _env_vars_read_by_code() -> set[str]:
    """Nomes literais em os.getenv("X"), os.environ.get/setdefault("X") e os.environ["X"]."""
    names: set[str] = set()
    for path in CODE_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            arg: ast.expr | None = None
            if isinstance(node, ast.Call) and node.args:
                func = node.func
                is_getenv = (isinstance(func, ast.Attribute) and func.attr == "getenv") or (
                    isinstance(func, ast.Name) and func.id == "getenv"
                )
                is_environ_get = (
                    isinstance(func, ast.Attribute) and func.attr in ("get", "setdefault") and _is_environ(func.value)
                )
                if is_getenv or is_environ_get:
                    arg = node.args[0]
            elif isinstance(node, ast.Subscript) and _is_environ(node.value):
                arg = node.slice
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names.add(arg.value)
    return names


def _compose_vars() -> dict[str, str]:
    """Variáveis interpoladas nos composes -> operador (``:?`` = obrigatória)."""
    found: dict[str, str] = {}
    for path in COMPOSE_FILES:
        lines = path.read_text(encoding="utf-8").splitlines()
        text = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
        for m in COMPOSE_VAR.finditer(text):
            if found.get(m["name"]) != ":?":
                found[m["name"]] = m["op"] or ""
    return found


def _entries(path: Path) -> list[tuple[str, str, bool]]:
    """(chave, valor, comentada) de cada linha CHAVE=valor, inclusive `# CHAVE=`."""
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ENV_LINE.match(line)
        if match:
            entries.append((match["key"], match["value"].strip(), bool(match["comment"])))
    return entries


def _allowed_keys() -> set[str]:
    return _env_vars_read_by_code() | PROVIDER_API_KEYS | set(_compose_vars())


def test_scanner_enxerga_as_leituras_conhecidas():
    """Sanidade do scanner: se ele parar de ver leituras, o teste seguinte vira vazio."""
    read = _env_vars_read_by_code()
    assert {"MONGO_CONNECTION_STRING", "APP_HOST", "APP_PORT", "OLLAMA_BASE_URL", "GEMINI_API_KEY"} <= read
    # F1-03: borda e telemetria (AGNO_TELEMETRY via os.environ.setdefault)
    assert {"ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "AGNO_TELEMETRY"} <= read


def test_env_example_lista_toda_variavel_lida_pelo_codigo():
    listed = {key for key, _, _ in _entries(ENV_EXAMPLE)}
    missing = _env_vars_read_by_code() - listed
    assert not missing, f"variáveis lidas pelo código e ausentes do .env.example: {sorted(missing)}"


def test_env_example_documenta_as_chaves_de_provider():
    listed = {key for key, _, _ in _entries(ENV_EXAMPLE)}
    assert PROVIDER_API_KEYS <= listed, sorted(PROVIDER_API_KEYS - listed)


def test_env_example_lista_as_variaveis_obrigatorias_do_compose():
    listed = {key for key, _, _ in _entries(ENV_EXAMPLE)}
    required = {name for name, op in _compose_vars().items() if op == ":?"}
    assert required, "o compose deveria exigir credenciais com ${VAR:?}"
    assert required <= listed, sorted(required - listed)


def test_env_example_sem_variavel_que_ninguem_le():
    listed = {key for key, _, _ in _entries(ENV_EXAMPLE)}
    orphan = listed - _allowed_keys()
    assert not orphan, f"variáveis no .env.example que nenhum código/compose lê: {sorted(orphan)}"


def _secret_like_keys_with_real_values(path: Path) -> list[str]:
    bad: list[str] = []
    for key, value, _commented in _entries(path):
        if not value:
            continue
        if SECRET_KEY.search(key) and value != PLACEHOLDER:
            bad.append(key)
        for match in URL_USERINFO.finditer(value):
            if any(part != PLACEHOLDER for part in match["userinfo"].split(":")):
                bad.append(key)
    return bad


def test_env_example_sem_segredo():
    assert not _secret_like_keys_with_real_values(ENV_EXAMPLE)


def test_env_development_so_com_placeholders():
    assert ENV_DEVELOPMENT.is_file()
    bad = _secret_like_keys_with_real_values(ENV_DEVELOPMENT)
    assert not bad, f"chaves com valor que não é {PLACEHOLDER} no .env.development: {sorted(set(bad))}"


def test_env_development_sem_variavel_que_ninguem_le():
    keys = {key for key, _, _ in _entries(ENV_DEVELOPMENT)}
    orphan = keys - _allowed_keys()
    assert not orphan, f"variáveis no .env.development que nenhum código/compose lê: {sorted(orphan)}"


# Não repassadas ao container: o Dockerfile fixa APP_HOST/APP_PORT (bind e porta
# publicada) e HOSTNAME vem do próprio container.
NOT_FORWARDED = {"APP_HOST", "APP_PORT", "HOSTNAME"}


def _app_environment(path: Path) -> dict[str, object]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    env = data["services"]["app"].get("environment") or {}
    assert isinstance(env, dict), "use a forma de mapa em environment"
    return env


def test_compose_repassa_ao_app_as_variaveis_lidas_pelo_codigo():
    """Chaves de provider, ENVIRONMENT, TLS etc. do .env chegam ao container do app."""
    env = _app_environment(COMPOSE_FILES[0])
    expected = (_env_vars_read_by_code() | PROVIDER_API_KEYS) - NOT_FORWARDED
    missing = expected - set(env)
    assert not missing, f"variáveis do .env que não chegam ao container: {sorted(missing)}"
    assert not NOT_FORWARDED & set(env), "APP_HOST/APP_PORT/HOSTNAME vêm do Dockerfile/container"


def test_compose_repasse_opcional_sem_default_inventado():
    """Variável opcional sem valor no .env fica ausente no container (default do código vale)."""
    env = _app_environment(COMPOSE_FILES[0])
    for key in PROVIDER_API_KEYS | {"AZURE_ENDPOINT", "AZURE_VERSION", "ENVIRONMENT", "USE_TLS"}:
        assert env[key] is None, f"{key} deve ser repasse puro (`{key}:`), sem valor no compose"


def test_compose_dev_continua_vencendo_para_hosts_do_container():
    dev = _app_environment(COMPOSE_FILES[1])
    assert dev["OLLAMA_BASE_URL"] == "http://ollama:11434"
    assert "@mongodb:27017" in str(dev["MONGO_CONNECTION_STRING"])
