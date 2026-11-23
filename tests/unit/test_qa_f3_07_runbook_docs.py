"""QA F3-07: o runbook da virada para o agno 3.1.12 bate com o código e com o compose.

Validação estática (sem Mongo, sem rede): links relativos, flags do ``python -m`` da migração,
serviços/arquivos do compose, versão do agno e a regra de nunca chamar o cleanup no mesmo release.
"""

from __future__ import annotations

import re
from importlib.metadata import version
from pathlib import Path

import pytest
import yaml

from src.infrastructure.runtime.agno import session_migration

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs" / "runbooks" / "f3-agno-3.md"
DOCS_TOCADOS = [
    RUNBOOK,
    ROOT / "README.pt-br.md",
    ROOT / "README.en.md",
    ROOT / "CONTRIBUTING.md",
]
MODULE = "src.infrastructure.runtime.agno.session_migration"
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    return {_slug(h) for h in re.findall(r"^#{1,6}\s+(.*)$", path.read_text(encoding="utf-8"), re.M)}


@pytest.mark.unit
@pytest.mark.parametrize("doc", [RUNBOOK, ROOT / "README.pt-br.md", ROOT / "README.en.md"], ids=lambda p: p.name)
def test_links_relativos_e_ancoras_do_runbook_existem(doc: Path) -> None:
    broken = []
    for target in LINK.findall(doc.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        path, _, anchor = target.partition("#")
        dest = doc if not path else (doc.parent / path)
        if not dest.exists() or (anchor and dest.suffix == ".md" and anchor not in _anchors(dest)):
            broken.append(target)
    assert broken == []


@pytest.mark.unit
def test_contributing_aponta_para_o_runbook() -> None:
    assert "docs/runbooks/f3-agno-3.md" in (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")


@pytest.mark.unit
def test_flags_do_comando_de_migracao_citadas_no_runbook_existem_no_parser() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    cited = set(re.findall(rf"{re.escape(MODULE)}\s+(--[a-z-]+)", text))
    parser = session_migration._parser()
    known = {opt for action in parser._actions for opt in action.option_strings if opt.startswith("--")}
    assert {"--check", "--down"} <= cited
    assert cited <= known
    with pytest.raises(SystemExit) as info:  # exclusivas, como o runbook diz
        parser.parse_args(["--check", "--down"])
    assert info.value.code == 2


@pytest.mark.unit
def test_comandos_citam_o_modulo_que_existe() -> None:
    assert (ROOT / MODULE.replace(".", "/")).with_suffix(".py").is_file()
    for doc in DOCS_TOCADOS:
        for cmd in re.findall(r"python -m (src\.\S+)", doc.read_text(encoding="utf-8")):
            assert cmd.rstrip("`") == MODULE, f"{doc.name}: {cmd}"


@pytest.mark.unit
def test_runbook_cita_so_servicos_e_arquivos_do_compose_que_existem() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    base = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    dev = yaml.safe_load((ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8"))["services"]
    for name in set(re.findall(r"docker compose (?:run --rm --no-deps|stop) (\w+)", text)):
        assert name in base, name
    for name in set(re.findall(r"\$COMPOSE (?:exec -T|up -d) (\w+)", text)):
        assert name in dev, name
    assert base["app"]["restart"] == "unless-stopped"  # o runbook depende disso (passo 1)
    for compose in set(re.findall(r"-f (docker-compose[\w.]*\.yml)", text)):
        assert (ROOT / compose).is_file()


@pytest.mark.unit
def test_docs_citam_a_versao_do_agno_instalada_e_nao_ha_cleanup_no_modulo() -> None:
    installed = version("agno")
    assert installed == "3.1.12"
    for doc in DOCS_TOCADOS:
        assert installed in doc.read_text(encoding="utf-8"), doc.name
    source = Path(session_migration.__file__).read_text(encoding="utf-8")
    assert "cleanup_legacy_runs_field(" not in source  # o runbook promete: nunca no mesmo release
