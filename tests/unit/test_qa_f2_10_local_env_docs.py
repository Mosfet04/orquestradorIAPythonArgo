"""QA F2-10: o passo a passo local (README pt-br/en, CONTRIBUTING, ``.env.example``) bate com o compose.

Validação estática do que o usuário novo copia e cola: variáveis que o compose exige, URL de conexão
do modo "app no host", portas, volumes, serviços citados em ``up -d`` e o documento de exemplo do
agente RAG (carregado pelo repositório Mongo real sobre a coleção em memória). A verificação com
Docker de verdade foi manual (relato do QA).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from tests.fakes.config_stores import MongoBackend

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_BASE = ROOT / "docker-compose.yml"
COMPOSE_DEV = ROOT / "docker-compose.dev.yml"
ENV_EXAMPLE = ROOT / ".env.example"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"

# (arquivo, título da seção "passo a passo", título que a encerra)
READMES = {
    "pt-br": (ROOT / "README.pt-br.md", "### Rodar localmente (passo a passo)", "### Verificação"),
    "en": (ROOT / "README.en.md", "### Run locally (step by step)", "### Verification"),
}
LANGS = list(READMES)
REQUIRED_VAR = re.compile(r"\$\{([A-Z][A-Z0-9_]*):\?")
MONGO_URL = re.compile(r"mongodb://[^\s`\"')]*@localhost:27017[^\s`\"')]*")


def _section(lang: str) -> str:
    path, start, end = READMES[lang]
    text = path.read_text(encoding="utf-8")
    assert start in text, f"{path.name}: seção '{start}' não encontrada"
    begin = text.index(start)
    return text[begin : text.index(end, begin)]


def _bash_blocks(section: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", section, flags=re.DOTALL)


def _commands(block: str) -> list[str]:
    """Linhas de comando sem comentários e sem espaço sobrando (compara pt-br com en)."""
    lines = []
    for raw in block.splitlines():
        line = re.sub(r"\s+#.*$", "", raw).strip() if not raw.lstrip().startswith("#") else ""
        if line:
            lines.append(re.sub(r"\s+", " ", line))
    return lines


def _compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _env_example_keys() -> set[str]:
    keys = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"([A-Z][A-Z0-9_]*)=", line)
        if match:
            keys.add(match.group(1))
    return keys


def _required_vars() -> set[str]:
    found: set[str] = set()
    for path in (COMPOSE_BASE, COMPOSE_DEV):
        code = "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
        )
        found |= set(REQUIRED_VAR.findall(code))
    return found


# ── variáveis exigidas pelo compose: o passo 1 manda preencher todas ─────────────────


def test_compose_dev_exige_exatamente_as_variaveis_que_o_env_example_declara():
    required = _required_vars()
    assert {
        "MONGO_ROOT_USERNAME",
        "MONGO_ROOT_PASSWORD",
        "MONGO_EXPRESS_USERNAME",
        "MONGO_EXPRESS_PASSWORD",
        "MONGO_CONNECTION_STRING",
        "API_KEY_RUN",
        "API_KEY_ADMIN",
    } <= required
    assert required <= _env_example_keys(), "variável exigida pelo compose ausente do .env.example"


@pytest.mark.parametrize("lang", LANGS)
def test_passo_1_manda_preencher_toda_variavel_que_o_compose_exige(lang):
    """Se o compose passar a exigir uma variável nova, o passo 1 do README tem de citá-la."""
    section = _section(lang)
    table = section[section.index("| Variável") if "| Variável" in section else section.index("| Variable") :]
    table = table[: table.index("\n\n")]
    for var in sorted(_required_vars()):
        assert f"`{var}`" in table, f"{lang}: {var} é exigida pelo compose mas não está na tabela do passo 1"


@pytest.mark.parametrize("lang", LANGS)
def test_passo_1_avisa_que_as_credenciais_do_mongo_entram_numa_url(lang):
    section = _section(lang)
    assert "URL" in section
    assert re.search(r"URL-safe|`-`, `_`, `\.`", section), f"{lang}: falta o aviso de valores URL-safe"


# ── URL de conexão do modo (a) ─────────────────────────────────────────────────────


def test_env_example_conecta_direto_no_atlas_local_do_host():
    url = next(
        line.split("=", 1)[1]
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line.startswith("MONGO_CONNECTION_STRING=")
    )
    parts = urlsplit(url)
    assert (parts.hostname, parts.port) == ("localhost", 27017)
    assert parse_qs(parts.query) == {"directConnection": ["true"]}
    # Sem banco no caminho a autenticação é no admin; com banco, authSource=admin é obrigatório.
    assert parts.path in ("", "/")


@pytest.mark.parametrize("source", ["README.pt-br.md", "README.en.md", "CONTRIBUTING.md"])
def test_toda_url_com_credencial_em_localhost_27017_tem_direct_connection(source):
    text = (ROOT / source).read_text(encoding="utf-8")
    urls = MONGO_URL.findall(text)
    assert urls, f"{source}: nenhuma URL do Atlas Local encontrada"
    for url in urls:
        # ``${VAR}``/``<placeholder>`` no userinfo: só o resto da URL importa.
        query = parse_qs(urlsplit(re.sub(r"^mongodb://[^@]*@", "mongodb://x@", url)).query)
        assert query.get("directConnection") == ["true"], f"{source}: sem directConnection=true: {url}"
        path = urlsplit(re.sub(r"^mongodb://[^@]*@", "mongodb://x@", url)).path
        if path not in ("", "/"):
            assert query.get("authSource") == ["admin"], f"{source}: banco no caminho sem authSource=admin: {url}"


# ── compose x README: serviços, portas, volumes e imagem ───────────────────────────


@pytest.mark.parametrize("lang", LANGS)
def test_servicos_citados_nos_comandos_up_existem_no_compose_dev(lang):
    services = set(_compose(COMPOSE_DEV)["services"]) | set(_compose(COMPOSE_BASE)["services"])
    found = []
    for block in _bash_blocks(_section(lang)):
        for command in _commands(block):
            match = re.search(r"\$COMPOSE up -d(?: --build)?((?: [a-z][a-z-]*)*)(?: |$)", command)
            if match:
                found += match.group(1).split()
    assert {"mongodb", "mongo-express"} <= set(found)
    assert set(found) <= services


@pytest.mark.parametrize("lang", LANGS)
def test_volumes_do_passo_de_limpeza_sao_os_do_compose(lang):
    declared = set(_compose(COMPOSE_DEV)["volumes"])
    mongo_volumes = {name for name in declared if name.startswith("mongodb_atlas_")}
    assert len(mongo_volumes) == 3
    section = _section(lang)
    cited = set(re.findall(r"`(mongodb_atlas_\w+)`", section))
    assert cited == mongo_volumes


@pytest.mark.parametrize("lang", LANGS)
def test_portas_citadas_no_readme_sao_as_publicadas_no_loopback(lang):
    path = READMES[lang][0]
    text = path.read_text(encoding="utf-8")
    published = {}
    for name, service in _compose(COMPOSE_DEV)["services"].items():
        for port in service.get("ports") or []:
            host, host_port, container_port = str(port).split(":")
            assert host == "127.0.0.1"
            assert host_port == container_port
            published[name] = host_port
    assert published == {"mongodb": "27017", "ollama": "11434", "mongo-express": "8081"}
    for port in published.values():
        assert port in text


@pytest.mark.parametrize("lang", LANGS)
def test_readme_cita_o_repositorio_da_imagem_do_compose_e_nao_o_mongo_7_como_servico(lang):
    image = _compose(COMPOSE_DEV)["services"]["mongodb"]["image"]
    repository = image.split(":")[0]
    section = _section(lang)
    assert repository in section
    # ``mongo:7`` só aparece na migração do volume antigo, sempre com tag fixa (nunca ``latest``/``7``).
    commands = [line for block in _bash_blocks(section) for line in _commands(block)]
    for tag in re.findall(r"\bmongo:([\w.\-]+)", "\n".join(commands)):
        assert re.fullmatch(r"\d+\.\d+\.\d+(-\w+)?", tag), f"{lang}: imagem do mongo sem tag fixa: {tag}"


# ── pt-br e en descrevem os mesmos comandos ───────────────────────────────────────


def test_passo_a_passo_tem_os_mesmos_comandos_em_pt_br_e_em_ingles():
    blocks = {lang: [_commands(b) for b in _bash_blocks(_section(lang))] for lang in LANGS}
    assert len(blocks["pt-br"]) == len(blocks["en"]) > 0
    for index, (pt, en) in enumerate(zip(blocks["pt-br"], blocks["en"], strict=True)):
        # ``<usuário>:<senha>``, ``<volume antigo>`` e o texto da pergunta mudam de idioma; o resto do comando não.
        normalize = lambda lines: [  # noqa: E731
            re.sub(
                r"<(volume antigo|old volume)>",
                "<volume>",
                re.sub(
                    r'message="[^"]*"',
                    "message=<texto>",
                    re.sub(r"<(usu[aá]rio|user)>:<(senha|password)>", "<u>:<p>", line),
                ),
            )
            for line in lines
        ]
        assert normalize(pt) == normalize(en), f"bloco bash #{index} difere entre pt-br e en"


def test_passo_a_passo_tem_os_mesmos_passos_em_pt_br_e_em_ingles():
    steps = {lang: re.findall(r"\*\*(\d[ab]?)\.", _section(lang)) for lang in LANGS}
    assert steps["pt-br"] == steps["en"] == ["1", "2", "3a", "3b", "4", "5", "6", "7", "8"]


@pytest.mark.parametrize("lang", LANGS)
def test_comandos_do_passo_a_passo_nao_usam_o_comando_pytest_sem_o_venv_ativo(lang):
    """O passo 3a ativa o ``.venv`` antes; o live test do passo 7 roda nele."""
    section = _section(lang)
    assert "source .venv/bin/activate" in section
    assert section.index("source .venv/bin/activate") < section.index("pytest -m live")


# ── documento de exemplo do agente RAG ────────────────────────────────────────────


def _readme_rag_document(lang: str) -> dict:
    block = re.search(r"```javascript\n(.*?)```", _section(lang), flags=re.DOTALL)
    assert block, f"{lang}: bloco javascript do agente RAG não encontrado"
    body = "\n".join(line for line in block.group(1).splitlines() if not line.lstrip().startswith("//"))
    payload = body[body.index("insertOne(") + len("insertOne(") : body.rindex(");")]
    return json.loads(payload)


@pytest.mark.parametrize("lang", LANGS)
async def test_documento_rag_do_readme_carrega_como_agente_com_embedder_de_768_dimensoes(lang, monkeypatch):
    document = _readme_rag_document(lang)
    repo = MongoBackend(monkeypatch).agents([document])
    [agent] = await repo.get_active_agents()
    assert agent.id == "rag-assistant"
    rag = agent.rag_config
    assert rag is not None and rag.active
    assert rag.doc_name == "basic-prog.txt"
    assert (ROOT / "docs" / rag.doc_name).is_file(), "o documento citado precisa existir em docs/"
    embedder = rag.model_config
    assert embedder is not None
    assert (embedder.provider, embedder.model_id) == ("ollama", "nomic-embed-text")
    assert dict(embedder.params or {}) == {"dimensions": 768}


@pytest.mark.parametrize("lang", LANGS)
def test_documento_rag_do_readme_usa_o_mesmo_modelo_de_chat_do_seed(lang):
    seed = (ROOT / "mongo-init" / "init-db.js").read_text(encoding="utf-8")
    document = _readme_rag_document(lang)
    assert f'"model": "{document["model"]}"' in seed
    assert f'"factoryIaModel": "{document["factoryIaModel"]}"' in seed
