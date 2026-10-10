"""Protege o workflow de CI do F0-02 contra regressão de segurança e de gates.

Validação estática do YAML (complementa `actionlint`/`zizmor`, que não estão no lock):
permissões mínimas, actions fixadas por SHA, escopo do segredo do Codacy, instalação só
pelos locks com hash e presença dos gates da fase F0.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOWS_DIR / "ci.yml"
LOCKS = {"requirements.lock", "requirements-dev.lock"}
CODACY_EXPRESSION = "secrets.CODACY_API_TOKEN"
# owner/repo[/caminho]@<sha de 40 hex>  # vX.Y.Z
PINNED_USES = re.compile(r"^[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}$")
USES_LINE = re.compile(r"^\s*(-\s+)?uses:\s*(?P<ref>\S+)(?P<rest>.*)$")


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")])


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} não é um mapeamento YAML"
    return data


def _steps(workflow: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any], dict[str, Any]]]:
    for job_id, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            yield job_id, job, step


def _commands(run: str) -> list[str]:
    """Divide um bloco `run` em comandos lógicos (junta continuações com `\\`)."""
    joined = re.sub(r"\\\n", " ", run)
    return [line.strip() for line in joined.splitlines() if line.strip() and not line.strip().startswith("#")]


@pytest.fixture(scope="module")
def ci() -> dict[str, Any]:
    return _load(CI_WORKFLOW)


def test_ci_existe_e_workflow_antigo_foi_removido():
    assert CI_WORKFLOW.is_file()
    assert not (WORKFLOWS_DIR / "tests-codacy-coverage.yml").exists()


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_gatilhos_so_push_e_pull_request(path):
    workflow = _load(path)
    # PyYAML (YAML 1.1) lê a chave `on` como o booleano True.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict), "gatilhos devem ser um mapeamento"
    # nada de pull_request_target/workflow_run (rodam com segredos sobre código de fork)
    assert set(triggers) == {"push", "pull_request"}


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_permissions_minimas_no_topo(path):
    workflow = _load(path)
    assert workflow.get("permissions") == {"contents": "read"}
    for job_id, job in workflow["jobs"].items():
        # nenhum job amplia o token além de leitura do conteúdo
        assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}, job_id


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_toda_action_fixada_por_sha_com_comentario_de_versao(path):
    refs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = USES_LINE.match(line)
        if not match:
            continue
        ref = match["ref"].strip("'\"")
        refs.append(ref)
        assert PINNED_USES.match(ref), f"`uses: {ref}` não está fixado por SHA de 40 hex"
        assert re.search(r"#\s*v\d+(\.\d+)*\b", match["rest"]), f"`uses: {ref}` sem comentário `# vX.Y.Z`"
    # o texto bruto e o YAML concordam (nenhum `uses` escapou da regex de linha)
    parsed = [step["uses"] for _, _, step in _steps(_load(path)) if "uses" in step]
    assert sorted(refs) == sorted(parsed)
    assert parsed, "workflow sem nenhuma action (regex ou YAML quebrado)"


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_checkout_nao_persiste_credenciais(path):
    checkouts = [step for _, _, step in _steps(_load(path)) if step.get("uses", "").startswith("actions/checkout@")]
    assert checkouts
    for step in checkouts:
        assert step.get("with", {}).get("persist-credentials") is False


def test_segredo_do_codacy_so_no_passo_de_envio(ci):
    # nada de segredo em env do workflow nem de job
    assert "secrets." not in yaml.safe_dump(ci.get("env", {}))
    for job_id, job in ci["jobs"].items():
        assert "secrets." not in yaml.safe_dump(job.get("env", {})), job_id

    with_secret = [
        (job_id, job, step) for job_id, job, step in _steps(ci) if "secrets." in yaml.safe_dump(step, width=10_000)
    ]
    assert len(with_secret) == 1, "o segredo deve aparecer em exatamente um passo"
    job_id, job, step = with_secret[0]

    # só em `env` do passo (nunca interpolado no script nem em `with`)
    assert CODACY_EXPRESSION in str(step.get("env", {}).get("CODACY_API_TOKEN", ""))
    assert "secrets." not in step.get("run", "")
    assert "with" not in step
    assert "codacy-coverage-reporter" in step["run"]
    # pyproject.toml: [tool.coverage.run] source = ["src"] faz o coverage.xml listar
    # caminhos relativos a src/ (ex.: "domain/..."). O job do Codacy não tem .git para o
    # reporter casar os arquivos, então o prefixo precisa ser explícito.
    assert re.search(r"report\s+-r\s+coverage\.xml\s+--prefix\s+src/(\s|$)", step["run"]), step["run"]

    # o job que envia não instala dependências nem roda código do repositório
    assert not any("pip install" in s.get("run", "") for s in job["steps"])
    assert not any(s.get("uses", "").startswith("actions/checkout@") for s in job["steps"])

    # sem segredo em PR de fork (lá ele nem existe): só push ou PR do próprio repositório
    condition = re.sub(r"\s+", " ", job.get("if", ""))
    assert "github.event_name == 'push'" in condition
    assert "github.event.pull_request.head.repo.full_name == github.repository" in condition, job_id


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_instalacao_so_pelos_locks_com_hash(path):
    installs = []
    for _, _, step in _steps(_load(path)):
        for command in _commands(step.get("run", "")):
            if "pip install" not in command:
                continue
            installs.append(command)
            args = shlex.split(command)
            args = args[args.index("install") + 1 :]
            assert "--require-hashes" in args, command
            files = [args[i + 1] for i, arg in enumerate(args) if arg == "-r"]
            assert set(files) == LOCKS, command
            # nada além dos locks: nenhum pacote avulso, nenhum `--upgrade pip`
            assert set(args) <= {"--require-hashes", "-r", *LOCKS}, command
    assert installs, "nenhuma instalação encontrada"


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_sem_download_executado_direto_no_shell(path):
    for _, _, step in _steps(_load(path)):
        run = step.get("run", "")
        assert not re.search(r"\|\s*(ba|z)?sh\b", run), "proibido `curl | bash`"
        assert "<(curl" not in run and "<(wget" not in run, "proibido `bash <(curl ...)`"
        if re.search(r"\b(curl|wget)\b", run):
            assert re.search(r"sha256sum\s+(--check|-c)\b", run), "download sem verificação de checksum"


def test_concorrencia_e_timeouts(ci):
    # cancela run antigo só em PR; em push (main inclusive) todo commit termina o envio ao Codacy
    assert ci["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    for job_id, job in ci["jobs"].items():
        assert isinstance(job.get("timeout-minutes"), int), job_id


def test_gates_da_fase_f0(ci):
    jobs = ci["jobs"]
    assert {"lint", "test", "security"} <= set(jobs)

    lint = " ".join(s.get("run", "") for s in jobs["lint"]["steps"])
    assert "ruff check src tests app.py" in lint
    assert re.search(r"\bmypy\b", lint)
    assert "lint-imports" in lint

    assert jobs["test"]["strategy"]["matrix"]["python-version"] == ["3.11", "3.12"]
    test = " ".join(s.get("run", "") for s in jobs["test"]["steps"])
    assert 'pytest -m "not live"' in test
    assert "--cov" in test

    security = " ".join(s.get("run", "") for s in jobs["security"]["steps"])
    assert "bandit -c pyproject.toml -r src" in security
    assert "pip-audit -r requirements.lock --require-hashes --disable-pip" in security
    # F1-03: bandit zerado; o job de segurança é bloqueante como os demais.
    for job_id in ("lint", "test", "security"):
        assert not jobs[job_id].get("continue-on-error"), job_id
        for step in jobs[job_id]["steps"]:
            assert not step.get("continue-on-error"), (job_id, step.get("name"))


def test_job_de_teste_roda_em_paralelo_com_xdist_do_dev_lock(ci):
    """F1-09: `-n auto` (pytest-xdist) no job de testes, e o plugin vem do dev lock com hash."""
    commands = [c for s in ci["jobs"]["test"]["steps"] for c in _commands(s.get("run", "")) if "pytest" in c]
    assert len(commands) == 1
    args = shlex.split(commands[0])
    assert "-n" in args and args[args.index("-n") + 1] == "auto", args
    assert "--cov" in args  # a cobertura dos workers é combinada pelo pytest-cov

    dev_in = (ROOT / "requirements-dev.in").read_text(encoding="utf-8")
    dev_lock = (ROOT / "requirements-dev.lock").read_text(encoding="utf-8")
    assert re.search(r"^pytest-xdist>=", dev_in, re.MULTILINE)
    assert re.search(r"^pytest-xdist==\S+ \\\n\s+--hash=sha256:", dev_lock, re.MULTILINE)
    assert re.search(r"^execnet==\S+ \\\n\s+--hash=sha256:", dev_lock, re.MULTILINE)


def test_pip_audit_ignora_so_a_vulnerabilidade_conhecida_do_agno(ci):
    """Única exceção aceita até a F3: PYSEC-2026-2333 (agno, backend ClickHouse não usado)."""
    security = " ".join(s.get("run", "") for s in ci["jobs"]["security"]["steps"])
    assert re.findall(r"--ignore-vuln\s+(\S+)", security) == ["PYSEC-2026-2333"]
    raw = CI_WORKFLOW.read_text(encoding="utf-8")
    comment = raw[: raw.index("--ignore-vuln PYSEC-2026-2333")].rsplit("- name: bandit", 1)[-1]
    assert "agno" in comment and "F3" in comment, "a exceção precisa de justificativa e prazo comentados"
